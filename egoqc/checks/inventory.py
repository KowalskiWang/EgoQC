"""数据完整性 / Completeness: what the recording contains and what is missing (calibration, per-frame timestamps,
IMU, labels)."""
import os, re

from ..config import FAIL, PASS, WARN
from ..util import Metrics, bi, bjoin, check, files, probe

ANNOT = re.compile(r"(annot|label|hand|pose|keypoint|kpt|mano|bbox|action|segment|caption|skeleton|joint)", re.I)
ANNOT_EXT = (".json", ".jsonl", ".csv", ".npz", ".npy", ".pkl", ".txt", ".xml", ".parquet", ".h5", ".hdf5")
SKIP = re.compile(r"(^meta_|_pts\.csv$|imu|_sync\.csv$|calibration|frames_index)", re.I)
YES, NO = bi("有", "yes"), bi("没有", "no")


def find_annotations(rec):
    hits = []
    for f in files(rec.root, "*"):
        b = os.path.basename(f)
        if b.lower().endswith(ANNOT_EXT) and ANNOT.search(b) and not SKIP.search(b):
            hits.append(os.path.relpath(f, rec.root))
    topics = []
    if rec.layout == "ego_mcap":
        from mcap.reader import make_reader
        for m in files(rec.root, "*.mcap"):
            with open(m, "rb") as fh:
                s = make_reader(fh).get_summary()
            for ch in s.channels.values():
                if not re.match(r"^/(camera/(left|right)|imu(/.*)?|audio)$", ch.topic):
                    topics.append(ch.topic)
    return hits, sorted(set(topics))


