"""码率与压缩 / Bitrate & compression: bits per pixel per picture and eye. Too few bits blur fingers, tool tips and
fine texture."""
import os

from ..config import FAIL, PASS, T, WARN
from ..util import INTRA_CODECS, Metrics, bi, check, probe


def run(rec, ctx):
    metrics, details, advice = Metrics(), [], []
    if rec.layout == "sbs":
        v = rec.meta["video"]
        codec = (v["codec"] or "").lower()
        n_real = len(rec.distinct) if rec.distinct is not None else v["frames"]
        real_fps = n_real / v["duration"] if v["duration"] else v["fps"]
        per_frame_fps = v["fps"] if codec in INTRA_CODECS else real_fps   # intra codecs store every repeat in full
        bpp = v["bit_rate"] / (v["width"] * v["height"] * per_frame_fps) if per_frame_fps else 0
        eye_rate = v["bit_rate"] / 2
        w, h = v["width"] // 2, v["height"]
    else:
        src = rec.sources()
        info = probe(src["left"][0]) if src["left"] else {}
        codec = (info.get("codec") or "h264").lower()
        tl = src["ts_left"]
        dur = (tl[-1] - tl[0]) / 1e6 if len(tl) > 1 else 0
        size = sum(os.path.getsize(f) for f in src["left"])
        eye_rate = size * 8 / dur if dur else 0
        w, h = rec.video_size() or (info.get("width"), info.get("height"))
        real_fps = (len(tl) - 1) / dur if dur else 0
        bpp = eye_rate / (w * h * real_fps) if real_fps and w else 0
    intra = codec in INTRA_CODECS
    p, wv = (T["bpp_mjpeg_pass"], T["bpp_mjpeg_warn"]) if intra else (T["bpp_h264_pass"], T["bpp_h264_warn"])
    mb = eye_rate / 1e6
    metrics.add("编码", "Codec", codec.upper()).add("单目码率", "Bitrate per eye", f"{mb:.1f} Mbit/s").add("单目分辨率", "Resolution per eye", f"{w}×{h}")
    metrics.add("每像素比特（每个真实画面）", "Bits per pixel (per real picture)", f"{bpp:.3f}")
    metrics.add("参考线", "Reference", bi(f"≥ {p}（{'帧内' if intra else '帧间'}编码）", f"≥ {p} ({'intra' if intra else 'inter'}-frame codec)"))
    details.append(bi("每像素比特 = 码率 ÷（分辨率 × 每秒画面数）；帧内编码（MJPEG）的重复帧也占满一帧，按标称帧率算。",
                      "Bits per pixel = bitrate ÷ (resolution × pictures per second); with intra-frame codecs (MJPEG) repeated "
                      "frames cost a full frame too, so the nominal rate is used."))
    if bpp >= p:
        return check("quality", PASS, bi(f"单目 {mb:.1f} Mbit/s，每像素 {bpp:.3f} bit，压缩不影响细节。",
                                         f"{mb:.1f} Mbit/s per eye, {bpp:.3f} bit per pixel: compression keeps the detail."),
                     metrics, details)
    status = WARN if bpp >= wv else FAIL
    advice.append(bi("提高录制码率（H.264 建议每目 ≥ 8 Mbit/s @1600×1200 30 fps），低码率会让手指、工具尖等细节变糊。",
                     "Raise the recording bitrate (H.264: ≥ 8 Mbit/s per eye at 1600×1200, 30 fps); low bitrates blur "
                     "fingers, tool tips and other fine detail."))
    return check("quality", status, bi(f"码率偏低：单目 {mb:.1f} Mbit/s，每像素只有 {bpp:.3f} bit。",
                                       f"Bitrate too low: {mb:.1f} Mbit/s per eye, only {bpp:.3f} bit per pixel."),
                 metrics, details, advice)
