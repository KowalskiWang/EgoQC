"""Self-calibration for recordings delivered without calibration (COLMAP, KB fisheye, one camera per eye).

Every image gets its own pose (no rig constraint) so the left->right pose can be checked frame by frame; the
median pose becomes the stereo extrinsics (baseline length unknown -> unit length, calib_metric = False).
"""
import os, time

import cv2
import numpy as np
from scipy.spatial.transform import Rotation as Rot

from .config import SELFCALIB_FRAMES, SELFCALIB_THREADS
from .util import bi


def _cfw(img):
    p = img.cam_from_world
    return p() if callable(p) else p


def run(rec, work, progress, max_seconds):
    import pycolmap
    pycolmap.logging.minloglevel = 2   # COLMAP's per-image INFO lines would flood the app's log
    t0 = time.time()
    fdir = os.path.join(work, "frames")
    for s in ("L", "R"):
        os.makedirs(os.path.join(fdir, s), exist_ok=True)
    total = rec.n_pairs()
    want = set(np.linspace(0, max(total - 1, 0), min(SELFCALIB_FRAMES, total)).astype(int).tolist())
    names, used_k = [], []
    progress(bi("自标定：抽取画面", "Self-calibration: extracting frames"), 0.02)
    for i, (k, ts, L, R) in enumerate(rec.pairs()):
        if i in want:
            n = f"f{k:06d}"
            cv2.imwrite(os.path.join(fdir, "L", n + ".jpg"), L, [cv2.IMWRITE_JPEG_QUALITY, 95])
            cv2.imwrite(os.path.join(fdir, "R", n + ".jpg"), R, [cv2.IMWRITE_JPEG_QUALITY, 95])
            names.append(n); used_k.append(k)
        if i > max(want):
            break
    if len(names) < 20:
        return None
    h, w = cv2.imread(os.path.join(fdir, "L", names[0] + ".jpg")).shape[:2]
    f0 = 0.32 * w
    db = os.path.join(work, "database.db")
    if os.path.exists(db):
        os.remove(db)
    images = [f"L/{n}.jpg" for n in names] + [f"R/{n}.jpg" for n in names]
    reader = pycolmap.ImageReaderOptions(); reader.camera_model = "OPENCV_FISHEYE"
    reader.camera_params = f"{f0},{f0},{w / 2},{h / 2},0,0,0,0"
    ext = pycolmap.FeatureExtractionOptions(); ext.num_threads = SELFCALIB_THREADS
    progress(bi("自标定：提取特征", "Self-calibration: features"), 0.12)
    pycolmap.extract_features(db, fdir, image_names=images, camera_mode=pycolmap.CameraMode.PER_FOLDER, reader_options=reader, extraction_options=ext)
    pairs = []
    for i, n in enumerate(names):
        pairs.append((f"L/{n}.jpg", f"R/{n}.jpg"))
        for d in range(1, 6):
            if i + d < len(names):
                m = names[i + d]
                pairs += [(f"L/{n}.jpg", f"L/{m}.jpg"), (f"R/{n}.jpg", f"R/{m}.jpg")]
                if d <= 2:
                    pairs += [(f"L/{n}.jpg", f"R/{m}.jpg"), (f"R/{n}.jpg", f"L/{m}.jpg")]
    pl = os.path.join(work, "pairs.txt"); open(pl, "w").write("\n".join(f"{a} {b}" for a, b in pairs) + "\n")
    po = pycolmap.ImportedPairingOptions(); po.match_list_path = pl
    progress(bi("自标定：特征匹配", "Self-calibration: matching"), 0.25)
    pycolmap.match_image_pairs(db, pairing_options=po)
    opts = pycolmap.IncrementalPipelineOptions()
    opts.ba_refine_principal_point = True; opts.ba_refine_focal_length = True; opts.ba_refine_extra_params = True
    opts.multiple_models = False; opts.num_threads = SELFCALIB_THREADS
    opts.ba_global_frames_ratio = 1.3; opts.ba_global_points_ratio = 1.3; opts.ba_global_max_num_iterations = 30
    progress(bi("自标定：三维重建", "Self-calibration: reconstruction"), 0.4)
    # Starting a model is the fragile part: with the principal points refined from the first few images, or with a
    # head that mostly turns in place, COLMAP can lock onto a degenerate start. So map with the principal points held
    # at the image centre first, fall back to other settings if that registers too little, and free the principal
    # points in a final global bundle adjustment.
    r = None
    for refine_pp, angle in ((False, 16.0), (True, 16.0), (False, 8.0)):
        opts.ba_refine_principal_point, opts.mapper.init_min_tri_angle = refine_pp, angle
        out = os.path.join(work, f"sparse_{int(refine_pp)}_{int(angle)}"); os.makedirs(out, exist_ok=True)
        recs = pycolmap.incremental_mapping(db, fdir, out, options=opts)
        if recs:
            cand = max(recs.values(), key=lambda x: x.num_reg_images())
            if r is None or cand.num_reg_images() > r.num_reg_images():
                r = cand
        if r is not None and r.num_reg_images() >= 0.8 * len(images):
            break
    if r is None:
        return None
    ba = pycolmap.BundleAdjustmentOptions()
    ba.refine_principal_point, ba.refine_focal_length, ba.refine_extra_params = True, True, True
    ba.print_summary = False   # stdout carries the app's JSON progress lines
    pycolmap.bundle_adjustment(r, ba)
    cams, poses = {}, {"L": {}, "R": {}}
    for img in r.images.values():
        if not img.has_pose:
            continue
        side = img.name[0]
        cams[side] = [float(x) for x in r.cameras[img.camera_id].params]
        p = _cfw(img)
        poses[side][img.name[2:-4]] = (p.rotation.matrix(), np.asarray(p.translation))
    both = sorted(set(poses["L"]) & set(poses["R"]))
    if len(both) < 10 or "L" not in cams or "R" not in cams:
        return None
    Rs, ts = [], []
    for n in both:
        RL, tL = poses["L"][n]; RR, tR = poses["R"][n]
        R = RR @ RL.T; Rs.append(R); ts.append(tR - R @ tL)
    Rs, ts = np.stack(Rs), np.stack(ts)
    Rm = Rot.from_matrix(Rs).mean().as_matrix(); tm = np.median(ts, 0)
    dev = np.degrees([Rot.from_matrix(R @ Rm.T).magnitude() for R in Rs])
    b = np.linalg.norm(ts, axis=1)

    def cam(p, Rx, tx, name):
        return dict(name=name, model="KB", size=(w, h),
                    K=np.array([[p[0], 0, p[2]], [0, p[1], p[3]], [0, 0, 1]], float), D=np.array(p[4:8], float), R=Rx, t=tx)
    c0 = cam(cams["L"], np.eye(3), np.zeros(3), "L")
    c1 = cam(cams["R"], Rm, tm / np.linalg.norm(tm), "R")
    return dict(c0=c0, c1=c1, used_k=used_k, n_images=len(images), n_registered=int(r.num_reg_images()),
                rig_rot_dev_deg=[float(np.median(dev)), float(np.percentile(dev, 90))],
                rig_baseline_dev=float(np.median(np.abs(b / np.median(b) - 1))),
                rel_rot_deg=float(np.degrees(Rot.from_matrix(Rm).magnitude())), seconds=time.time() - t0,
                params={"L": cams["L"], "R": cams["R"]})
