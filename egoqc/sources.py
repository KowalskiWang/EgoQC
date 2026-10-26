"""One Recording object per stereo recording: frame pairs with timestamps, calibration, IMU, metadata.

Lessons built in (all verified on real data):
  * MP4 files may start mid-GOP: the pts csv lists every packet but a decoder drops the leading P-frames, so the
    pts rows before the first keyframe are skipped (else left/right are paired 14 frames apart).
  * MCAP video streams may start with P-frames: frames are dumped from the first SPS/IDR on, left and right are
    always paired by device timestamp, never by index.
  * Calibration may live next to the recording (yaml) or inside the MCAP as attachments; OpenCV FileStorage
    headers (%YAML:1.0) are stripped before parsing.
  * 2ego side-by-side videos have no per-frame timestamps: frame k is placed at start + k / fps (the vendor's
    convention, container time); only distinct frames are used once the repeat check has run.
"""
import csv, json, os

import cv2
import numpy as np
import yaml

from .util import files, probe, run


def _yaml(text):
    return yaml.safe_load("\n".join(l for l in (text or "").splitlines() if not l.startswith("%YAML"))) if text else None


def _read_pts(paths):
    ts = []
    for f in paths:
        with open(f) as fh:
            rows = list(csv.reader(fh))[1:]
        ts += [int(r[0]) for r in rows if r]
    return np.array(ts, np.int64)


def first_key_packet(mp4):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_packets", "-show_entries", "packet=flags",
               "-of", "csv=p=0", mp4]).stdout
    for i, line in enumerate(out.splitlines()):
        if "K" in line:
            return i
    return 0


def _nal_types(buf):
    out, i = [], buf.find(b"\x00\x00\x01")
    while i >= 0 and i + 3 < len(buf):
        out.append(buf[i + 3] & 0x1F)
        i = buf.find(b"\x00\x00\x01", i + 3)
    return out


def _decode(paths):
    for p in paths:
        cap = cv2.VideoCapture(p)
        while True:
            ok, img = cap.read()
            if not ok:
                break
            yield img
        cap.release()


def calib_from_yaml(text):
    """Orbbec/EGO calibration yaml -> (cam0, cam1); cam1 extrinsics map cam0 points into cam1 (X1 = R X0 + t), metres."""
    d = _yaml(text)
    if not d or "cameras" not in d:
        return None
    out = []
    for c in d["cameras"][:2]:
        i, dd = c["intrinsics"], c["distortion"]
        out.append(dict(name=c.get("name", ""), model=c.get("distortion_model", "KB"),
                        size=(int(c["image_width"]), int(c["image_height"])),
                        K=np.array([[i["fx"], 0, i["cx"]], [0, i["fy"], i["cy"]], [0, 0, 1]], float),
                        D=np.array([dd.get("k1", 0), dd.get("k2", 0), dd.get("k3", 0), dd.get("k4", 0)], float),
                        R=np.array(c["extrinsics"]["rotation"], float),
                        t=np.array(c["extrinsics"]["translation"], float) / 1000.0))
    return tuple(out) if len(out) == 2 else None


