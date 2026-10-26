"""EgoQC desktop app: drop a stereo-ego dataset in, get a quality report. Chinese / English, switchable.

Jobs run one at a time in a separate process (egoqc_cli.py) so the window stays responsive and a crash in a
check cannot take the app down. Past reports in the output folder are listed on start.
"""
import datetime, json, os, shutil, subprocess, sys

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)

from PySide6.QtCore import QEvent, QLocale, QProcess, QSettings, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QDesktopServices, QFont, QFontMetrics  # noqa: E402
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,  # noqa: E402
                               QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSpinBox,
                               QVBoxLayout, QWidget)

from egoqc import __version__, config  # noqa: E402
from egoqc.util import pick  # noqa: E402

CLI = os.path.join(APP_ROOT, "egoqc_cli.py")
LOG = os.path.expanduser("~/Library/Logs/EgoQC.log" if sys.platform == "darwin" else "~/.egoqc/EgoQC.log")
REVEAL = {"darwin": ("在 Finder 中显示", "Show in Finder"), "win32": ("在资源管理器中显示", "Show in Explorer")}.get(
    sys.platform, ("打开所在文件夹", "Open folder"))

S = {   # every text in the window: (中文, English)
    "window": ("EgoQC · 双目 Ego 数据质检", "EgoQC · Stereo ego data QC"),
    "title": ("EgoQC 双目 Ego 数据质检", "EgoQC — stereo ego data QC"),
    "subtitle": ("拖入数据集，自动检查真实帧率、左右同步、双目标定、手部两目对应、空间尺度、相机-IMU 和码率，生成中英双语报告。",
                 "Drop in a dataset: EgoQC checks the real frame rate, left/right sync, stereo calibration, hands in both "
                 "views, metric scale, camera–IMU alignment and bitrate, and writes a bilingual report."),
    "drop_main": ("把数据集拖到这里", "Drop a dataset here"),
    "drop_sub": ("文件夹、zip / rar 压缩包或单个视频都可以，可一次拖多个\n支持 Orbbec 分路 MP4 / MCAP、左右拼接视频（2ego 的 IMU 自动配对）",
                 "Folders, zip / rar archives or single videos, several at once\n"
                 "Orbbec-style left/right MP4 or MCAP, side-by-side videos (2ego IMU files are paired automatically)"),
    "pick_folder": ("选择文件夹…", "Choose folder…"),
    "pick_file": ("选择文件…", "Choose files…"),
    "dlg_folder": ("选择数据集文件夹", "Choose a dataset folder"),
    "dlg_file": ("选择压缩包或视频", "Choose archives or videos"),
    "filter": ("数据集 (*.zip *.rar *.7z *.tar *.mp4 *.mov *.mkv *.mcap);;所有文件 (*)",
               "Datasets (*.zip *.rar *.7z *.tar *.mp4 *.mov *.mkv *.mcap);;All files (*)"),
    "selfcalib": ("无标定时自动自标定（较慢）", "Self-calibrate if uncalibrated (slow)"),
    "autoopen": ("完成后打开报告", "Open report when done"),
    "max_s": ("每段最多分析", "Analyse up to"),
    "sec": (" 秒", " s"),
    "out_ok": ("报告保存到：", "Reports: "),
    "out_bad": ("⚠ 报告位置不可用：", "⚠ Report folder unavailable: "),
    "out_btn": ("报告位置…", "Report folder…"),
    "jobs": ("任务与报告", "Jobs and reports"),
    "empty": ("还没有报告。拖入数据集后，任务和历史报告会列在这里。", "No reports yet. Jobs and past reports show up here."),
    "foot": ("一次运行一个任务，其余排队", "one job at a time, the rest wait in line"),
    "queued": ("排队中", "Queued"), "running": ("运行中", "Running"), "failed": ("失败", "Failed"),
    "stopped": ("已停止", "Stopped"), "done": ("完成", "Done"),
    "stop": ("停止", "Stop"), "reveal": REVEAL, "open": ("打开报告", "Open report"),
    "preparing": ("准备…", "Preparing…"),
    "busy_out": ("有任务在运行，等它们结束后再改报告位置。", "Jobs are running; change the report folder after they finish."),
    "dlg_out": ("选择报告保存位置", "Choose the report folder"),
    "quit_q": ("还有任务在运行，确定退出？", "A job is still running. Quit anyway?"),
    "no_out": ("报告位置不可用：{}\n请接上硬盘，或点“报告位置…”另选。", "Report folder unavailable: {}\nConnect the drive, or pick another one with “Report folder…”."),
    "no_report": ("没有生成报告。", "No report was written."),
    "last_out": (" 最后的输出：", " Last output: "),
    "cant_start": ("无法启动检查进程：", "Could not start the check process: "),
    "n_recs": ("{} 段录制：", "{} recording(s): "),
    "none_found": ("没有找到可识别的录制", "no recording recognised"),
    "pass": ("{} 段可直接用", "{} ready"), "warn": ("{} 段有注意项", "{} with caveats"),
    "fixable": ("{} 段修正后可用", "{} usable after fixes"), "fail": ("{} 段不建议用", "{} not recommended"),
    "sep": ("，", ", "),
}
STYLE = {"pass": ("#1e8e3e", "#e6f4ea"), "warn": ("#a86400", "#fdf1d8"), "fixable": ("#b25b00", "#fde9d6"),
         "fail": ("#c62828", "#fbe7e7"), "none": ("#6b6a65", "#efeeea"), "run": ("#2a78d6", "#e7f1fb"), "queue": ("#6b6a65", "#efeeea")}

