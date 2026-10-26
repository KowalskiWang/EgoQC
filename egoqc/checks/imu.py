"""相机-IMU / Camera–IMU: IMU stream health, video<->IMU time offset (and whether it stays constant), axes and gyro units.

Camera motion per frame pair: background features of the left view are triangulated with the stereo calibration
and tracked into the next frame; PnP gives the rotation (head translation does not bias it; rotation needs no
metric scale, so a self-calibration works too). Gyro speed is cross-correlated with the camera's angular speed
in 8 s windows -> offset per window (t_imu = t_video + offset); the rotation increments are then aligned with
the integrated gyro (rotation + scale) -> IMU->camera rotation and gyro units.
"""
import os

import cv2
import numpy as np
from scipy.spatial.transform import Rotation as Rot

from .. import charts
from ..config import FAIL, NA, PASS, T, WARN
from ..stereo import F, Rectifier
from ..util import Metrics, bi, check


def _umeyama(src, dst):
    U, S, Vt = np.linalg.svd(dst.T @ src)
    D = np.diag([1, 1, np.sign(np.linalg.det(U @ Vt))])
    R = U @ D @ Vt
    return R, float(np.sum(dst * (src @ R.T)) / max(np.sum(src * src), 1e-12))


def camera_motion(rec, rect, max_seconds, progress):
    """List of (t_mid_us, rotvec_cam (rad), dt_s) between consecutive frames (PnP on stereo-triangulated points)."""
    c0 = rect.c0
    w, h = rect.size
    mask = np.zeros((h, w), np.uint8); mask[: int(h * 0.6)] = 255
    lk = dict(winSize=(25, 25), maxLevel=5, criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, 0.01))
    out, prev = [], None
    dur = rec.duration()
    n_exp = rec.n_pairs() * (min(1.0, max_seconds / dur) if dur > 0 else 1.0)
    for i, (k, t, L, R) in enumerate(rec.pairs(max_seconds=max_seconds)):
        gL, gR = cv2.cvtColor(L, cv2.COLOR_BGR2GRAY), cv2.cvtColor(R, cv2.COLOR_BGR2GRAY)
        if gL.shape[::-1] != (w, h):
            gL, gR = cv2.resize(gL, (w, h)), cv2.resize(gR, (w, h))
        if prev is not None:
            pgL, pgR, pt = prev
            p0 = cv2.goodFeaturesToTrack(pgL, 500, 0.01, 12, mask=mask)
            if p0 is not None and len(p0) >= 40:
                pr, sr, _ = cv2.calcOpticalFlowPyrLK(pgL, pgR, p0, None, **lk)
                p1, s1, _ = cv2.calcOpticalFlowPyrLK(pgL, gL, p0, None, **lk)
                ok = (sr.ravel() == 1) & (s1.ravel() == 1)
                rl, rr = rect.rect_points(p0.reshape(-1, 2), 0), rect.rect_points(pr.reshape(-1, 2), 1)
                disp = rl[:, 0] - rr[:, 0]
                ok &= (np.abs(rl[:, 1] - rr[:, 1]) < 1.5) & (disp > 2.0)
                n1 = cv2.fisheye.undistortPoints(p1.reshape(-1, 1, 2).astype(np.float64), c0["K"], c0["D"]).reshape(-1, 2)
                ok &= np.linalg.norm(n1, axis=1) < 2.5
                if ok.sum() >= 25:
                    Xc = rect.triangulate(rl[ok], rr[ok])
                    good = np.isfinite(Xc).all(1)
                    if good.sum() >= 20:
                        res = cv2.solvePnPRansac(Xc[good], n1[ok][good], np.eye(3), None, reprojectionError=1.5 / F,
                                                 iterationsCount=150, flags=cv2.SOLVEPNP_SQPNP)
                        if res[0] and res[3] is not None and len(res[3]) >= 15:
                            out.append(((pt + t) / 2, -res[1].ravel(), (t - pt) / 1e6))
        prev = (gL, gR, t)
        if i % 200 == 0:
            progress(bi(f"IMU：估计相机转动 {i} 帧", f"IMU: camera rotation, frame {i}"), min(0.9, i / max(n_exp, 1)))
    return out


