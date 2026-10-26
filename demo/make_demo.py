"""Synthetic stereo-ego demo data for EgoQC - no real footage needed.

Renders a textured room seen by a head-mounted stereo fisheye rig (Kannala-Brandt lenses, 10 cm baseline) with an
IMU, and writes three recordings in layouts EgoQC reads:

  demo_good     everything right: calibration, per-frame timestamps, IMU, bitrate
  demo_issues   five planted problems:
                  1. the right MP4 starts 14 frames further before its first keyframe than the left one
                  2. the calibration is for 1280x960 but the video is a 1280x864 centre crop
                  3. the IMU clock is 45 ms off the time offset in its calibration
                  4. 20 % of the frames repeat the previous one
                  5. ~1 Mbit/s per eye
  demo_sbs      one side-by-side video, 60 fps nominal but ~15 real pictures per second, no calibration,
                no timestamps (EgoQC self-calibrates it with COLMAP)

usage: python demo/make_demo.py [OUT_DIR] [--seconds 15] [--workers 4]      needs ffmpeg; a few minutes
"""
import argparse, json, os, subprocess, sys
from multiprocessing import Pool

import cv2
import numpy as np
from scipy.spatial.transform import Rotation as Rot

W, H, FPS = 1280, 960, 30
K_L = dict(fx=520.0, fy=520.0, cx=641.3, cy=478.9, k=(0.030, -0.006, 0.0015, -0.0003))
K_R = dict(fx=521.2, fy=520.6, cx=638.7, cy=481.4, k=(0.028, -0.005, 0.0012, -0.0002))
BASELINE = 0.100                                                         # m, right eye at +x of the left eye
R_LR = Rot.from_euler("xyz", [0.15, -0.35, 0.10], degrees=True).as_matrix()   # rotation of the right eye
R_CAM_IMU = np.array([[0, -1, 0], [0, 0, -1], [1, 0, 0]], float)         # IMU x forward, y left, z up
T_IMU0 = 1_700_000_000_000_000                                           # device clock origin (us)
SHIFT_CAL = -0.0125                                                       # calibrated t_imu - t_cam (s)

# room, table and objects as axis-aligned boxes (world: x right, y down, z forward; eyes at y = 0)
BOXES = [  # name, lo, hi, texel size (m), inside?
    ("room", (-2.0, -0.9, -1.5), (2.0, 1.6, 2.5), 0.004, True),
    ("table", (-0.7, 0.72, 0.25), (0.7, 1.6, 1.05), 0.0012, False),
    ("box_a", (-0.36, 0.60, 0.45), (-0.20, 0.72, 0.60), 0.0005, False),
    ("box_b", (0.05, 0.64, 0.38), (0.20, 0.72, 0.50), 0.0005, False),
    ("box_c", (0.25, 0.54, 0.60), (0.45, 0.72, 0.80), 0.0006, False),
    ("box_d", (-0.15, 0.66, 0.74), (0.02, 0.72, 0.90), 0.0005, False),
    ("shelf", (-2.0, -0.2, 0.4), (-1.6, 0.9, 1.8), 0.002, False),
]
LIGHT = np.array([0.3, -1.0, 0.25]) / np.linalg.norm([0.3, -1.0, 0.25])


# ---------------------------------------------------------------- scene
def _noise(rng, size, amp, cells):
    n = rng.normal(0, amp, (cells, cells, 3)).astype(np.float32)
    return cv2.resize(n, (size, size), interpolation=cv2.INTER_CUBIC)


def _wood(rng, size, base, along_x=True):
    img = np.empty((size, size, 3), np.float32); img[:] = base
    y = np.arange(size, dtype=np.float32)[:, None]
    x = np.arange(size, dtype=np.float32)[None, :]
    grain = 10 * np.sin(y / 3.1 + 6 * np.sin(x / 90.0 + rng.uniform(0, 6))) + 6 * np.sin(y / 11.0)
    img += grain[..., None] * np.array([0.6, 0.8, 1.0], np.float32)
    for yy in range(0, size, int(rng.integers(90, 140))):        # plank seams
        cv2.line(img, (0, yy), (size, yy), tuple(float(v) for v in base * 0.55), 2)
        for xx in rng.integers(0, size, 3):
            cv2.line(img, (int(xx), yy), (int(xx), yy + 120), tuple(float(v) for v in base * 0.6), 2)
    img += _noise(rng, size, 6, 64)
    img = np.clip(img, 0, 255).astype(np.uint8)
    return img if along_x else np.ascontiguousarray(img.transpose(1, 0, 2))


