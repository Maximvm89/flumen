"""Legami Workspace — PySide6 desktop GUI.

Navigable, color-coded tree of the FTP project (server-only / local-only / both),
with lazy loading for speed: the top level shows instantly and each folder's
contents load only when you expand it, over a single persistent connection.

Run:  python -m workspace_app        (from the toolkit folder, venv active)
"""

from __future__ import annotations

import datetime as _dt
import os
import platform
import sys
import threading
import webbrowser

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QBrush, QColor, QAction, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QGroupBox, QTabWidget,
    QComboBox, QTableWidget, QTableWidgetItem, QDialog, QFormLayout,
    QDialogButtonBox, QInputDialog, QMenu, QPlainTextEdit, QCheckBox,
)

from animpipe.config import ProjectConfig, SFTPCredentials, CACHED_CONFIG
from animpipe.sftp import SFTPClient
from animpipe import tasks as tasksmod
from animpipe import schema as schema_mod
from animpipe import review as reviewmod
from animpipe import clipboard as clipboardmod
from animpipe import bugreport
from animpipe.version import get_version
from . import core
from . import applog

SERVER_BG, SERVER_FG = QColor(250, 238, 218), QColor(133, 79, 11)
LOCAL_BG, LOCAL_FG = QColor(230, 241, 251), QColor(12, 68, 124)
SYNC_BG, SYNC_FG = QColor(225, 245, 238), QColor(8, 80, 65)
NEWER_L_BG = QColor(203, 225, 255)
NEWER_R_BG = QColor(255, 230, 191)
DIFF_BG, DIFF_FG = QColor(252, 235, 235), QColor(163, 45, 45)

# Task status visuals: status -> (text color, leading dot). No cell background —
# the colored dot + bold text carry the signal, which reads on any theme.
STATUS_STYLE = {
    "todo":        (QColor(150, 155, 165), "⚪"),
    "in_progress": (QColor(96, 165, 250), "🔵"),
    "review":      (QColor(245, 158, 66), "🟠"),
    "done":        (QColor(74, 222, 128), "🟢"),
}
# A small icon per task type and per department/step (first substring match wins).
TYPE_ICON = {"asset": "📦", "shot": "🎬"}
STEP_ICONS = (
    ("model", "🧊"), ("surfac", "🎨"), ("textur", "🎨"), ("shad", "🎨"),
    ("look", "🎨"), ("groom", "🧶"), ("rig", "🦴"), ("layout", "📐"),
    ("anim", "🎞"), ("light", "💡"), ("comp", "🟦"), ("fx", "✨"),
)


def step_icon(step: str) -> str:
    s = (step or "").lower()
    for key, ic in STEP_ICONS:
        if key in s:
            return ic
    return "▫️"


# Review-item status visuals: status -> (text color, leading dot).
REVIEW_STATUS_STYLE = {
    "to_review": (QColor(245, 158, 66), "🟠"),
    "reviewed":  (QColor(150, 155, 165), "⚪"),
    "approved":  (QColor(74, 222, 128), "🟢"),
}

ROLE_NODE = Qt.UserRole
ROLE_LOADED = Qt.UserRole + 1


def _fmt_time(ts):
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""


def node_style(node):
    loc = node.location
    if loc == core.LOC_SERVER_ONLY:
        return ("server only", SERVER_BG, SERVER_FG)
    if loc == core.LOC_LOCAL_ONLY:
        return ("local only", LOCAL_BG, LOCAL_FG)
    st = node.file_status
    if st == core.LOCAL_NEWER:
        return ("both · local newer", NEWER_L_BG, LOCAL_FG)
    if st == core.REMOTE_NEWER:
        return ("both · server newer", NEWER_R_BG, SERVER_FG)
    if st == core.SIZE_DIFFERS:
        return ("both · differs", DIFF_BG, DIFF_FG)
    return ("both", SYNC_BG, SYNC_FG)


def is_download_candidate(f):
    return f.in_remote and not f.is_dir and (
        f.location == core.LOC_SERVER_ONLY or f.file_status == core.REMOTE_NEWER)


def is_upload_candidate(f):
    return f.in_local and not f.is_dir and (
        f.location == core.LOC_LOCAL_ONLY or f.file_status == core.LOCAL_NEWER)


