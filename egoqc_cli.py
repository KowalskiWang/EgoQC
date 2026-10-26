"""Command line: check one dataset and write the report.

usage: python -I egoqc_cli.py <folder | archive | video> [--out DIR] [--lang zh|en] [--max-seconds 150] [--no-selfcalib] [--keep-work]
Progress is printed as JSON lines on stdout (read by the desktop app); the last line holds the report path.
"""
import argparse, json, os, signal, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from egoqc import config  # noqa: E402
from egoqc.runner import Cancelled, run_job  # noqa: E402
from egoqc.util import Progress  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="EgoQC: quality check for stereo egocentric datasets / 双目 Ego 数据集质检")
    ap.add_argument("input", help="dataset folder, archive (zip/rar/7z/tar) or video / 数据集文件夹、压缩包或视频")
    ap.add_argument("--out", default=config.OUTPUT_ROOT, help=f"report folder (default {config.OUTPUT_ROOT})")
    ap.add_argument("--lang", choices=("zh", "en"), default=config.LANG, help="language the report opens in (it can be switched inside)")
    ap.add_argument("--max-seconds", type=float, default=config.MAX_SECONDS, help="analysed span per recording for the heavy checks")
    ap.add_argument("--no-selfcalib", action="store_true", help="skip COLMAP self-calibration when there is no calibration")
    ap.add_argument("--keep-work", action="store_true", help="keep extracted archives and intermediate files")
    a = ap.parse_args()

    def stop(*_):   # the app's Stop button sends SIGTERM; unwind so the job folder gets removed
        raise Cancelled()
    signal.signal(signal.SIGTERM, stop)
    os.makedirs(a.out, exist_ok=True)
    opts = dict(max_seconds=a.max_seconds, selfcalib=not a.no_selfcalib, keep_work=a.keep_work, lang=a.lang)
    try:
        report = run_job(a.input, a.out, opts, Progress())
    except Cancelled:
        sys.exit(130)
    print(json.dumps({"done": True, "report": report}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