CSS = """
QMainWindow, QWidget#root { background: #f6f5f2; }
QLabel#title { font-size: 22px; font-weight: 700; color: #141413; }
QLabel#subtitle { color: #5c5b56; font-size: 13px; }
QFrame#drop { background: #ffffff; border: 2px dashed #c9c6bd; border-radius: 16px; }
QFrame#drop[hover="true"] { border-color: #2a78d6; background: #eef5fd; }
QLabel#dropMain { font-size: 17px; font-weight: 600; color: #141413; }
QLabel#dropSub { color: #6b6a65; font-size: 12px; }
QPushButton { padding: 6px 14px; border-radius: 8px; border: 1px solid #d6d3cb; background: #ffffff; color: #141413; }
QPushButton:hover { border-color: #2a78d6; }
QPushButton#primary { background: #2a78d6; color: white; border: none; font-weight: 600; }
QPushButton#lang { padding: 3px 12px; border-radius: 0; font-size: 12px; color: #5c5b56; }
QPushButton#lang:checked { background: #2a78d6; color: white; border-color: #2a78d6; }
QListWidget { background: transparent; border: none; }
QFrame#job { background: #ffffff; border: 1px solid #e4e2dc; border-radius: 12px; }
QLabel#jobName { font-weight: 600; font-size: 14px; color: #141413; }
QLabel#jobStep { color: #5c5b56; font-size: 12px; }
QProgressBar { border: none; background: #ecebe6; border-radius: 3px; text-align: center; }
QProgressBar::chunk { background: #2a78d6; border-radius: 3px; }
QCheckBox, QLabel#opt { color: #3a3935; font-size: 13px; }
"""


def log(*parts):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(p) for p in parts) + "\n")
    except OSError:
        pass


def chip_style(kind):
    fg, bg = STYLE.get(kind, STYLE["none"])
    return f"color:{fg}; background:{bg}; border-radius:9px; padding:2px 10px; font-size:12px; font-weight:600;"


