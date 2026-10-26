"""真实帧率与时间戳 / Real frame rate & timestamps: how many real (distinct) pictures per second the recording holds, and whether its timing is sound.

Repeated frames: intra-only codecs (MJPEG ...) -> byte-identical packets (MD5 of each packet, no decoding);
inter codecs (H.264/H.265) -> mean |grey difference| to the previous frame on a downscaled image below
T['repeat_mad'] grey levels (repeats re-encoded by x264 differ by < 0.5, moving frames by several levels).
"""
import os, subprocess

import numpy as np

from .. import charts
from ..config import FAIL, PASS, T, WARN
from ..util import INTRA_CODECS, Metrics, bi, check, probe, run as sh


def packet_hashes(video):
    out = sh(["ffmpeg", "-nostdin", "-v", "error", "-i", video, "-map", "0:v", "-c", "copy", "-f", "framemd5", "-"]).stdout
    return [l.split(",")[-1].strip() for l in out.splitlines() if l and not l.startswith("#")]


def mad_series(paths, width, halves=False):
    """Mean |grey diff| between consecutive decoded frames (downscaled to `width`). Optionally per image half."""
    out, outl, outr, prev = [], [], [], None
    for p in paths:
        info = probe(p)
        if not info:
            continue
        h = int(round(info["height"] * width / info["width"] / 2) * 2)
        proc = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", p, "-vf", f"scale={width}:{h},format=gray", "-f", "rawvideo", "-"],
                                stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        n = width * h
        while True:
            b = proc.stdout.read(n)
            if len(b) < n:
                break
            x = np.frombuffer(b, np.uint8).reshape(h, width).astype(np.int16)
            if prev is not None and prev.shape == x.shape:
                d = np.abs(x - prev)
                out.append(d.mean())
                if halves:
                    outl.append(d[:, : width // 2].mean()); outr.append(d[:, width // 2:].mean())
            prev = x
        proc.wait()
    return np.array(out), np.array(outl), np.array(outr)


def _runs(is_new):
    k = np.where(is_new)[0]
    return k, np.diff(np.r_[k, len(is_new)])


def run_check(rec, ctx):
    out, metrics, details, advice, figs = ctx["out"], Metrics(), [], [], []
    if rec.layout == "sbs":
        v = rec.meta["video"]; fps = v["fps"] or 60.0
        sens = None
        if (v["codec"] or "").lower() in INTRA_CODECS:
            ctx["progress"](bi("帧率：比较每帧压缩数据的指纹", "Frame rate: fingerprinting compressed frames"))
            h = packet_hashes(rec.spec["video"])
            is_new = np.r_[True, np.array([a != b for a, b in zip(h, h[1:])], bool)]
            method = bi("逐帧压缩数据字节比较（MD5），字节完全相同即为重复帧",
                        "byte comparison of each compressed frame (MD5); byte-identical = repeated frame")
        else:
            ctx["progress"](bi("帧率：解码比较相邻帧像素差", "Frame rate: comparing decoded neighbouring frames"))
            d, dl, dr = mad_series([rec.spec["video"]], 960, halves=True)
            is_new = np.r_[True, d > T["repeat_mad"]]
            method = bi(f"解码后相邻帧平均灰度差（960 px 宽灰度图），小于 {T['repeat_mad']} 级视为重复帧",
                        f"mean grey difference between decoded neighbouring frames (960 px wide); below {T['repeat_mad']} grey "
                        "levels = repeated frame")
            sens = {th: int(1 + (d > th).sum()) for th in (0.5, 2.0)}
            rec._half_mad = (dl, dr)
        k, runs = _runs(is_new)
        rec.distinct = k.tolist()
        n = len(is_new); dur = n / fps
        real = len(k) / dur if dur else 0
        ratio = real / fps if fps else 0
        gap_ms = runs.max() / fps * 1000 if len(runs) else 0
        rep = 100 * (1 - len(k) / n)
        metrics.add("文件帧数", "Frames in file", n).add("不重复画面", "Distinct pictures", len(k))
        metrics.add("重复帧占比", "Repeated frames", f"{rep:.0f}%")
        metrics.add("真实帧率", "Real frame rate", f"{real:.1f} fps（标称 {fps:.0f}）", f"{real:.1f} fps (nominal {fps:.0f})")
        metrics.add("最长画面间隔", "Longest gap", f"{gap_ms:.0f} ms").add("间隔中位数", "Median gap", f"{np.median(runs) / fps * 1000:.0f} ms")
        if sens:
            vals = " / ".join(f"{c / dur:.1f}" for c in sens.values())
            details.append(bi(f"判定阈值敏感性：阈值 0.5 / 2.0 灰度级时真实帧率分别为 {vals} fps。",
                              f"Threshold sensitivity: at 0.5 / 2.0 grey levels the real frame rate is {vals} fps."))
        ni = rec.spec.get("name_info") or {}
        if ni.get("start") and ni.get("end"):
            span = (int(ni["end"]) - int(ni["start"])) / 1000
            if abs(span - dur) > 1.0:
                details.append(bi(f"文件名起止时间跨度 {span:.2f} s，按标称帧率算视频只有 {dur:.2f} s。",
                                  f"The start/end times in the file name span {span:.2f} s, the video at its nominal rate only {dur:.2f} s."))
        details.append(bi("判定方法：" + method["zh"] + "。", "Method: " + method["en"] + "."))
        edges = np.arange(0, dur + 2, 2.0)
        cnt = np.histogram(k / fps, edges)[0] / 2.0
        okb = edges[1:] <= dur + 1e-6
        figs.append(charts.fps_over_time(edges[:-1][okb] + 1, cnt[okb], fps, os.path.join(out, "fps.png"),
                                         bi("每 2 秒的真实画面数", "Distinct pictures per second (2 s bins)")))
        if ratio >= T["real_fps_pass"] and gap_ms <= T["gap_warn_ms"]:
            status, summary = PASS, bi(f"真实帧率 {real:.1f} fps，与标称一致。", f"Real frame rate {real:.1f} fps, as nominal.")
        elif ratio >= T["real_fps_warn"] and gap_ms <= T["gap_fail_ms"]:
            status = WARN
            summary = bi(f"真实帧率 {real:.1f} fps，低于标称 {fps:.0f} fps，有 {rep:.0f}% 重复帧。",
                         f"Real frame rate {real:.1f} fps, below the nominal {fps:.0f} fps; {rep:.0f}% repeated frames.")
        else:
            status = FAIL
            summary = bi(f"标称 {fps:.0f} fps，实际只有 {real:.1f} fps 的不重复画面，{rep:.0f}% 是重复帧，最长间隔 {gap_ms:.0f} ms。",
                         f"Nominal {fps:.0f} fps, but only {real:.1f} fps of distinct pictures: {rep:.0f}% of frames are repeats, "
                         f"longest gap {gap_ms:.0f} ms.")
            advice.append(bi("请厂商说明重复帧在哪个环节产生（相机、驱动、录制程序还是封装），并在实际录制条件下达到稳定帧率。",
                             "Ask the vendor where the repeated frames come from (camera, driver, recorder or muxer) and for a "
                             "steady real frame rate under actual recording conditions."))
        if not rec.has_frame_timestamps:
            details.append(bi("文件没有逐帧时间戳，画面间隔按视频时间轴（标称帧率）计算，不一定等于真实采集间隔。",
                              "The file has no per-frame timestamps; gaps are measured on the container timeline (nominal rate) "
                              "and may differ from the true capture intervals."))
        return check("frames", status, summary, metrics, details, advice, figs)

    # ---- recorder layouts with per-frame device timestamps ----
    src = rec.sources()
    tl = src["ts_left"]
    status, summary, correctable = PASS, None, False
    if len(tl) < 2:
        return check("frames", FAIL, bi("读不到逐帧时间戳。", "No per-frame timestamps could be read."), metrics)
    dt = np.diff(tl) / 1000.0
    med = float(np.median(dt)); fps = 1000.0 / med if med else 0
    dur = (tl[-1] - tl[0]) / 1e6
    gaps = int((dt > 1.5 * med).sum()); gmax = float(dt.max())
    metrics.add("时间戳帧率", "Timestamp frame rate", f"{fps:.1f} fps").add("帧间隔中位数", "Median interval", f"{med:.2f} ms")
    metrics.add("最长帧间隔", "Longest interval", f"{gmax:.0f} ms").add("超过 1.5 倍间隔的断档", "Gaps > 1.5 intervals", gaps)
    metrics.add("时长", "Duration", f"{dur:.1f} s")
    sk = src.get("skipped") or {}
    if src["layout"] == "mp4":
        kl = [v for k, v in sk.items() if "left" in k]; kr = [v for k, v in sk.items() if "right" in k]
        if any(sk.values()):
            lst = ", ".join(f"{k}: {v}" for k, v in sk.items())
            details.append(bi(f"MP4 开头有无法解码的帧（从关键帧前的 P 帧开始）：{lst} 帧。",
                              f"The MP4 files start with frames that cannot be decoded (P-frames before the first keyframe): {lst}."))
        if kl and kr and sum(kl) != sum(kr):
            status, correctable = FAIL, True
            off = abs(sum(kr) - sum(kl))
            summary = bi(f"左右两路 MP4 开头不可解码的帧数不同（相差 {off} 帧），按“第 n 帧 = pts 第 n 行”直接读取会让左右错 {off} 帧；"
                         "EgoQC 已按首个关键帧对齐。",
                         f"Left and right MP4 start with a different number of undecodable frames ({off} apart): reading "
                         f"\"frame n = pts row n\" pairs left and right {off} frames apart. EgoQC aligned them at the first keyframe.")
            advice.append(bi("读取这批 MP4 时，跳过每个文件首个关键帧之前的 pts 行；或请厂商让录制从关键帧开始。",
                             "When reading these MP4s, skip the pts rows before each file's first keyframe, or ask the vendor "
                             "to start recordings on a keyframe."))
    else:
        if sk.get("left") or sk.get("right"):
            details.append(bi(f"MCAP 视频流开头有 P 帧：左目跳过 {sk.get('left')} 条、右目 {sk.get('right')} 条消息，左右须按时间戳配对。",
                              f"The MCAP video streams start with P-frames: {sk.get('left')} left and {sk.get('right')} right "
                              "messages skipped; left and right must be paired by timestamp."))
            if sk.get("left") != sk.get("right"):
                status, correctable = WARN, True
                summary = bi("MCAP 左右视频开头可解码位置不同，按消息序号配对会错位；EgoQC 已按时间戳配对。",
                             "Left and right MCAP streams become decodable at different messages: pairing by message index "
                             "would be off. EgoQC paired them by timestamp.")
                advice.append(bi("按设备时间戳配对左右帧，不要按消息序号。", "Pair left and right frames by device timestamp, not by message index."))
    # repeated pictures in the left stream
    ctx["progress"](bi("帧率：检查重复画面", "Frame rate: looking for repeated pictures"))
    d, _, _ = mad_series(src["left"], 480)
    if len(d):
        rep = float((d < T["repeat_mad"] * 0.3).mean())
        metrics.add("近乎相同的相邻帧", "Near-identical neighbours", f"{100 * rep:.1f}%")
        if rep > 0.05:
            details.append(bi(f"{100 * rep:.0f}% 的相邻帧几乎相同，可能有重复帧或长时间静止画面。",
                              f"{100 * rep:.0f}% of neighbouring frames are nearly identical: repeated frames or a long still scene."))
            if status == PASS:
                status = WARN
                summary = bi(f"{100 * rep:.0f}% 的相邻帧几乎相同，需确认是否为重复帧。",
                             f"{100 * rep:.0f}% of neighbouring frames are nearly identical; check whether they are repeated frames.")
            elif summary is not None:
                summary = bi(summary["zh"] + f"另有 {100 * rep:.0f}% 的相邻帧几乎相同（重复帧？）。",
                             summary["en"] + f" Also, {100 * rep:.0f}% of neighbouring frames are nearly identical (repeats?).")
    meta_n = (rec.meta.get("statistics") or {}).get("video_left_frame_count")
    if meta_n and abs(meta_n - len(tl)) > 1 and src["layout"] == "mp4":
        details.append(bi(f"元数据记录 {meta_n} 帧，可解码 {len(tl)} 帧。", f"Metadata lists {meta_n} frames, {len(tl)} are decodable."))
    if status == PASS and gmax > T["gap_warn_ms"]:
        status = WARN
        summary = bi(f"时间戳帧率 {fps:.1f} fps，但有 {gaps} 处断档，最长 {gmax:.0f} ms。",
                     f"Timestamps run at {fps:.1f} fps but have {gaps} gaps, the longest {gmax:.0f} ms.")
    if summary is None:
        summary = bi(f"逐帧时间戳完整，{fps:.1f} fps，无明显断档。", f"Per-frame timestamps complete at {fps:.1f} fps, no notable gaps.")
    return check("frames", status, summary, metrics, details, advice, figs, correctable)


run = run_check
