"""Self-contained HTML report (images embedded) - verdict first, overview matrix, one card per check.

Every text exists in Chinese and English; a switch in the header changes the language (the choice is remembered in
the viewer's browser), and #en / #zh in the URL selects one directly.
"""
import base64, html, os

from .config import T
from .util import bi, pick

COLS = [("inventory", bi("完整性", "Files")), ("frames", bi("帧率", "FPS")), ("sync", bi("同步", "Sync")),
        ("geometry", bi("标定", "Calib.")), ("hands", bi("手部", "Hands")), ("scale", bi("尺度", "Scale")),
        ("imu", bi("IMU", "IMU")), ("quality", bi("码率", "Bitrate"))]
VLABEL = {"pass": bi("可直接使用", "Ready to use"), "warn": bi("可用，有注意项", "Usable, with caveats"),
          "fixable": bi("修正后可用", "Usable after fixes"), "fail": bi("不建议直接使用", "Not recommended as is"),
          "none": bi("未识别", "Not recognised")}
LAYOUT = {"ego_mp4": bi("左右两路 MP4", "left/right MP4"), "ego_mcap": bi("MCAP", "MCAP"),
          "sbs": bi("左右拼接视频", "side-by-side video"), "unsupported": bi("不支持", "unsupported")}

METHOD = [
    (bi("数据完整性", "Completeness"),
     bi("盘点每段录制带了什么：出厂标定、逐帧时间戳、IMU、元数据、标注。缺标定 = 不能直接得到度量 3D；缺逐帧时间戳 = 画面时刻只能按标称帧率推算。",
        "What each recording contains: factory calibration, per-frame timestamps, IMU, metadata, labels. No calibration = "
        "no metric 3D out of the box; no per-frame timestamps = frame times only from the nominal frame rate.")),
    (bi("真实帧率与时间戳", "Real frame rate & timestamps"),
     bi(f"统计不重复的画面数。帧内编码（MJPEG）按每帧压缩数据的字节指纹比较；帧间编码（H.264/H.265）解码后比较相邻帧平均灰度差，小于 {T['repeat_mad']} 级视为重复。"
        f"真实帧率 ≥ 标称的 {int(T['real_fps_pass'] * 100)}% 且最长间隔 ≤ {T['gap_warn_ms']} ms 为通过。有时间戳的录制同时检查断档和开头不可解码帧造成的左右错位。",
        f"Counts distinct pictures. Intra-frame codecs (MJPEG): byte fingerprint of each compressed frame; inter-frame codecs "
        f"(H.264/H.265): mean grey difference of decoded neighbours, below {T['repeat_mad']} grey levels = repeat. Pass: real "
        f"rate ≥ {int(T['real_fps_pass'] * 100)}% of nominal and longest gap ≤ {T['gap_warn_ms']} ms. With timestamps it also "
        "looks for gaps and for left/right offsets caused by undecodable frames at the start.")),
    (bi("左右同步", "Left/right sync"),
     bi("分路录制：左右时间戳的差值 + 两目运动能量的互相关（峰值应在 0 帧）。左右拼接视频：两半画面是否总是同时更新。",
        "Separate streams: left/right timestamp difference + cross-correlation of both views' motion energy (peak must be "
        "at 0 frames). Side-by-side video: both halves must always update together.")),
    (bi("双目标定与几何", "Stereo calibration"),
     bi(f"用标定做立体校正，统计 SIFT 匹配点的纵向误差（f = 500 px）：中位 ≤ {T['epi_pass_px']} px 且系统偏移 ≤ {T['epi_bias_px']} px 为通过。"
        "标定分辨率与视频不同时自动尝试几种裁剪方式。没有标定时用 COLMAP 自标定，并用没参与标定的帧检验，同时看左右相对位姿逐帧是否稳定。",
        f"Rectifies with the calibration and measures the vertical offset of SIFT matches (f = 500 px). Pass: median ≤ "
        f"{T['epi_pass_px']} px and systematic offset ≤ {T['epi_bias_px']} px. When the calibration's resolution differs from "
        "the video, several crops are tried. Without calibration, COLMAP self-calibrates and is tested on frames it did not "
        "use, together with the per-frame stability of the left/right pose.")),
    (bi("手部两目对应", "Hands in both views"),
     bi(f"左右两目各自独立检测手部 21 个关键点（MediaPipe），两目关键点满足极线约束（纵向误差中位 ≤ 6 px）记为干净配对。"
        f"单目检出 ≥ {int(T['det_pass'] * 100)}% 且干净配对 ≥ {int(T['clean_pass'] * 100)}% 为通过；受场景影响，宜横向比较。",
        f"The 21 hand keypoints are detected independently in each view (MediaPipe); a pair is clean when the keypoints meet "
        f"the epipolar constraint (median vertical error ≤ 6 px). Pass: detected in ≥ {int(T['det_pass'] * 100)}% of views and "
        f"≥ {int(T['clean_pass'] * 100)}% clean pairs; scenes matter, so compare like with like.")),
    (bi("空间尺度", "Metric scale"),
     bi(f"三角化的掌长（手腕到中指根关节）应在成人范围 7–10 cm（超出 6–11.5 cm 为不通过：基线/尺度错误）；"
        f"手腕深度覆盖 ≥ {int(T['palm_span_min_m'] * 100)} cm 时，再看两手合并的掌长随深度的稳健斜率：两手方向一致且每 10 cm 变化 > {int(T['palm_trend_warn'] * 100)}% 为注意，"
        f"> {int(T['palm_trend_fail'] * 100)}% 为不通过。",
        f"The triangulated palm length (wrist to middle-finger knuckle) should be adult-sized, 7–10 cm (outside 6–11.5 cm "
        f"fails: wrong baseline/scale). When the wrists cover ≥ {int(T['palm_span_min_m'] * 100)} cm of depth, the robust slope "
        f"of palm length over depth (both hands) is judged too: both hands the same way and > {int(T['palm_trend_warn'] * 100)}% "
        f"per 10 cm = warning, > {int(T['palm_trend_fail'] * 100)}% = fail.")),
    (bi("相机-IMU", "Camera–IMU"),
     bi(f"用双目三角化的背景点和 PnP 求每帧相机转动，与陀螺仪转速逐窗相关，得到时间偏移及其稳定性；与标定（或文件约定）相差 ≤ {T['imu_offset_pass_ms']} ms 为通过，"
        f"各时间段偏移相差 > {T['imu_offset_spread_ms']} ms 为不通过。另查 IMU 采样率、断档、冻结、轴向和陀螺单位。",
        f"The camera's rotation per frame (PnP on stereo-triangulated background points) is correlated with the gyro speed "
        f"window by window, giving the time offset and how steady it is. Pass: within {T['imu_offset_pass_ms']} ms of the "
        f"calibration (or the file convention); windows more than {T['imu_offset_spread_ms']} ms apart fail. Also checks IMU "
        "rate, gaps, frozen values, axes and gyro units.")),
    (bi("码率与压缩", "Bitrate & compression"),
     bi(f"每个真实画面每像素分到的比特数：H.264 ≥ {T['bpp_h264_pass']}，MJPEG ≥ {T['bpp_mjpeg_pass']} 为通过。",
        f"Bits per pixel per real picture. Pass: H.264 ≥ {T['bpp_h264_pass']}, MJPEG ≥ {T['bpp_mjpeg_pass']}.")),
]

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#141413;--ink2:#5c5b56;--line:#e4e2dc;--pass:#1e8e3e;--pass-bg:#e6f4ea;--warn:#a86400;--warn-bg:#fdf1d8;
--fail:#c62828;--fail-bg:#fbe7e7;--na:#6b6a65;--na-bg:#efeeea;--fix:#b25b00;--fix-bg:#fde9d6;--accent:#2a78d6}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#f2f1ec;--ink2:#a9a8a1;--line:#33322f;--pass:#5cc97a;--pass-bg:#1d3324;
--warn:#f0b13a;--warn-bg:#3a2d12;--fail:#f07070;--fail-bg:#3d1c1c;--na:#a9a8a1;--na-bg:#2a2a27;--fix:#f59a4b;--fix-bg:#3b2614}}
*{box-sizing:border-box}html[data-lang=zh] .l-en,html[data-lang=en] .l-zh{display:none!important}
.langsw{margin-left:auto;display:inline-flex;border:1px solid var(--line);border-radius:99px;overflow:hidden;background:var(--card)}
.langsw button{border:0;background:transparent;color:var(--ink2);font:inherit;font-size:12px;padding:4px 12px;cursor:pointer}
.langsw button[aria-pressed=true]{background:var(--accent);color:#fff}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
main{max-width:1120px;margin:0 auto;padding:0 20px 60px}header.top{max-width:1120px;margin:0 auto;padding:22px 20px 8px;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:.5px}.brand span{color:var(--accent)}.meta{color:var(--ink2);font-size:13px;word-break:break-all}
.hero{border-radius:16px;padding:22px 24px;margin:10px 0 22px;background:var(--card);border:1px solid var(--line);border-left:8px solid var(--na)}
.hero.v-pass{border-left-color:var(--pass)}.hero.v-warn{border-left-color:var(--warn)}.hero.v-fixable{border-left-color:var(--fix)}.hero.v-fail{border-left-color:var(--fail)}
.hero .vl{display:inline-block;font-weight:700;font-size:14px;padding:3px 10px;border-radius:99px}
.hero h1{margin:8px 0 6px;font-size:24px}.hero p{margin:4px 0;color:var(--ink2)}.hero ul{margin:12px 0 0;padding-left:20px}.hero li{margin:3px 0}
h2{font-size:18px;margin:26px 0 10px}.chip{display:inline-block;font-size:12px;font-weight:600;padding:2px 9px;border-radius:99px;white-space:nowrap}
.s-pass{color:var(--pass);background:var(--pass-bg)}.s-warn{color:var(--warn);background:var(--warn-bg)}.s-fail{color:var(--fail);background:var(--fail-bg)}
.s-na,.s-info,.s-none{color:var(--na);background:var(--na-bg)}.s-fixable{color:var(--fix);background:var(--fix-bg)}
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
table.mx{border-collapse:collapse;width:100%;font-size:13px}table.mx th,table.mx td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
table.mx th{color:var(--ink2);font-weight:600;background:var(--bg)}table.mx tr:last-child td{border-bottom:0}table.mx td.rid{min-width:180px;max-width:300px;word-break:break-all}table.mx td.rid a{color:var(--ink);text-decoration:none;font-weight:600}
table.mx td.rid small{display:block;color:var(--ink2);font-size:12px;word-break:normal}table.mx td.rid small.parent{font-size:11px;opacity:.8;word-break:break-all}table.mx td,table.mx th{white-space:nowrap}table.mx td.rid{white-space:normal}
table.mx td.rid a:hover{color:var(--accent)}.legend{color:var(--ink2);font-size:12px;margin:8px 2px}
section.rec{margin-top:18px}section.rec>details{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:4px 18px 14px}
section.rec summary{cursor:pointer;padding:12px 0;font-weight:700;font-size:16px;list-style:none;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
section.rec summary::-webkit-details-marker{display:none}section.rec summary .path{font-weight:400;color:var(--ink2);font-size:12px;word-break:break-all}
.why{margin:0 0 12px;padding:10px 14px;border-radius:10px;background:var(--bg);font-size:14px}.why ul{margin:4px 0 0;padding-left:18px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));grid-auto-flow:dense;gap:12px}
.split{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.3fr);gap:18px;align-items:start}.split figure{margin-top:0}
.card{border:1px solid var(--line);border-radius:12px;padding:14px 16px;background:var(--card)}.card.wide{grid-column:1/-1}
.card h3{margin:0;font-size:15px;display:flex;gap:8px;align-items:center;justify-content:space-between}.card .sum{margin:8px 0 10px;font-weight:600}
dl.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;margin:0 0 8px;font-size:13px}dl.kv dt{color:var(--ink2)}dl.kv dd{margin:0;font-variant-numeric:tabular-nums}
.card ul{margin:6px 0;padding-left:18px;font-size:13px;color:var(--ink2)}.advice{margin-top:8px;padding:8px 12px;border-radius:8px;background:var(--bg);font-size:13px}
.advice b{display:block;margin-bottom:2px}.advice ul{color:var(--ink)}figure{margin:10px 0 4px}figure img{max-width:100%;border-radius:8px;border:1px solid var(--line)}
pre{white-space:pre-wrap;word-break:break-all;font-size:11px;color:var(--ink2)}details.tech summary{cursor:pointer;font-size:12px;color:var(--ink2)}.method details{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 18px}
.method summary{cursor:pointer;font-weight:600;padding:8px 0}.method dt{font-weight:600;margin-top:10px}.method dd{margin:2px 0 0;color:var(--ink2);font-size:14px}
footer{color:var(--ink2);font-size:12px;max-width:1120px;margin:0 auto;padding:0 20px 30px;word-break:break-all}
@media (max-width:900px){.split{grid-template-columns:1fr}}@media (max-width:640px){main,header.top,footer{padding-left:16px;padding-right:16px}.cards{grid-template-columns:1fr}.hero h1{font-size:20px}}
"""

JS = """(function(){var d=document.documentElement;function set(l){if(l!=='zh'&&l!=='en')return;d.setAttribute('data-lang',l);
d.lang=l==='zh'?'zh-CN':'en';document.querySelectorAll('[data-tip-zh]').forEach(function(x){x.title=x.getAttribute('data-tip-'+l)});
document.querySelectorAll('.langsw button').forEach(function(b){b.setAttribute('aria-pressed',b.dataset.l===l)});
try{localStorage.setItem('egoqc-lang',l)}catch(e){}}
var h=(location.hash||'').replace('#','');var s=null;try{s=localStorage.getItem('egoqc-lang')}catch(e){}
set(h==='en'||h==='zh'?h:(s||d.getAttribute('data-lang')));
document.querySelectorAll('.langsw button').forEach(function(b){b.addEventListener('click',function(){set(b.dataset.l)})});})();"""


def e(x):
    return html.escape(str(x))


def t(x):
    """Both languages of a text as spans (only one is visible); plain values as they are."""
    if isinstance(x, dict) and "zh" in x:
        if x["zh"] == x.get("en", x["zh"]):
            return e(x["zh"])
        return f'<span class="l-zh">{e(x["zh"])}</span><span class="l-en">{e(x["en"])}</span>'
    return e("" if x is None else x)


def tip(x):
    """Tooltip attributes for both languages (the script swaps title= when the language changes)."""
    return f'title="{e(pick(x, "zh"))}" data-tip-zh="{e(pick(x, "zh"))}" data-tip-en="{e(pick(x, "en"))}"'


def _img(job, rel, cls=""):
    if isinstance(rel, dict):   # one chart per language
        return "".join(_img(job, rel[lang], f"l-{lang}") for lang in ("zh", "en"))
    p = os.path.join(job, rel)
    if not os.path.exists(p):
        return ""
    mime = "image/png" if p.endswith(".png") else "image/jpeg"
    return (f'<figure{f" class={chr(34)}{cls}{chr(34)}" if cls else ""}><img alt="" '
            f'src="data:{mime};base64,{base64.b64encode(open(p, "rb").read()).decode()}"></figure>')


def _is_trace(d):
    return isinstance(d, str) and d.startswith("Traceback")


def _card(c, job):
    """Charts sit beside the card text; image pairs (two views side by side) span the card below it."""
    figs = c.get("figures", [])
    charts = [f for f in figs if isinstance(f, dict) or f.endswith(".png")]
    photos = [f for f in figs if not (isinstance(f, dict) or f.endswith(".png"))]
    txt = []
    if c["metrics"]:
        txt.append('<dl class="kv">' + "".join(f"<dt>{t(k)}</dt><dd>{t(v)}</dd>" for k, v in c["metrics"]) + "</dl>")
    det = [d for d in c.get("details", []) if d]
    if det:
        txt.append("<ul>" + "".join(f"<li>{t(d)}</li>" for d in det if not _is_trace(d)) + "</ul>")
        txt += [f'<details class="tech"><summary>{t(bi("技术细节（程序出错，请把报告发给维护者）", "Technical details (a check crashed; please send this report to the maintainers)"))}'
                f'</summary><pre>{e(d)}</pre></details>' for d in det if _is_trace(d)]
    if c.get("advice"):
        txt.append(f'<div class="advice"><b>{t(bi("建议", "What to do"))}</b><ul>' + "".join(f"<li>{t(a)}</li>" for a in c["advice"]) + "</ul></div>")
    out = [f'<article class="card{" wide" if figs else ""}"><h3>{t(c["title"])}<span class="chip s-{c["status"]}">{t(c["label"])}</span></h3>',
           f'<p class="sum">{t(c["summary"])}</p>']
    if charts:
        out.append('<div class="split"><div>' + "".join(txt) + '</div><div>' + "".join(_img(job, f) for f in charts) + "</div></div>")
    else:
        out += txt
    out += [_img(job, f) for f in photos]
    out.append("</article>")
    return "".join(out)


def _split_id(rid):
    """'folder / sub / recording' -> ('folder / sub', 'recording')."""
    head, _, leaf = rid.rpartition(" / ")
    return head, leaf


def _top_issues(res):
    agg = {}
    for r in res["recordings"]:
        for c in r["checks"]:
            if c["status"] in ("fail", "warn"):
                a = agg.setdefault(c["id"], dict(title=c["title"], fail=0, warn=0, ex=None))
                a[c["status"]] += 1
                if c["status"] == "fail" and not a["ex"]:
                    a["ex"] = c["summary"]
                a["ex"] = a["ex"] or c["summary"]
    order = [k for k, _ in COLS]
    items = sorted(agg.items(), key=lambda kv: (-kv[1]["fail"], -kv[1]["warn"], order.index(kv[0]) if kv[0] in order else 99))
    n = len(res["recordings"])
    out = []
    for _, a in items[:6]:
        if n > 1 and a["fail"] == n:
            cnt = bi(f"{n} 段都不通过", f"fails in all {n}")
        elif n > 1 and a["warn"] == n:
            cnt = bi(f"{n} 段都需注意", f"warning in all {n}")
        else:
            cnt = bi("，".join(x for x in (f"{a['fail']} 段不通过" if a["fail"] else "", f"{a['warn']} 段注意" if a["warn"] else "") if x),
                     ", ".join(x for x in (f"{a['fail']} failed" if a["fail"] else "", f"{a['warn']} warning" if a["warn"] else "") if x))
        out.append(f"<li><b>{t(a['title'])}</b>"
                   f'<span class="l-zh">（{e(cnt["zh"])}）：{e(pick(a["ex"], "zh"))}</span>'
                   f'<span class="l-en"> ({e(cnt["en"])}): {e(pick(a["ex"], "en"))}</span></li>')
    return out


def render(res, job):
    recs = res["recordings"]
    c = res["counts"]
    lang = (res.get("options") or {}).get("lang", "zh")
    count_txt = bi(" · ".join(f"{VLABEL[k]['zh']} {c[k]} 段" for k in ("pass", "warn", "fixable", "fail", "none") if c.get(k)),
                   " · ".join(f"{VLABEL[k]['en']}: {c[k]}" for k in ("pass", "warn", "fixable", "fail", "none") if c.get(k)))
    parts = [f'<!doctype html><html lang="{"zh-CN" if lang == "zh" else "en"}" data-lang="{e(lang)}"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<title>EgoQC · {e(res["dataset"])}</title><style>{CSS}</style></head><body>',
             f'<header class="top"><div class="brand">Ego<span>QC</span> {t(bi("质检报告", "quality report"))}</div>'
             f'<div class="meta">{e(res["created"])} · {e(res["input"])}</div>'
             '<div class="langsw" role="group" aria-label="Language"><button type="button" data-l="zh">中文</button>'
             '<button type="button" data-l="en">EN</button></div></header><main>',
             f'<section class="hero v-{res["verdict"]}"><span class="vl chip s-{res["verdict"]}">{t(res["verdict_label"])}</span>'
             f'<h1>{e(res["dataset"])}</h1>'
             + (f'<p><span class="l-zh">共 {len(recs)} 段录制：{e(count_txt["zh"])}</span>'
                f'<span class="l-en">{len(recs)} recording(s): {e(count_txt["en"])}</span></p>' if recs else
                "<p>" + t(bi("没有找到可识别的录制。EgoQC 支持：Orbbec 分路 MP4（*_left / *_right + _pts.csv）、Orbbec MCAP、"
                             "左右拼接的单个视频（宽约为高的 2 倍以上），以及装着这些文件的文件夹或 zip / rar / 7z 压缩包。",
                             "No recording was recognised. EgoQC reads Orbbec-style left/right MP4 (+ _pts.csv), Orbbec-style "
                             "MCAP, single side-by-side videos (width ≥ 2.5 × height), and folders or zip / rar / 7z archives "
                             "holding them.")) + "</p>")]
    top = _top_issues(res)
    if top:
        parts.append("<ul>" + "".join(top) + "</ul>")
    bad_arch = [a for a in res.get("archives", []) if not a["ok"]]
    if bad_arch:
        parts.append("<p>" + t(bi("解压失败：", "Could not extract: ")) + "、".join(e(a["file"]) for a in bad_arch) + "</p>")
    parts.append("</section>")
    # overview matrix
    parts.append(f'<h2>{t(bi("总览", "Overview"))}</h2><div class="tablewrap"><table class="mx"><tr><th>{t(bi("录制", "Recording"))}</th>'
                 f'<th>{t(bi("结论", "Verdict"))}</th>' + "".join(f"<th>{t(lab)}</th>" for _, lab in COLS) + "</tr>")
    for i, r in enumerate(recs):
        cm = {x["id"]: x for x in r["checks"]}
        cells = "".join((f'<td><span class="chip s-{cm[k]["status"]}" {tip(cm[k]["summary"])}>{t(cm[k]["label"])}</span></td>'
                         if k in cm else "<td></td>") for k, _ in COLS)
        dur = (cm.get("inventory", {}).get("facts") or {}).get("duration_s")
        parent, leaf = _split_id(r["id"])
        lay = LAYOUT.get(r["layout"], bi(r["layout"]))
        sub = bi(lay["zh"] + (f" · {dur:.1f} s" if dur else ""), lay["en"] + (f" · {dur:.1f} s" if dur else ""))
        parts.append(f'<tr><td class="rid"><a href="#rec-{i}">{e(leaf)}</a><small>{t(sub)}</small>'
                     + (f'<small class="parent">{e(parent)}</small>' if parent else "") + '</td>'
                     f'<td><span class="chip s-{r["verdict"]}">{t(VLABEL[r["verdict"]])}</span></td>{cells}</tr>')
    parts.append('</table></div><p class="legend">' + t(bi("把鼠标停在标签上可看一句话结论；点录制名跳到详情。“修正后可用”= 问题可自动修正（EgoQC 已按修正后的方式检查）。",
                                                         "Hover a label for its one-line finding; click a recording for details. "
                                                         "\"Usable after fixes\" = the problem can be corrected automatically "
                                                         "(EgoQC already checked the corrected data).")) + "</p>")
    # per recording
    for i, r in enumerate(recs):
        open_attr = " open" if len(recs) <= 3 or r["verdict"] in ("fail", "fixable") else ""
        parts.append(f'<section class="rec" id="rec-{i}"><details{open_attr}><summary><span class="chip s-{r["verdict"]}">{t(VLABEL[r["verdict"]])}</span>'
                     f'{e(_split_id(r["id"])[1])}<span class="path">{e(r.get("path", ""))}</span></summary>')
        if r.get("reasons"):
            parts.append(f'<div class="why">{t(bi("主要问题", "Main issues"))}<ul>' + "".join(f"<li>{t(x)}</li>" for x in r["reasons"]) + "</ul></div>")
        parts.append('<div class="cards">' + "".join(_card(x, job) for x in r["checks"]) + "</div></details></section>")
    parts.append(f'<section class="method"><h2>{t(bi("检查方法与判定标准", "Methods and pass criteria"))}</h2><details><summary>'
                 f'{t(bi("展开", "Show"))}</summary><dl>' + "".join(f"<dt>{t(a)}</dt><dd>{t(b)}</dd>" for a, b in METHOD) + "</dl></details></section></main>")
    parts.append(f'<footer>{e(res["tool"])} · {t(bi("机器可读结果：", "Machine-readable results: "))}{e(os.path.join(job, "report.json"))}</footer>'
                 f'<script>{JS}</script></body></html>')
    out = os.path.join(job, "report.html")
    open(out, "w").write("".join(parts))
    return out