def run(rec, ctx):
    m, metrics, details, advice = rec.meta, Metrics(), [], []
    dev = (m.get("device") or {})
    name = " ".join(str(x) for x in (dev.get("name"), dev.get("serial_number")) if x)
    metrics.add("设备", "Device", name or bi("未知", "unknown"))
    if dev.get("firmware_version"):
        metrics.add("固件", "Firmware", dev["firmware_version"])
    sess = m.get("session") or {}
    if sess.get("software"):
        metrics.add("录制软件", "Recorder", f"{sess['software']} {sess.get('software_version', '')}".strip())
    fmt = {"ego_mp4": bi("左右两路 MP4", "separate left/right MP4"), "ego_mcap": bi("MCAP", "MCAP"),
           "sbs": bi("左右拼接单视频", "single side-by-side video")}[rec.layout]
    facts = dict(layout=rec.layout)
    if rec.layout == "sbs":
        v = m["video"]
        metrics.add("格式", "Format", bi(f"{fmt['zh']}（{v['codec']}）", f"{fmt['en']} ({v['codec']})"))
        metrics.add("画面", "Picture", bi(f"{v['width']}×{v['height']}（单目 {v['width'] // 2}×{v['height']}）",
                                         f"{v['width']}×{v['height']} (per eye {v['width'] // 2}×{v['height']})"))
        metrics.add("标称帧率", "Nominal frame rate", f"{v['fps']:.0f} fps")
        metrics.add("时长", "Duration", f"{v['duration']:.1f} s")
        metrics.add("总帧数", "Frames in file", v["frames"])
        facts["duration_s"] = float(v["duration"] or 0)
        if "Lavc" in (v.get("encoder") or "") or "Lavf" in (v.get("encoder") or ""):
            details.append(bi(f"文件由 ffmpeg 封装（{v['encoder']}），原始采集时间戳不在文件里。",
                              f"The file was written by ffmpeg ({v['encoder']}); the original capture timestamps are not in it."))
    else:
        src = rec.sources()
        info = probe(src["left"][0]) if src["left"] else {}
        size = rec.video_size()
        nl, nr = len(src["ts_left"]), len(src["ts_right"])
        dur = (src["ts_left"][-1] - src["ts_left"][0]) / 1e6 if nl > 1 else 0
        codec = info.get("codec", "h264")
        metrics.add("格式", "Format", bi(f"{fmt['zh']}（{codec}）", f"{fmt['en']} ({codec})"))
        metrics.add("单目画面", "Picture per eye", f"{size[0]}×{size[1]}" if size and size[0] else "?")
        metrics.add("时长", "Duration", f"{dur:.1f} s")
        metrics.add("帧数（左/右）", "Frames (left/right)", f"{nl}/{nr}")
        facts["duration_s"] = float(dur)
        st = m.get("statistics") or {}
        if st:
            metrics.add("元数据丢帧 视频/IMU", "Drops in metadata video/IMU",
                        f"{st.get('video_left_dropped_frames', '?')}+{st.get('video_right_dropped_frames', '?')} / {st.get('imu_dropped_samples', '?')}")
    c, csrc = rec.raw_calib()
    has_calib = c is not None
    metrics.add("出厂标定", "Factory calibration", {"file": bi("有（随录制的 yaml）", "yes (yaml next to the recording)"),
                                                     "mcap": bi("有（MCAP 附件）", "yes (MCAP attachment)")}.get(csrc, NO))
    metrics.add("逐帧时间戳", "Per-frame timestamps", YES if rec.has_frame_timestamps else
                bi("没有（只能按标称帧率推算）", "no (only the nominal frame rate)"))
    imu = rec.imu()
    metrics.add("IMU", "IMU", bi(f"有（{imu['source']}）", f"yes ({imu['source']})") if imu else NO)
    if not m and rec.layout != "sbs":
        details.append(bi("没有 meta 元数据文件。", "No meta data file."))
    ann, topics = find_annotations(rec)
    metrics.add("标注文件", "Label files", bi(f"{len(ann)} 个", f"{len(ann)}") if ann else bi("未发现", "none found"))
    if ann:
        lst = "、".join(ann[:8]) + ("…" if len(ann) > 8 else "")
        details.append(bi("可能的标注文件：" + lst, "Possible label files: " + lst.replace("、", ", ")))
    if topics:
        details.append(bi("MCAP 里除视频/IMU/音频外的话题：" + "、".join(topics), "MCAP topics besides video/IMU/audio: " + ", ".join(topics)))
    missing = [x for x, ok in ((bi("出厂标定", "factory calibration"), has_calib),
                               (bi("逐帧时间戳", "per-frame timestamps"), rec.has_frame_timestamps)) if not ok]
    if missing:
        status = FAIL
        what = bjoin(missing, bi("和", " and "))
        summary = bi(f"缺少{what['zh']}，" + ("无法直接得到度量 3D。" if not has_calib else "视频帧的真实时刻无法确定。"),
                     f"Missing {what['en']}: " + ("no metric 3D without further work." if not has_calib
                                                  else "the true capture time of each frame is unknown."))
        if not has_calib:
            advice.append(bi("向厂商索取每台设备的出厂标定（两目内参、畸变、双目外参含基线、IMU 外参与时间偏移）。",
                             "Ask the vendor for each device's factory calibration (intrinsics, distortion, stereo extrinsics "
                             "incl. baseline, IMU extrinsics and time offset)."))
        if not rec.has_frame_timestamps:
            advice.append(bi("要求每帧输出与 IMU 同一时钟的硬件时间戳。",
                             "Ask for a hardware timestamp per frame, on the same clock as the IMU."))
    elif not imu:
        status, summary = WARN, bi("标定和时间戳齐全，但没有 IMU 数据。", "Calibration and timestamps are present, but there is no IMU data.")
    else:
        status, summary = PASS, bi("标定、逐帧时间戳和 IMU 都齐全。", "Calibration, per-frame timestamps and IMU are all present.")
    if not ann and not topics:
        details.append(bi("没有发现手部/动作等标注，只有原始传感器数据。", "No hand/action labels found, only raw sensor data."))
    return check("inventory", status, summary, metrics, details, advice, facts=facts)