class Job(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self, config_path="config.yaml"):
        super().__init__()
        from animpipe.version import get_version
        self.setWindowTitle(f"Legami Workspace — {get_version()}")
        self.resize(1040, 680)
        self.config_path = config_path
        self.cfg: ProjectConfig | None = None
        self._conn: SFTPClient | None = None
        self._lock = threading.Lock()
        self._jobs: list[Job] = []
        self._summary = "Ready."
        self._full_tree: core.TreeNode | None = None  # cached full scan for filters
        self._ledger: dict = {}  # rel -> (user, time), who uploaded each file
        self._tasks: list[dict] = []

        self._build_menu()
        self._build_ui()
        self._load_config()
        # Auto-load shortly after the window appears.
        QTimer.singleShot(150, self._load_tasks)
        QTimer.singleShot(200, self._load_root)
        QTimer.singleShot(250, self._load_review_items)

    # ---- connection (persistent, serialized via lock) -----------------------
    def _conn_do(self, fn):
        with self._lock:
            if self._conn is None:
                c = SFTPClient(self._creds())
                c.connect()
                self._conn = c
            try:
                return fn(self._conn)
            except (OSError, EOFError):  # transport dropped -> reconnect once
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    pass
                c = SFTPClient(self._creds())
                c.connect()
                self._conn = c
                return fn(self._conn)

    def closeEvent(self, event):
        if self._conn:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(event)

    # ---- UI -----------------------------------------------------------------
    def _build_menu(self):
        m = self.menuBar().addMenu("File")
        act = QAction("Open config…", self)
        act.triggered.connect(self._pick_config)
        m.addAction(act)

        h = self.menuBar().addMenu("Help")
        report = QAction("Report a bug…", self)
        report.triggered.connect(self._on_report_bug)
        h.addAction(report)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        box = QGroupBox("Project")
        grid = QGridLayout(box)
        self.lbl_project = QLabel("—")
        grid.addWidget(QLabel("Project:"), 0, 0)
        grid.addWidget(self.lbl_project, 0, 1, 1, 3)
        grid.addWidget(QLabel("Signed in as:"), 2, 0)
        self.lbl_user = QLabel("—")
        self.lbl_user.setStyleSheet("font-weight:600;")
        grid.addWidget(self.lbl_user, 2, 1, 1, 2)
        self.b_signin = QPushButton("Sign in…")
        self.b_signin.clicked.connect(self._sign_in)
        grid.addWidget(self.b_signin, 2, 3)
        grid.addWidget(QLabel("Local folder:"), 1, 0)
        self.ed_local = QLineEdit()
        grid.addWidget(self.ed_local, 1, 1, 1, 2)
        b_local = QPushButton("Browse…")
        b_local.clicked.connect(self._pick_local)
        grid.addWidget(b_local, 1, 3)
        outer.addWidget(box)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_tasks_page(), "Tasks")
        self.tabs.addTab(self._build_dailies_page(), "Dailies")
        self.tabs.addTab(self._build_files_page(), "Files")
        self.tabs.setCurrentIndex(0)  # Tasks is the default view

        self.status = self.statusBar()
        self.status.showMessage("Ready.")
        self.b_report = QPushButton("Report a bug…")
        self.b_report.setToolTip("Open a pre-filled GitHub issue with the app log "
                                 "and a screenshot.")
        self.b_report.clicked.connect(self._on_report_bug)
        self.status.addPermanentWidget(self.b_report)

    def _build_files_page(self) -> QWidget:
        page = QWidget()
        fl = QVBoxLayout(page)

        actions = QHBoxLayout()
        self.b_mirror = QPushButton("Create Local Structure")
        self.b_mirror.clicked.connect(self._on_mirror)
        self.b_configure = QPushButton("Configure Blender → this folder")
        self.b_configure.clicked.connect(self._on_configure)
        self.b_refresh = QPushButton("Refresh")
        self.b_refresh.clicked.connect(self._refresh)
        actions.addWidget(self.b_mirror)
        actions.addWidget(self.b_configure)
        actions.addStretch(1)
        actions.addWidget(self.b_refresh)
        fl.addLayout(actions)

        filt = QHBoxLayout()
        filt.addWidget(QLabel("Filter:"))
        self._filter_btns: dict[str, QPushButton] = {}
        for key, text, bg, fg in (("server", "server only", SERVER_BG, SERVER_FG),
                                  ("local", "local only", LOCAL_BG, LOCAL_FG),
                                  ("sync", "both (in sync)", SYNC_BG, SYNC_FG),
                                  ("differs", "differs", DIFF_BG, DIFF_FG)):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setStyleSheet(
                f"QPushButton {{ padding:3px 12px; border-radius:6px; "
                f"border:1px solid {bg.darker(115).name()}; color:{fg.name()}; }} "
                f"QPushButton:checked {{ background:{bg.name()}; font-weight:600; }}")
            b.toggled.connect(self._apply_filter)
            self._filter_btns[key] = b
            filt.addWidget(b)
        filt.addStretch(1)
        fl.addLayout(filt)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(
            ["Name", "Where", "Uploaded by", "Local", "Remote", "Modified"])
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, 6):
            self.tree.header().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.tree.itemExpanded.connect(self._on_expand)
        fl.addWidget(self.tree, 1)

        transfer = QHBoxLayout()
        self.b_download = QPushButton("⬇ Download selected")
        self.b_download.clicked.connect(lambda: self._on_transfer("download", True))
        self.b_upload = QPushButton("⬆ Upload selected")
        self.b_upload.clicked.connect(lambda: self._on_transfer("upload", True))
        self.b_download_all = QPushButton("⬇ Download all from server")
        self.b_download_all.clicked.connect(lambda: self._on_transfer("download", False))
        self.b_upload_all = QPushButton("⬆ Upload all local changes")
        self.b_upload_all.clicked.connect(lambda: self._on_transfer("upload", False))
        for b in (self.b_download, self.b_upload, self.b_download_all, self.b_upload_all):
            transfer.addWidget(b)
        fl.addLayout(transfer)
        return page

    def _build_tasks_page(self) -> QWidget:
        page = QWidget()
        tl = QVBoxLayout(page)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Show:"))
        self.cb_scope = QComboBox()
        self.cb_scope.addItems(["My tasks", "All tasks"])  # default: My tasks
        self.cb_scope.currentIndexChanged.connect(self._render_tasks)
        controls.addWidget(self.cb_scope)
        controls.addWidget(QLabel("Status:"))
        self.cb_status_filter = QComboBox()
        self.cb_status_filter.addItem("All", None)
        for s in tasksmod.STATUSES:
            self.cb_status_filter.addItem(tasksmod.STATUS_LABELS[s], s)
        self.cb_status_filter.currentIndexChanged.connect(self._render_tasks)
        controls.addWidget(self.cb_status_filter)
        controls.addSpacing(12)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("Search… (entity, step, assignee)")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.textChanged.connect(self._render_tasks)
        self.ed_search.setMinimumWidth(220)
        controls.addWidget(self.ed_search, 1)
        controls.addStretch(0)
        b_new_asset = QPushButton("New Asset…")
        b_new_asset.clicked.connect(self._new_asset)
        b_new_shot = QPushButton("New Shot…")
        b_new_shot.clicked.connect(self._new_shot)
        b_gen = QPushButton("Generate from structure")
        b_gen.clicked.connect(self._generate_tasks)
        b_reload = QPushButton("Refresh")
        b_reload.clicked.connect(self._load_tasks)
        controls.addWidget(b_new_asset)
        controls.addWidget(b_new_shot)
        controls.addWidget(b_gen)
        controls.addWidget(b_reload)
        tl.addLayout(controls)

        self.tasks_table = QTableWidget(0, 6)
        self.tasks_table.setHorizontalHeaderLabels(
            ["Type", "Entity", "Step", "Status", "Assignees", "Updated by"])
        self.tasks_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tasks_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tasks_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tasks_table.setSortingEnabled(True)
        self.tasks_table.setAlternatingRowColors(True)
        self.tasks_table.setShowGrid(False)
        self.tasks_table.verticalHeader().setDefaultSectionSize(26)
        self.tasks_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tasks_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tasks_table.customContextMenuRequested.connect(self._task_context_menu)
        tl.addWidget(self.tasks_table, 1)

        rowops = QHBoxLayout()
        b_assign_me = QPushButton("Assign me")
        b_assign_me.clicked.connect(lambda: self._assign_selected(self._creds().user, True))
        b_unassign_me = QPushButton("Unassign me")
        b_unassign_me.clicked.connect(lambda: self._assign_selected(self._creds().user, False))
        b_assign_to = QPushButton("Assign to…")
        b_assign_to.clicked.connect(self._assign_to)
        rowops.addWidget(b_assign_me)
        rowops.addWidget(b_unassign_me)
        rowops.addWidget(b_assign_to)
        rowops.addStretch(1)
        rowops.addWidget(QLabel("Set status:"))
        self.cb_set_status = QComboBox()
        for s in tasksmod.STATUSES:
            self.cb_set_status.addItem(tasksmod.STATUS_LABELS[s], s)
        rowops.addWidget(self.cb_set_status)
        b_apply_status = QPushButton("Apply")
        b_apply_status.clicked.connect(self._apply_status)
        rowops.addWidget(b_apply_status)
        tl.addLayout(rowops)
        return page

    # ---- dailies / review ---------------------------------------------------
    def _build_dailies_page(self) -> QWidget:
        page = QWidget()
        dl = QVBoxLayout(page)
        self._review_items: list[dict] = []

        top = QHBoxLayout()
        top.addWidget(QLabel("Show:"))
        self._review_filter_btns: dict[str, QPushButton] = {}
        for st in reviewmod.REVIEW_STATUSES:
            color, dot = REVIEW_STATUS_STYLE[st]
            b = QPushButton(f"{dot} {reviewmod.REVIEW_LABELS[st]}")
            b.setCheckable(True)
            b.setChecked(st != "reviewed")   # default: hide 'reviewed'
            b.setStyleSheet(
                "QPushButton { padding:3px 12px; border-radius:6px; } "
                f"QPushButton:checked {{ font-weight:600; color:{color.name()}; }}")
            b.toggled.connect(self._render_review_tree)
            self._review_filter_btns[st] = b
            top.addWidget(b)
        top.addStretch(1)
        b_refresh = QPushButton("Refresh")
        b_refresh.clicked.connect(self._load_review_items)
        top.addWidget(b_refresh)
        dl.addLayout(top)

        self.review_tree = QTreeWidget()
        self.review_tree.setColumnCount(3)
        self.review_tree.setHeaderLabels(["Item", "Artist", "Status"])
        self.review_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.review_tree.setAlternatingRowColors(True)
        self.review_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.review_tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.review_tree.itemDoubleClicked.connect(lambda *_: self._play_clip())
        dl.addWidget(self.review_tree, 1)

        ops = QHBoxLayout()
        ops.addWidget(QLabel("Set status:"))
        self.cb_review_status = QComboBox()
        for st in reviewmod.REVIEW_STATUSES:
            self.cb_review_status.addItem(reviewmod.REVIEW_LABELS[st], st)
        ops.addWidget(self.cb_review_status)
        b_apply = QPushButton("Apply")
        b_apply.clicked.connect(self._set_review_status)
        ops.addWidget(b_apply)
        ops.addSpacing(18)
        b_play = QPushButton("▶ Play")
        b_play.clicked.connect(self._play_clip)
        ops.addWidget(b_play)
        b_copy = QPushButton("Copy clip")
        b_copy.setToolTip("Copy the clip to the clipboard (e.g. for SyncSketch).")
        b_copy.clicked.connect(self._copy_review_clip)
        ops.addWidget(b_copy)
        b_sheet = QPushButton("Texture sheet")
        b_sheet.setToolTip("Open the look's texture/UV sheet (surface looks only).")
        b_sheet.clicked.connect(self._open_review_sheet)
        ops.addWidget(b_sheet)
        ops.addStretch(1)
        b_export = QPushButton("Export…")
        b_export.setToolTip("Export the visible items to a dated folder + index.html.")
        b_export.clicked.connect(self._export_review)
        ops.addWidget(b_export)
        dl.addLayout(ops)
        return page

    def _load_review_items(self):
        if not self.cfg:
            return
        remote = self.cfg.remote_root

        def work():
            return self._conn_do(lambda c: reviewmod.review_items(
                tasksmod.load_tasks(c, remote)))

        def done(items):
            self._busy_buttons(False)
            self._review_items = items
            self._render_review_tree()

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Loading review items…")

    def _render_review_tree(self):
        from collections import OrderedDict
        tree = self.review_tree
        tree.clear()
        active = {st for st, b in self._review_filter_btns.items() if b.isChecked()}
        groups: "OrderedDict[str, list]" = OrderedDict()
        for it in self._review_items:
            if it["status"] not in active:
                continue
            groups.setdefault(it["date"] or "(no date)", []).append(it)

        shown = 0
        for date, items in groups.items():
            parent = QTreeWidgetItem([f"{date}    ({len(items)})", "", ""])
            pf = parent.font(0)
            pf.setBold(True)
            parent.setFont(0, pf)
            parent.setFirstColumnSpanned(True)
            tree.addTopLevelItem(parent)
            for it in items:
                label = f"{step_icon(it['step'])}  {it['entity']} · {it['version']}"
                color, dot = REVIEW_STATUS_STYLE.get(it["status"], (None, "•"))
                child = QTreeWidgetItem([
                    label, it["by"],
                    f"{dot}  {reviewmod.REVIEW_LABELS.get(it['status'], it['status'])}"])
                if color is not None:
                    child.setForeground(2, QBrush(color))
                    cf = child.font(2)
                    cf.setBold(True)
                    child.setFont(2, cf)
                child.setData(0, Qt.UserRole, it)
                parent.addChild(child)
                shown += 1
            parent.setExpanded(True)
        self.status.showMessage(f"{shown} review item(s) shown.")

    def _selected_review_items(self) -> list[dict]:
        out = []
        for node in self.review_tree.selectedItems():
            d = node.data(0, Qt.UserRole)
            if d:
                out.append(d)
        return out

    def _set_review_status(self):
        if not self.cfg:
            return
        items = self._selected_review_items()
        if not items:
            QMessageBox.information(self, "No items selected",
                                    "Select review item(s) first.")
            return
        status = self.cb_review_status.currentData()
        remote = self.cfg.remote_root
        actor = self._creds_user_safe()
        targets = [(it["task_id"], it["source"]) for it in items]

        def work():
            for tid, src in targets:
                self._conn_do(lambda c, a=tid, b=src: reviewmod.set_review_status(
                    c, remote, a, b, status, actor))
            return len(targets)

        def done(_n):
            self._busy_buttons(False)
            self._load_review_items()   # approve may have completed a task
            self._load_tasks()          # reflect task-status changes in Tasks tab

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Updating review status…")

    def _review_clip_local(self, it: dict) -> str:
        return os.path.join(self.cfg.resolved_local_root(), *it["source"].split("/"))

    def _ensure_clip_then(self, it: dict, after, busy: str):
        """Make sure the item's mp4 is local (download if needed), then call after(path)."""
        local = self._review_clip_local(it)
        if os.path.isfile(local):
            after(local)
            return
        remote = self.cfg.remote_root

        def work():
            self._conn_do(lambda c: c.download(remote.rstrip("/") + "/" + it["source"],
                                               local))
            return local

        def done(p):
            self._busy_buttons(False)
            after(p)

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg=busy)

    def _play_clip(self):
        items = self._selected_review_items()
        if not items:
            QMessageBox.information(self, "No item selected",
                                    "Select a review item to play.")
            return
        self._ensure_clip_then(items[0], clipboardmod.open_path, "Fetching clip…")

    def _copy_review_clip(self):
        items = self._selected_review_items()
        if not items:
            QMessageBox.information(self, "No item selected", "Select a review item.")
            return

        def _copy(p):
            ok = clipboardmod.copy_file(p)
            self.status.showMessage(
                f"Copied {os.path.basename(p)} to clipboard." if ok
                else "Could not copy to clipboard on this platform.")

        self._ensure_clip_then(items[0], _copy, "Fetching clip…")

    def _open_review_sheet(self):
        items = self._selected_review_items()
        if not items:
            QMessageBox.information(self, "No item selected", "Select a review item.")
            return
        it = items[0]
        if not it.get("sheet"):
            QMessageBox.information(self, "No texture sheet",
                                    "This item has no texture/UV sheet (it's not a "
                                    "surface look review).")
            return
        local = os.path.join(self.cfg.resolved_local_root(), *it["sheet"].split("/"))
        if os.path.isfile(local):
            clipboardmod.open_path(local)
            return
        remote = self.cfg.remote_root

        def work():
            self._conn_do(lambda c: c.download(
                remote.rstrip("/") + "/" + it["sheet"], local))
            return local

        def done(p):
            self._busy_buttons(False)
            clipboardmod.open_path(p)

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Fetching texture sheet…")

    def _export_review(self):
        if not self.cfg:
            return
        active = {st for st, b in self._review_filter_btns.items() if b.isChecked()}
        items = [it for it in self._review_items if it["status"] in active]
        if not items:
            QMessageBox.information(self, "Nothing to export",
                                    "No visible items to export.")
            return
        date_str = reviewmod.today_str()
        remote = self.cfg.remote_root
        local_root = self.cfg.resolved_local_root()
        actor = self._creds_user_safe()

        def work():
            return self._conn_do(lambda c: reviewmod.write_review_folder(
                c, remote_root=remote, local_root=local_root, items=items,
                date_str=date_str, username=actor))

        def done(res):
            self._busy_buttons(False)
            self.status.showMessage(
                f"Exported {res['count']} clip(s) -> {res['folder_local']}")
            idx = os.path.join(res["folder_local"], "index.html")
            if os.path.isfile(idx):
                webbrowser.open("file://" + idx)

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Exporting review…")

    # ---- bug report ---------------------------------------------------------
    def _env_text(self) -> str:
        plat = f"{platform.system()} {platform.release()}"
        project = user = remote = local = ""
        if self.cfg:
            project = f"{self.cfg.name} [{self.cfg.code}]"
            try:
                local = self.cfg.resolved_local_root()
            except Exception:  # noqa: BLE001
                pass
        try:
            creds = self._creds()
            user, remote = creds.user, creds.remote_root or ""
        except Exception:  # noqa: BLE001
            pass
        return bugreport.environment_text(
            version=get_version(), platform_str=plat, project=project,
            user=user, remote_root=remote, local_root=local)

    def _on_report_bug(self):
        # Grab the screen BEFORE the dialog covers it (best-effort).
        shot = None
        try:
            screen = QApplication.instance().primaryScreen()
            if screen:
                shot = screen.grabWindow(0)
        except Exception:  # noqa: BLE001
            shot = None

        dlg = BugReportDialog(shot, self)
        if dlg.exec() != QDialog.Accepted:
            return

        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = os.path.join(os.path.expanduser("~"), ".legami", "bug_reports", ts)
        os.makedirs(folder, exist_ok=True)
        attached = []
        if dlg.include_screenshot and shot is not None:
            if shot.save(os.path.join(folder, "screenshot.png"), "PNG"):
                attached.append("screenshot.png")
        log_tail = ""
        if dlg.include_log:
            if applog.copy_full(applog.LOG_PATH, os.path.join(folder, "log.txt")):
                attached.append("log.txt")
            log_tail = applog.read_tail(applog.LOG_PATH, 200)

        url, _ = bugreport.build_issue(
            dlg.title, dlg.description, self._env_text(), log_tail, attached)
        webbrowser.open(url)
        clipboardmod.reveal(folder)
        self.status.showMessage(
            "Opened a pre-filled GitHub issue — drag log.txt/screenshot.png from the "
            "folder into it, then Submit.")

    # ---- config / creds -----------------------------------------------------
    def _load_config(self):
        # Dev: a local config.yaml. Artist: the show config the app downloaded from
        # the server into the cache on sign-in (ProjectConfig.load falls back to it).
        if not os.path.isfile(self.config_path) and not os.path.isfile(CACHED_CONFIG):
            self.lbl_project.setText("Not connected — Sign in to load the project.")
            self._refresh_user_label()
            QTimer.singleShot(200, self._sign_in)   # generic bundle, first run
            return
        try:
            self.cfg = ProjectConfig.load(self.config_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Config error", str(exc))
            return
        self.lbl_project.setText(f"{self.cfg.name} [{self.cfg.code}]  →  {self.cfg.remote_root}")
        if not self.ed_local.text().strip():
            self.ed_local.setText(self.cfg.resolved_local_root())
        self._refresh_user_label()

    def _refresh_user_label(self):
        try:
            self.lbl_user.setText(SFTPCredentials.from_env(".env").user)
            self.b_signin.setText("Switch user / project…")
        except Exception:  # noqa: BLE001
            self.lbl_user.setText("(not signed in)")
            self.b_signin.setText("Sign in…")

    def _sign_in(self):
        """Collect server + project root + login, connect, and download the show
        config from the server. Nothing about the show is baked into the app."""
        try:
            saved = SFTPCredentials.from_env(".env")
        except Exception:  # noqa: BLE001
            saved = None
        dlg = QDialog(self)
        dlg.setWindowTitle("Sign in")
        form = QFormLayout(dlg)
        ed_host = QLineEdit(saved.host if saved else "")
        ed_port = QLineEdit(str(saved.port) if saved else "22")
        ed_root = QLineEdit(saved.remote_root if saved and saved.remote_root else "")
        ed_root.setPlaceholderText("e.g. /shared/Legami")
        ed_user = QLineEdit(saved.user if saved else "")
        ed_pw = QLineEdit()
        ed_pw.setEchoMode(QLineEdit.Password)
        if saved and saved.password:
            ed_pw.setText(saved.password)
        form.addRow("Server host:", ed_host)
        form.addRow("Port:", ed_port)
        form.addRow("Project root:", ed_root)
        form.addRow("Username:", ed_user)
        form.addRow("Password:", ed_pw)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if dlg.exec() != QDialog.Accepted:
            return
        host, root, user = (ed_host.text().strip(), ed_root.text().strip(),
                            ed_user.text().strip())
        if not (host and root and user):
            QMessageBox.warning(self, "Sign in",
                                "Server host, project root and username are required.")
            return
        try:
            port = int(ed_port.text().strip() or "22")
        except ValueError:
            port = 22
        creds = SFTPCredentials(host=host, port=port, user=user,
                                password=ed_pw.text() or None, remote_root=root)
        # Connect + download the show config (this also validates the login).
        self.status.showMessage("Connecting…")
        try:
            from animpipe.project_sync import fetch_project_config
            fetch_project_config(creds, root)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Sign in failed",
                                 f"Could not connect or load the project from "
                                 f"{host}:{root}\n\n{exc}")
            return
        creds.save_user()
        self._load_config()   # now loads the downloaded config from the cache
        self.status.showMessage(f"Signed in as {user}.")

    def _creds(self) -> SFTPCredentials:
        return SFTPCredentials.from_env(".env")

    # ---- job runners --------------------------------------------------------
    def _spawn(self, fn, on_done, busy_msg="Working…"):
        self.status.showMessage(busy_msg)
        job = Job(fn)
        self._jobs.append(job)
        job.done.connect(lambda r, j=job: (self._jobs.remove(j) if j in self._jobs else None, on_done(r)))
        job.failed.connect(lambda m, j=job: (self._jobs.remove(j) if j in self._jobs else None, self._on_error(m)))
        job.start()

    def _busy_buttons(self, on):
        for b in (self.b_mirror, self.b_configure, self.b_refresh, self.b_download,
                  self.b_upload, self.b_download_all, self.b_upload_all):
            b.setEnabled(not on)

    def _run(self, fn, on_done):
        self._busy_buttons(True)
        self._spawn(fn, lambda r: (self._busy_buttons(False), on_done(r)))

    def _on_error(self, msg):
        self._busy_buttons(False)
        self.status.showMessage(f"Error: {msg}")
        QMessageBox.critical(self, "Error", msg)

    # ---- pickers ------------------------------------------------------------
    def _pick_config(self):
        p, _ = QFileDialog.getOpenFileName(self, "Select config.yaml", "",
                                           "YAML (*.yaml *.yml)")
        if p:
            self.config_path = p
            self._load_config()
            self._load_root()

    def _pick_local(self):
        p = QFileDialog.getExistingDirectory(self, "Select local project folder")
        if p:
            self.ed_local.setText(p)
            self._load_root()

    # ---- actions ------------------------------------------------------------
    def _on_mirror(self):
        if not self.cfg:
            return
        local = self.ed_local.text().strip()
        remote = self.cfg.remote_root

        def work():
            return self._conn_do(lambda c: core.mirror_structure(c, remote, local))

        def done(created):
            self._busy_buttons(False)
            QMessageBox.information(self, "Structure created",
                                    f"Mirrored {len(created)} folder(s) locally.")
            self._refresh()

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Creating local structure…")

    def _on_configure(self):
        if not self.cfg:
            return
        local = self.ed_local.text().strip()
        try:
            core.set_local_root_in_config(self.config_path, local)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Could not configure", str(exc))
            return
        QMessageBox.information(
            self, "Blender configured",
            f"Saved local folder to config.yaml.\nThe launcher and Blender addon "
            f"will now save into:\n{local}")

    # ---- lazy tree ----------------------------------------------------------
    def _make_item(self, node, lazy=True) -> QTreeWidgetItem:
        label, bg, fg = node_style(node)
        cols = [
            node.name, label,
            core.uploader_for(node, self._ledger),
            core.human_size(node.local_size) if node.in_local and not node.is_dir else "",
            core.human_size(node.remote_size) if node.in_remote and not node.is_dir else "",
            _fmt_time(node.local_mtime or node.remote_mtime),
        ]
        item = QTreeWidgetItem(cols)
        for c in range(6):
            item.setBackground(c, QBrush(bg))
            item.setForeground(c, QBrush(fg))
        item.setData(0, ROLE_NODE, node)
        if lazy:
            item.setData(0, ROLE_LOADED, False)
            if node.is_dir:
                item.addChild(QTreeWidgetItem(["Loading…"]))  # placeholder -> arrow
        else:
            item.setData(0, ROLE_LOADED, True)  # filtered tree is fully built
        return item

    def _load_root(self):
        if not self.cfg:
            return
        local = self.ed_local.text().strip()
        remote = self.cfg.remote_root

        def work():
            ledger = self._conn_do(lambda c: core.load_ledgers(c, remote))
            children = self._conn_do(lambda c: core.merge_children(
                c.listdir(remote), local, ""))
            count, total = core.local_total_all(local)
            return children, count, total, ledger

        def done(result):
            self._busy_buttons(False)
            children, count, total, ledger = result
            self._ledger = ledger
            self.tree.clear()
            for node in children:
                self.tree.addTopLevelItem(self._make_item(node))
            self._summary = (
                f"Local: {count} files, {core.human_size(total)} on disk"
                f"   |   expand a folder to compare it with the server")
            self.status.showMessage(self._summary)

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Loading project…")

    def _on_expand(self, item: QTreeWidgetItem):
        if item.data(0, ROLE_LOADED):
            return
        node = item.data(0, ROLE_NODE)
        if node is None or not node.is_dir:
            return
        item.setData(0, ROLE_LOADED, True)
        local = self.ed_local.text().strip()
        remote = core.remote_path_for(self.cfg.remote_root, node.rel)

        def work():
            return core.merge_children(
                self._conn_do(lambda c: c.listdir(remote)), local, node.rel)

        def done(children):
            item.takeChildren()  # drop the placeholder
            for child in children:
                item.addChild(self._make_item(child))
            if not children:
                item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)
            self.status.showMessage(self._summary)  # restore disk-usage line

        self._spawn(work, done, busy_msg=f"Loading {node.rel}…")

    # ---- filtering ----------------------------------------------------------
    def _active_filters(self) -> set[str]:
        return {k for k, b in self._filter_btns.items() if b.isChecked()}

    def _refresh(self):
        """Re-read from the server. Re-applies the active filter (full rescan) or
        reloads the lazy top level."""
        self._full_tree = None  # force fresh data
        if self._active_filters():
            self._apply_filter()
        else:
            self._load_root()

    def _apply_filter(self):
        if not self.cfg:
            return
        selected = self._active_filters()
        if not selected:
            self._load_root()  # back to fast browsing
            return
        if self._full_tree is not None:
            self._populate_filtered(self._full_tree, selected)  # instant re-filter
            return
        local = self.ed_local.text().strip()
        remote = self.cfg.remote_root

        def work():
            ledger = self._conn_do(lambda c: core.load_ledgers(c, remote))
            tree = self._conn_do(lambda c: core.build_merged_tree(c, remote, local))
            return tree, ledger

        def done(result):
            self._busy_buttons(False)
            tree, ledger = result
            self._full_tree = tree
            self._ledger = ledger
            self._populate_filtered(tree, self._active_filters())

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Scanning server to filter…")

    @staticmethod
    def _matches(node, selected) -> bool:
        if node.is_dir:
            return False
        loc, st = node.location, node.file_status
        return (("server" in selected and loc == core.LOC_SERVER_ONLY)
                or ("local" in selected and loc == core.LOC_LOCAL_ONLY)
                or ("sync" in selected and loc == core.LOC_BOTH and st == core.IN_SYNC)
                or ("differs" in selected and loc == core.LOC_BOTH
                    and st not in (None, core.IN_SYNC)))

    def _populate_filtered(self, root, selected):
        self.tree.clear()

        def build(node):
            items = []
            for name in sorted(node.children,
                               key=lambda n: (not node.children[n].is_dir, n.lower())):
                child = node.children[name]
                if child.is_dir:
                    sub = build(child)
                    if sub:  # only show folders that contain matches
                        it = self._make_item(child, lazy=False)
                        it.addChildren(sub)
                        items.append(it)
                elif self._matches(child, selected):
                    items.append(self._make_item(child, lazy=False))
            return items

        self.tree.addTopLevelItems(build(root))
        self.tree.expandAll()
        count = sum(1 for f in core.iter_files(root) if self._matches(f, selected))
        labels = ", ".join(self._filter_btns[k].text() for k in
                           ("server", "local", "sync", "differs") if k in selected)
        self.status.showMessage(f"Filter [{labels}]: {count} file(s) match")

    # ---- transfers ----------------------------------------------------------
    def _selected_nodes(self):
        out = []
        for item in self.tree.selectedItems():
            n = item.data(0, ROLE_NODE)
            if n:
                out.append(n)
        return out

    def _on_transfer(self, direction: str, selected: bool):
        if not self.cfg:
            return
        local_root = self.ed_local.text().strip()
        remote_root = self.cfg.remote_root
        username = self._creds().user
        pred = is_download_candidate if direction == "download" else is_upload_candidate
        roots = self._selected_nodes() if selected else [None]  # None = whole project
        if selected and not roots:
            QMessageBox.information(self, "Nothing selected", "Select files or folders first.")
            return
        verb = "Upload" if direction == "upload" else "Download"

        def work():
            # Build a (sub)tree for each root by a scoped recursive walk, collect candidates.
            chosen: dict[str, core.TreeNode] = {}
            for r in roots:
                rel = r.rel if r else ""
                rroot = core.remote_path_for(remote_root, rel) if rel else remote_root
                lroot = core.local_path_for(local_root, rel) if rel else local_root
                if r and not r.is_dir:  # a single selected file
                    if pred(r):
                        chosen[r.rel] = r
                    continue
                tree = self._conn_do(lambda c, rr=rroot, lr=lroot:
                                     core.build_merged_tree(c, rr, lr))
                for f in core.iter_files(tree):
                    if pred(f):
                        # f.rel is relative to the sub-root; re-anchor under rel
                        full_rel = f"{rel}/{f.rel}" if rel else f.rel
                        f.rel = full_rel
                        chosen[full_rel] = f
            files = list(chosen.values())
            if not files:
                return 0
            for f in files:
                rp = core.remote_path_for(remote_root, f.rel)
                lp = core.local_path_for(local_root, f.rel)
                self._conn_do(lambda c, a=lp, b=rp:
                              c.upload(a, b) if direction == "upload" else c.download(b, a))
            if direction == "upload":
                self._conn_do(lambda c: core.record_uploads(
                    c, remote_root, username, [f.rel for f in files]))
            return len(files)

        def done(n):
            self._busy_buttons(False)
            if n == 0:
                self.status.showMessage("Nothing to transfer (already in sync).")
            else:
                self.status.showMessage(f"{verb}ed {n} file(s). Refreshing…")
                self._refresh()

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg=f"{verb}ing…")


    # ---- tasks --------------------------------------------------------------
    def _load_tasks(self):
        if not self.cfg:
            return
        remote = self.cfg.remote_root

        def work():
            return self._conn_do(lambda c: tasksmod.load_tasks(c, remote))

        def done(tasks):
            self._busy_buttons(False)
            self._tasks = tasks
            self._render_tasks()
            self.status.showMessage(f"{len(tasks)} task(s) loaded.")

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Loading tasks…")

    def _render_tasks(self):
        scope_mine = self.cb_scope.currentText() == "My tasks"
        me = self._creds_user_safe()
        status_filter = self.cb_status_filter.currentData()
        query = self.ed_search.text()

        rows = []
        for t in self._tasks:
            if scope_mine and me not in (t.get("assignees") or []):
                continue
            if status_filter and t.get("status") != status_filter:
                continue
            if not tasksmod.matches_query(t, query):
                continue
            rows.append(t)
        rows.sort(key=lambda t: (t.get("type", ""), t.get("entity", ""), t.get("step", "")))

        self.tasks_table.setSortingEnabled(False)
        self.tasks_table.setRowCount(len(rows))
        for i, t in enumerate(rows):
            status = t.get("status", "")
            ttype = t.get("type", "")
            step = t.get("step", "")
            fg, dot = STATUS_STYLE.get(status, (None, "•"))
            cells = [
                f"{TYPE_ICON.get(ttype, '•')}  {ttype}",
                t.get("entity", ""),
                f"{step_icon(step)}  {step}",
                f"{dot}  {tasksmod.STATUS_LABELS.get(status, status)}",
                ", ".join(t.get("assignees") or []),
                t.get("updated_by", ""),
            ]
            for j, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if j == 0:
                    item.setData(Qt.UserRole, t)
                if j == 3:
                    if fg is not None:
                        item.setForeground(QBrush(fg))
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                self.tasks_table.setItem(i, j, item)
        self.tasks_table.setSortingEnabled(True)

    def _creds_user_safe(self) -> str:
        try:
            return self._creds().user
        except Exception:  # noqa: BLE001
            return ""

    def _selected_tasks(self) -> list[dict]:
        out = []
        for idx in self.tasks_table.selectionModel().selectedRows():
            item = self.tasks_table.item(idx.row(), 0)
            t = item.data(Qt.UserRole) if item else None
            if t:
                out.append(t)
        return out

    def _assign_selected(self, username: str, add: bool):
        if not self.cfg or not username:
            return
        chosen = self._selected_tasks()
        if not chosen:
            QMessageBox.information(self, "No tasks selected", "Select tasks first.")
            return
        remote = self.cfg.remote_root
        actor = self._creds_user_safe()
        ids = [t["id"] for t in chosen]

        def work():
            for tid in ids:
                self._conn_do(lambda c, x=tid: tasksmod.assign(
                    c, remote, x, username, add=add, actor=actor))
            return len(ids)

        def done(n):
            self._busy_buttons(False)
            self._load_tasks()

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Updating assignees…")

    def _assign_to(self):
        name, ok = QInputDialog.getText(self, "Assign to", "Username:")
        if ok and name.strip():
            self._assign_selected(name.strip(), True)

    def _apply_status(self):
        if not self.cfg:
            return
        status = self.cb_set_status.currentData()
        chosen = self._selected_tasks()
        if not chosen:
            QMessageBox.information(self, "No tasks selected", "Select tasks first.")
            return
        remote = self.cfg.remote_root
        actor = self._creds_user_safe()
        ids = [t["id"] for t in chosen]

        def work():
            for tid in ids:
                self._conn_do(lambda c, x=tid: tasksmod.set_status(
                    c, remote, x, status, actor=actor))
            return len(ids)

        def done(n):
            self._busy_buttons(False)
            self._load_tasks()

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Updating status…")

    def _task_context_menu(self, pos):
        item = self.tasks_table.itemAt(pos)
        if not item:
            return
        t = self.tasks_table.item(item.row(), 0).data(Qt.UserRole)
        if not t:
            return
        menu = QMenu(self)
        pubs = tasksmod.published_files(t)
        act_open = menu.addAction("Open in Blender (latest)")
        ver_menu = menu.addMenu("Open version")
        ver_actions = {}
        if pubs:
            for p in pubs:
                desc = (p.get("description") or "").splitlines()[0][:40]
                a = ver_menu.addAction(f"{p['name']}   ·   {p.get('by','?')}"
                                       + (f"   ·   {desc}" if desc else ""))
                ver_actions[a] = p["rel"]
        else:
            ver_menu.setEnabled(False)
        act_hist = menu.addAction("Publish history…")
        menu.addSeparator()
        act_delete = menu.addAction("Delete task")
        chosen = menu.exec(self.tasks_table.viewport().mapToGlobal(pos))
        if chosen == act_open:
            self._open_task_in_blender(t)
        elif chosen in ver_actions:
            self._open_task_in_blender(t, ver_actions[chosen])
        elif chosen == act_hist:
            self._show_history(t)
        elif chosen == act_delete:
            self._delete_task(t)

    def _show_history(self, task: dict):
        import datetime as _dt
        pubs = task.get("publishes") or []
        if not pubs:
            QMessageBox.information(self, "Publish history",
                                   f"No publishes yet for {task.get('entity')} — "
                                   f"{task.get('step')}.")
            return
        lines = []
        for p in reversed(pubs):  # newest first
            when = _dt.datetime.fromtimestamp(p.get("time", 0)).strftime("%Y-%m-%d %H:%M")
            files = ", ".join(os.path.basename(f) for f in (p.get("files") or []))
            desc = p.get("description") or "(no description)"
            lines.append(f"{when}  ·  {p.get('by','?')}\n  {desc}\n  files: {files}")
        QMessageBox.information(
            self, f"Publish history — {task.get('entity')} / {task.get('step')}",
            "\n\n".join(lines))

    def _open_task_in_blender(self, task: dict, blend_rel: str | None = None):
        if not self.cfg:
            return
        from animpipe.launcher import launch
        local_root = self.ed_local.text().strip() or self.cfg.resolved_local_root()
        self.cfg.local_root = local_root  # launcher syncs into the GUI's folder
        remote_root = self.cfg.remote_root
        work_abs = os.path.join(local_root, *tasksmod.task_work_rel(task).split("/"))

        # Open priority: chosen version > latest published > latest local work file.
        # Surface publishes a materials-only LOOK library (no scene) — opening it
        # gives Blender's "Library file, loading empty scene". So for surface, never
        # auto-open the publish: fall through to the work file (or a fresh shading
        # scene). Use 'Apply look…' in another task to consume a look.
        if blend_rel is None and task.get("step") != "surface":
            pubs = tasksmod.published_files(task)
            blend_rel = pubs[0]["rel"] if pubs else None
        open_file = None
        if blend_rel:
            open_file = core.local_path_for(local_root, blend_rel)
        else:
            import glob
            work_dir = os.path.join(local_root, *tasksmod.task_work_rel(task).split("/"))
            cands = (sorted(glob.glob(os.path.join(work_dir, "*.blend")), reverse=True)
                     if os.path.isdir(work_dir) else [])
            open_file = cands[0] if cands else None  # newest work file, opened locally

        extra_env = {
            "LEGAMI_TASK_ID": task.get("id", ""),
            "LEGAMI_TASK_TYPE": task.get("type", ""),
            "LEGAMI_TASK_ENTITY": task.get("entity", ""),
            "LEGAMI_TASK_STEP": task.get("step", ""),
            "LEGAMI_TASK_TITLE": task.get("title", ""),
            "LEGAMI_TASK_WORK_DIR": work_abs,
        }
        # Opening a surface task with nothing saved yet: start from a clean,
        # shading-ready scene instead of Blender's default cube/camera/light.
        if task.get("step") == "surface" and not open_file:
            extra_env["LEGAMI_NEW_SURFACE"] = "1"
        cfg = self.cfg
        creds = self._creds()

        def work():
            # Pull the chosen published file down so Blender opens the real version.
            if blend_rel:
                self._conn_do(lambda c: c.download(
                    core.remote_path_for(remote_root, blend_rel), open_file))
            # Surface/rig shade or rig the published model — pre-fetch it so the
            # "Load published model" button (and auto-load) work offline-fast.
            if task.get("step") in ("surface", "rig") and task.get("type") == "asset":
                mp = self._fetch_model_publish(task, local_root, remote_root)
                if mp:
                    extra_env["LEGAMI_MODEL_PUBLISH"] = mp
            return launch(cfg, creds, extra_env=extra_env, open_file=open_file)

        def done(rc):
            self._busy_buttons(False)
            if rc == 0:
                what = os.path.basename(open_file) if open_file else "new scene"
                self.status.showMessage(
                    f"Opening Blender ({what}) — {task.get('entity')} · {task.get('step')}")
            else:
                QMessageBox.warning(
                    self, "Could not launch Blender",
                    "Blender wasn't found. Set tools.blender_path in config.yaml "
                    "or the LEGAMI_BLENDER environment variable.")

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Fetching version + launching Blender…")

    def _fetch_model_publish(self, task: dict, local_root: str,
                             remote_root: str) -> str | None:
        """Download the asset's latest published model .blend locally and return
        its path, so a surface/rig session can load it. Runs on the Job thread
        (has the connection); returns None if there's no published model yet."""
        model_id = tasksmod.model_task_id(task.get("entity", ""))
        model_task = self._conn_do(
            lambda c: tasksmod.get_task(c, remote_root, model_id))
        if not model_task:
            return None
        pubs = tasksmod.published_files(model_task)
        if not pubs:
            return None
        rel = pubs[0]["rel"]
        local = core.local_path_for(local_root, rel)
        self._conn_do(lambda c: c.download(
            core.remote_path_for(remote_root, rel), local))
        return local

    def _new_asset(self):
        if not self.cfg:
            return
        dlg = NewAssetDialog(self.cfg.schema, self.cfg.naming or {}, self)
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.values()
        paths = schema_mod.asset_paths(self.cfg.schema, self.cfg.remote_root,
                                       v["category"], v["name"])
        self._create_entity("asset", v["entity"], v["steps"], paths)

    def _new_shot(self):
        if not self.cfg:
            return
        existing = tasksmod.sequences_from_tasks(self._tasks)
        dlg = NewShotDialog(self.cfg.schema, self.cfg.naming or {}, existing, self)
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.values()
        paths = schema_mod.shot_paths(self.cfg.schema, self.cfg.remote_root,
                                      v["seq"], v["shot"])
        self._create_entity("shot", v["entity"], v["steps"], paths)

    def _create_entity(self, ttype: str, entity: str, steps: list, paths: list):
        """Create the folder structure + one task per step (skipping tasks that
        already exist), then reload."""
        remote = self.cfg.remote_root
        actor = self._creds_user_safe()

        def work():
            self._conn_do(lambda c: c.create_all(paths))
            existing = {t["id"] for t in
                        self._conn_do(lambda c: tasksmod.load_tasks(c, remote))}
            created = 0
            for step in steps:
                t = tasksmod.new_task(ttype, entity, step)
                if t["id"] in existing:
                    continue
                self._conn_do(lambda c, x=t: tasksmod.save_task(c, remote, x, actor))
                created += 1
            return created

        def done(created):
            self._busy_buttons(False)
            self._load_tasks()
            skipped = len(steps) - created
            extra = f" ({skipped} already existed)" if skipped else ""
            self.status.showMessage(
                f"Created {entity}: {created} task(s) + folders{extra}.")

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg=f"Creating {entity}…")

    def _delete_task(self, task: dict):
        if not self.cfg:
            return
        if QMessageBox.question(
                self, "Delete task?",
                f"Delete task '{task.get('entity')} — {task.get('step')}'?\n"
                f"This removes it from the server.") != QMessageBox.Yes:
            return
        remote = self.cfg.remote_root
        tid = task["id"]

        def work():
            return self._conn_do(lambda c: tasksmod.delete_task(c, remote, tid))

        self._busy_buttons(True)
        self._spawn(work, lambda _r: (self._busy_buttons(False), self._load_tasks()),
                    busy_msg="Deleting task…")

    def _generate_tasks(self):
        if not self.cfg:
            return
        remote = self.cfg.remote_root
        local = self.ed_local.text().strip()

        def work():
            tree = self._conn_do(lambda c: core.build_merged_tree(c, remote, local))
            generated = tasksmod.generate_from_tree(tree)
            existing = {t["id"] for t in self._conn_do(
                lambda c: tasksmod.load_tasks(c, remote))}
            created = 0
            for t in generated:
                if t["id"] not in existing:
                    self._conn_do(lambda c, x=t: tasksmod.save_task(c, remote, x))
                    created += 1
            return created

        def done(created):
            self._busy_buttons(False)
            self.status.showMessage(f"Generated {created} new task(s).")
            self._load_tasks()

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Scanning structure for tasks…")


