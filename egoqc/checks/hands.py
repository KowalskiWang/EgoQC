"""手部两目对应 + 空间尺度 / Hands in both views + metric scale.

MediaPipe Hand Landmarker runs independently on the left and right views. Keypoints are mapped into the
rectified stereo frame, the same hand is paired across views (smallest vertical offset, handedness preferred)
and triangulated. 'Clean' pairs = median |dy| of the 21 keypoints <= 6 px (f = 500).
Scale: palm length (wrist -> middle-finger MCP) must not change with depth; with a metric calibration it must
also be an adult size (7-10 cm).
"""
import os

import cv2
import numpy as np

from .. import charts
from ..config import FAIL, HAND_MAX_PAIRS, NA, PASS, T, WARN
from ..stereo import Rectifier
from ..util import Metrics, bi, check

MODEL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "models", "hand_landmarker.task")
SKELETON = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11), (11, 12),
            (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (0, 17), (17, 18), (18, 19), (19, 20)]
BINS = np.round(np.arange(0.10, 0.801, 0.05), 2)   # 5 cm wrist-depth bins


def _landmarker():
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision
    o = vision.HandLandmarkerOptions(base_options=BaseOptions(model_asset_path=MODEL), running_mode=vision.RunningMode.VIDEO,
                                     num_hands=2, min_hand_detection_confidence=0.5, min_hand_presence_confidence=0.5,
                                     min_tracking_confidence=0.5)
    return mp, vision.HandLandmarker.create_from_options(o)


def _detect(mp, lm, img, t_ms):
    h, w = img.shape[:2]
    res = lm.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(img, cv2.COLOR_BGR2RGB)), t_ms)
    return [dict(uv=np.array([[p.x * w, p.y * h] for p in res.hand_landmarks[i]]), label=res.handedness[i][0].category_name.lower())
            for i in range(len(res.hand_landmarks))]


def _draw(img, dets, scale):
    for d in dets:
        col = (0, 170, 255) if d["label"] == "left" else (255, 120, 0)
        pts = (d["uv"] * scale).astype(int)
        for a, b in SKELETON:
            cv2.line(img, tuple(pts[a]), tuple(pts[b]), col, 2)
        for p in pts:
            cv2.circle(img, tuple(p), 3, (255, 255, 255), -1)


HAND = {"left": bi("左手", "left hand"), "right": bi("右手", "right hand")}


