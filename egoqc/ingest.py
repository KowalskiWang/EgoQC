"""Turn whatever was dropped in (folder, archive, video file) into a list of recordings to check.

Archives are extracted into the job's work folder (the user's data is never modified). Recognised layouts:
  ego_mp4   Orbbec/EGO recorder folder: camera_left/right MP4 (+ _pts.csv), calibration yaml, IMU csv, meta json
  ego_mcap  the same recorder writing MCAP (foxglove.CompressedVideo /camera/left|right, /imu..., calibration
            as files or MCAP attachments)
  sbs       one side-by-side stereo video (width >= 2.5 x height), e.g. 3840x1080, optionally with the 2ego
            IMU files <task>_<device>_IMU_<start>_<end>_*.bin + _sync.csv next to it
"""
import os, re

from .util import bi, files, probe, run

ARCHIVES = (".zip", ".rar", ".7z", ".tar", ".tgz", ".tar.gz", ".tar.xz")
VIDEOS = (".mp4", ".mov", ".mkv", ".avi", ".m4v")
SBS_NAME = re.compile(r"(?P<prefix>.+?)_2ego_(?P<start>\d{13})_(?P<end>\d{13})(?:_(?P<fps>\d+)fps)?(?:_(?P<w>\d+)x(?P<h>\d+))?")


def is_archive(p):
    return p.lower().endswith(ARCHIVES)


def extract(archive, dest):
    os.makedirs(dest, exist_ok=True)
    r = run(["bsdtar", "-xf", archive, "-C", dest])
    return r.returncode == 0, (r.stderr or "").strip()[:300]


def prepare(input_path, work_dir, progress=None):
    """Returns (scan_roots, single_video_or_None, notes). Extracts archives found in/at the input."""
    notes, roots, single = [], [], None
    ext_root = os.path.join(work_dir, "extracted")
    p = os.path.abspath(input_path)
    if os.path.isfile(p) and is_archive(p):
        dest = os.path.join(ext_root, os.path.splitext(os.path.basename(p))[0])
        if progress:
            progress(0.01, bi(f"解压 {os.path.basename(p)}", f"Extracting {os.path.basename(p)}"))
        ok, err = extract(p, dest)
        notes.append(dict(file=os.path.basename(p), ok=ok, error=err))
        roots.append(dest)
    elif os.path.isfile(p) and p.lower().endswith(VIDEOS):
        single = p
        roots.append(os.path.dirname(p))
    elif os.path.isdir(p):
        roots.append(p)
        arcs = [a for a in files(p, "*") if os.path.isfile(a) and is_archive(a)]
        for i, a in enumerate(arcs):
            rel = os.path.relpath(a, p)
            dest = os.path.join(ext_root, os.path.splitext(rel)[0])
            if progress:
                progress(0.01 + 0.04 * i / max(len(arcs), 1), bi(f"解压 {rel}", f"Extracting {rel}"))
            ok, err = extract(a, dest)
            notes.append(dict(file=rel, ok=ok, error=err))
        if arcs:
            roots.append(ext_root)
    else:
        raise FileNotFoundError(f"unsupported input / 不支持的输入: {input_path}")
    return roots, single, notes


def _ego_roots(root):
    found = set()
    for pat in ("meta_*.json", "*_calibration_camera.yaml", "*.mcap", "*camera_left*.mp4"):
        for f in files(root, pat):
            d = os.path.dirname(f)
            if os.path.basename(d).startswith("part"):
                d = os.path.dirname(d)
            found.add(d)
    # keep only folders that really hold a stereo recording
    out = []
    for d in sorted(found):
        if files(d, "*.mcap"):
            out.append((d, "ego_mcap"))
        elif files(d, "*camera_left*.mp4") and files(d, "*camera_right*.mp4"):
            out.append((d, "ego_mp4"))
    return out


def sbs_imu_files(video):
    """2ego IMU files that belong to a side-by-side video (same <task>_<device> prefix, same folder)."""
    m = SBS_NAME.match(os.path.basename(video))
    if not m:
        return [], None
    prefix = m.group("prefix")
    folder = os.path.dirname(video)
    bins = sorted(f for f in os.listdir(folder) if f.startswith(prefix + "_IMU_") and f.endswith(".bin") and not f.startswith("._"))
    syncs = sorted(f for f in os.listdir(folder) if f.startswith(prefix + "_IMU_") and f.endswith("_sync.csv") and not f.startswith("._"))
    return [(os.path.join(folder, b), os.path.join(folder, s)) for b, s in zip(bins, syncs)], m


def discover(roots, single=None):
    """List of recording specs: dict(id, layout, root, video?, imu?)."""
    specs, seen_dirs = [], set()
    if single:
        cand = [single]
    else:
        for r in roots:
            for d, layout in _ego_roots(r):
                if d in seen_dirs:
                    continue
                seen_dirs.add(d)
                specs.append(dict(id=_rid(d, roots), layout=layout, root=d))
        cand = [v for r in roots for v in files(r, "*") if v.lower().endswith(VIDEOS)]
    for v in cand:
        if any(v.startswith(d + os.sep) for d in seen_dirs) or "camera_left" in v or "camera_right" in v:
            continue
        info = probe(v)
        if not info:
            continue
        if info["width"] >= 2.5 * info["height"]:
            imu, m = sbs_imu_files(v)
            specs.append(dict(id=_rid(v, roots), layout="sbs", root=os.path.dirname(v), video=v, imu=imu,
                              name_info=m.groupdict() if m else None))
        else:
            specs.append(dict(id=_rid(v, roots), layout="unsupported", root=os.path.dirname(v), video=v,
                              reason=bi(f"{info['width']}×{info['height']} 不是左右拼接的双目视频",
                                        f"{info['width']}×{info['height']} is not a side-by-side stereo video")))
    # unique ids
    used = {}
    for s in specs:
        n = used.get(s["id"], 0); used[s["id"]] = n + 1
        if n:
            s["id"] += f" ({n + 1})"
    return specs


def _rid(path, roots):
    for r in sorted(roots, key=len, reverse=True):
        if path == r:
            return os.path.basename(path)
        if path.startswith(r + os.sep):
            return os.path.relpath(path, r).replace(os.sep, " / ")
    return os.path.basename(path)
