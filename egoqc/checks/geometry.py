"""双目标定与几何 / Stereo calibration: does the stereo calibration actually fit the images?

With calibration: rectify sampled pairs and measure the vertical offset dy of SIFT matches (0 for a perfect
calibration; f = 500 px). If the calibration's resolution differs from the video (e.g. 1920x1200 calibration on
1920x1080 video), centre/top/bottom crops are tried and the best one is used downstream.
Without calibration (and self-calibration on): COLMAP self-calibration, then the same test on frames that were
not used for it.
"""
import copy, os

import cv2
import numpy as np

from .. import selfcalib
from ..config import FAIL, NA, PASS, T, WARN
from ..stereo import Rectifier, epipolar_residuals, rectified_preview
from ..util import Metrics, bi, check


N_FRAMES = 24   # pairs used for the epipolar test


def sample_frames(rec, n, max_seconds=None, exclude_k=()):
    """n pairs spread evenly over the analysed span; a pick whose frame is in exclude_k moves to the next frame."""
    total, dur = rec.n_pairs(), rec.duration()
    if max_seconds and dur > max_seconds:
        total = int(total * max_seconds / dur)
    want = set(np.linspace(min(10, total - 1), max(total - 10, 0), n).astype(int).tolist())
    ex, out, pending = set(exclude_k), [], 0
    last = max(want)
    for i, (k, ts, L, R) in enumerate(rec.pairs(max_seconds=max_seconds)):
        if i in want:
            pending += 1
        if pending and k not in ex:
            out.append((L, R)); pending -= 1
        if i >= last and pending == 0:
            break
    return out


def _stats(rect, frames):
    dy, dx, xy, per = epipolar_residuals(rect, frames)
    if not len(dy):
        return None
    w, h = rect.size
    central = (np.abs(xy[:, 0] - w / 2) < w * 0.3) & (np.abs(xy[:, 1] - h / 2) < h * 0.3)
    return dict(median=float(np.median(np.abs(dy))), p90=float(np.percentile(np.abs(dy), 90)), bias=float(np.median(dy)),
                within1=float(np.mean(np.abs(dy) < 1)), n=int(len(dy)), per_frame_bias=[round(p, 2) for p in per],
                central=float(np.median(np.abs(dy[central]))) if central.any() else None,
                periphery=float(np.median(np.abs(dy[~central]))) if (~central).any() else None)


CROPS = {"raw": bi("原始标定", "calibration as delivered"), "center": bi("中心裁剪", "centre crop"),
         "top": bi("顶部对齐裁剪", "top-aligned crop"), "bottom": bi("底部对齐裁剪", "bottom-aligned crop"),
         "scale": bi("等比缩放", "uniform scaling")}


def _crop_variants(c0, c1, vs):
    """Calibration variants for a video whose size differs from the calibration's: (code, cam0, cam1)."""
    (wc, hc), (wv, hv) = c0["size"], vs
    out = [("raw", 0.0, 0.0, 1.0)]
    if wc == wv and hc != hv:
        dh = hc - hv
        out += [("center", 0.0, -dh / 2, 1.0), ("top", 0.0, 0.0, 1.0), ("bottom", 0.0, -dh, 1.0)]
    elif hc == hv and wc != wv:
        dw = wc - wv
        out += [("center", -dw / 2, 0.0, 1.0), ("bottom", -dw, 0.0, 1.0)]
    elif wc and wv and abs(wv / wc - hv / hc) < 1e-3:
        out += [("scale", 0.0, 0.0, wv / wc)]
    variants = []
    for code, dx, dy, sc in out:
        cc = []
        for c in (c0, c1):
            c = copy.deepcopy(c)
            c["K"] = c["K"].copy(); c["K"][:2] *= sc; c["K"][0, 2] += dx; c["K"][1, 2] += dy
            c["size"] = vs
            cc.append(c)
        variants.append((code, cc[0], cc[1]))
    return variants