class Recording:
    def __init__(self, spec, tmp_dir):
        self.spec, self.id, self.layout, self.root = spec, spec["id"], spec["layout"], spec["root"]
        self.tmp = tmp_dir
        os.makedirs(tmp_dir, exist_ok=True)
        self.distinct = None          # sorted container indices of distinct frames (sbs), set by the frame check
        self.calib_eff = None         # calibration the checks should use (set by the geometry check)
        self.calib_metric = False     # True when the calibration carries a real baseline in metres
        self.calib_source = None      # 'file' | 'mcap' | 'self' | None
        self._src = None
        self.meta = self._meta()

    # ---------- metadata ----------
    def _meta(self):
        if self.layout == "sbs":
            info = probe(self.spec["video"])
            ni = self.spec.get("name_info") or {}
            return dict(device=dict(name="2ego" if ni else None, serial_number=(ni.get("prefix") or "").split("_")[-1] or None),
                        video=info, start_ms=int(ni["start"]) if ni.get("start") else None,
                        end_ms=int(ni["end"]) if ni.get("end") else None)
        m = {}
        mf = files(self.root, "meta_*.json")
        if mf:
            try:
                m = json.load(open(mf[0]))
            except Exception:  # noqa: BLE001
                m = {}
        return m

    def duration(self):
        """Seconds of video (container duration for sbs, first-to-last frame timestamp otherwise)."""
        if self.layout == "sbs":
            return float(self.meta["video"]["duration"] or 0)
        tl = self.sources()["ts_left"]
        return (int(tl[-1]) - int(tl[0])) / 1e6 if len(tl) > 1 else 0.0

    def video_size(self):
        if self.layout == "sbs":
            v = self.meta["video"]
            return (v["width"] // 2, v["height"])
        s = (self.meta.get("streams") or {}).get("color_left") or {}
        if "width" in s:
            return (int(s["width"]), int(s["height"]))
        src = self.sources()
        info = probe(src["left"][0]) if src["left"] else {}
        return (info.get("width"), info.get("height")) if info else None

    # ---------- calibration ----------
    def calib_text(self, kind):
        f = files(self.root, f"*_calibration_{kind}.yaml")
        if f:
            return open(f[0]).read(), "file"
        if self.layout == "ego_mcap":
            from mcap.reader import make_reader
            for m in files(self.root, "*.mcap"):
                with open(m, "rb") as fh:
                    for a in make_reader(fh).iter_attachments():
                        if a.name.endswith(f"_calibration_{kind}.yaml"):
                            return a.data.decode(), "mcap"
        return None, None

    def raw_calib(self):
        text, src = self.calib_text("camera")
        c = calib_from_yaml(text) if text else None
        return c, src

    def imu_calib(self):
        text, _ = self.calib_text("imu")
        d = _yaml(text) if text else None
        if not d or "cam0" not in d:
            return None
        c = d["cam0"]
        return dict(T_cam_imu=np.array(c.get("T_cam_imu", np.eye(4).tolist()), float),
                    timeshift_s=float(c["timeshift_cam_imu"]) if "timeshift_cam_imu" in c else None)

    # ---------- frames ----------
    @property
    def has_frame_timestamps(self):
        return self.layout in ("ego_mp4", "ego_mcap")

    def sources(self):
        """ego: dict(left=[files], right=[files], ts_left, ts_right, skipped, layout); sbs: dict(video, fps, base_us, n)."""
        if self._src is not None:
            return self._src
        if self.layout == "sbs":
            v = self.meta["video"]
            base = (self.meta["start_ms"] or 0) * 1000
            self._src = dict(video=self.spec["video"], fps=v["fps"] or 60.0, base_us=base, n=v["frames"] or int(v["duration"] * (v["fps"] or 60)))
            return self._src
        mcaps = files(self.root, "*.mcap")
        if not mcaps:
            L, R = files(self.root, "*camera_left*.mp4"), files(self.root, "*camera_right*.mp4")
            L = [f for f in L if not f.endswith("_pts.csv")]; R = [f for f in R if not f.endswith("_pts.csv")]
            out = dict(left=L, right=R, layout="mp4", skipped={})
            for side, fl in (("left", L), ("right", R)):
                ts = []
                for f in fl:
                    t = _read_pts([f[:-4] + "_pts.csv"]) if os.path.exists(f[:-4] + "_pts.csv") else np.array([], np.int64)
                    k = first_key_packet(f)
                    out["skipped"][os.path.basename(f)] = k
                    ts.append(t[k:])
                out["ts_" + side] = np.concatenate(ts) if ts else np.array([], np.int64)
            self._src = out
            return out
        from mcap.reader import make_reader
        from mcap_protobuf.decoder import DecoderFactory
        out = dict(layout="mcap", skipped={})
        for side in ("left", "right"):
            h264, tsf = os.path.join(self.tmp, f"{side}.h264"), os.path.join(self.tmp, f"{side}_ts.npy")
            if not (os.path.exists(h264) and os.path.exists(tsf)):
                ts, started, skipped = [], False, 0
                with open(h264 + ".part", "wb") as fo:
                    for m in mcaps:
                        with open(m, "rb") as fh:
                            r = make_reader(fh, decoder_factories=[DecoderFactory()])
                            for _, ch, msg, dec in r.iter_decoded_messages(topics=[f"/camera/{side}"]):
                                if not started:
                                    started = bool({5, 7} & set(_nal_types(dec.data)))
                                    if not started:
                                        skipped += 1
                                        continue
                                fo.write(dec.data)
                                t = dec.timestamp.seconds * 1_000_000 + dec.timestamp.nanos // 1000
                                ts.append(t if t > 0 else msg.log_time // 1000)
                os.replace(h264 + ".part", h264)
                np.save(tsf, np.array(ts, np.int64))
                json.dump({"skipped": skipped}, open(tsf + ".json", "w"))
            out[side] = [h264]
            out["ts_" + side] = np.load(tsf)
            try:
                out["skipped"][side] = json.load(open(tsf + ".json"))["skipped"]
            except Exception:  # noqa: BLE001
                out["skipped"][side] = None
        self._src = out
        return out

    def n_pairs(self):
        s = self.sources()
        if self.layout == "sbs":
            return len(self.distinct) if self.distinct is not None else s["n"]
        return min(len(s["ts_left"]), len(s["ts_right"]))

    def pairs(self, max_pairs=None, stride=1, max_seconds=None):
        """Yields (k, ts_us, imgL, imgR). k counts yielded pairs (ego) or the container index (sbs)."""
        s = self.sources()
        if self.layout == "sbs":
            keep = set(self.distinct) if self.distinct is not None else None
            cap = cv2.VideoCapture(s["video"]); k = n = 0
            try:
                while True:
                    ok, img = cap.read()
                    if not ok:
                        break
                    if max_seconds and k / s["fps"] > max_seconds:
                        break
                    if keep is None or k in keep:
                        if n % stride == 0:
                            w = img.shape[1] // 2
                            yield k, int(s["base_us"] + k / s["fps"] * 1e6), img[:, :w], img[:, w:]
                        n += 1
                        if max_pairs and n >= max_pairs:
                            break
                    k += 1
            finally:
                cap.release()
            return
        gl, gr = _decode(s["left"]), _decode(s["right"])
        tl, tr = s["ts_left"], s["ts_right"]
        i = j = k = 0
        t0 = int(tl[0]) if len(tl) else 0
        try:
            imL, imR = next(gl), next(gr)
            while i < len(tl) and j < len(tr):
                d = int(tl[i]) - int(tr[j])
                if abs(d) <= 8000:
                    if max_seconds and (tl[i] - t0) / 1e6 > max_seconds:
                        return
                    if k % stride == 0:
                        yield k, int(tl[i]), imL, imR
                    k += 1
                    if max_pairs and k >= max_pairs * stride:
                        return
                    i += 1; j += 1
                    imL, imR = next(gl), next(gr)
                elif d < 0:
                    i += 1; imL = next(gl)
                else:
                    j += 1; imR = next(gr)
        except StopIteration:
            return

    # ---------- IMU ----------
    def imu(self):
        """dict(t_us, gyro (N,3), acc (N,3), gyro_unit, acc_unit, source, freeze=[(t0,t1)...]) or None."""
        if hasattr(self, "_imu"):
            return self._imu
        self._imu = None
        if self.layout == "sbs":
            if self.spec.get("imu"):
                self._imu = load_2ego_imu(self.spec["imu"])
            return self._imu
        csvs = [f for f in files(self.root, "*imu*.csv") if not f.endswith("_pts.csv")]
        if csvs:
            tg, g, ta, a = [], [], [], []
            for f in csvs:
                with open(f) as fh:
                    rd = csv.reader(fh); hdr = next(rd, [])
                    if "type" not in hdr:
                        continue
                    for r in rd:
                        if len(r) < 5:
                            continue
                        if r[4] == "gyro":
                            tg.append(int(r[0])); g.append([float(r[1]), float(r[2]), float(r[3])])
                        elif r[4] == "accel":
                            ta.append(int(r[0])); a.append([float(r[1]), float(r[2]), float(r[3])])
            if tg:
                tg, g = np.array(tg, np.int64), np.array(g); o = np.argsort(tg)
                acc = None
                if len(ta) == len(tg):
                    ta, a = np.array(ta, np.int64), np.array(a); oa = np.argsort(ta); acc = a[oa]
                self._imu = dict(t_us=tg[o], gyro=g[o], acc=acc, gyro_unit="rad/s", acc_unit="m/s²", source="csv", freeze=[])
            return self._imu
        mc = files(self.root, "*.mcap")
        if mc:
            from mcap.reader import make_reader
            from mcap_protobuf.decoder import DecoderFactory
            tg, g, a, ta, aa, decs, fac = [], [], [], [], [], {}, DecoderFactory()
            for m in mc:
                with open(m, "rb") as fh:
                    r = make_reader(fh, decoder_factories=[fac])
                    for sch, ch, msg in r.iter_messages(topics=["/imu", "/imu/gyro", "/imu/accel"]):
                        if ch.message_encoding == "json":
                            d = json.loads(msg.data)
                            t = int(d.get("timestamp_us", msg.log_time // 1000))
                            if ch.topic.endswith("accel"):
                                ta.append(t); aa.append([d["x"], d["y"], d["z"]])
                            else:
                                tg.append(t); g.append([d["x"], d["y"], d["z"]])
                        else:
                            if sch.id not in decs:
                                decs[sch.id] = fac.decoder_for(ch.message_encoding, sch)
                            dec = decs[sch.id](msg.data)
                            tg.append(dec.timestamp.seconds * 1_000_000 + dec.timestamp.nanos // 1000)
                            w, l = dec.angular_velocity, dec.linear_acceleration
                            g.append([w.x, w.y, w.z]); a.append([l.x, l.y, l.z])
            if tg:
                tg, g = np.array(tg, np.int64), np.array(g, float); o = np.argsort(tg)
                acc = np.array(a, float)[o] if a else (np.array(aa, float)[np.argsort(ta)] if len(aa) == len(tg) else None)
                self._imu = dict(t_us=tg[o], gyro=g[o], acc=acc, gyro_unit="rad/s", acc_unit="m/s²", source="mcap", freeze=[])
        return self._imu


def load_2ego_imu(pairs):
    """2ego IMU: 27-byte records = u8 flag, u16 BE packet seq, 12 x i16 BE (two accel xyz + gyro xyz sets; the
    second set is used). sync.csv row k = time of record k (seconds relative to the first segment's start)."""
    t_all, a_all, g_all = [], [], []
    starts = []
    for b, _ in pairs:
        try:
            starts.append(int(os.path.basename(b).split("_IMU_")[1].split("_")[0]))
        except Exception:  # noqa: BLE001
            pass
    base_ms = min(starts) if starts else 0
    for b, s in pairs:
        raw = open(b, "rb").read()
        n = len(raw) // 27
        r = np.frombuffer(raw[: n * 27], np.uint8).reshape(n, 27)
        v = (r[:, 3:].reshape(n, 12, 2)[:, :, 0].astype(np.int16) << 8 | r[:, 3:].reshape(n, 12, 2)[:, :, 1]).astype(np.int16)
        rows = list(csv.reader(open(s)))[1:]
        t = np.array([float(x[2]) for x in rows if len(x) >= 3])
        m = min(len(t), n)
        t_all.append(t[:m]); a_all.append(v[:m, 6:9].astype(float)); g_all.append(v[:m, 9:12].astype(float))
    if not t_all:
        return None
    t = np.concatenate(t_all); a = np.concatenate(a_all); g = np.concatenate(g_all)
    o = np.argsort(t, kind="stable"); t, a, g = t[o], a[o], g[o]
    same = np.r_[False, np.all(np.c_[a, g][1:] == np.c_[a, g][:-1], axis=1)]
    # frozen stretches: identical samples repeated for a while
    freeze, i = [], 0
    while i < len(same):
        if same[i]:
            j = i
            while j < len(same) and same[j]:
                j += 1
            if t[j - 1] - t[i - 1] > 0.1:
                freeze.append((float(t[i - 1]), float(t[j - 1])))
            i = j
        else:
            i += 1
    keep = ~same
    sa = 9.80665 / np.median(np.linalg.norm(a[keep], axis=1))
    return dict(t_us=(base_ms * 1000 + t[keep] * 1e6).astype(np.int64), gyro=g[keep], acc=a[keep] * sa, gyro_unit="raw",
                acc_unit="m/s²*", acc_raw_g_norm_lsb=float(1 / sa * 9.80665), source="2ego bin", freeze=freeze)   # * scaled by gravity
