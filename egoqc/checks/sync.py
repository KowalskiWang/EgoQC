"""左右同步 / Left/right sync: are the two views captured at the same instant and paired correctly?

Separate streams: |t_left - t_right| of paired frames + cross-correlation of the per-frame motion energy of the
two views (the peak must sit at lag 0). Side-by-side video: both halves must always change together.
"""
import cv2
import numpy as np

from ..config import FAIL, PASS, T, WARN
from ..util import Metrics, bi, check


def _lag(a, b, max_lag=30):
    a = (a - a.mean()) / (a.std() + 1e-9); b = (b - b.mean()) / (b.std() + 1e-9)
    lags = np.arange(-max_lag, max_lag + 1)
    cc = [np.mean(a[max(0, -l):len(a) - max(0, l)] * b[max(0, l):len(b) - max(0, -l)]) for l in lags]
    i = int(np.argmax(cc))
    return int(lags[i]), float(cc[i])


def run(rec, ctx):
    metrics, details, advice = Metrics(), [], []
    if rec.layout == "sbs":
        if getattr(rec, "_half_mad", None) is not None:
            dl, dr = rec._half_mad
        else:
            ctx["progress"](bi("同步：比较左右两半的变化", "Sync: comparing how both halves change"))
            prev, dl, dr = None, [], []
            for _, _, L, R in rec.pairs(max_seconds=ctx["opts"]["max_seconds"]):
                g = cv2.cvtColor(cv2.resize(np.hstack([L, R]), (960, 270)), cv2.COLOR_BGR2GRAY).astype(np.int16)
                if prev is not None:
                    d = np.abs(g - prev); dl.append(d[:, :480].mean()); dr.append(d[:, 480:].mean())
                prev = g
            dl, dr = np.array(dl), np.array(dr)
        moving = (dl > 2.0) | (dr > 2.0)
        one = int((((dl < 0.7) & (dr > 2.0)) | ((dr < 0.7) & (dl > 2.0))).sum())
        corr = float(np.corrcoef(dl, dr)[0, 1]) if len(dl) > 3 else float("nan")
        metrics.add("只有一半更新的帧", "Frames where only one half changed", one)
        metrics.add("两半变化量相关", "Correlation of the halves' changes", f"{corr:.3f}")
        metrics.add("参与比较的画面变化", "Picture changes compared", int(moving.sum()))
        details.append(bi("左右两目在同一张拼接画面里，帧级配对天然一致；这里检查两半是否总是同时更新。曝光是否严格同步见“双目标定与几何”的逐帧位姿稳定性（自标定时）。",
                          "Both eyes share one side-by-side picture, so frame pairing is given; this checks that both halves "
                          "always update together. Whether the exposures are simultaneous shows in the per-frame rig stability "
                          "under \"Stereo calibration\" (with self-calibration)."))
        if one == 0:
            return check("sync", PASS, bi("左右两半总是同时更新，没有一目卡住的情况。", "Both halves always update together; no eye ever freezes."),
                         metrics, details)
        status = WARN if one <= 0.005 * max(moving.sum(), 1) else FAIL
        advice.append(bi("检查两路传感器的采集与拼接，确保每张拼接画面的左右来自同一时刻。",
                         "Check the capture and stitching of both sensors so that each side-by-side picture holds one instant."))
        return check("sync", status, bi(f"有 {one} 帧只有一半画面更新，左右可能不同步。",
                                        f"{one} frames updated only one half: left and right may be out of sync."), metrics, details, advice)
    src = rec.sources()
    tl, tr = src["ts_left"], src["ts_right"]
    j = np.clip(np.searchsorted(tr, tl), 1, len(tr) - 1)
    d = np.minimum(np.abs(tl - tr[j - 1]), np.abs(tl - tr[j]))
    ok = d < 8000
    if not ok.any():
        return check("sync", FAIL, bi("左右时间戳完全对不上（相差都超过 8 ms）。", "Left and right timestamps never match (all > 8 ms apart)."),
                     metrics)
    metrics.add("左右时间戳平均差", "Mean |t_left − t_right|", f"{d[ok].mean():.0f} µs")
    metrics.add("最大差", "Max difference", f"{d[ok].max() / 1000:.2f} ms")
    metrics.add("配上对的帧", "Frames paired", f"{ok.sum()}/{len(tl)}")
    ctx["progress"](bi("同步：左右运动互相关", "Sync: cross-correlating left/right motion"))
    eL, eR, prev = [], [], None
    for _, _, L, R in rec.pairs(max_seconds=min(ctx["opts"]["max_seconds"], 120)):
        gl = cv2.cvtColor(cv2.resize(L, (400, 300)), cv2.COLOR_BGR2GRAY).astype(np.float32)
        gr = cv2.cvtColor(cv2.resize(R, (400, 300)), cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            eL.append(np.abs(gl[150:] - prev[0][150:]).mean()); eR.append(np.abs(gr[150:] - prev[1][150:]).mean())
        prev = (gl, gr)
    if len(eL) < 20:
        return check("sync", WARN, bi("视频太短，无法做运动互相关。", "Too short for the motion cross-correlation."), metrics, details)
    lag, peak = _lag(np.array(eL), np.array(eR))
    metrics.add("运动互相关峰值", "Motion cross-correlation peak", bi(f"{lag:+d} 帧（r = {peak:.2f}）", f"{lag:+d} frames (r = {peak:.2f})"))
    details.append(bi("方法：两目下半幅（手部区域）逐帧运动能量做互相关，同步时峰值在 0 帧。",
                      "Method: per-frame motion energy of the lower half (hand area) of both views is cross-correlated; "
                      "in sync the peak sits at 0 frames."))
    if lag != 0:
        advice.append(bi("检查左右帧的配对方式（时间戳、关键帧、丢帧），修正后再用。",
                         "Check how left and right frames are paired (timestamps, keyframes, dropped frames) before use."))
        return check("sync", FAIL, bi(f"左右画面错开 {abs(lag)} 帧（运动互相关峰值在 {lag:+d} 帧）。",
                                      f"Left and right are {abs(lag)} frames apart (correlation peak at {lag:+d})."), metrics, details, advice)
    if d[ok].mean() > T["lr_ts_pass_us"]:
        return check("sync", WARN, bi(f"画面配对正确，但左右时间戳平均相差 {d[ok].mean() / 1000:.1f} ms。",
                                      f"Pairing is right, but left/right timestamps differ by {d[ok].mean() / 1000:.1f} ms on average."),
                     metrics, details)
    return check("sync", PASS, bi(f"左右逐帧同步：时间戳平均差 {d[ok].mean():.0f} µs，运动互相关峰值在 0 帧。",
                                  f"Frame-accurate sync: mean timestamp difference {d[ok].mean():.0f} µs, correlation peak at 0 frames."),
                 metrics, details)
