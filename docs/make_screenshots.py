"""Regenerate the README images from a demo report (see demo/make_demo.py).

usage: python docs/make_screenshots.py <job folder of a report> [--chrome PATH]
Writes docs/images/{en,zh}/*.png: the app window (Qt, rendered off-screen), the report overview and a few check
cards (headless Chrome). Needs Google Chrome or Chromium.
"""
import argparse, json, os, shutil, subprocess, sys, tempfile

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

from egoqc import report  # noqa: E402

CHROMES = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium",
           "google-chrome", "chromium", "chromium-browser"]
BG = (246, 245, 242)


def trim(path, pad=24):
    """Cut the empty page background below (and right of) the content."""
    im = Image.open(path).convert("RGB")
    a = np.asarray(im).astype(int)
    diff = np.abs(a - np.array(BG)).sum(2) > 12
    rows, cols = np.where(diff.any(1))[0], np.where(diff.any(0))[0]
    if len(rows):
        im = im.crop((0, 0, min(im.width, cols.max() + pad), min(im.height, rows.max() + pad)))
    im.save(path, optimize=True)


def shoot(chrome, html_path, png, width, height=4000):
    prof = tempfile.mkdtemp(prefix="egoqc_chrome_")
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={prof}",
                    "--blink-settings=preferredColorScheme=1", "--force-device-scale-factor=1", f"--window-size={width},{height}",
                    f"--screenshot={png}", "file://" + html_path], capture_output=True, timeout=120)
    shutil.rmtree(prof, ignore_errors=True)
    trim(png)


def page(body, lang, width):
    return (f'<!doctype html><html data-lang="{lang}"><head><meta charset="utf-8"><style>{report.CSS}'
            f"body{{padding:20px 0}}main{{max-width:{width - 40}px}}</style></head><body><main>{body}</main></body></html>")


def overview_html(job, lang):
    """The report's top: header, verdict and overview table (per-recording details cut off)."""
    full = open(os.path.join(job, "report.html")).read()
    full = full.replace('data-lang="zh"', f'data-lang="{lang}"').replace('data-lang="en"', f'data-lang="{lang}"')
    cut = full.index('<section class="rec"')
    return full[:cut] + "</main></body></html>"


def cards_html(job, res, picks, lang, width):
    cards = []
    for rec_id, cid in picks:
        rec = next(r for r in res["recordings"] if r["id"].endswith(rec_id))
        c = next(c for c in rec["checks"] if c["id"] == cid)
        cards.append(report._card(c, job))
    return page('<div class="cards" style="grid-template-columns:1fr">' + "".join(cards) + "</div>", lang, width)


def app_window(job_root, out_png, lang):
    """The window with the demo report in its history and one job running (Qt off-screen)."""
    code = f"""
import os, sys
sys.path.insert(0, {ROOT!r}); sys.path.insert(0, {os.path.join(ROOT, 'app')!r})
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import egoqc_app as A
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from egoqc.util import bi
app = A.App([]); app.setStyle("Fusion"); app.setStyleSheet(A.CSS)
f = QFont(); f.setFamilies(["PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Helvetica Neue"]); app.setFont(f)
w = A.Main(); w.lang = {lang!r}; w.retranslate()
w.resize(1000, 690); w.show()

def setup():   # after the window's own start-up step (which reloads the history from its report folder)
    w.out_root = {job_root!r}; w.load_history()                 # history: the (sanitised) demo report
    w.out_root, w.out_ok = "~/EgoQC_Reports", True
    h = w.add_row("my_dataset.zip", "/Users/you/Downloads/my_dataset.zip")
    h.update(state="running", msg=bi("[2/3] rec_002：手部检测 420/1500 帧", "[2/3] rec_002: hand detection 420/1500 frames")); w.render_row(h)
    h["row"].bar.setValue(470)
    w.retranslate()

QTimer.singleShot(300, setup)
QTimer.singleShot(1500, lambda: (w.grab().save({out_png!r}), app.quit()))
app.exec()
"""
    subprocess.run([sys.executable, "-I", "-c", code], check=True, capture_output=True)


def sanitised_copy(job, root):
    """Copy of the job with local paths replaced by ~/data/... so no machine-specific path ends up in an image."""
    dst = os.path.join(root, os.path.basename(job))
    shutil.copytree(job, dst, ignore=shutil.ignore_patterns("report.html", "_shot_*"))
    res = json.load(open(os.path.join(dst, "report.json")))
    prefix = os.path.dirname(res["input"])
    fix = lambda p: p.replace(prefix, "~/data", 1) if isinstance(p, str) else p  # noqa: E731
    res["input"] = fix(res["input"]); res["job_dir"] = "~/EgoQC_Reports/" + os.path.basename(job)
    for r in res["recordings"]:
        r["path"] = fix(r.get("path"))
    json.dump(res, open(os.path.join(dst, "report.json"), "w"), ensure_ascii=False, indent=1)
    report.render(res, dst)
    return dst, res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("--chrome", default=next((c for c in CHROMES if shutil.which(c) or os.path.exists(c)), None))
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="egoqc_shots_")
    job, res = sanitised_copy(os.path.abspath(a.job), tmp)
    for lang in ("en", "zh"):
        out = os.path.join(ROOT, "docs", "images", lang); os.makedirs(out, exist_ok=True)
        app_window(tmp, os.path.join(out, "app.png"), lang)
        h = os.path.join(job, f"_shot_overview_{lang}.html")   # inside the job folder so relative paths still work
        open(h, "w").write(overview_html(job, lang))
        shoot(a.chrome, h, os.path.join(out, "report_overview.png"), 1280, 1400)
        for name, picks in (("card_frames", [("demo_sbs_stereo.mp4", "frames")]),
                            ("card_calibration", [("demo_issues", "geometry")]),
                            ("card_imu", [("demo_issues", "imu")]),
                            ("card_selfcalib", [("demo_sbs_stereo.mp4", "geometry")])):
            h = os.path.join(job, f"_shot_{name}_{lang}.html")
            open(h, "w").write(cards_html(job, res, picks, lang, 1100))
            shoot(a.chrome, h, os.path.join(out, f"{name}.png"), 1100, 1600)
        print("wrote", out)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