class JobRow(QFrame):
    def __init__(self, name, path, on_open, on_reveal, on_cancel):
        super().__init__()
        self.setObjectName("job")
        lay = QVBoxLayout(self); lay.setContentsMargins(14, 10, 14, 10); lay.setSpacing(6)
        top = QHBoxLayout()
        self.name = QLabel(name); self.name.setObjectName("jobName")
        self.chip = QLabel(); self.chip.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        top.addWidget(self.name, 1); top.addWidget(self.chip, 0, Qt.AlignVCenter)
        lay.addLayout(top)
        self.path = QLabel(path); self.path.setObjectName("jobStep"); self.path.setWordWrap(True)
        lay.addWidget(self.path)
        self.bar = QProgressBar(); self.bar.setRange(0, 1000); self.bar.setTextVisible(False); self.bar.setFixedHeight(6)
        lay.addWidget(self.bar)
        bottom = QHBoxLayout()
        self.step = QLabel(""); self.step.setObjectName("jobStep")
        bottom.addWidget(self.step, 1)
        self.btn_cancel = QPushButton(); self.btn_cancel.clicked.connect(on_cancel); self.btn_cancel.setVisible(False)
        self.btn_reveal = QPushButton(); self.btn_reveal.clicked.connect(on_reveal); self.btn_reveal.setVisible(False)
        self.btn_open = QPushButton(); self.btn_open.setObjectName("primary"); self.btn_open.clicked.connect(on_open); self.btn_open.setVisible(False)
        for b in (self.btn_cancel, self.btn_reveal, self.btn_open):
            bottom.addWidget(b)
        lay.addLayout(bottom)

    def set_chip(self, text, kind):
        self.chip.setText(text); self.chip.setStyleSheet(chip_style(kind))


