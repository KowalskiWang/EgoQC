"""Run every check on every recording of one dropped-in dataset and write report.html + report.json."""
import datetime, json, os, re, shutil, traceback

from . import __version__, ingest, report
from .checks import frames, geometry, hands, imu, inventory, quality, sync
from .config import FAIL, LANG, MAX_SECONDS, NA, TITLES, WARN
from .sources import Recording
from .util import bi, check

CRITICAL = {"inventory", "frames", "sync", "geometry", "imu", "quality"}
VERDICTS = {"pass": bi("可直接使用", "Ready to use"), "warn": bi("可用，有注意项", "Usable, with caveats"),
            "fixable": bi("修正后可用", "Usable after fixes"), "fail": bi("不建议直接使用", "Not recommended as is"),
            "none": bi("未识别", "Not recognised")}
RANK = ["pass", "warn", "fixable", "fail"]
STEP = {"inventory": bi("盘点文件", "taking inventory"), "frames": bi("真实帧率", "real frame rate"),
        "sync": bi("左右同步", "left/right sync"), "geometry": bi("双目标定", "stereo calibration"),
        "hands": bi("手部两目对应", "hands in both views"), "imu": bi("相机-IMU", "camera–IMU"), "quality": bi("码率", "bitrate")}


def verdict(checks):
    fails = [c for c in checks if c["status"] == FAIL and c["id"] in CRITICAL]
    if any(not c.get("correctable") for c in fails):
        return "fail"
    if fails:
        return "fixable"
    if any(c["status"] in (WARN, FAIL) for c in checks):
        return "warn"
    return "pass"


def reasons(checks, n=4):
    order = {FAIL: 0, WARN: 1}
    bad = sorted((c for c in checks if c["status"] in order), key=lambda c: (order[c["status"]], c["id"] not in CRITICAL))
    return [bi(f"{c['title']['zh']}：{c['summary']['zh']}", f"{c['title']['en']}: {c['summary']['en']}") for c in bad[:n]]


def _safe(name):
    return re.sub(r"[^\w一-鿿.-]+", "_", name).strip("_")[:60] or "dataset"


class Cancelled(BaseException):   # not an Exception, so the per-check error guard lets it through
    pass


def run_job(input_path, out_root, opts=None, progress=None):
    """Returns the report.html path. A cancelled job leaves nothing behind; a crashed one leaves error.txt."""
    opts = dict(dict(max_seconds=MAX_SECONDS, selfcalib=True, lang=LANG), **(opts or {}))
    progress = progress or (lambda f, m, **kw: None)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = os.path.basename(os.path.normpath(input_path))
    job = os.path.join(out_root, f"{stamp}_{_safe(os.path.splitext(name)[0] if os.path.isfile(input_path) else name)}")
    work = os.path.join(job, "work")
    os.makedirs(work, exist_ok=True)
    progress(0.0, bi("读取数据", "Reading data"), job=job)
    try:
        return _run(input_path, name, job, work, opts, progress)
    except Cancelled:
        shutil.rmtree(job, ignore_errors=True)
        raise
    except BaseException:
        open(os.path.join(job, "error.txt"), "w").write(traceback.format_exc())
        raise
    finally:
        if not opts.get("keep_work", False):   # extracted archives and intermediate frames can be GBs
            shutil.rmtree(work, ignore_errors=True)


def _run(input_path, name, job, work, opts, progress):
    assets = os.path.join(job, "assets")
    os.makedirs(assets, exist_ok=True)
    roots, single, notes = ingest.prepare(input_path, work, progress)
    specs = ingest.discover(roots, single)
    progress(0.05, bi(f"找到 {len(specs)} 段录制", f"Found {len(specs)} recording(s)"))
    recs = []
    for i, spec in enumerate(specs):
        base, span = 0.05 + 0.93 * i / max(len(specs), 1), 0.93 / max(len(specs), 1)
        if spec["layout"] == "unsupported":
            recs.append(dict(id=spec["id"], layout="unsupported", verdict="none", checks=[check("inventory", NA, spec["reason"])],
                             reasons=[spec["reason"]]))
            continue
        rec = Recording(spec, os.path.join(work, "tmp", f"{i:03d}"))
        rout = os.path.join(assets, f"{i:03d}"); os.makedirs(rout, exist_ok=True)
        steps = [("inventory", inventory.run), ("frames", frames.run), ("sync", sync.run), ("geometry", geometry.run),
                 ("hands", hands.run), ("imu", imu.run), ("quality", quality.run)]
        weights = dict(inventory=0.03, frames=0.12, sync=0.10, geometry=0.25, hands=0.25, imu=0.20, quality=0.05)
        done, checks = 0.0, []
        for cid, fn in steps:
            def prog(msg, frac=None, _d=done, _w=weights[cid]):   # frac: optional progress within this check (0-1)
                m = msg if isinstance(msg, dict) else bi(msg)
                progress(base + span * (_d + _w * min(max(frac or 0.0, 0.0), 1.0)),
                         bi(f"[{i + 1}/{len(specs)}] {spec['id']}：{m['zh']}", f"[{i + 1}/{len(specs)}] {spec['id']}: {m['en']}"))
            prog(STEP[cid])
            ctx = dict(out=rout, opts=opts, progress=prog)
            try:
                r = fn(rec, ctx)
                checks += list(r) if isinstance(r, tuple) else [r]
            except Exception as e:  # noqa: BLE001 - one broken check must not stop the job
                tb = traceback.format_exc(limit=3)
                checks.append(check(cid, NA, bi(f"检查出错：{type(e).__name__}: {e}", f"The check crashed: {type(e).__name__}: {e}"),
                                    details=[tb]))
            done += weights[cid]
        for c in checks:
            c["figures"] = [{k: os.path.join("assets", f"{i:03d}", v) for k, v in f.items()} if isinstance(f, dict)
                            else os.path.join("assets", f"{i:03d}", f) for f in c.get("figures", [])]
        v = verdict(checks)
        recs.append(dict(id=spec["id"], layout=spec["layout"], path=spec.get("video") or spec["root"], verdict=v, checks=checks,
                         reasons=reasons(checks)))
    counts = {k: sum(1 for r in recs if r["verdict"] == k) for k in VERDICTS}
    worst = max((r["verdict"] for r in recs if r["verdict"] in RANK), key=RANK.index, default="none")
    result = dict(tool=f"EgoQC {__version__}", created=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), input=os.path.abspath(input_path),
                  dataset=name, verdict=worst, verdict_label=VERDICTS[worst], counts=counts, archives=notes, recordings=recs, options=opts,
                  job_dir=job, titles=TITLES)
    json.dump(result, open(os.path.join(job, "report.json"), "w"), ensure_ascii=False, indent=1, default=str)
    html = report.render(result, job)
    progress(1.0, bi("完成", "Done"))
    return html