def _steps_group(steps: list[str]) -> tuple[QGroupBox, list]:
    """A 'Steps' group box with a checkbox per step (all checked by default).
    Returns (groupbox, [checkboxes])."""
    box = QGroupBox("Steps (a task is created for each)")
    grid = QGridLayout(box)
    boxes = []
    for i, s in enumerate(steps):
        cb = QCheckBox(s)
        cb.setChecked(True)
        cb._step = s
        grid.addWidget(cb, i // 3, i % 3)
        boxes.append(cb)
    return box, boxes


def _checked_steps(boxes: list) -> list[str]:
    return [cb._step for cb in boxes if cb.isChecked()]


class _NewEntityDialog(QDialog):
    """Shared scaffold: a roomy form + a Steps group + preview/error + buttons."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(500)
        self._root = QVBoxLayout(self)
        self._root.setSpacing(12)
        self.form = QFormLayout()
        self.form.setSpacing(10)
        self.form.setLabelAlignment(Qt.AlignRight)
        self._root.addLayout(self.form)

    def _finish(self, step_boxes):
        self._step_boxes = step_boxes
        self.lbl_preview = QLabel("")
        self.lbl_preview.setStyleSheet("color:#3aa;")
        self.lbl_preview.setWordWrap(True)
        self._root.addWidget(self.lbl_preview)
        self.lbl_err = QLabel("")
        self.lbl_err.setStyleSheet("color:#d46a6a;")
        self.lbl_err.setWordWrap(True)
        self._root.addWidget(self.lbl_err)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Create")
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        self._root.addWidget(bb)
        self._update_preview()

    def _err(self, msg):
        self.lbl_err.setText(msg)

    def _on_ok(self):
        raise NotImplementedError

    def _update_preview(self):
        raise NotImplementedError


class NewAssetDialog(_NewEntityDialog):
    """Create an asset: category + name + which steps to make tasks for."""

    def __init__(self, schema: dict, naming: dict, parent=None):
        super().__init__("New Asset", parent)
        self.naming = naming or {}
        self.cb_category = QComboBox()
        self.cb_category.addItems(tasksmod.asset_categories(schema))
        self.cb_category.currentTextChanged.connect(self._update_preview)
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText(tasksmod.NAMING_HINTS["asset_name"])
        self.ed_name.textChanged.connect(self._update_preview)
        self.form.addRow("Category", self.cb_category)
        self.form.addRow("Asset name", self.ed_name)
        box, boxes = _steps_group(tasksmod.steps_for(schema, "asset"))
        for cb in boxes:
            cb.toggled.connect(self._update_preview)
        self._root.addWidget(box)
        self._finish(boxes)

    def _entity(self) -> str:
        return f"{self.cb_category.currentText()}/{self.ed_name.text().strip()}"

    def _update_preview(self):
        n = len(_checked_steps(self._step_boxes))
        self.lbl_preview.setText(
            f"Will create:  03_assets/{self._entity()}  —  {n} task(s) + folders")

    def _on_ok(self):
        name = self.ed_name.text().strip()
        if not self.cb_category.currentText():
            return self._err("Pick an asset category.")
        if not tasksmod.validate_name(self.naming, "asset_name", name):
            return self._err("Asset name invalid — " + tasksmod.NAMING_HINTS["asset_name"])
        if not _checked_steps(self._step_boxes):
            return self._err("Select at least one step.")
        self.accept()

    def values(self) -> dict:
        return {"type": "asset", "category": self.cb_category.currentText(),
                "name": self.ed_name.text().strip(), "entity": self._entity(),
                "steps": _checked_steps(self._step_boxes)}


class NewShotDialog(_NewEntityDialog):
    """Create a shot: pick an existing sequence or type a new one, the shot code,
    and which steps to make tasks for."""

    def __init__(self, schema: dict, naming: dict, existing_seqs: list[str],
                 parent=None):
        super().__init__("New Shot", parent)
        self.naming = naming or {}
        self._existing = set(existing_seqs or [])
        self.cb_seq = QComboBox()
        self.cb_seq.setEditable(True)
        self.cb_seq.addItems(sorted(self._existing))
        self.cb_seq.setCurrentText("")
        self.cb_seq.lineEdit().setPlaceholderText(tasksmod.NAMING_HINTS["sequence"])
        self.cb_seq.currentTextChanged.connect(self._update_preview)
        self.lbl_seq_hint = QLabel("")
        self.lbl_seq_hint.setStyleSheet("color:#9aa;")
        self.ed_shot = QLineEdit()
        self.ed_shot.setPlaceholderText(tasksmod.NAMING_HINTS["shot"])
        self.ed_shot.textChanged.connect(self._update_preview)
        self.form.addRow("Sequence", self.cb_seq)
        self.form.addRow("", self.lbl_seq_hint)
        self.form.addRow("Shot", self.ed_shot)
        box, boxes = _steps_group(tasksmod.steps_for(schema, "shot"))
        for cb in boxes:
            cb.toggled.connect(self._update_preview)
        self._root.addWidget(box)
        self._finish(boxes)

    def _seq(self) -> str:
        return self.cb_seq.currentText().strip()

    def _entity(self) -> str:
        return f"{self._seq()}/{self.ed_shot.text().strip()}"

    def _update_preview(self):
        seq = self._seq()
        if not seq:
            self.lbl_seq_hint.setText("")
        elif seq in self._existing:
            self.lbl_seq_hint.setText("existing sequence")
        else:
            self.lbl_seq_hint.setText("new sequence — will be created")
        n = len(_checked_steps(self._step_boxes))
        self.lbl_preview.setText(
            f"Will create:  04_sequences/{self._entity()}  —  {n} task(s) + folders")

    def _on_ok(self):
        seq, shot = self._seq(), self.ed_shot.text().strip()
        if not tasksmod.validate_name(self.naming, "sequence", seq):
            return self._err("Sequence invalid — " + tasksmod.NAMING_HINTS["sequence"])
        if not tasksmod.validate_name(self.naming, "shot", shot):
            return self._err("Shot invalid — " + tasksmod.NAMING_HINTS["shot"])
        if not _checked_steps(self._step_boxes):
            return self._err("Select at least one step.")
        self.accept()

    def values(self) -> dict:
        return {"type": "shot", "seq": self._seq(),
                "shot": self.ed_shot.text().strip(), "entity": self._entity(),
                "steps": _checked_steps(self._step_boxes)}


class BugReportDialog(QDialog):
    """Collect a bug report (title + description) and choose what to attach.
    The screenshot was grabbed before this opened."""

    def __init__(self, screenshot, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Report a bug")
        self._has_shot = screenshot is not None
        self.title = ""
        self.description = ""
        self.include_log = True
        self.include_screenshot = self._has_shot

        root = QVBoxLayout(self)
        form = QFormLayout()
        self.ed_title = QLineEdit()
        self.ed_title.setPlaceholderText("Short summary of the problem")
        form.addRow("Title:", self.ed_title)
        self.ed_desc = QPlainTextEdit()
        self.ed_desc.setPlaceholderText(
            "What happened? What did you expect? Steps to reproduce?")
        self.ed_desc.setMinimumHeight(140)
        form.addRow("Description:", self.ed_desc)
        root.addLayout(form)

        self.cb_log = QCheckBox("Attach app log (workspace.log)")
        self.cb_log.setChecked(True)
        root.addWidget(self.cb_log)
        self.cb_shot = QCheckBox("Attach screenshot")
        self.cb_shot.setChecked(self._has_shot)
        self.cb_shot.setEnabled(self._has_shot)
        root.addWidget(self.cb_shot)
        if self._has_shot:
            thumb = QLabel()
            thumb.setPixmap(screenshot.scaledToWidth(360, Qt.SmoothTransformation))
            thumb.setStyleSheet("border:1px solid #ccc;")
            root.addWidget(thumb)

        self.lbl_err = QLabel("")
        self.lbl_err.setStyleSheet("color:#A32D2D;")
        root.addWidget(self.lbl_err)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Open GitHub issue")
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _on_ok(self):
        title = self.ed_title.text().strip()
        if not title:
            self.lbl_err.setText("Please enter a title.")
            return
        self.title = title
        self.description = self.ed_desc.toPlainText()
        self.include_log = self.cb_log.isChecked()
        self.include_screenshot = self.cb_shot.isChecked()
        self.accept()


def _app_icon_path() -> str:
    """The window icon, shipped as data (sys._MEIPASS root when frozen, else the
    packaging/ folder in a source checkout)."""
    name = "legami.png"
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, name)
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "packaging", name)


def main():
    applog.setup_logging()
    app = QApplication(sys.argv)
    icon = _app_icon_path()
    if os.path.isfile(icon):
        app.setWindowIcon(QIcon(icon))
    config = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    win = MainWindow(config)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