class DropZone(QFrame):
    def __init__(self, on_paths, tr):
        super().__init__()
        self.setObjectName("drop"); self.setAcceptDrops(True); self.on_paths, self.tr_ = on_paths, tr
        self.setMinimumHeight(170)
        lay = QVBoxLayout(self); lay.setAlignment(Qt.AlignCenter)
        self.main = QLabel(); self.main.setObjectName("dropMain"); self.main.setAlignment(Qt.AlignCenter)
        self.sub = QLabel(); self.sub.setObjectName("dropSub"); self.sub.setAlignment(Qt.AlignCenter)
        row = QHBoxLayout(); row.setAlignment(Qt.AlignCenter)
        self.bf = QPushButton(); self.bf.clicked.connect(self.pick_folder)
        self.bfi = QPushButton(); self.bfi.clicked.connect(self.pick_file)
        row.addWidget(self.bf); row.addWidget(self.bfi)
        lay.addWidget(self.main); lay.addWidget(self.sub); lay.addSpacing(8); lay.addLayout(row)

    def retranslate(self):
        tr = self.tr_
        self.main.setText(tr("drop_main")); self.sub.setText(tr("drop_sub"))
        self.bf.setText(tr("pick_folder")); self.bfi.setText(tr("pick_file"))

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction(); self.setProperty("hover", True); self.style().polish(self)

    def dragLeaveEvent(self, ev):
        self.setProperty("hover", False); self.style().polish(self)

    def dropEvent(self, ev):
        self.setProperty("hover", False); self.style().polish(self)
        self.on_paths([u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()])

    def pick_folder(self):
        p = QFileDialog.getExistingDirectory(self, self.tr_("dlg_folder"))
        if p:
            self.on_paths([p])

    def pick_file(self):
        ps, _ = QFileDialog.getOpenFileNames(self, self.tr_("dlg_file"), "", self.tr_("filter"))
        if ps:
            self.on_paths(ps)


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("EgoQC", "EgoQC")
        self.lang = self.settings.value("lang", "zh" if QLocale.system().name().startswith("zh") else "en")
        self.out_root = self.settings.value("out_root", config.OUTPUT_ROOT)
        self.resize(1000, 720)
        root = QWidget(); root.setObjectName("root"); self.setCentralWidget(root)
        lay = QVBoxLayout(root); lay.setContentsMargins(24, 20, 24, 16); lay.setSpacing(12)
        head = QHBoxLayout()
        self.title = QLabel(); self.title.setObjectName("title")
        head.addWidget(self.title, 1)
        self.lang_btns, sw = {}, QHBoxLayout()
        sw.setSpacing(0)
        for code, label in (("zh", "中文"), ("en", "EN")):
            b = QPushButton(label); b.setObjectName("lang"); b.setCheckable(True); b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, c=code: self.set_lang(c))
            sw.addWidget(b); self.lang_btns[code] = b
        head.addLayout(sw)
        lay.addLayout(head)
        self.subtitle = QLabel(); self.subtitle.setObjectName("subtitle"); self.subtitle.setWordWrap(True)
        lay.addWidget(self.subtitle)
        self.drop = DropZone(self.enqueue, self.tr)
        lay.addWidget(self.drop)
        opts = QHBoxLayout()
        self.cb_self = QCheckBox(); self.cb_self.setChecked(self.settings.value("selfcalib", "true") == "true")
        self.cb_open = QCheckBox(); self.cb_open.setChecked(self.settings.value("autoopen", "true") == "true")
        self.max_lab = QLabel(); self.max_lab.setObjectName("opt")
        self.sp = QSpinBox(); self.sp.setRange(20, 3600); self.sp.setValue(int(self.settings.value("max_seconds", config.MAX_SECONDS)))
        opts.addWidget(self.cb_self); opts.addSpacing(16); opts.addWidget(self.cb_open); opts.addSpacing(16)
        opts.addWidget(self.max_lab); opts.addWidget(self.sp); opts.addSpacing(16)
        self.out_lab = QLabel(); self.out_lab.setObjectName("opt"); self.out_lab.setMinimumWidth(120)
        self.out_lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred); self.out_lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.bo = QPushButton(); self.bo.clicked.connect(self.pick_out)
        opts.addWidget(self.out_lab, 1); opts.addWidget(self.bo)
        lay.addLayout(opts)
        self.hdr = QLabel(); self.hdr.setObjectName("jobName")
        lay.addWidget(self.hdr)
        self.empty = QLabel(); self.empty.setObjectName("jobStep")
        lay.addWidget(self.empty)
        self.list = QListWidget(); self.list.setSpacing(4); self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        lay.addWidget(self.list, 1)
        self.foot = QLabel(); self.foot.setObjectName("jobStep")
        lay.addWidget(self.foot)
        self.queue, self.proc, self.current, self.rows = [], None, None, []
        self.out_ok = True
        self.retranslate()
        # The report folder may be on an external drive; the first access from this app can make macOS ask for
        # permission and block until answered, so touch it only once the window is up.
        QTimer.singleShot(0, lambda: (self.update_out_label(), self.load_history()))

    # ---------- language ----------
    def tr(self, key):
        return S[key][0 if self.lang == "zh" else 1]

    def set_lang(self, code):
        self.lang = code; self.settings.setValue("lang", code); self.retranslate()

    def retranslate(self):
        for code, b in self.lang_btns.items():
            b.setChecked(code == self.lang)
        self.setWindowTitle(self.tr("window")); self.title.setText(self.tr("title")); self.subtitle.setText(self.tr("subtitle"))
        self.drop.retranslate()
        self.cb_self.setText(self.tr("selfcalib")); self.cb_open.setText(self.tr("autoopen"))
        self.max_lab.setText(self.tr("max_s")); self.sp.setSuffix(self.tr("sec"))
        self.bo.setText(self.tr("out_btn")); self.hdr.setText(self.tr("jobs")); self.empty.setText(self.tr("empty"))
        self.foot.setText(f"EgoQC {__version__} · {self.tr('foot')}")
        self.show_out()
        for h in self.rows:
            self.render_row(h)

    # ---------- settings ----------
    def update_out_label(self):
        self.out_ok = os.path.isdir(os.path.dirname(self.out_root.rstrip("/"))) or os.path.isdir(self.out_root)
        self.show_out()

    def show_out(self):
        """Report folder, shortened in the middle to fit; the full path is in the tooltip."""
        head = self.tr("out_ok") if self.out_ok else self.tr("out_bad")
        fm = QFontMetrics(self.out_lab.font())
        room = max(80, self.out_lab.width() - fm.horizontalAdvance(head) - 4)
        self.out_lab.setText(head + fm.elidedText(self.out_root, Qt.ElideMiddle, room))
        self.out_lab.setToolTip(self.out_root)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.show_out()

    def pick_out(self):
        if self.proc or self.queue:
            QMessageBox.information(self, "EgoQC", self.tr("busy_out"))
            return
        p = QFileDialog.getExistingDirectory(self, self.tr("dlg_out"), self.out_root)
        if p:
            self.out_root = p; self.settings.setValue("out_root", p); self.update_out_label(); self.load_history()

    def closeEvent(self, ev):
        self.settings.setValue("selfcalib", "true" if self.cb_self.isChecked() else "false")
        self.settings.setValue("autoopen", "true" if self.cb_open.isChecked() else "false")
        self.settings.setValue("max_seconds", self.sp.value())
        if self.proc and self.proc.state() != QProcess.NotRunning:
            if QMessageBox.question(self, "EgoQC", self.tr("quit_q")) != QMessageBox.Yes:
                ev.ignore(); return
            self.proc.kill()
        ev.accept()

    # ---------- rows ----------
    def add_row(self, name, path, top=True):
        h = dict(state="queued")
        h["row"] = JobRow(name, path, lambda: self.open_report(h), lambda: self.reveal(h), self.cancel)
        h["item"] = QListWidgetItem()
        if top:
            self.list.insertItem(0, h["item"]); self.rows.insert(0, h)
        else:
            self.list.addItem(h["item"]); self.rows.append(h)
        self.list.setItemWidget(h["item"], h["row"])
        self.empty.setVisible(False)
        self.render_row(h)
        return h

    def render_row(self, h):
        """Texts of one job row for its state, in the current language."""
        row, st = h["row"], h["state"]
        row.btn_cancel.setText(self.tr("stop")); row.btn_reveal.setText(self.tr("reveal")); row.btn_open.setText(self.tr("open"))
        done = st == "done"
        row.btn_cancel.setVisible(st == "running"); row.btn_open.setVisible(done); row.btn_reveal.setVisible(done)
        row.bar.setVisible(st in ("queued", "running"))
        if st == "queued":
            row.set_chip(self.tr("queued"), "queue"); row.step.setText("")
        elif st == "running":
            row.set_chip(self.tr("running"), "run"); row.step.setText(pick(h.get("msg"), self.lang) or self.tr("preparing"))
        elif done:
            r = h.get("r") or {}
            row.set_chip(pick(r.get("verdict_label"), self.lang) or self.tr("done"), r.get("verdict", "none"))
            n, c = len(r.get("recordings", [])), r.get("counts", {})
            bits = self.tr("sep").join(self.tr(k).format(c[k]) for k in ("pass", "warn", "fixable", "fail") if c.get(k))
            row.step.setText(f"{r.get('created', '')} · " + (self.tr("n_recs").format(n) + bits if n else self.tr("none_found")))
        else:
            row.set_chip(self.tr("failed" if st == "failed" else "stopped"), "fail" if st == "failed" else "none")
            row.step.setText(self.tr("no_report") + (self.tr("last_out") + h["err"] if h.get("err") else ""))
        row.layout().activate()
        h["item"].setSizeHint(row.sizeHint())

    def load_history(self):
        """Past reports in the report folder, below the jobs of this session (which stay untouched)."""
        for h in [h for h in self.rows if h.get("hist")]:
            self.list.takeItem(self.list.row(h["item"])); h["row"].deleteLater(); self.rows.remove(h)
        if os.path.isdir(self.out_root):
            jobs = sorted((d for d in os.listdir(self.out_root) if os.path.exists(os.path.join(self.out_root, d, "report.json"))), reverse=True)
            for d in jobs[:50]:
                rep = os.path.join(self.out_root, d, "report.html")
                if any(h.get("report") == rep for h in self.rows):
                    continue
                try:
                    r = json.load(open(os.path.join(self.out_root, d, "report.json")))
                except Exception:  # noqa: BLE001
                    continue
                h = self.add_row(r.get("dataset", d), r.get("input", ""), top=False)
                h.update(state="done", r=r, report=rep, hist=True)
                self.render_row(h)
        self.empty.setVisible(not self.rows)

    # ---------- jobs ----------
    def enqueue(self, paths):
        log("enqueue", paths)
        for p in paths:
            if not os.path.exists(p):
                continue
            h = self.add_row(os.path.basename(os.path.normpath(p)), p)
            h["input"] = p
            self.queue.append(h)
        self.start_next()

    def start_next(self):
        if self.proc is not None or not self.queue:
            return
        if not (os.path.isdir(self.out_root) or os.path.isdir(os.path.dirname(self.out_root.rstrip("/")))):
            QMessageBox.warning(self, "EgoQC", self.tr("no_out").format(self.out_root))
            return
        os.makedirs(self.out_root, exist_ok=True)
        h = self.queue.pop(0); self.current = h
        h.update(state="running", msg=None, log=[]); self.render_row(h)
        args = ["-I", CLI, h["input"], "--out", self.out_root, "--max-seconds", str(self.sp.value()), "--lang", self.lang]
        if not self.cb_self.isChecked():
            args.append("--no-selfcalib")
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.SeparateChannels)
        self.proc.readyReadStandardOutput.connect(self.on_stdout)
        self.proc.readyReadStandardError.connect(self.on_stderr)
        self.proc.finished.connect(self.on_finished)
        self.proc.errorOccurred.connect(self.on_error)
        log("start", sys.executable, args)
        self.proc.start(sys.executable, args)

    def on_stdout(self):
        h = self.current
        while self.proc and self.proc.canReadLine():
            line = bytes(self.proc.readLine()).decode("utf-8", "replace").strip()
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if d.get("job"):
                h["job"] = d["job"]
            if "progress" in d:
                h["msg"] = d.get("msg")
                h["row"].bar.setValue(int(d["progress"] * 1000)); h["row"].step.setText(pick(h["msg"], self.lang))
            if d.get("done"):
                h["report"] = d["report"]

    def on_stderr(self):
        if self.proc:
            self.current.setdefault("log", []).append(bytes(self.proc.readAllStandardError()).decode("utf-8", "replace"))

    def on_error(self, err):
        log("process error", err, self.proc.errorString() if self.proc else "")
        if err == QProcess.FailedToStart:   # finished() never comes in this case
            self.current.setdefault("log", []).append(self.tr("cant_start") + self.proc.errorString())
            self.on_finished(-1, None)

    def on_finished(self, code, _status):
        log("finished", code, self.current.get("input") if self.current else None)
        h, self.proc = self.current, None
        if h.get("report") and os.path.exists(h["report"]):
            try:
                h["r"] = json.load(open(os.path.join(os.path.dirname(h["report"]), "report.json")))
            except Exception:  # noqa: BLE001
                h["r"] = {}
            h["state"] = "done"; self.render_row(h)
            if self.cb_open.isChecked():
                self.open_report(h)
        else:
            job = h.get("job")
            if h.get("cancelled") and job and not os.path.exists(os.path.join(job, "report.json")):
                shutil.rmtree(job, ignore_errors=True)   # force-killed before it could clean up itself
            tail = "".join(h.get("log", [])).strip()
            h["err"] = tail.splitlines()[-1][-300:] if tail else ""
            h["state"] = "stopped" if h.get("cancelled") else "failed"
            self.render_row(h)
        self.current = None
        QTimer.singleShot(200, self.start_next)

    def cancel(self):
        if self.proc and self.current:
            self.current["cancelled"] = True
            self.proc.terminate()   # SIGTERM: the job cleans up its folder; force-kill if it hangs
            QTimer.singleShot(8000, lambda p=self.proc: p and p.state() != QProcess.NotRunning and p.kill())

    def open_report(self, h):
        if h.get("report"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(h["report"]))

    def reveal(self, h):
        p = h.get("report")
        if not p:
            return
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", p])
        elif sys.platform == "win32":
            subprocess.run(["explorer", "/select,", os.path.normpath(p)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(p)))


class App(QApplication):
    """Also accepts folders/files dropped on the Dock icon (QFileOpenEvent)."""

    def __init__(self, argv):
        super().__init__(argv)
        self.win, self.pending = None, []

    def event(self, ev):
        if ev.type() == QEvent.FileOpen:
            log("FileOpen", ev.file(), ev.url().toString())
            p = ev.file() or ev.url().toLocalFile()
            if self.win:
                self.win.enqueue([p])
            else:
                self.pending.append(p)
            return True
        return super().event(ev)


def main():
    log("launch", sys.argv)
    app = App(sys.argv)
    app.setApplicationName("EgoQC"); app.setStyle("Fusion"); app.setStyleSheet(CSS)
    f = QFont(); f.setFamilies(["PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", "Helvetica Neue", "Segoe UI"])
    app.setFont(f)
    w = Main(); app.win = w
    w.show()
    paths = [a for a in sys.argv[1:] if os.path.exists(a)] + app.pending
    if paths:
        w.enqueue(paths)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
