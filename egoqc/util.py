"""Small shared helpers: file discovery, ffprobe, result records, progress output."""
import glob, json, os, subprocess, sys, time

from .config import LABEL, TITLES


def files(root, pattern):
    """Recursive glob that skips macOS AppleDouble files (._*) written on exFAT drives."""
    out = glob.glob(os.path.join(root, "**", pattern), recursive=True)
    return sorted(f for f in out if not os.path.basename(f).startswith("._") and "/._" not in f)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, **kw)


def probe(path):
    """First video stream + format info of a media file (ffprobe), {} if unreadable."""
    r = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path])
    try:
        d = json.loads(r.stdout)
    except Exception:  # noqa: BLE001
        return {}
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return {}
    fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
    n, dd = (fr.split("/") + ["1"])[:2]
    fps = float(n) / float(dd) if float(dd) else 0.0
    fmt = d.get("format", {})
    return dict(codec=v.get("codec_name"), width=int(v.get("width", 0)), height=int(v.get("height", 0)), fps=fps,
                frames=int(v["nb_frames"]) if str(v.get("nb_frames", "")).isdigit() else None,
                duration=float(v.get("duration") or fmt.get("duration") or 0), bit_rate=float(v.get("bit_rate") or fmt.get("bit_rate") or 0),
                encoder=(fmt.get("tags") or {}).get("encoder", ""), size=int(fmt.get("size") or 0))


INTRA_CODECS = {"mjpeg", "prores", "png", "ffv1", "huffyuv", "rawvideo", "dnxhd", "jpeg2000"}


def bi(zh, en=None):
    """Bilingual text. Every sentence a user reads exists in Chinese and English; the report and the app show one
    and can switch. Plain values (numbers, file names, units) can stay plain strings."""
    return {"zh": zh, "en": zh if en is None else en}


def pick(x, lang):
    if isinstance(x, dict) and "zh" in x:
        return x.get(lang) or x["zh"]
    return "" if x is None else str(x)


def bjoin(parts, sep=bi("；", "; ")):
    """Join bilingual (or plain) parts per language."""
    return {lang: pick(sep, lang).join(pick(p, lang) for p in parts) for lang in ("zh", "en")}


class Metrics(list):
    """Ordered (label, value) rows; labels bilingual, values plain or bilingual."""

    def add(self, zh, en, value, value_en=None):
        self.append((bi(zh, en), value if value_en is None else bi(value, value_en)))
        return self


def check(cid, status, summary, metrics=None, details=None, advice=None, figures=None, correctable=False, facts=None):
    """One check result. summary = one plain sentence (bilingual); details/advice = short bullets (bilingual);
    figures = file names (charts come as {zh, en} pairs); facts = machine-readable numbers for other code."""
    return dict(id=cid, title=TITLES[cid], status=status, label=LABEL[status], summary=summary,
                metrics=[list(r) for r in (metrics or [])], details=details or [], advice=advice or [],
                figures=figures or [], correctable=correctable, facts=facts or {})


class Progress:
    """JSON-lines progress on stdout (read by the desktop app) + a human log line on stderr."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.t0 = time.time()

    def __call__(self, frac, msg, **extra):
        if self.enabled:
            print(json.dumps({"progress": round(float(frac), 4), "msg": msg, **extra}, ensure_ascii=False), flush=True)
        print(f"[{time.time() - self.t0:6.0f}s] {pick(msg, 'en')}", file=sys.stderr, flush=True)