def run(rec, ctx):
    imu = rec.imu()
    if imu is None:
        return check("imu", NA, bi("这段录制没有 IMU 数据。", "This recording has no IMU data."))
    metrics, details, advice, figs = Metrics(), [], [], []
    t = imu["t_us"].astype(np.int64); g = imu["gyro"].astype(float)
    dt = np.diff(t) / 1000.0
    rate_med = 1000.0 / np.median(dt) if len(dt) else 0
    span = (t[-1] - t[0]) / 1e6 if len(t) > 1 else 0
    gaps20, gaps100, gmax = int((dt > T["imu_gap_warn_ms"]).sum()), int((dt > T["imu_gap_fail_ms"]).sum()), float(dt.max()) if len(dt) else 0
    frz = sum(b - a for a, b in imu.get("freeze", []))
    metrics.add("IMU 采样率", "IMU rate", bi(f"{rate_med:.0f} Hz（平均 {len(t) / max(span, 1e-6):.0f} Hz）",
                                             f"{rate_med:.0f} Hz (mean {len(t) / max(span, 1e-6):.0f} Hz)"))
    metrics.add(f">{T['imu_gap_warn_ms']} ms 断档", f"Gaps > {T['imu_gap_warn_ms']} ms", gaps20)
    metrics.add("最长断档", "Longest gap", f"{gmax:.0f} ms")
    metrics.add("数值冻结", "Frozen values", f"{frz:.1f} s" if frz else bi("无", "none"))
    acc_u = imu["acc_unit"].rstrip("*")
    scaled = imu["acc_unit"].endswith("*")
    metrics.add("单位", "Units", bi(f"陀螺 {imu['gyro_unit']}，加速度 {acc_u}" + ("（按重力估算比例）" if scaled else ""),
                                    f"gyro {imu['gyro_unit']}, accel {acc_u}" + (" (scaled by gravity)" if scaled else "")))
    stream_status = PASS
    if rate_med < T["imu_rate_min"] or gaps100 or frz > T["imu_freeze_s"]:
        stream_status = WARN
        if gaps100:
            details.append(bi(f"IMU 有 {gaps100} 处超过 {T['imu_gap_fail_ms']} ms 的断档，最长 {gmax:.0f} ms。",
                              f"The IMU has {gaps100} gaps longer than {T['imu_gap_fail_ms']} ms, the longest {gmax:.0f} ms."))
        if frz > T["imu_freeze_s"]:
            details.append(bi(f"IMU 有 {frz:.1f} s 数值完全不变（冻结）。", f"IMU values are frozen (unchanged) for {frz:.1f} s."))
        advice.append(bi("请厂商排查 IMU 断档/冻结，并提供 IMU 型号、量程、单位和数据格式说明。",
                         "Ask the vendor about the IMU gaps/freezes, and for the IMU model, range, units and data format."))
    if rec.calib_eff is None:
        return check("imu", stream_status if stream_status != PASS else WARN,
                     bi("IMU 数据流已检查；没有可用的相机标定，无法检验与视频的时间对齐。",
                        "IMU stream checked; without a usable camera calibration its time alignment with the video cannot be tested."),
                     metrics, details, advice)
    rect = Rectifier(*rec.calib_eff)
    ctx["progress"](bi("IMU：估计相机转动", "IMU: estimating camera rotation"))
    mot = camera_motion(rec, rect, ctx["opts"]["max_seconds"], ctx["progress"])
    if len(mot) < 40:
        return check("imu", WARN, bi("画面中可用于估计相机转动的背景特征太少，无法检验时间对齐。",
                                     "Too few background features to estimate the camera's rotation; time alignment not tested."),
                     metrics, details, advice)
    tc = np.array([m[0] for m in mot], float); wc = np.array([m[1] for m in mot]); dtc = np.array([m[2] for m in mot])
    speed = np.linalg.norm(wc, axis=1) / dtc
    # gyro speed smoothed over one frame interval
    gb = g - np.median(g, 0)
    gspeed = np.linalg.norm(gb, axis=1)
    k = max(1, int(round(np.median(dtc) * rate_med)))
    gsm = np.convolve(gspeed, np.ones(k) / k, mode="same")
    tg = (t - t[0]) / 1e6; tcs = (tc - t[0]) / 1e6
    has_ts = rec.has_frame_timestamps
    rng = (-0.3, 0.3, 0.001) if has_ts else (-6.0, 6.0, 0.01)   # (lo, hi, step) in seconds

    def best_offset(sel, lo, hi, st):
        best = (-2, 0.0)
        for off in np.arange(lo, hi, st):
            x = tcs[sel] + off
            ok = (x > tg[0] + 0.2) & (x < tg[-1] - 0.2)
            if ok.sum() < 15:
                continue
            c = np.corrcoef(speed[sel][ok], np.interp(x[ok], tg, gsm))[0, 1]
            if c > best[0]:
                best = (c, off)
        return best

    allsel = np.ones(len(tcs), bool)
    c_all, off_all = best_offset(allsel, *rng)
    if not has_ts:
        c_all, off_all = best_offset(allsel, off_all - 0.1, off_all + 0.1, 0.002)
    win = []
    for lo in np.arange(tcs[0], tcs[-1] - 4, 4.0):
        sel = (tcs >= lo) & (tcs < lo + 8)
        if sel.sum() < 15:
            continue
        c, o = best_offset(sel, off_all - (0.15 if has_ts else 3.0), off_all + (0.15 if has_ts else 3.0), 0.001 if has_ts else 0.01)
        win.append((lo + 4, o * 1000, c))
    good = [w for w in win if w[2] >= 0.5]
    spread = (np.percentile([w[1] for w in good], 90) - np.percentile([w[1] for w in good], 10)) if len(good) >= 3 else 0.0
    ical = rec.imu_calib()
    has_ref = bool(ical and ical.get("timeshift_s") is not None)
    ref_ms = ical["timeshift_s"] * 1000 if has_ref else (None if has_ts else 0.0)
    if win:
        figs.append(charts.offsets_over_time(np.array([w[0] for w in win]), np.array([w[1] for w in win]), np.array([w[2] for w in win]),
                                             ref_ms, os.path.join(rec_out(ctx), "imu_offset.png"),
                                             bi("每 8 秒窗口测得的 IMU − 视频时间偏移（点越大相关越好）",
                                                "IMU − video offset per 8 s window (bigger dot = better correlation)"),
                                             bi("标定值", "calibration") if has_ref else bi("文件约定（0）", "file convention (0)")))
    metrics.add("测得时间偏移（IMU − 视频）", "Measured offset (IMU − video)",
                bi(f"{off_all * 1000:+.0f} ms（相关 {c_all:.2f}）", f"{off_all * 1000:+.0f} ms (r = {c_all:.2f})"))
    metrics.add("各窗口偏移范围（p10–p90）", "Spread over windows (p10–p90)", f"{spread:.0f} ms")
    if ref_ms is not None:
        metrics.add("参考值", "Reference", bi(f"{ref_ms:+.1f} ms（{'标定文件' if has_ref else '按文件约定应为 0'}）",
                                              f"{ref_ms:+.1f} ms ({'calibration file' if has_ref else 'file convention: 0'})"))
    # rotation + gyro units from increments
    cum = np.vstack([np.zeros(3), np.cumsum(0.5 * (gb[1:] + gb[:-1]) * np.diff(tg)[:, None], axis=0)])
    ta, tb = tcs - dtc / 2 + off_all, tcs + dtc / 2 + off_all
    ins = (ta > tg[0]) & (tb < tg[-1]) & (np.linalg.norm(wc, axis=1) > np.radians(0.2))
    pr = np.stack([np.interp(tb, tg, cum[:, c]) - np.interp(ta, tg, cum[:, c]) for c in range(3)], 1)
    rot_txt = None
    if ins.sum() >= 30:
        R, s = _umeyama(pr[ins], wc[ins])
        resid = np.median(np.linalg.norm(wc[ins] - s * pr[ins] @ R.T, axis=1)) / np.median(np.linalg.norm(wc[ins], axis=1))
        metrics.add("转动增量残差", "Rotation increment residual", f"{100 * resid:.0f}%")
    if ins.sum() >= 30 and resid > 0.5:
        details.append(bi(f"即使按测得的偏移对齐，转动增量残差仍有 {100 * resid:.0f}%：逐帧时刻不准（帧时间抖动、丢帧或偏移漂移），"
                          "陀螺单位和 IMU 外参无法从这段数据可靠估计。",
                          f"Even aligned at the measured offset, rotation increments leave a {100 * resid:.0f}% residual: frame "
                          "times are inaccurate (jitter, drops or drifting offset), so gyro units and IMU extrinsics cannot be "
                          "estimated reliably from this recording."))
    elif ins.sum() >= 30:
        if imu["gyro_unit"] == "raw":
            metrics.add("陀螺单位（估计）", "Gyro units (estimated)", f"{1 / (s * 180 / np.pi):.1f} LSB/(°/s)")
        else:
            metrics.add("陀螺尺度", "Gyro scale", bi(f"{s:.3f}（应为 1）", f"{s:.3f} (should be 1)"))
        if ical is not None:
            Rc = ical["T_cam_imu"][:3, :3]
            rot_txt = float(np.degrees(Rot.from_matrix(R @ Rc.T).magnitude()))
            metrics.add("IMU 外参旋转与标定之差", "IMU rotation vs calibration", f"{rot_txt:.2f}°")
        else:
            ang = np.degrees(Rot.from_matrix(R).magnitude())
            metrics.add("IMU 到相机旋转（估计）", "IMU-to-camera rotation (estimated)", bi(f"{ang:.1f}° 偏离单位阵", f"{ang:.1f}° from identity"))
        if resid > 0.3:
            details.append(bi(f"按测得的偏移对齐后转动增量残差 {100 * resid:.0f}%，逐帧时刻有一定误差。",
                              f"Aligned at the measured offset, rotation increments leave a {100 * resid:.0f}% residual: "
                              "frame times carry some error."))
    details.append(bi("方法：用双目三角化的背景点和 PnP 求每帧的相机转动，与陀螺仪转速逐窗做相关。偏移为正表示同一动作在 IMU 里出现得更晚。",
                      "Method: the camera's rotation per frame comes from PnP on stereo-triangulated background points and is "
                      "correlated with the gyro speed window by window. A positive offset means the same motion appears later "
                      "in the IMU."))
    # status
    status, summary, corr_ok = stream_status, None, c_all >= 0.5
    off = off_all * 1000
    if not corr_ok:
        status = max_status(status, WARN)
        summary = bi(f"相机转动与陀螺仪相关性低（r = {c_all:.2f}），时间对齐无法可靠测量。",
                     f"Camera rotation and gyro correlate poorly (r = {c_all:.2f}); the time alignment cannot be measured reliably.")
    elif spread > T["imu_offset_spread_ms"]:
        status = FAIL
        summary = bi(f"视频与 IMU 的时间偏移不稳定：各时间段相差 {spread:.0f} ms（整体约 {off:+.0f} ms）。",
                     f"The video–IMU time offset drifts: {spread:.0f} ms between time windows (about {off:+.0f} ms overall).")
        advice.append(bi("需要每帧与 IMU 同一时钟的硬件时间戳。", "Needs a hardware timestamp per frame on the IMU's clock."))
    elif ref_ms is not None:
        diff = abs(off - ref_ms)
        st2 = PASS if diff <= T["imu_offset_pass_ms"] else (WARN if diff <= T["imu_offset_warn_ms"] else FAIL)
        status = max_status(status, st2)
        if has_ts:
            summary = bi(f"时间偏移 {off:+.0f} ms，标定值 {ref_ms:+.1f} ms，相差 {diff:.0f} ms" + ("，一致。" if st2 == PASS else "。"),
                         f"Time offset {off:+.0f} ms, calibration {ref_ms:+.1f} ms: {diff:.0f} ms apart"
                         + (", consistent." if st2 == PASS else "."))
        else:
            summary = bi(f"按文件约定（第 k 帧 = 起始 + k/帧率）对齐，视频与 IMU 相差 {off:+.0f} ms" + ("，一致。" if st2 == PASS else "。"),
                         f"Aligned by the file convention (frame k = start + k/fps), video and IMU are {off:+.0f} ms apart"
                         + (", consistent." if st2 == PASS else "."))
        if st2 == FAIL:
            advice.append(bi("按测得的偏移校正后再融合 IMU，或请厂商修正时间同步。",
                             "Correct by the measured offset before fusing the IMU, or ask the vendor to fix the time sync."))
    else:
        summary = bi(f"时间偏移 {off:+.0f} ms，没有标定值可比较。", f"Time offset {off:+.0f} ms; no calibration value to compare with.")
    if rot_txt is not None and rot_txt > 3:
        status = max_status(status, WARN)
        details.append(bi(f"测得的 IMU 外参旋转与标定相差 {rot_txt:.1f}°。", f"The measured IMU rotation differs from the calibration by {rot_txt:.1f}°."))
    if stream_status != PASS and status == PASS:
        status = WARN
    if stream_status != PASS:
        summary = bi(summary["zh"] + "IMU 数据流有断档或冻结。", summary["en"] + " The IMU stream has gaps or freezes.")
    return check("imu", status, summary, metrics, details, advice, figs)


ORDER = [PASS, WARN, FAIL]


def max_status(a, b):
    return max(a, b, key=lambda s: ORDER.index(s) if s in ORDER else -1)


def rec_out(ctx):
    return ctx["out"]