def _poster(rng, w, h):
    """A framed picture: soft colour field with a few shapes and a caption."""
    p = np.empty((h, w, 3), np.float32); p[:] = rng.integers(60, 220, 3)
    p += _noise(rng, max(w, h), 25, 6)[:h, :w]
    p = np.clip(p, 0, 255).astype(np.uint8)
    for _ in range(int(rng.integers(3, 8))):
        c = tuple(int(x) for x in rng.integers(20, 240, 3))
        cx, cy, r = int(rng.integers(0, w)), int(rng.integers(0, h)), int(rng.integers(8, max(9, min(w, h) // 3)))
        (cv2.circle(p, (cx, cy), r, c, -1) if rng.random() < 0.5 else cv2.rectangle(p, (cx - r, cy - r), (cx + r, cy + r // 2), c, -1))
    cv2.putText(p, "".join(rng.choice(list("EGOQCDEMO2468"), 5)), (6, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (30, 30, 30), 2)
    cv2.rectangle(p, (0, 0), (w - 1, h - 1), (40, 40, 40), 6)
    return p


def texture(seed, kind, size=1024):
    """Feature-rich but calm procedural textures (BGR) for each surface type."""
    rng = np.random.default_rng(seed)
    if kind == "floor":
        return _wood(rng, size, np.array([70, 110, 150], np.float32))
    if kind == "wood":
        return _wood(rng, size, np.array([60, 95, 140], np.float32), along_x=bool(rng.random() < 0.5))
    if kind == "ceiling":
        img = np.full((size, size, 3), 228, np.float32) + _noise(rng, size, 4, 32)
        for i in range(0, size, 120):
            cv2.line(img, (i, 0), (i, size), (200, 200, 205), 3); cv2.line(img, (0, i), (size, i), (200, 200, 205), 3)
        for _ in range(6):
            x, y = (int(v) for v in rng.integers(0, size - 120, 2))
            cv2.rectangle(img, (x, y), (x + 110, y + 50), (250, 250, 250), -1); cv2.rectangle(img, (x, y), (x + 110, y + 50), (170, 170, 170), 2)
        return np.clip(img, 0, 255).astype(np.uint8)
    if kind == "shelf":
        img = np.full((size, size, 3), (52, 56, 60), np.float32) + _noise(rng, size, 5, 32)
        for row in range(0, size, 170):
            cv2.rectangle(img, (0, row), (size, row + 14), (90, 100, 110), -1)
            x = 0
            while x < size:
                w = int(rng.integers(14, 40)); hb = int(rng.integers(100, 150))
                c = tuple(int(v) for v in rng.integers(40, 220, 3))
                cv2.rectangle(img, (x, row + 165 - hb), (x + w - 2, row + 165), c, -1)
                cv2.line(img, (x + 3, row + 175 - hb), (x + 3, row + 150), (230, 230, 230), 1)
                x += w
        return np.clip(img, 0, 255).astype(np.uint8)
    if kind == "print":   # product packaging on the table
        img = np.empty((size, size, 3), np.float32); img[:] = rng.integers(80, 240, 3)
        img = np.clip(img + _noise(rng, size, 20, 8), 0, 255).astype(np.uint8)
        for _ in range(40):
            c = tuple(int(v) for v in rng.integers(0, 256, 3))
            x, y, r = int(rng.integers(0, size)), int(rng.integers(0, size)), int(rng.integers(15, 90))
            (cv2.circle(img, (x, y), r, c, -1) if rng.random() < 0.5 else cv2.rectangle(img, (x, y), (x + r, y + 2 * r), c, -1))
        for _ in range(10):
            cv2.putText(img, "".join(rng.choice(list("EGOQC0123456789"), 4)), tuple(int(v) for v in rng.integers(0, size, 2)),
                        cv2.FONT_HERSHEY_SIMPLEX, float(rng.uniform(1.0, 3.0)), (20, 20, 20), 3)
        return img
    # wall: light paint, posters, a few sockets and notes
    img = np.full((size, size, 3), (188, 196, 204), np.float32) + _noise(rng, size, 5, 24)
    img = np.clip(img, 0, 255).astype(np.uint8)
    for _ in range(int(rng.integers(4, 7))):
        w, h = int(rng.integers(120, 300)), int(rng.integers(120, 260))
        x, y = int(rng.integers(0, size - w)), int(rng.integers(0, size - h))
        img[y:y + h, x:x + w] = _poster(rng, w, h)
    for _ in range(25):
        x, y = (int(v) for v in rng.integers(0, size - 40, 2))
        c = tuple(int(v) for v in rng.choice([(90, 200, 240), (120, 230, 160), (230, 200, 120), (240, 240, 240)]))
        cv2.rectangle(img, (x, y), (x + 34, y + 34), c, -1); cv2.line(img, (x + 5, y + 12), (x + 28, y + 12), (60, 60, 60), 1)
    return img


KINDS = {"room": {(1, 1): "floor", (1, 0): "ceiling"}, "table": "wood", "shelf": "shelf"}


def scene():
    tex = {}
    for b, (name, *_rest) in enumerate(BOXES):
        for axis in range(3):
            for side in range(2):
                k = KINDS.get(name, "print")
                k = k.get((axis, side), "wall") if isinstance(k, dict) else k
                tex[(b, axis, side)] = texture(1000 + 10 * b + 2 * axis + side, k)
    return tex


def unproject(cam):
    """Unit ray per pixel (camera frame) for a Kannala-Brandt fisheye."""
    u, v = np.meshgrid(np.arange(W, dtype=np.float64) + 0.5, np.arange(H, dtype=np.float64) + 0.5)
    x, y = (u - cam["cx"]) / cam["fx"], (v - cam["cy"]) / cam["fy"]
    rd = np.hypot(x, y)
    k1, k2, k3, k4 = cam["k"]
    th = rd.copy()
    for _ in range(12):   # Newton: theta * (1 + k1 t^2 + ...) = rd
        t2 = th * th
        f = th * (1 + t2 * (k1 + t2 * (k2 + t2 * (k3 + t2 * k4)))) - rd
        df = 1 + t2 * (3 * k1 + t2 * (5 * k2 + t2 * (7 * k3 + t2 * 9 * k4)))
        th -= f / df
    s = np.where(rd > 1e-12, np.sin(th) / np.maximum(rd, 1e-12), 1.0)
    d = np.stack([x * s, y * s, np.cos(th)], -1)
    vig = np.clip(np.cos(th), 0, 1) ** 0.35
    return d.reshape(-1, 3).astype(np.float32), vig.reshape(-1).astype(np.float32)


def pose(t):
    """Head pose at time t: R_wc (camera->world) and eye position of the left camera."""
    yaw = 0.42 * np.sin(2 * np.pi * 0.11 * t) + 0.14 * np.sin(2 * np.pi * 0.37 * t + 1.0)
    pitch = 0.62 + 0.13 * np.sin(2 * np.pi * 0.23 * t + 0.5) + 0.04 * np.sin(2 * np.pi * 0.9 * t)
    roll = 0.06 * np.sin(2 * np.pi * 0.3 * t + 2.0)
    R = Rot.from_euler("y", yaw) * Rot.from_euler("x", -pitch) * Rot.from_euler("z", roll)
    c = np.array([0.06 * np.sin(2 * np.pi * 0.13 * t), 0.015 * np.sin(2 * np.pi * 0.5 * t), -0.25 + 0.05 * np.sin(2 * np.pi * 0.07 * t)])
    return R.as_matrix(), c


def render(tex, rays, vig, R_wc, o, rng):
    d = rays @ R_wc.T.astype(np.float32)
    o = o.astype(np.float32)
    n = len(d)
    best_t = np.full(n, np.inf, np.float32)
    best_key = np.full(n, -1, np.int32)
    inv = 1.0 / np.where(np.abs(d) < 1e-9, 1e-9, d)
    for b, (_, lo, hi, _, inside) in enumerate(BOXES):
        t1 = (np.asarray(lo, np.float32) - o) * inv
        t2 = (np.asarray(hi, np.float32) - o) * inv
        tn, tf = np.minimum(t1, t2), np.maximum(t1, t2)
        if inside:
            t = tf.min(1); axis = tf.argmin(1)
            side = (t2[np.arange(n), axis] == t).astype(np.int32)
            hit = t > 0
        else:
            t = tn.max(1); axis = tn.argmax(1)
            hit = (t > 1e-4) & (t <= tf.min(1))
            side = (t2[np.arange(n), axis] == t).astype(np.int32)
        better = hit & (t < best_t)
        best_t[better] = t[better]
        best_key[better] = (b * 6 + axis * 2 + side)[better]
    out = np.zeros((n, 3), np.float32)
    p = o + d * best_t[:, None]
    for key in np.unique(best_key):
        if key < 0:
            continue
        sel = best_key == key
        b, axis, side = key // 6, (key % 6) // 2, key % 2
        a1, a2 = [a for a in range(3) if a != axis]
        img = tex[(b, axis, side)]
        ts = BOXES[b][3]
        u = (p[sel, a1] / ts).astype(np.int64) % img.shape[1]
        v = (p[sel, a2] / ts).astype(np.int64) % img.shape[0]
        nrm = np.zeros(3); nrm[axis] = -1 if side == 0 else 1
        if BOXES[b][4]:
            nrm = -nrm
        shade = 0.55 + 0.45 * max(0.0, float(nrm @ LIGHT)) + 0.08 * axis
        out[sel] = img[v, u] * min(shade, 1.15)
    out *= (vig * (1.0 - 0.10 * np.clip(best_t, 0, 4) / 4))[:, None]
    out += rng.normal(0, 1.6, out.shape).astype(np.float32)
    return np.clip(out, 0, 255).astype(np.uint8).reshape(H, W, 3)


_W = {}


def _init():
    _W["tex"] = scene()
    _W["L"], _W["R"] = unproject(K_L), unproject(K_R)


def _frame(k):
    """Left and right image of frame k (exposure centre at k / FPS)."""
    R_wc, c = pose(k / FPS)
    rng = np.random.default_rng(k)
    left = render(_W["tex"], *_W["L"], R_wc, c, rng)
    R_wr = R_wc @ R_LR.T                       # right camera -> world
    c_r = c + R_wc @ np.array([BASELINE, 0, 0])
    right = render(_W["tex"], *_W["R"], R_wr, c_r, rng)
    return k, left, right


# ---------------------------------------------------------------- IMU
def imu_samples(seconds, shift_true, rate=200.0, seed=7):
    """(timestamps_us, gyro rad/s, accel m/s^2) in the IMU frame; stamps carry the IMU clock offset shift_true."""
    rng = np.random.default_rng(seed)
    tau = np.arange(-1.0, seconds + 1.0, 1.0 / rate)
    dt = 1e-3
    gyro, acc = [], []
    for t in tau:
        R0, c0 = pose(t); R1, c1 = pose(t + dt); Rm, cm = pose(t - dt)
        w_cam = Rot.from_matrix(R0.T @ R1).as_rotvec() / dt          # body rate in the camera frame
        a_world = (c1 - 2 * c0 + cm) / dt ** 2
        f_cam = R0.T @ (a_world - np.array([0.0, 9.80665, 0.0]))     # specific force (gravity +y = down)
        gyro.append(R_CAM_IMU.T @ w_cam); acc.append(R_CAM_IMU.T @ f_cam)
    gyro = np.array(gyro) + np.array([0.004, -0.003, 0.002]) + rng.normal(0, 0.004, (len(tau), 3))
    acc = np.array(acc) + rng.normal(0, 0.03, (len(tau), 3))
    stamps = (T_IMU0 + (tau + shift_true) * 1e6).astype(np.int64)
    return stamps, gyro, acc


def write_imu_csv(path, stamps, gyro, acc):
    with open(path, "w") as f:
        f.write("timestamp_us,x,y,z,type\n")
        for t, g, a in zip(stamps, gyro, acc):
            f.write(f"{t},{g[0]:.6f},{g[1]:.6f},{g[2]:.6f},gyro\n{t},{a[0]:.5f},{a[1]:.5f},{a[2]:.5f},accel\n")


# ---------------------------------------------------------------- files
def calib_yaml(path, size):
    cams = []
    for name, K, R, t in (("left", K_L, np.eye(3), np.zeros(3)), ("right", K_R, R_LR, -R_LR @ np.array([BASELINE * 1000, 0, 0]))):
        cams.append(dict(name=name, distortion_model="KB", image_width=size[0], image_height=size[1],
                         intrinsics=dict(fx=K["fx"], fy=K["fy"], cx=K["cx"], cy=K["cy"]),
                         distortion=dict(k1=K["k"][0], k2=K["k"][1], k3=K["k"][2], k4=K["k"][3]),
                         extrinsics=dict(rotation=np.round(R, 9).tolist(), translation=np.round(t, 6).tolist())))
    import yaml
    with open(path, "w") as f:
        f.write("# camera 1 extrinsics map camera-0 points into camera 1: X1 = R X0 + t (t in mm)\n")
        yaml.safe_dump(dict(cameras=cams), f, sort_keys=False)


def imu_yaml(path):
    T = np.eye(4); T[:3, :3] = R_CAM_IMU; T[:3, 3] = [0.021, 0.012, -0.004]
    import yaml
    with open(path, "w") as f:
        f.write("# Kalibr convention: p_cam = T_cam_imu p_imu, t_imu = t_cam + timeshift_cam_imu\n")
        yaml.safe_dump(dict(cam0=dict(T_cam_imu=np.round(T, 9).tolist(), timeshift_cam_imu=SHIFT_CAL)), f, sort_keys=False)


def meta_json(path, size, n):
    json.dump(dict(device=dict(name="EgoQC demo rig", serial_number="SIM-0001", firmware_version="synthetic"),
                   session=dict(software="EgoQC make_demo", software_version="1.0"),
                   streams=dict(color_left=dict(width=size[0], height=size[1], fps=FPS), color_right=dict(width=size[0], height=size[1], fps=FPS)),
                   statistics=dict(video_left_frame_count=n, video_right_frame_count=n, video_left_dropped_frames=0,
                                   video_right_dropped_frames=0, imu_dropped_samples=0)),
              open(path, "w"), indent=1)


def encoder(path, size, fps, x264):
    return subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{size[0]}x{size[1]}",
                             "-r", str(fps), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", *x264, path], stdin=subprocess.PIPE)


def start_before_keyframe(full_mp4, mp4, drop_before, n):
    """Cut an MP4 (keyframe every 30 frames) so it starts drop_before frames *before* the keyframe at frame 30:
    those leading P-frames cannot be decoded, but a pts file still lists them. Returns the frame indices kept."""
    first = 30 - drop_before
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", full_mp4, "-c", "copy",
                    "-bsf:v", f"noise=drop=lt(n\\,{first})", mp4], check=True)
    os.remove(full_mp4)
    return list(range(first, n))


def write_pts(path, idx, start_us):
    with open(path, "w") as f:
        f.write("timestamp_us\n")
        f.writelines(f"{start_us + int(round(k * 1e6 / FPS))}\n" for k in idx)


def side_files(d, name, n, idx_by_side, imu_shift, size):
    start = T_IMU0                                   # frame k exposure centre = start + k / FPS (camera clock)
    for side, idx in idx_by_side.items():
        write_pts(os.path.join(d, f"{name}_camera_{side}_pts.csv"), idx, start)
    calib_yaml(os.path.join(d, f"{name}_calibration_camera.yaml"), (W, H))
    imu_yaml(os.path.join(d, f"{name}_calibration_imu.yaml"))
    write_imu_csv(os.path.join(d, f"{name}_imu.csv"), *imu_samples(n / FPS, imu_shift))
    meta_json(os.path.join(d, f"meta_{name}.json"), size, n)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", nargs="?", default="demo_data")
    ap.add_argument("--seconds", type=float, default=15)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    n = int(a.seconds * FPS)
    dg, di, ds = (os.path.join(a.out, x) for x in ("demo_good", "demo_issues", "demo_sbs"))
    for d in (dg, di, ds):
        os.makedirs(d, exist_ok=True)
    good = ["-preset", "medium", "-crf", "17", "-g", "30", "-bf", "0"]
    low = ["-preset", "medium", "-b:v", "1000k", "-maxrate", "1200k", "-bufsize", "2000k", "-bf", "0",   # problem 5
           "-x264-params", "keyint=30:min-keyint=30:scenecut=0"]
    hc = 864
    crop = slice((H - hc) // 2, (H - hc) // 2 + hc)                         # problem 2: centre crop, calibration says 960
    enc = {"gl": encoder(os.path.join(dg, "demo_good_camera_left.mp4"), (W, H), FPS, good),
           "gr": encoder(os.path.join(dg, "demo_good_camera_right.mp4"), (W, H), FPS, good),
           "il": encoder(os.path.join(di, "full_left.mp4"), (W, hc), FPS, low),
           "ir": encoder(os.path.join(di, "full_right.mp4"), (W, hc), FPS, low),
           "s": encoder(os.path.join(ds, "demo_sbs_stereo.mp4"), (W, H // 2), 60, ["-preset", "medium", "-crf", "20"])}
    rng = np.random.default_rng(3)
    rep = rng.random(n) < 0.20                                              # problem 4: 20 % repeated frames
    rep[:31] = False
    held = np.random.default_rng(5).integers(3, 6, n)
    last = None
    print(f"rendering {n} stereo frames ({W}x{H}) ...", flush=True)
    with Pool(a.workers, initializer=_init) as pool:
        for k, left, right in pool.imap(_frame, range(n), chunksize=2):
            enc["gl"].stdin.write(left.tobytes()); enc["gr"].stdin.write(right.tobytes())
            if not rep[k] or last is None:
                last = (np.ascontiguousarray(left[crop]), np.ascontiguousarray(right[crop]))
            enc["il"].stdin.write(last[0].tobytes()); enc["ir"].stdin.write(last[1].tobytes())
            if k % 2 == 0:   # side-by-side: every 2nd picture (15 real fps), each held 3-5 frames at 60 fps
                pic = np.hstack([cv2.resize(left, (W // 2, H // 2), interpolation=cv2.INTER_AREA),
                                 cv2.resize(right, (W // 2, H // 2), interpolation=cv2.INTER_AREA)])
                for _ in range(held[k]):
                    enc["s"].stdin.write(pic.tobytes())
            if k % 50 == 0:
                print(f"  {k}/{n}", flush=True)
    for e in enc.values():
        e.stdin.close(); e.wait()
    side_files(dg, "demo_good", n, {"left": range(n), "right": range(n)}, SHIFT_CAL, (W, H))
    idx = {side: start_before_keyframe(os.path.join(di, f"full_{side}.mp4"), os.path.join(di, f"demo_issues_camera_{side}.mp4"), drop, n)
           for side, drop in (("left", 2), ("right", 16))}                  # problem 1: 14 frames more before the keyframe
    side_files(di, "demo_issues", n, idx, SHIFT_CAL + 0.045, (W, hc))       # problem 3: IMU 45 ms off its calibration
    print(f"done: {os.path.abspath(a.out)}")


if __name__ == "__main__":
    sys.exit(main())