def run(rec, ctx):
    out, opts = ctx["out"], ctx["opts"]
    metrics, details, advice, figs = Metrics(), [], [], []
    cal, csrc = rec.raw_calib()
    c0, c1 = cal if cal else (None, None)
    if c0 is not None:
        vs = rec.video_size()
        ctx["progress"](bi("标定：抽帧做极线检验", "Calibration: epipolar test on sampled frames"))
        frames = sample_frames(rec, N_FRAMES, opts["max_seconds"])
        variants = _crop_variants(c0, c1, vs) if vs and tuple(vs) != tuple(c0["size"]) else [("raw", c0, c1)]
        results = []
        for code, a, b in variants:
            st = _stats(Rectifier(a, b), frames[:6] if len(variants) > 1 else frames)
            if st:
                results.append((st["median"], code, a, b, st))
        if not results:
            return check("geometry", WARN, bi("画面纹理太少，无法做极线检验。", "Too little texture for the epipolar test."), metrics)
        results.sort(key=lambda x: x[0])
        _, code, a, b, st = results[0]
        if len(variants) > 1:
            st = _stats(Rectifier(a, b), frames)
        rec.calib_eff, rec.calib_metric, rec.calib_source = (a, b), True, csrc
        rect = Rectifier(a, b)
        metrics.add("标定来源", "Calibration source", {"file": bi("随录制的 yaml", "yaml next to the recording"),
                                                         "mcap": bi("MCAP 附件", "MCAP attachment")}.get(csrc, csrc))
        metrics.add("基线", "Baseline", f"{rect.B * 1000:.1f} mm")
        metrics.add("两目相对旋转", "Rotation between the eyes", f"{np.degrees(np.linalg.norm(cv2.Rodrigues(b['R'])[0])):.2f}°")
        metrics.add("极线误差 |dy| 中位", "Epipolar error |dy| median", f"{st['median']:.2f} px")
        metrics.add("<1 px 占比", "Share < 1 px", f"{100 * st['within1']:.0f}%")
        metrics.add("系统偏移（dy 中位）", "Systematic offset (median dy)", f"{st['bias']:+.2f} px")
        metrics.add("匹配点", "Matches", st["n"])
        if vs and tuple(vs) != tuple(c0["size"]):
            order = sorted(results, key=lambda r: r[1] != "raw")
            details.append(bi(f"标定写的是 {c0['size'][0]}×{c0['size'][1]}，视频是 {vs[0]}×{vs[1]}。"
                              + "；".join(f"{CROPS[r[1]]['zh']} {r[0]:.2f} px" for r in order) + "。",
                              f"The calibration is for {c0['size'][0]}×{c0['size'][1]}, the video is {vs[0]}×{vs[1]}. "
                              + "; ".join(f"{CROPS[r[1]]['en']} {r[0]:.2f} px" for r in order) + "."))
        figs.append(_preview(rec, rect, frames, out))
        status = PASS if st["median"] <= T["epi_pass_px"] and abs(st["bias"]) <= T["epi_bias_px"] else (
            WARN if st["median"] <= T["epi_warn_px"] else FAIL)
        if code != "raw":
            advice.append(bi(f"这批数据的标定没有按视频分辨率修正，应按“{CROPS[code]['zh']}”调整主点后使用（EgoQC 已自动这样做）。",
                             f"The calibration was not adapted to the video resolution; shift the principal point for a "
                             f"{CROPS[code]['en']} before use (EgoQC already did)."))
            return check("geometry", FAIL, bi(f"标定分辨率与视频不符；按{CROPS[code]['zh']}修正后极线误差 {st['median']:.2f} px。",
                                              f"Calibration resolution does not match the video; after a {CROPS[code]['en']} "
                                              f"correction the epipolar error is {st['median']:.2f} px."),
                         metrics, details, advice, figs, correctable=True)
        md, bs = st["median"], st["bias"]
        summary = {PASS: bi(f"出厂标定与画面吻合：极线误差中位 {md:.2f} px，无系统偏移。",
                            f"The factory calibration fits the images: median epipolar error {md:.2f} px, no systematic offset."),
                   WARN: bi(f"标定基本可用：极线误差中位 {md:.2f} px，系统偏移 {bs:+.2f} px。",
                            f"Calibration roughly fits: median epipolar error {md:.2f} px, systematic offset {bs:+.2f} px."),
                   FAIL: bi(f"标定与画面不吻合：极线误差中位 {md:.2f} px，系统偏移 {bs:+.2f} px。",
                            f"Calibration does not fit the images: median epipolar error {md:.2f} px, systematic offset {bs:+.2f} px.")}[status]
        if status != PASS:
            advice.append(bi("请厂商复核这台设备的标定（标定后结构是否变动、分辨率/裁剪模式是否对应）。",
                             "Ask the vendor to re-check this device's calibration (did the rig change after calibration, does "
                             "it match the resolution/crop mode?)."))
        return check("geometry", status, summary, metrics, details, advice, figs)

    if not opts.get("selfcalib", True):
        return check("geometry", NA, bi("没有标定，且未开启自标定。", "No calibration, and self-calibration is off."), metrics,
                     advice=[bi("开启“无标定时自标定”，或向厂商索取出厂标定。",
                                "Turn on self-calibration, or ask the vendor for the factory calibration.")])
    ctx["progress"](bi("自标定（COLMAP，约 3–10 分钟）", "Self-calibration (COLMAP, about 3–10 min)"))
    sc = selfcalib.run(rec, os.path.join(rec.tmp, "selfcalib"), ctx["progress"], opts["max_seconds"])
    if not sc:
        return check("geometry", FAIL, bi("没有标定，自标定也没有成功（画面纹理或运动不足）。",
                                          "No calibration, and self-calibration failed (too little texture or motion)."), metrics,
                     advice=[bi("向厂商索取出厂标定。", "Ask the vendor for the factory calibration.")])
    rec.calib_eff, rec.calib_metric, rec.calib_source = (sc["c0"], sc["c1"]), False, "self"
    rect = Rectifier(sc["c0"], sc["c1"])
    ctx["progress"](bi("自标定：用未参与标定的帧检验", "Self-calibration: testing on frames it did not use"), 0.9)
    frames = sample_frames(rec, N_FRAMES, opts["max_seconds"], exclude_k=sc["used_k"])
    st = _stats(rect, frames)
    pl, pr = sc["params"]["L"], sc["params"]["R"]
    metrics.add("标定来源", "Calibration source", bi("自标定（COLMAP）", "self-calibration (COLMAP)"))
    metrics.add("参与重建", "Images registered", f"{sc['n_registered']}/{sc['n_images']}")
    metrics.add("左目 fx / cx / cy", "Left fx / cx / cy", f"{pl[0]:.1f} / {pl[2]:.1f} / {pl[3]:.1f}")
    metrics.add("右目 fx / cx / cy", "Right fx / cx / cy", f"{pr[0]:.1f} / {pr[2]:.1f} / {pr[3]:.1f}")
    metrics.add("两目相对旋转", "Rotation between the eyes", f"{sc['rel_rot_deg']:.2f}°")
    metrics.add("逐帧相对旋转波动", "Per-frame rig rotation jitter", f"{sc['rig_rot_dev_deg'][0]:.3f}° (p90 {sc['rig_rot_dev_deg'][1]:.3f}°)")
    metrics.add("逐帧基线长度波动", "Per-frame baseline jitter", f"{100 * sc['rig_baseline_dev']:.2f}%")
    metrics.add("用时", "Time", f"{sc['seconds']:.0f} s")
    if st:
        metrics.add("极线误差 |dy| 中位（未参与标定的帧）", "Epipolar error |dy| median (unused frames)", f"{st['median']:.2f} px")
        metrics.add("系统偏移", "Systematic offset", f"{st['bias']:+.2f} px")
        figs.append(_preview(rec, rect, frames, out))
    details.append(bi("没有出厂标定，以上为自标定结果；基线真实长度未知，三维结果只有相对尺度。",
                      "No factory calibration: these are self-calibration results. The true baseline is unknown, so 3D is "
                      "only up to scale."))
    details.append(bi("逐帧相对位姿稳定说明两目结构刚性好、曝光同步；波动大则说明结构松动或两目不同时曝光。",
                      "A steady per-frame relative pose means a rigid rig and synchronised exposures; jitter means a loose "
                      "rig or eyes exposed at different times."))
    advice.append(bi("自标定只能代替出厂标定做检验；正式使用需要厂商的出厂标定（含基线长度），或补拍带已知尺寸标定板的录像。",
                     "Self-calibration is only good for checking. For real use get the factory calibration (with the "
                     "baseline), or record a calibration target of known size."))
    stable = sc["rig_rot_dev_deg"][0] < 0.05
    if st and st["median"] <= T["epi_pass_px"] and stable:
        return check("geometry", WARN, bi(f"无出厂标定；自标定后两目几何一致（极线误差 {st['median']:.2f} px，逐帧位姿稳定），但绝对尺度未知。",
                                          f"No factory calibration; after self-calibration the two eyes agree (epipolar error "
                                          f"{st['median']:.2f} px, steady rig), but the absolute scale is unknown."),
                     metrics, details, advice, figs)
    return check("geometry", FAIL, bi("无出厂标定，自标定后两目几何仍不一致。",
                                      "No factory calibration, and the eyes still disagree after self-calibration."),
                 metrics, details, advice, figs)


def _preview(rec, rect, frames, out):
    L, R = frames[len(frames) // 2]
    img = rectified_preview(rect, L, R, scale=0.4)
    p = os.path.join(out, "rectified.jpg")
    cv2.imwrite(p, img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return os.path.basename(p)