def run(rec, ctx):
    out, opts = ctx["out"], ctx["opts"]
    if rec.calib_eff is None:
        return (check("hands", NA, bi("没有可用的标定，无法做两目对应检验。", "No usable calibration, so the two views cannot be compared.")),
                check("scale", NA, bi("没有可用的标定，无法检验尺度。", "No usable calibration, so scale cannot be checked.")))
    if not os.path.exists(MODEL):
        msg = bi("缺少手部模型 models/hand_landmarker.task（运行 packaging/setup.sh 会自动下载）。",
                 "The hand model models/hand_landmarker.task is missing (packaging/setup.sh downloads it).")
        return check("hands", NA, msg), check("scale", NA, msg)
    rect = Rectifier(*rec.calib_eff)
    total = rec.n_pairs()
    dur = rec.duration()
    in_window = total * min(1.0, opts["max_seconds"] / dur) if dur > 0 else total   # pairs inside the analysed span
    stride = max(1, int(np.ceil(in_window / HAND_MAX_PAIRS)))
    mp, lmL = _landmarker(); _, lmR = _landmarker()
    rows, nL, nR, nPair, m, t0 = [], 0, 0, 0, 0, None
    sample = None
    try:
        for i, (k, ts, L, R) in enumerate(rec.pairs(stride=stride, max_seconds=opts["max_seconds"])):
            t0 = ts if t0 is None else t0
            tms = int((ts - t0) / 1000)
            dL, dR = _detect(mp, lmL, L, tms), _detect(mp, lmR, R, tms)
            m += 1; nL += bool(dL); nR += bool(dR)
            for d in dL:
                d["rect"] = rect.rect_points(d["uv"], 0)
            for d in dR:
                d["rect"] = rect.rect_points(d["uv"], 1)
            cands = []
            for a, x in enumerate(dL):
                for b, y in enumerate(dR):
                    disp = x["rect"][:, 0] - y["rect"][:, 0]
                    if np.median(disp) <= 1:
                        continue
                    cands.append((np.median(np.abs(x["rect"][:, 1] - y["rect"][:, 1])) + (0 if x["label"] == y["label"] else 15), a, b))
            ua, ub, pairs = set(), set(), []
            for c, a, b in sorted(cands):
                if a in ua or b in ub or c > 40:
                    continue
                ua.add(a); ub.add(b); pairs.append((a, b))
            nPair += bool(pairs)
            for a, b in pairs:
                x, y = dL[a], dR[b]
                rows.append(dict(k=k, ts=ts, label=x["label"], label_r=y["label"], dy=x["rect"][:, 1] - y["rect"][:, 1],
                                 X=rect.triangulate(x["rect"], y["rect"])))
            if len(pairs) >= 2 and (sample is None or abs(i - m // 2) < 5):
                sample = (L.copy(), R.copy(), dL, dR)
            if i % 50 == 0:
                ctx["progress"](bi(f"手部检测 {i * stride}/{int(in_window)} 帧", f"Hand detection {i * stride}/{int(in_window)} frames"),
                                i * stride / max(in_window, 1))
    finally:
        lmL.close(); lmR.close()
    if m == 0 or (nL + nR) < 0.05 * m:
        return (check("hands", NA, bi("画面中几乎没有手，跳过手部检验。", "Hardly any hands in view; hand check skipped.")),
                check("scale", NA, bi("没有手部数据，无法用掌长检验尺度。", "No hand data, so scale cannot be checked with the palm.")))
    figs = []
    if sample:
        L, R, dL, dR = sample
        s = 800.0 / L.shape[1]
        a, b = cv2.resize(L, None, fx=s, fy=s), cv2.resize(R, None, fx=s, fy=s)
        _draw(a, dL, s); _draw(b, dR, s)
        p = os.path.join(out, "hands.jpg"); cv2.imwrite(p, np.hstack([a, b]), [cv2.IMWRITE_JPEG_QUALITY, 80]); figs.append(os.path.basename(p))
    det = (nL / m + nR / m) / 2
    pair_rate = nPair / m
    det_lr = f"{100 * nL / m:.0f}% / {100 * nR / m:.0f}%"
    if not rows:
        res = check("hands", FAIL, bi(f"单目检出 {100 * det:.0f}%，但左右两目的手从未配上。",
                                      f"Hands found in {100 * det:.0f}% of single views, but never matched across the two views."),
                    Metrics().add("单目检出率 左/右", "Detected left/right view", det_lr), figures=figs)
        return res, check("scale", NA, bi("没有配对成功的手。", "No hand was matched across the views."))
    dy = np.stack([np.abs(r["dy"]) for r in rows]); med = np.median(dy, 1)
    X = np.stack([r["X"] for r in rows]); z = X[:, 0, 2]
    clean = (med <= 6) & np.isfinite(z) & (z > 0)
    labs = np.array([r["label"] for r in rows])
    agree = float(np.mean([r["label"] == r["label_r"] for r in rows]))
    palm = np.linalg.norm(X[:, 0] - X[:, 9], axis=1)
    # jumps: wrist speed in palm lengths per second (metric-free), threshold = 2.4 m/s at an 8 cm palm
    jumps = []
    for h in ("left", "right"):
        mm = np.where(clean & (labs == h))[0]
        if len(mm) < 10:
            continue
        pl = np.median(palm[mm])
        tss = np.array([rows[i]["ts"] for i in mm]) / 1e6
        for j in range(len(mm) - 1):
            dt = tss[j + 1] - tss[j]
            if dt <= 0 or dt > 0.3:
                continue
            v = np.linalg.norm(X[mm[j + 1], 0] - X[mm[j], 0]) / pl / dt
            jumps.append(v > T["jump_speed"] / 0.08)
    jump_rate = float(np.mean(jumps)) if jumps else 0.0
    err = float(np.median(dy[clean])) if clean.any() else float("nan")
    metrics = Metrics()
    metrics.add("分析帧数", "Frames analysed", bi(f"{m}（每 {stride} 帧取 1）", f"{m} (every {stride}th)") if stride > 1 else m)
    metrics.add("单目检出率 左/右", "Detected left/right view", det_lr)
    metrics.add("双目配对率", "Matched across views", f"{100 * pair_rate:.0f}%")
    metrics.add("干净配对占比", "Clean pairs", f"{100 * clean.mean():.0f}%")
    metrics.add("干净配对关键点极线误差", "Keypoint epipolar error (clean)", f"{err:.1f} px" if clean.any() else "—")
    metrics.add("左右手标签两目一致", "Handedness agrees in both views", f"{100 * agree:.0f}%")
    metrics.add("跳变率（标签互换/错配）", "Jumps (swapped/mismatched)", f"{100 * jump_rate:.1f}%")
    details = [bi("左右两目各自独立检测手部 21 个关键点，再看两目关键点是否满足极线约束；数值受场景（遮挡、手在画面边缘、运动模糊）影响，宜横向比较。",
                  "The 21 hand keypoints are detected independently in each view, then checked against the epipolar "
                  "constraint. Scenes (occlusion, hands at the image border, motion blur) affect the numbers, so compare "
                  "recordings of similar scenes.")]
    if det >= T["det_pass"] and clean.mean() >= T["clean_pass"]:
        status = PASS
    elif det >= T["det_warn"] and clean.mean() >= T["clean_warn"]:
        status = WARN
    else:
        status = FAIL
    summary = bi(f"单目检出 {100 * det:.0f}%，双目配对 {100 * pair_rate:.0f}%，其中 {100 * clean.mean():.0f}% 两目关键点对得上（误差 {err:.1f} px）。",
                 f"Hands found in {100 * det:.0f}% of views and matched across views in {100 * pair_rate:.0f}% of frames; "
                 f"{100 * clean.mean():.0f}% of pairs agree keypoint by keypoint (error {err:.1f} px).")
    hands_res = check("hands", status, summary, metrics, details, figures=figs)

    # ---- scale ----
    # Metric scale = the absolute palm length (a baseline error scales every length). A calibration error that
    # distorts depth makes the palm length of *both* hands drift the same way with depth; single hands also drift
    # ±10 % with hand pose (detector bias), and over a short depth range that dominates. So the trend is a robust
    # (Theil-Sen) slope pooled over both hands, judged only when the wrists cover >= palm_span_min_m of depth and
    # both hands agree in direction.
    def profile(zz, rr):
        need = max(40, int(0.08 * len(rr)))
        res = []
        for lo, hi in zip(BINS[:-1], BINS[1:]):
            sel = (zz >= lo) & (zz < hi)
            if sel.sum() >= need:
                res.append(((lo + hi) / 2, float(np.median(rr[sel])), int(sel.sum())))
        return res

    def slope(zz, rr):   # relative palm change per 10 cm of depth
        if len(zz) < 30 or np.ptp(zz) < 0.05:
            return None
        idx = np.random.default_rng(0).choice(len(zz), min(len(zz), 400), replace=False)
        a, b = np.triu_indices(len(idx), 1)
        dz = zz[idx][b] - zz[idx][a]; ok = np.abs(dz) > 0.02
        return float(np.median((rr[idx][b] - rr[idx][a])[ok] / dz[ok]) * 0.10) if ok.sum() >= 30 else None

    srows, meds, slopes, pz, pr = [], {}, {}, [], []
    for h in ("left", "right"):
        mm = clean & (labs == h)
        if mm.sum() < 40:
            continue
        L = palm[mm]; medL = float(np.median(L)); meds[h] = medL
        zz = z[mm] * (1.0 if rec.calib_metric else 0.08 / medL)   # relative data placed on a metric-like axis
        rr = L / medL
        srows.append((h, profile(zz, rr))); slopes[h] = slope(zz, rr); pz.append(zz); pr.append(rr)
    if not srows:
        return hands_res, check("scale", NA, bi("干净配对的手太少，无法检验尺度。", "Too few clean hand pairs to check scale."))
    Z, R = np.concatenate(pz), np.concatenate(pr)
    srows.append(("pooled", profile(Z, R)))
    span = float(np.percentile(Z, 95) - np.percentile(Z, 5))
    pooled = slope(Z, R)
    k = [v for v in slopes.values() if v is not None]
    agree = len(k) == 1 or (len(k) == 2 and np.sign(k[0]) == np.sign(k[1]))
    judged = pooled is not None and span >= T["palm_span_min_m"]
    trend = abs(pooled) if judged and agree else 0.0
    figs2 = [charts.palm_by_depth(srows, os.path.join(out, "palm.png"),
                                  bi("掌长随深度的变化（应保持水平）", "Palm length vs depth (should stay flat)"), rec.calib_metric)]
    sm = Metrics()
    for h, v in meds.items():
        sm.add(f"{HAND[h]['zh']}掌长", f"Palm length, {HAND[h]['en']}",
               f"{v * 100:.1f} cm" if rec.calib_metric else bi(f"{v:.3f}（相对）", f"{v:.3f} (relative)"))
    sm.add("手腕深度范围（p5–p95）", "Wrist depth range (p5–p95)",
           f"{np.percentile(Z, 5):.2f}–{np.percentile(Z, 95):.2f} m" if rec.calib_metric
           else bi(f"{span * 100:.0f} cm（按掌长换算）", f"{span * 100:.0f} cm (scaled by the palm)"))
    if pooled is not None:
        per = {lang: ", ".join(f"{HAND[h][lang]} {100 * v:+.0f}%" for h, v in slopes.items() if v is not None) for lang in ("zh", "en")}
        sm.add("每 10 cm 深度的掌长变化", "Palm change per 10 cm depth",
               f"{100 * pooled:+.1f}%（{per['zh']}）" + ("" if judged else "，深度范围窄，不评判"),
               f"{100 * pooled:+.1f}% ({per['en']})" + ("" if judged else "; depth range too narrow to judge"))
    else:
        sm.add("每 10 cm 深度的掌长变化", "Palm change per 10 cm depth", "—")
    sdet = [bi("掌长 = 手腕到中指根关节的距离，是手上几乎不变形的一段。基线（尺度）错了，掌长整体偏大或偏小；"
               "深度方向有误差时，两只手的掌长会随深度朝同一方向变化。单只手受手势影响会有 ±10% 起伏，所以趋势用两手合并的稳健斜率判断。",
               "Palm length = wrist to middle-finger knuckle, a part of the hand that hardly deforms. A wrong baseline makes "
               "every palm too long or too short; a depth error makes both hands' palms drift the same way with depth. "
               "Single hands vary ±10 % with pose, so the trend is a robust slope over both hands together.")]
    if not judged:
        sdet.append(bi(f"手腕深度只覆盖 {span * 100:.0f} cm（需要 ≥ {T['palm_span_min_m'] * 100:.0f} cm），这段数据不评判随深度的趋势。",
                       f"The wrists only cover {span * 100:.0f} cm of depth (≥ {T['palm_span_min_m'] * 100:.0f} cm needed), so "
                       "the depth trend is not judged."))
    elif not agree:
        sdet.append(bi("两只手的趋势方向相反，是手势造成的检测偏差，不是尺度误差。",
                       "The two hands trend in opposite directions: detector bias from hand pose, not a scale error."))
    trend_txt = (bi(f"掌长每 10 cm 深度变化 {100 * pooled:+.0f}%，两手方向一致，深度方向可能有误差",
                    f"palm length changes {100 * pooled:+.0f}% per 10 cm of depth in both hands: depth may be off")
                 if trend > T["palm_trend_warn"] else None)
    if not rec.calib_metric:
        sdet.append(bi("没有出厂标定，基线长度未知：只能检验掌长随深度是否一致（相对尺度），不能检验绝对尺度。",
                       "Without a factory calibration the baseline is unknown: only the consistency over depth (relative "
                       "scale) can be checked, not the absolute scale."))
        st = FAIL if trend > T["palm_trend_fail"] else WARN
        summary = (bi(f"相对尺度：{trend_txt['zh']}；绝对尺度未知。", f"Relative scale: {trend_txt['en']}; absolute scale unknown.")
                   if trend_txt else bi("相对尺度：掌长随深度一致；绝对尺度未知。",
                                        "Relative scale: palm length is consistent over depth; absolute scale unknown."))
        return hands_res, check("scale", st, summary, sm, sdet, figures=figs2)
    cm_zh, cm_en = "、".join(f"{v * 100:.1f}" for v in meds.values()), ", ".join(f"{v * 100:.1f}" for v in meds.values())
    bad = any(not T["palm_bad_lo_m"] <= v <= T["palm_bad_hi_m"] for v in meds.values())
    odd = any(not T["palm_lo_m"] <= v <= T["palm_hi_m"] for v in meds.values())
    if bad or trend > T["palm_trend_fail"]:
        st = FAIL
        s = (bi(f"掌长 {cm_zh} cm，明显超出成人范围，双目尺度（基线）可能有误。",
                f"Palm length {cm_en} cm, far outside the adult range: the stereo scale (baseline) is probably wrong.")
             if bad else bi(trend_txt["zh"] + "。", trend_txt["en"][0].upper() + trend_txt["en"][1:] + "."))
    elif odd or trend > T["palm_trend_warn"]:
        st = WARN
        s = bi(f"掌长 {cm_zh} cm" + ("，不在成人常见范围（7–10 cm）" if odd else "") + (f"；{trend_txt['zh']}" if trend_txt else "") + "。",
               f"Palm length {cm_en} cm" + (", outside the usual adult range (7–10 cm)" if odd else "")
               + (f"; {trend_txt['en']}" if trend_txt else "") + ".")
    else:
        st = PASS
        s = bi(f"掌长 {cm_zh} cm，在成人范围" + ("，随深度无明显变化，尺度正确。" if judged else "，尺度正确（深度范围窄，未评判随深度趋势）。"),
               f"Palm length {cm_en} cm, in the adult range" + (" and flat over depth: the scale is right." if judged
                                                              else ": the scale is right (depth range too narrow for the trend)."))
    sdet.append(bi("这项检验能发现 >10% 的尺度/深度误差；要验证到 1–2%，需要在画面里拍已知尺寸的标定板。",
                   "This catches scale/depth errors above ~10 %; to verify 1–2 %, film a target of known size."))
    return hands_res, check("scale", st, s, sm, sdet, figures=figs2)
