"""Small, consistent charts for the report (matplotlib, light theme, CJK-capable font).

Every chart is drawn twice, once per report language; the functions return {"zh": file, "en": file}.
"""
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

BLUE, ORANGE, AQUA, RED, INK, INK2, GRID, SURF = "#2a78d6", "#eb6834", "#1baf7a", "#d03b3b", "#0b0b0b", "#52514e", "#e4e3df", "#ffffff"
for fam in ("PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans SC", "Arial Unicode MS", "DejaVu Sans"):
    try:
        matplotlib.font_manager.findfont(fam, fallback_to_default=False)
        plt.rcParams["font.family"] = [fam]
        break
    except Exception:  # noqa: BLE001
        continue
plt.rcParams.update({"axes.unicode_minus": False, "figure.facecolor": SURF, "axes.facecolor": SURF, "axes.edgecolor": GRID,
                     "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2, "text.color": INK, "axes.grid": True,
                     "grid.color": GRID, "grid.linewidth": 0.8, "axes.spines.top": False, "axes.spines.right": False, "font.size": 9})

TXT = {
    "nominal": {"zh": "标称 {:.0f} fps", "en": "nominal {:.0f} fps"},
    "video_time": {"zh": "视频时间（s）", "en": "video time (s)"},
    "real_per_s": {"zh": "真实画面 / 秒", "en": "distinct pictures / s"},
    "imu_minus_video": {"zh": "IMU − 视频（ms）", "en": "IMU − video (ms)"},
    "wrist_depth": {"zh": "手腕深度（m）", "en": "wrist depth (m)"},
    "wrist_depth_rel": {"zh": "手腕深度（按掌长 8 cm 换算，近似 m）", "en": "wrist depth (scaled to an 8 cm palm, ≈ m)"},
    "palm_rel": {"zh": "掌长 / 中位掌长", "en": "palm length / median"},
    "left": {"zh": "左手", "en": "left hand"},
    "right": {"zh": "右手", "en": "right hand"},
    "pooled": {"zh": "两手合并", "en": "both hands"},
}


def _save(fig, path):
    fig.tight_layout(); fig.savefig(path, dpi=130); plt.close(fig)
    return os.path.basename(path)


def _both(draw, path, *args):
    """Draw once per language into <stem>_zh.png / <stem>_en.png."""
    stem, ext = os.path.splitext(path)
    return {lang: draw(lang, f"{stem}_{lang}{ext}", *args) for lang in ("zh", "en")}


def fps_over_time(t, fps, nominal, path, title):
    def draw(lang, p, t, fps, nominal, title):
        fig, ax = plt.subplots(figsize=(6.4, 2.5))
        ax.plot(t, fps, color=ORANGE, lw=2, marker="o", ms=3.5, mec=SURF, mew=1)
        ax.axhline(nominal, color=INK2, lw=1, ls="--")
        ax.text(t[0] if len(t) else 0, nominal * 1.03, TXT["nominal"][lang].format(nominal), color=INK2, fontsize=8)
        ax.set_ylim(0, max(nominal * 1.15, (max(fps) if len(fps) else 0) * 1.1))
        ax.set_xlabel(TXT["video_time"][lang]); ax.set_ylabel(TXT["real_per_s"][lang]); ax.set_title(title[lang], loc="left", fontsize=10)
        return _save(fig, p)
    return _both(draw, path, t, fps, nominal, title)


def offsets_over_time(t, off_ms, corr, ref_ms, path, title, ref_label):
    def draw(lang, p, t, off_ms, corr, ref_ms, title, ref_label):
        fig, ax = plt.subplots(figsize=(6.4, 2.5))
        ax.scatter(t, off_ms, s=12 + 50 * np.clip(corr, 0, 1), color=BLUE, edgecolor=SURF, linewidth=1, zorder=3)
        if ref_ms is not None:
            ax.axhline(ref_ms, color=INK2, lw=1, ls="--")
            ax.text(t[0] if len(t) else 0, ref_ms, f" {ref_label[lang]}", color=INK2, fontsize=8, va="bottom")
        ax.set_xlabel(TXT["video_time"][lang]); ax.set_ylabel(TXT["imu_minus_video"][lang]); ax.set_title(title[lang], loc="left", fontsize=10)
        return _save(fig, p)
    return _both(draw, path, t, off_ms, corr, ref_ms, title, ref_label)


def palm_by_depth(rows, path, title, metric=True):
    """rows: list of (key in left|right|pooled, [(depth_mid, rel_median, n)...])."""
    def draw(lang, p, rows, title, metric):
        fig, ax = plt.subplots(figsize=(6.4, 2.5))
        ys = [1.0]
        for key, pts in rows:
            if not pts:
                continue
            x, y, _ = zip(*pts)
            pooled = key == "pooled"   # the curve the verdict is based on: drawn on top, the single hands lighter
            col = {"left": BLUE, "right": ORANGE}.get(key, INK)
            ax.plot(x, y, color=col, lw=2.6 if pooled else 1.4, alpha=1 if pooled else 0.75,
                    marker="o", ms=4.5 if pooled else 3, mec=SURF, mew=1, label=TXT[key][lang], zorder=4 if pooled else 3)
            ys += list(y)
        ax.axhline(1.0, color=INK2, lw=1, ls="--"); ax.set_ylim(min(0.8, min(ys) - 0.03), max(1.2, max(ys) + 0.03))
        ax.set_xlabel(TXT["wrist_depth" if metric else "wrist_depth_rel"][lang]); ax.set_ylabel(TXT["palm_rel"][lang])
        ax.set_title(title[lang], loc="left", fontsize=10)
        ax.legend(frameon=False, fontsize=8)
        return _save(fig, p)
    return _both(draw, path, rows, title, metric)
