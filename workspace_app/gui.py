"""Flumen Workspace — PySide6 desktop GUI.

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
    QDialogButtonBox, QInputDialog, QMenu, QPlainTextEdit, QCheckBox, QSpinBox,
    QListWidget, QListWidgetItem, QProgressBar,
)

from flumen.config import (ProjectConfig, SFTPCredentials, CACHED_CONFIG,
                             save_local_root)
from flumen.sftp import SFTPClient
from flumen import tasks as tasksmod
from flumen import schema as schema_mod
from flumen import review as reviewmod
from flumen import users as usersmod
from flumen import storage as storagemod
from flumen import plan as planmod
from flumen import clipboard as clipboardmod
from flumen import bugreport
from flumen.version import get_version
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
    "omitted":     (QColor(110, 110, 118), "⊘"),
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
        from flumen.version import get_version
        self.setWindowTitle(f"Flumen Workspace — {get_version()}")
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
        self._roster: list[dict] = []

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
        act_users = QAction("Manage users…", self)
        act_users.triggered.connect(self._manage_users)
        m.addAction(act_users)

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
        # Persist the folder the moment the artist changes it, so it survives a
        # restart without needing the "Configure Blender" button.
        self.ed_local.editingFinished.connect(self._persist_local_root)
        grid.addWidget(self.ed_local, 1, 1, 1, 2)
        b_local = QPushButton("Browse…")
        b_local.clicked.connect(self._pick_local)
        grid.addWidget(b_local, 1, 3)
        outer.addWidget(box)

        # Global activity indicator: a thin indeterminate bar above the tabs,
        # visible whenever ANY background job (FTP, scans) is running — the
        # status-bar text alone was too easy to miss.
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)          # indeterminate ("working…")
        self.busy_bar.setTextVisible(False)
        self.busy_bar.setFixedHeight(5)
        self.busy_bar.hide()
        outer.addWidget(self.busy_bar)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_tasks_page(), "Tasks")
        self.tabs.addTab(self._build_plan_page(), "Plan")
        self.tabs.addTab(self._build_dailies_page(), "Dailies")
        self.tabs.addTab(self._build_files_page(), "Files")
        self.tabs.addTab(self._build_storage_page(), "Storage")
        self.tabs.setCurrentIndex(0)  # Tasks is the default view
        # First visit to the Storage tab kicks off its scan automatically.
        self.tabs.currentChanged.connect(self._maybe_scan_storage)

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

    # ---- storage / cleanup ---------------------------------------------------
    _STORAGE_CARD_STYLE = {
        storagemod.MIRRORED: (SYNC_BG, SYNC_FG),
        storagemod.TEMP: (SYNC_BG, SYNC_FG),
        storagemod.OLD_WORK: (SERVER_BG, SERVER_FG),
        storagemod.ACTIVE_WORK: (LOCAL_BG, LOCAL_FG),
        storagemod.LOCAL_ONLY: (DIFF_BG, DIFF_FG),
    }

    def _build_storage_page(self) -> QWidget:
        page = QWidget()
        self._storage_page = page
        self._storage_records: list[dict] | None = None
        sl = QVBoxLayout(page)

        # The headline answers the actual question: how much can I free?
        self.lb_reclaim = QLabel("Scanning finds out how much you can free…")
        f = self.lb_reclaim.font()
        f.setPointSize(f.pointSize() + 4)
        f.setBold(True)
        self.lb_reclaim.setFont(f)
        sl.addWidget(self.lb_reclaim)

        cards = QHBoxLayout()
        self._storage_cards: dict[str, QLabel] = {}
        for cat in storagemod.CATEGORIES:
            bg, fg = self._STORAGE_CARD_STYLE[cat]
            lb = QLabel("—")
            lb.setStyleSheet(
                f"QLabel {{ background:{bg.name()}; color:{fg.name()}; "
                f"padding:6px 10px; border-radius:8px; }}")
            lb.setToolTip(storagemod.LABELS[cat])
            self._storage_cards[cat] = lb
            cards.addWidget(lb)
        cards.addStretch(1)
        sl.addLayout(cards)

        controls = QHBoxLayout()
        self.ed_storage_search = QLineEdit()
        self.ed_storage_search.setPlaceholderText("Search files…")
        self.ed_storage_search.setClearButtonEnabled(True)
        self.ed_storage_search.textChanged.connect(self._render_storage)
        controls.addWidget(self.ed_storage_search, 1)
        self.b_storage_scan = QPushButton("Rescan")
        self.b_storage_scan.clicked.connect(self._scan_storage)
        controls.addWidget(self.b_storage_scan)
        sl.addLayout(controls)

        self.storage_tree = QTreeWidget()
        self.storage_tree.setColumnCount(3)
        self.storage_tree.setHeaderLabels(["File", "Size", "Modified"])
        self.storage_tree.setAlternatingRowColors(True)
        self.storage_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.storage_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        sl.addWidget(self.storage_tree, 1)

        ops = QHBoxLayout()
        ops.addWidget(QLabel("Safe categories come pre-checked; superseded work "
                             "versions are opt-in. Deleted files go to the Trash."))
        ops.addStretch(1)
        self.b_free_space = QPushButton("Free up space…")
        self.b_free_space.clicked.connect(self._free_up_space)
        ops.addWidget(self.b_free_space)
        sl.addLayout(ops)
        return page

    def _maybe_scan_storage(self, index: int):
        if (self.tabs.widget(index) is self._storage_page
                and self._storage_records is None and self.cfg):
            self._scan_storage()

    def _scan_storage(self):
        if not self.cfg:
            self.status.showMessage("Sign in first — no project loaded.")
            return
        local_root = self.cfg.resolved_local_root()
        if not local_root or not os.path.isdir(local_root):
            self.status.showMessage("No local folder yet — nothing to scan.")
            return
        remote = self.cfg.remote_root.rstrip("/")
        self.b_storage_scan.setEnabled(False)

        def work():
            files = storagemod.scan_local(local_root)
            # One remote listing per local folder: enough to size-verify every
            # local file, without walking server folders we don't mirror.
            dirs = sorted({f["rel"].rsplit("/", 1)[0] if "/" in f["rel"] else ""
                           for f in files if not storagemod.is_temp(f["rel"])})
            sizes: dict[str, int] = {}

            def index(c):
                for d in dirs:
                    try:
                        entries = c.listdir(remote + ("/" + d if d else ""))
                    except Exception:  # noqa: BLE001 — folder only exists locally
                        continue
                    for e in entries:
                        if not e.get("is_dir"):
                            sizes[(d + "/" if d else "") + e["name"]] = e.get("size")

            self._conn_do(index)
            return storagemod.classify(files, sizes)

        def done(recs):
            self.b_storage_scan.setEnabled(True)
            self._storage_records = recs
            self._render_storage()

        self._spawn(work, done, busy_msg="Scanning local storage…")

    def _render_storage(self):
        recs = self._storage_records
        if recs is None:
            return
        summary = storagemod.summarize(recs)
        self.lb_reclaim.setText(
            f"You can safely free {storagemod.human_size(summary['reclaimable'])}")
        for cat in storagemod.CATEGORIES:
            s = summary[cat]
            self._storage_cards[cat].setText(
                f"{storagemod.LABELS[cat].split(' — ')[0]}: "
                f"{storagemod.human_size(s['size'])} ({s['count']})")

        query = (self.ed_storage_search.text() or "").strip().lower()
        tree = self.storage_tree
        tree.clear()
        for cat in storagemod.CATEGORIES:
            in_cat = [r for r in recs if r["category"] == cat
                      and (not query or query in r["rel"].lower())]
            if not in_cat:
                continue
            deletable = cat in storagemod.DELETABLE
            checked = cat in storagemod.SAFE
            cat_size = sum(r["size"] for r in in_cat)
            top = QTreeWidgetItem([f"{storagemod.LABELS[cat]}",
                                   storagemod.human_size(cat_size), ""])
            tf = top.font(0)
            tf.setBold(True)
            top.setFont(0, tf)
            if deletable:
                top.setFlags(top.flags() | Qt.ItemIsUserCheckable
                             | Qt.ItemIsAutoTristate)
                top.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
            tree.addTopLevelItem(top)

            groups: dict[str, list[dict]] = {}
            for r in in_cat:
                groups.setdefault(storagemod.group_key(r["rel"]), []).append(r)
            for gkey, grecs in sorted(groups.items(),
                                      key=lambda kv: -sum(r["size"] for r in kv[1])):
                gsize = sum(r["size"] for r in grecs)
                gnode = QTreeWidgetItem([f"{gkey}  ({len(grecs)})",
                                         storagemod.human_size(gsize), ""])
                if deletable:
                    gnode.setFlags(gnode.flags() | Qt.ItemIsUserCheckable
                                   | Qt.ItemIsAutoTristate)
                    gnode.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
                top.addChild(gnode)
                for r in sorted(grecs, key=lambda x: -x["size"]):
                    when = (_dt.datetime.fromtimestamp(r["mtime"])
                            .strftime("%Y-%m-%d %H:%M") if r["mtime"] else "")
                    node = QTreeWidgetItem([r["rel"],
                                            storagemod.human_size(r["size"]), when])
                    node.setData(0, Qt.UserRole, r)
                    if deletable:
                        node.setFlags(node.flags() | Qt.ItemIsUserCheckable)
                        node.setCheckState(0, Qt.Checked if checked
                                           else Qt.Unchecked)
                    gnode.addChild(node)
            top.setExpanded(bool(query) or (cat == storagemod.MIRRORED
                                            and len(in_cat) <= 200))
        self.status.showMessage(f"{len(recs)} local file(s) scanned.")

    def _storage_checked(self) -> list[dict]:
        out = []
        tree = self.storage_tree
        for i in range(tree.topLevelItemCount()):
            top = tree.topLevelItem(i)
            for j in range(top.childCount()):
                gnode = top.child(j)
                for k in range(gnode.childCount()):
                    node = gnode.child(k)
                    rec = node.data(0, Qt.UserRole)
                    if rec and node.checkState(0) == Qt.Checked:
                        out.append(rec)
        return out

    def _free_up_space(self):
        from PySide6.QtCore import QFile
        recs = self._storage_checked()
        if not recs:
            QMessageBox.information(self, "Nothing selected",
                                    "Check the files to delete first (safe "
                                    "categories come pre-checked after a scan).")
            return
        total = sum(r["size"] for r in recs)
        n_old = sum(1 for r in recs if r["category"] == storagemod.OLD_WORK)
        msg = (f"Move {len(recs)} file(s) — {storagemod.human_size(total)} — to "
               f"the Trash?\n\nFiles on the server re-download on demand.")
        if n_old:
            msg += (f"\n\n⚠ {n_old} superseded work version(s) included — those "
                    f"are NOT on the server; the Trash is their only copy.")
        if QMessageBox.question(self, "Free up space", msg) != QMessageBox.Yes:
            return
        local_root = self.cfg.resolved_local_root()
        sizes = {r["rel"]: r["size"] for r in recs}
        paths = [(r["rel"], os.path.join(local_root, *r["rel"].split("/")))
                 for r in recs]

        def work():
            gone, failed = [], 0
            for rel, path in paths:
                if not os.path.isfile(path) or QFile.moveToTrash(path):
                    gone.append(rel)
                else:
                    failed += 1
            return gone, failed

        def done(res):
            gone, failed = res
            gone_set = set(gone)
            freed = sum(sizes[rel] for rel in gone)
            self._storage_records = [r for r in (self._storage_records or [])
                                     if r["rel"] not in gone_set]
            self._render_storage()
            note = (f"Freed {storagemod.human_size(freed)} — {len(gone)} "
                    f"file(s) moved to the Trash.")
            if failed:
                note += f" ({failed} could not be moved.)"
            self.status.showMessage(note)

        self._spawn(work, done, busy_msg="Moving files to the Trash…")

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
        b_assign_to.clicked.connect(lambda: self._assign_to(add=True))
        b_unassign_from = QPushButton("Unassign…")
        b_unassign_from.setToolTip("Remove someone from the selected tasks "
                                   "(fix a wrong assignment)")
        b_unassign_from.clicked.connect(lambda: self._assign_to(add=False))
        rowops.addWidget(b_assign_me)
        rowops.addWidget(b_unassign_me)
        rowops.addWidget(b_assign_to)
        rowops.addWidget(b_unassign_from)
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

    # ---- production plan ------------------------------------------------------
    _HEALTH_COLORS = {
        planmod.HEALTH_LATE: QColor("#e05555"),
        planmod.HEALTH_DUE_SOON: QColor("#e0a030"),
        planmod.HEALTH_ON_TRACK: QColor("#5fae62"),
        planmod.HEALTH_DONE: QColor("#7a7a7a"),
        planmod.HEALTH_UNPLANNED: QColor("#9a86c8"),
    }

    def _build_plan_page(self) -> QWidget:
        page = QWidget()
        pl = QVBoxLayout(page)

        # Header: the plan-vs-capacity math, refreshed with the task list.
        self.lb_plan_header = QLabel("Sign in and load tasks to see the plan.")
        self.lb_plan_header.setWordWrap(True)
        f = self.lb_plan_header.font()
        f.setBold(True)
        self.lb_plan_header.setFont(f)
        pl.addWidget(self.lb_plan_header)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Artist:"))
        self.cb_plan_artist = QComboBox()
        self.cb_plan_artist.addItem("Everyone", None)
        self.cb_plan_artist.currentIndexChanged.connect(self._render_plan)
        controls.addWidget(self.cb_plan_artist)
        self.chk_plan_done = QCheckBox("Show done")
        self.chk_plan_done.toggled.connect(self._render_plan)
        controls.addWidget(self.chk_plan_done)
        controls.addSpacing(12)
        self.ed_plan_search = QLineEdit()
        self.ed_plan_search.setPlaceholderText("Search… (entity, step, assignee)")
        self.ed_plan_search.setClearButtonEnabled(True)
        self.ed_plan_search.textChanged.connect(self._render_plan)
        self.ed_plan_search.setMinimumWidth(220)
        controls.addWidget(self.ed_plan_search, 1)
        controls.addStretch(0)
        self.b_autoplan = QPushButton("Auto-plan…")
        self.b_autoplan.setToolTip(
            "Propose due dates: spreads each artist's remaining tasks over the "
            "workdays to the deadline, in pipeline order. You review before "
            "anything is written; dates you set by hand are overwritten only "
            "if you apply.")
        self.b_autoplan.clicked.connect(self._auto_plan)
        controls.addWidget(self.b_autoplan)
        b_reload = QPushButton("Refresh")
        b_reload.clicked.connect(self._load_tasks)
        controls.addWidget(b_reload)
        pl.addLayout(controls)

        self.plan_table = QTableWidget(0, 7)
        self.plan_table.setHorizontalHeaderLabels(
            ["Health", "Due", "Entity", "Step", "Assignees", "Est (d)", "Status"])
        self.plan_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.plan_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.plan_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.plan_table.setSortingEnabled(True)
        self.plan_table.setAlternatingRowColors(True)
        self.plan_table.setShowGrid(False)
        self.plan_table.verticalHeader().setDefaultSectionSize(26)
        self.plan_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch)
        pl.addWidget(self.plan_table, 1)

        rowops = QHBoxLayout()
        b_est = QPushButton("Set estimate…")
        b_est.clicked.connect(self._plan_set_estimate)
        b_due = QPushButton("Set due date…")
        b_due.clicked.connect(self._plan_set_due)
        b_clear = QPushButton("Clear dates")
        b_clear.clicked.connect(lambda: self._plan_write(due=""))
        rowops.addWidget(b_est)
        rowops.addWidget(b_due)
        rowops.addWidget(b_clear)
        rowops.addStretch(1)
        rowops.addWidget(QLabel("Assign + estimates live on the tasks — the "
                                "Tasks tab assigns, statuses track progress."))
        pl.addLayout(rowops)
        return page

    def _planning_cfg(self) -> dict:
        return planmod.planning_config(getattr(self, "_project_settings", {}))

    def _render_plan(self):
        tasks = getattr(self, "_tasks", None) or []
        cfg = self._planning_cfg()
        today = _dt.date.today()

        # header math
        s = planmod.plan_summary(tasks, getattr(self, "_roster", []), today, cfg)
        if s["deadline"]:
            fit = ("✓ fits" if s["fits"]
                   else f"▲ OVER by {s['remaining_days'] - s['capacity_days']:.1f}d")
            self.lb_plan_header.setText(
                f"Deadline {s['deadline']} — {s['workdays_left']} workdays left"
                f"   ·   remaining {s['remaining_days']}d vs capacity "
                f"{s['capacity_days']}d ({fit})"
                + (f"   ·   {s['unassigned_days']}d unassigned"
                   if s["unassigned_days"] else ""))
        else:
            self.lb_plan_header.setText(
                "No deadline set — add planning.deadline to project_settings.")

        # artist filter combo (rebuild, keep selection)
        current = self.cb_plan_artist.currentData()
        self.cb_plan_artist.blockSignals(True)
        self.cb_plan_artist.clear()
        self.cb_plan_artist.addItem("Everyone", None)
        for u in sorted(a.get("username", "") for a in
                        (getattr(self, "_roster", []) or []) if a.get("active")):
            self.cb_plan_artist.addItem(u, u)
            if u == current:
                self.cb_plan_artist.setCurrentIndex(
                    self.cb_plan_artist.count() - 1)
        self.cb_plan_artist.blockSignals(False)

        artist = self.cb_plan_artist.currentData()
        query = self.ed_plan_search.text()
        rows = []
        for t in tasks:
            if t.get("status") == "omitted":
                continue                     # cut from the show: never planned
            if not self.chk_plan_done.isChecked() and t.get("status") == "done":
                continue
            if artist and artist not in (t.get("assignees") or []):
                continue
            if not tasksmod.matches_query(t, query):
                continue
            rows.append(t)
        order = {planmod.HEALTH_LATE: 0, planmod.HEALTH_DUE_SOON: 1,
                 planmod.HEALTH_ON_TRACK: 2, planmod.HEALTH_UNPLANNED: 3,
                 planmod.HEALTH_DONE: 4}
        rows.sort(key=lambda t: (order.get(planmod.health(t, today, cfg), 9),
                                 t.get("due") or "9999",
                                 t.get("entity", ""), t.get("step", "")))

        self.plan_table.setSortingEnabled(False)
        self.plan_table.setRowCount(len(rows))
        for i, t in enumerate(rows):
            h = planmod.health(t, today, cfg)
            own = t.get("estimate_days")
            est = planmod.estimate_of(t, cfg)
            cells = [
                planmod.HEALTH_LABELS[h],
                t.get("due", ""),
                t.get("entity", ""),
                f"{step_icon(t.get('step', ''))}  {t.get('step', '')}",
                ", ".join(usersmod.display_name(self._roster, u)
                          for u in (t.get("assignees") or [])),
                f"{est:g}" + ("" if own else " (default)"),
                tasksmod.STATUS_LABELS.get(t.get("status", ""), ""),
            ]
            for j, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if j == 0:
                    item.setData(Qt.UserRole, t)
                    item.setForeground(QBrush(self._HEALTH_COLORS[h]))
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                self.plan_table.setItem(i, j, item)
        self.plan_table.setSortingEnabled(True)

    def _plan_selected(self) -> list[dict]:
        out = []
        for idx in self.plan_table.selectionModel().selectedRows():
            item = self.plan_table.item(idx.row(), 0)
            t = item.data(Qt.UserRole) if item else None
            if t:
                out.append(t)
        return out

    def _plan_set_estimate(self):
        chosen = self._plan_selected()
        if not chosen:
            QMessageBox.information(self, "No tasks selected",
                                    "Select tasks in the plan first.")
            return
        val, ok = QInputDialog.getDouble(
            self, "Set estimate", "Workdays per task (0 = back to the step "
            "default):", 2.0, 0.0, 60.0, 1)
        if ok:
            self._plan_write(estimate=val)

    def _plan_set_due(self):
        chosen = self._plan_selected()
        if not chosen:
            QMessageBox.information(self, "No tasks selected",
                                    "Select tasks in the plan first.")
            return
        text, ok = QInputDialog.getText(
            self, "Set due date", "Due date (YYYY-MM-DD, empty clears):",
            text=self._planning_cfg().get("deadline", ""))
        if not ok:
            return
        text = text.strip()
        if text and planmod.parse_date(text) is None:
            QMessageBox.warning(self, "Bad date",
                                f"'{text}' is not a YYYY-MM-DD date.")
            return
        self._plan_write(due=text)

    def _plan_write(self, estimate: float | None = None,
                    due: str | None = None):
        chosen = self._plan_selected()
        if not chosen or not self.cfg:
            return
        remote = self.cfg.remote_root
        actor = self._creds_user_safe()
        ids = [t["id"] for t in chosen]

        def work():
            return [self._conn_do(lambda c, x=tid: tasksmod.set_plan(
                c, remote, x, estimate_days=estimate, due=due, actor=actor))
                for tid in ids]

        def done(updated):
            self._busy_buttons(False)
            self._merge_tasks(updated)
            self.status.showMessage(f"{len(ids)} task(s) updated.")

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Updating plan…")

    def _auto_plan(self):
        if not self.cfg:
            return
        if not self._can_delete_remote():
            QMessageBox.information(self, "Supervisors only",
                                    "Auto-planning writes due dates on every "
                                    "task — ask your supervisor.")
            return
        cfg = self._planning_cfg()
        if not cfg.get("deadline"):
            QMessageBox.warning(self, "No deadline",
                                "Set planning.deadline in project_settings "
                                "first — the auto-plan spreads work over the "
                                "days remaining to it.")
            return
        tasks = getattr(self, "_tasks", None) or []
        proposal, warns = planmod.propose_schedule(
            tasks, _dt.date.today(), cfg)
        if not proposal:
            QMessageBox.information(self, "Nothing to plan",
                                    "No assigned, unfinished tasks to "
                                    "schedule. Assign tasks first (Tasks tab).")
            return
        by_id = {t["id"]: t for t in tasks}
        lines = [f"{due}   {by_id[tid].get('entity')} · {by_id[tid].get('step')}"
                 f"   ({', '.join(by_id[tid].get('assignees') or [])})"
                 for tid, due in sorted(proposal.items(), key=lambda kv: kv[1])]
        if warns:
            lines += [""] + [f"⚠ {w}" for w in warns]
        box = QMessageBox(self)
        box.setWindowTitle("Auto-plan proposal")
        box.setText(f"{len(proposal)} due date(s) proposed"
                    + (f", {len(warns)} warning(s)" if warns else "")
                    + ".\n\nApply writes them onto the tasks (overwriting "
                      "existing due dates of scheduled tasks).")
        box.setDetailedText("\n".join(lines))
        box.setStandardButtons(QMessageBox.Apply | QMessageBox.Cancel)
        if box.exec() != QMessageBox.Apply:
            return
        remote = self.cfg.remote_root
        actor = self._creds_user_safe()

        def work():
            return [self._conn_do(lambda c, x=tid, d=due: tasksmod.set_plan(
                c, remote, x, due=d, actor=actor))
                for tid, due in proposal.items()]

        def done(updated):
            self._busy_buttons(False)
            self._merge_tasks(updated)
            self.status.showMessage(
                f"Auto-plan applied to {len(proposal)} task(s).")

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Applying plan…")

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
        top.addSpacing(12)
        self.ed_review_search = QLineEdit()
        self.ed_review_search.setPlaceholderText("Search… (entity, step, version, artist)")
        self.ed_review_search.setClearButtonEnabled(True)
        self.ed_review_search.textChanged.connect(self._render_review_tree)
        self.ed_review_search.setMinimumWidth(220)
        top.addWidget(self.ed_review_search, 1)
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
        b_delete = self.b_delete_review = QPushButton("Delete review…")
        b_delete.setToolTip("Delete the selected review's turntable + texture sheet "
                            "from the server and locally (the published look is kept).")
        b_delete.clicked.connect(self._delete_review)
        ops.addWidget(b_delete)
        b_export = QPushButton("Export…")
        b_export.setToolTip("Gather the SELECTED items' clips + texture sheets into a "
                            "fresh dated folder (and index.html) and open it — drag "
                            "the clips into SyncSketch. With nothing selected, exports "
                            "all visible items.")
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
        query = self.ed_review_search.text()
        groups: "OrderedDict[str, list]" = OrderedDict()
        for it in self._review_items:
            if it["status"] not in active:
                continue
            if not reviewmod.matches_query(it, query):
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

    def _can_delete_remote(self) -> bool:
        """Server-side deletions are supervisor-only. An empty roster (project
        not yet initialized) allows — same bootstrap rule as roster editing."""
        if not self._roster:
            return True
        return usersmod.is_supervisor(self._roster, self._creds_user_safe())

    def _delete_review(self):
        if not self.cfg:
            return
        if not self._can_delete_remote():
            QMessageBox.warning(self, "Supervisors only",
                                "Deleting review media from the server is "
                                "restricted to supervisors.")
            return
        items = self._selected_review_items()
        if not items:
            QMessageBox.information(self, "No item selected",
                                    "Select the review(s) to delete.")
            return
        listing = "\n".join(f"  • {it['entity']} · {it['version']}" for it in items)
        resp = QMessageBox.question(
            self, "Delete review",
            f"Delete the review media for {len(items)} item(s)?\n\n{listing}\n\n"
            "This removes the turntable and texture sheet from the server and your "
            "local copy. The published look/model is kept. This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if resp != QMessageBox.Yes:
            return
        remote = self.cfg.remote_root
        local_root = self.cfg.resolved_local_root()

        def work():
            for it in items:
                self._conn_do(lambda c, x=it: reviewmod.delete_review(
                    c, remote, x, local_root))
            return len(items)

        def done(n):
            self._busy_buttons(False)
            self.status.showMessage(f"Deleted {n} review(s).")
            self._load_review_items()

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Deleting review…")

    def _export_review(self):
        if not self.cfg:
            return
        # Export the SELECTED items (so the folder holds only what you'll drag into
        # SyncSketch); fall back to all visible items if nothing is selected.
        items = self._selected_review_items()
        if not items:
            active = {st for st, b in self._review_filter_btns.items()
                      if b.isChecked()}
            query = self.ed_review_search.text()
            items = [it for it in self._review_items
                     if it["status"] in active and reviewmod.matches_query(it, query)]
        if not items:
            QMessageBox.information(self, "Nothing to export",
                                    "Select the review(s) to export, or show some "
                                    "with the status filters.")
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
            folder = res["folder_local"]
            self.status.showMessage(
                f"Exported {res['count']} clip(s) -> {folder} "
                f"(opening folder — drag the clips into SyncSketch)")
            # Open the folder in Finder/Explorer so the clips can be dragged straight
            # into SyncSketch; the index.html review sheet is in there too.
            if os.path.isdir(folder):
                clipboardmod.reveal(folder)

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
        folder = os.path.join(os.path.expanduser("~"), ".flumen", "bug_reports", ts)
        os.makedirs(folder, exist_ok=True)
        attached = []
        if dlg.include_screenshot and shot is not None:
            if shot.save(os.path.join(folder, "screenshot.png"), "PNG"):
                attached.append("screenshot.png")
        log_tail = ""
        if dlg.include_log:
            if applog.copy_full(applog.LOG_PATH, os.path.join(folder, "log.txt")):
                attached.append("log.txt")
            # Blender's console (turntable/publish/render output) is its own file.
            if applog.copy_full(applog.BLENDER_LOG_PATH,
                                os.path.join(folder, "blender.log")):
                attached.append("blender.log")
            log_tail = applog.read_tail(applog.LOG_PATH, 200)

        url, _ = bugreport.build_issue(
            dlg.title, dlg.description, self._env_text(), log_tail, attached)
        webbrowser.open(url)
        clipboardmod.reveal(folder)
        self.status.showMessage(
            "Opened a pre-filled GitHub issue — drag the files (log.txt, "
            "blender.log, screenshot.png) from the folder into it, then Submit.")

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
        # Restore the saved folder (resolved_local_root reads the per-user file) and
        # use it for the session, so it's filled in and sticky on every restart.
        resolved = self.cfg.resolved_local_root()
        if not self.ed_local.text().strip():
            self.ed_local.setText(resolved)
        self.cfg.local_root = self.ed_local.text().strip() or resolved
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
        # Prefill the server convention (/shared/<ProjectName>) so the artist only
        # types the project name; a saved sign-in keeps its full root.
        ed_root = QLineEdit(saved.remote_root if saved and saved.remote_root
                            else "/shared/")
        ed_root.setPlaceholderText("/shared/<project name>")
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
            from flumen.project_sync import fetch_project_config
            fetch_project_config(creds, root)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Sign in failed",
                                 f"Could not connect or load the project from "
                                 f"{host}:{root}\n\n{exc}")
            return
        creds.save_user()
        # Record presence so the project roster tracks everyone who can sign in
        # (the user IS allowed on the project — they just authenticated to it).
        try:
            with SFTPClient(creds) as c:
                usersmod.register_self(c, root, user)
        except Exception:  # noqa: BLE001 — never block sign-in on roster bookkeeping
            pass
        self._load_config()   # now loads the downloaded config from the cache
        self.status.showMessage(f"Signed in as {user}.")

    def _creds(self) -> SFTPCredentials:
        return SFTPCredentials.from_env(".env")

    # ---- job runners --------------------------------------------------------
    def _update_busy(self):
        """Show the top activity bar while any background job is in flight."""
        self.busy_bar.setVisible(bool(self._jobs))

    def _spawn(self, fn, on_done, busy_msg="Working…"):
        self.status.showMessage(busy_msg)
        job = Job(fn)
        self._jobs.append(job)
        self._update_busy()
        job.done.connect(lambda r, j=job: (
            self._jobs.remove(j) if j in self._jobs else None,
            self._update_busy(), on_done(r)))
        job.failed.connect(lambda m, j=job: (
            self._jobs.remove(j) if j in self._jobs else None,
            self._update_busy(), self._on_error(m)))
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
            self._persist_local_root()
            self._load_root()

    def _persist_local_root(self):
        """Save the local folder per-user (survives restarts) and use it for the
        rest of this session. Called whenever the field changes — browse, manual
        edit, or the Configure button — so the artist never has to re-set it."""
        local = self.ed_local.text().strip()
        if not local:
            return
        try:
            save_local_root(local)
        except Exception:  # noqa: BLE001 — never let persistence crash the UI
            pass
        if self.cfg:
            self.cfg.local_root = local

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
        if not local:
            QMessageBox.warning(self, "Local folder", "Enter a local folder first.")
            return
        # Authoritative: persist per-user (survives the cached config.yaml being
        # re-downloaded from the server on each sign-in). The field also persists on
        # browse/edit, so this button is just the explicit confirmation path.
        self._persist_local_root()
        # Best-effort: also write it into a real (non-cached) config.yaml for devs.
        try:
            if os.path.abspath(self.config_path) != os.path.abspath(CACHED_CONFIG):
                core.set_local_root_in_config(self.config_path, local)
        except Exception:  # noqa: BLE001 — the per-user file already persisted it
            pass
        QMessageBox.information(
            self, "Blender configured",
            f"Saved local folder.\nThe launcher and Blender addon will now save "
            f"into:\n{local}")

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
            # The tree may have been cleared/refreshed while this scan ran, deleting
            # the row's C++ object — touching it then raises 'already deleted'.
            from shiboken6 import isValid
            if not isValid(item):
                return
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
            tasks = self._conn_do(lambda c: tasksmod.load_tasks(c, remote))
            roster = self._conn_do(lambda c: usersmod.load_roster(c, remote))
            # Fresh project settings ride along (deadline + planning defaults
            # for the Plan tab) — served-side truth, not the local mirror.
            import json as _json
            try:
                settings = _json.loads(self._conn_do(lambda c: c.read_text(
                    remote.rstrip("/") + "/02_pipeline/project_settings.json"))
                    or "{}")
            except Exception:  # noqa: BLE001
                settings = {}
            return tasks, roster, settings

        def done(result):
            self._busy_buttons(False)
            self._tasks, self._roster, self._project_settings = result
            self._render_tasks()
            self._render_plan()
            # Server-side deletion is supervisor-only — reflect it in the UI.
            can = self._can_delete_remote()
            self.b_delete_review.setEnabled(can)
            if not can:
                self.b_delete_review.setToolTip(
                    "Deleting from the server is restricted to supervisors.")
            self.status.showMessage(f"{len(self._tasks)} task(s) loaded.")

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
                ", ".join(usersmod.display_name(self._roster, u)
                          for u in (t.get("assignees") or [])),
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

    def _merge_tasks(self, updated: list[dict | None]):
        """Fold edited task records back into the loaded list and re-render —
        no full server reload per edit (that's what made every assign/status
        change crawl: it re-downloaded EVERY task file)."""
        tasks = getattr(self, "_tasks", None) or []
        by_id = {t.get("id"): i for i, t in enumerate(tasks)}
        for t in updated or []:
            if not t:
                continue
            i = by_id.get(t.get("id"))
            if i is None:
                tasks.append(t)
            else:
                tasks[i] = t
        self._tasks = tasks
        self._render_tasks()
        self._render_plan()

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
            return [self._conn_do(lambda c, x=tid: tasksmod.assign(
                c, remote, x, username, add=add, actor=actor)) for tid in ids]

        def done(updated):
            self._busy_buttons(False)
            self._merge_tasks(updated)
            self.status.showMessage(f"{len(ids)} task(s) updated.")

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Updating assignees…")

    def _manage_users(self):
        if not self.cfg:
            QMessageBox.information(self, "Not signed in",
                                    "Sign in to a project first.")
            return
        remote = self.cfg.remote_root
        me = self._creds_user_safe()

        def load():
            return self._conn_do(lambda c: usersmod.load_roster(c, remote))

        def opened(roster):
            self._busy_buttons(False)
            self._roster = roster
            # Supervisors edit; before any supervisor exists the roster is
            # un-bootstrapped, so anyone may open it and run Discover.
            can_edit = (not usersmod.has_supervisor(roster)
                        or usersmod.is_supervisor(roster, me))
            dlg = ManageUsersDialog(roster, me, can_edit, self)
            if dlg.exec() != QDialog.Accepted:
                return
            if dlg.do_discover:
                self._discover_users()
            elif dlg.result_users is not None:
                self._save_roster(dlg.result_users)

        self._busy_buttons(True)
        self._spawn(load, opened, busy_msg="Loading users…")

    def _save_roster(self, users: list[dict]):
        remote = self.cfg.remote_root

        def work():
            self._conn_do(lambda c: usersmod.save_roster(c, remote, users))
            return self._conn_do(lambda c: usersmod.load_roster(c, remote))

        def done(roster):
            self._busy_buttons(False)
            self._roster = roster
            self._render_tasks()
            self.status.showMessage(f"Saved {len(users)} project user(s).")

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Saving users…")

    def _discover_users(self):
        remote = self.cfg.remote_root
        me = self._creds_user_safe()
        # Seed from existing task assignees too, not just sign-ins/uploads.
        extra = sorted({a for t in self._tasks for a in (t.get("assignees") or [])})

        def work():
            return self._conn_do(lambda c: usersmod.discover(
                c, remote, extra_usernames=extra, promote_supervisor=me))

        def done(roster):
            self._busy_buttons(False)
            self._roster = roster
            self._render_tasks()
            self.status.showMessage(f"Discovered {len(roster)} project user(s).")

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Discovering users…")

    def _assign_to(self, add: bool = True):
        if not self._selected_tasks():
            QMessageBox.information(self, "No tasks selected", "Select tasks first.")
            return
        roster = usersmod.active_users(self._roster)
        if not roster:
            QMessageBox.information(
                self, "No users yet",
                "No project users to assign. Open File → Manage users… and run "
                "Discover to build the roster.")
            return
        dlg = UserPickerDialog(roster, self)
        dlg.setWindowTitle("Assign to" if add else "Unassign")
        if dlg.exec() != QDialog.Accepted:
            return
        chosen = dlg.selected_usernames()
        if not chosen:
            return
        chosen_tasks = self._selected_tasks()
        remote = self.cfg.remote_root
        actor = self._creds_user_safe()
        ids = [t["id"] for t in chosen_tasks]

        def work():
            out = []
            for tid in ids:
                for name in chosen:
                    out.append(self._conn_do(
                        lambda c, x=tid, n=name: tasksmod.assign(
                            c, remote, x, n, add=add, actor=actor)))
            return out

        def done(updated):
            self._busy_buttons(False)
            self._merge_tasks(updated)
            self.status.showMessage(f"{len(ids)} task(s) updated.")

        self._busy_buttons(True)
        self._spawn(work, done, busy_msg="Updating assignees…")

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
            return [self._conn_do(lambda c, x=tid: tasksmod.set_status(
                c, remote, x, status, actor=actor)) for tid in ids]

        def done(updated):
            self._busy_buttons(False)
            self._merge_tasks(updated)
            self.status.showMessage(f"{len(ids)} task(s) updated.")

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
        # Surface publishes are look libraries (no scene) — not openable as workfiles.
        # Anim artifacts (…_anim.blend) are actions-only and also not openable.
        pubs = ([] if t.get("step") == "surface"
                else [p for p in tasksmod.published_files(t)
                      if not p["name"].endswith("_anim.blend")])
        act_open = menu.addAction("Open latest publish")
        act_open.setEnabled(bool(pubs))
        # A task with no local work file yet still needs an entry point: offer a
        # fresh scene in the task's context instead of a dead 'Open latest work'.
        import glob as _glob
        local_root = (self.ed_local.text().strip()
                      or (self.cfg.resolved_local_root() if self.cfg else ""))
        work_abs = os.path.join(local_root, *tasksmod.task_work_rel(t).split("/"))
        has_work = bool(local_root and os.path.isdir(work_abs)
                        and _glob.glob(os.path.join(work_abs, "*.blend")))
        act_work = menu.addAction("Open latest work" if has_work
                                  else "Start working (new scene)")
        ver_menu = menu.addMenu("Open version")
        ver_actions = {}
        if pubs:
            for p in pubs:
                # splitlines() on "" is [] — an empty description must not crash.
                lines = (p.get("description") or "").splitlines()
                desc = lines[0][:40] if lines else ""
                a = ver_menu.addAction(f"{p['name']}   ·   {p.get('by','?')}"
                                       + (f"   ·   {desc}" if desc else ""))
                ver_actions[a] = p["rel"]
        else:
            ver_menu.setEnabled(False)
        act_hist = menu.addAction("Publish history…")
        # A shot's element breakdown is shared across its steps — edit it here.
        act_elements = menu.addAction("Elements…") if t.get("type") == "shot" else None
        menu.addSeparator()
        act_delete = menu.addAction("Delete task")
        act_delete.setEnabled(self._can_delete_remote())
        if not act_delete.isEnabled():
            act_delete.setToolTip("Supervisors only.")
        chosen = menu.exec(self.tasks_table.viewport().mapToGlobal(pos))
        if chosen == act_open:
            self._open_task_in_blender(t)
        elif chosen == act_work:
            self._open_task_in_blender(t, work_latest=has_work,
                                       start_new=not has_work)
        elif chosen in ver_actions:
            self._open_task_in_blender(t, ver_actions[chosen])
        elif chosen == act_hist:
            self._show_history(t)
        elif act_elements is not None and chosen == act_elements:
            self._open_elements_editor(t)
        elif chosen == act_delete:
            self._delete_task(t)

    def _open_elements_editor(self, task: dict):
        """Edit a shot's element breakdown (the assets + camera it contains).
        Loads/saves assembly.json on the Job thread so the UI never blocks."""
        if not self.cfg:
            return
        from flumen import elements as elementsmod
        shot_entity = task["entity"]
        remote = self.cfg.remote_root
        asset_entities = sorted({t["entity"] for t in self._tasks
                                 if t.get("type") == "asset"})

        def done(result):
            assembly, dressings_by_asset = result
            self._busy_buttons(False)
            dlg = ElementsDialog(shot_entity, assembly, asset_entities, self,
                                 dressings_by_asset=dressings_by_asset)
            if dlg.exec() != QDialog.Accepted:
                return
            new_assembly = dlg.assembly()
            actor = self._creds_user_safe()

            def save_work():
                return self._conn_do(lambda c: elementsmod.save_assembly(
                    c, remote, shot_entity, new_assembly, actor))

            self._busy_buttons(True)
            self._spawn(
                save_work,
                lambda _r: (self._busy_buttons(False),
                            self.status.showMessage(
                                f"Saved {len(new_assembly['elements'])} element(s) "
                                f"for {shot_entity}.")),
                busy_msg="Saving elements…")

        def load_work():
            assembly = self._conn_do(
                lambda c: elementsmod.load_assembly(c, remote, shot_entity))
            # Published dressing names per environment asset (for the dropdown).
            dressings = {}
            for ent in asset_entities:
                if not ent.startswith("environments/"):
                    continue
                tid = tasksmod.make_id("asset", ent, "dressing")
                t = self._conn_do(lambda c, x=tid: tasksmod.get_task(c, remote, x))
                if t:
                    names = [d["dressing"] for d in tasksmod.published_dressings(t)]
                    if names:
                        dressings[ent] = names
            return assembly, dressings

        self._busy_buttons(True)
        self._spawn(load_work, done, busy_msg="Loading elements…")

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

    def _open_task_in_blender(self, task: dict, blend_rel: str | None = None,
                              work_latest: bool = False,
                              start_new: bool = False):
        if not self.cfg:
            return
        from flumen.launcher import launch
        local_root = self.ed_local.text().strip() or self.cfg.resolved_local_root()
        self.cfg.local_root = local_root  # launcher syncs into the GUI's folder
        remote_root = self.cfg.remote_root
        work_abs = os.path.join(local_root, *tasksmod.task_work_rel(task).split("/"))

        # 'Open latest work': the newest .blend in the task's LOCAL work folder —
        # never a publish. Missing (and not a fresh surface scene) -> warn, no launch.
        if work_latest:
            import glob as _glob
            cands = (sorted(_glob.glob(os.path.join(work_abs, "*.blend")),
                            reverse=True) if os.path.isdir(work_abs) else [])
            if not cands and task.get("step") != "surface":
                QMessageBox.warning(
                    self, "No work file",
                    f"No local work file for this task.\n\n{work_abs}\n\n"
                    f"Open the latest publish (or start from scratch) and use "
                    f"'Save to task' in Blender to create one.")
                return
            blend_rel = None                 # force the work-file path below

        # A surface publish is a materials-only LOOK library (no scene) — opening it
        # gives Blender's "Library file, loading empty scene", and worse, applying a
        # look from within it fails ("Cannot load from the current blend file"). So a
        # surface task NEVER opens a publish (even a chosen version): fall through to
        # the work file or a fresh shading scene. Looks are consumed via 'Apply look…'.
        if task.get("step") == "surface":
            blend_rel = None
        # 'Start working': a fresh scene in the task's context — no publish, no
        # work file. Blender opens empty; 'Save to task' creates the work file.
        if start_new:
            blend_rel = None
        # Open priority: chosen version > latest published > latest local work file.
        # Skip …_anim.blend (actions-only artifacts), never an openable workfile.
        if (blend_rel is None and not work_latest and not start_new
                and task.get("step") != "surface"):
            pubs = [p for p in tasksmod.published_files(task)
                    if not p["name"].endswith("_anim.blend")]
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
            "FLUMEN_TASK_ID": task.get("id", ""),
            "FLUMEN_TASK_TYPE": task.get("type", ""),
            "FLUMEN_TASK_ENTITY": task.get("entity", ""),
            "FLUMEN_TASK_STEP": task.get("step", ""),
            "FLUMEN_TASK_TITLE": task.get("title", ""),
            "FLUMEN_TASK_WORK_DIR": work_abs,
        }
        # Opening a surface task with nothing saved yet: start from a clean,
        # shading-ready scene instead of Blender's default cube/camera/light.
        if task.get("step") == "surface" and not open_file:
            extra_env["FLUMEN_NEW_SURFACE"] = "1"
        # 'Start working (new scene)' on any other step: truly empty scene —
        # no default Cube/Camera/Light leaking into shot builds or dressings.
        elif start_new and not open_file:
            extra_env["FLUMEN_NEW_SCENE"] = "1"
        cfg = self.cfg
        creds = self._creds()

        def work():
            # Pull the chosen published file down so Blender opens the real version.
            if blend_rel:
                self._conn_do(lambda c: c.download(
                    core.remote_path_for(remote_root, blend_rel), open_file))
            # Surface/rig shade or rig the published model — pre-fetch it so the
            # "Load published model" button (and auto-load) work offline-fast.
            if (task.get("step") in ("surface", "rig", "dressing")
                    and task.get("type") == "asset"):
                mp = self._fetch_model_publish(task, local_root, remote_root)
                if mp:
                    extra_env["FLUMEN_MODEL_PUBLISH"] = mp
            return launch(cfg, creds, extra_env=extra_env, open_file=open_file,
                          log_path=applog.prepare_blender_log())

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
                    "or the FLUMEN_BLENDER environment variable.")

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
        # Sidecar textures (model/env publishes reference //textures/<name>) —
        # silent when the publish carries none.
        pub_dir = rel.rsplit("/", 1)[0]
        try:
            self._conn_do(lambda c: c.download_dir(
                core.remote_path_for(remote_root, pub_dir + "/textures"),
                os.path.join(os.path.dirname(local), "textures")))
        except Exception:  # noqa: BLE001
            pass
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
        if not self._can_delete_remote():
            QMessageBox.warning(self, "Supervisors only",
                                "Deleting tasks from the server is restricted "
                                "to supervisors.")
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
        self._schema = schema
        self.cb_category = QComboBox()
        self.cb_category.addItems(tasksmod.asset_categories(schema))
        self.cb_category.currentTextChanged.connect(self._on_category_changed)
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText(tasksmod.NAMING_HINTS["asset_name"])
        self.ed_name.textChanged.connect(self._update_preview)
        self.form.addRow("Category", self.cb_category)
        self.form.addRow("Asset name", self.ed_name)
        # Steps depend on the category (per-type template overlay: e.g. only
        # environments have a dressing step) — rebuilt on category change.
        box, boxes = _steps_group(self._category_steps())
        for cb in boxes:
            cb.toggled.connect(self._update_preview)
        self._steps_box = box
        self._root.addWidget(box)
        self._finish(boxes)

    def _category_steps(self) -> list[str]:
        return tasksmod.steps_for(self._schema, "asset",
                                  self.cb_category.currentText() or None)

    def _on_category_changed(self):
        # Rebuild the step checkboxes for the new category, keeping the checked
        # state of steps that exist in both.
        prior = {cb._step: cb.isChecked() for cb in self._step_boxes}
        new_box, new_boxes = _steps_group(self._category_steps())
        for cb in new_boxes:
            if cb._step in prior:
                cb.setChecked(prior[cb._step])
            cb.toggled.connect(self._update_preview)
        self._root.replaceWidget(self._steps_box, new_box)
        self._steps_box.deleteLater()
        self._steps_box, self._step_boxes = new_box, new_boxes
        self._update_preview()

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


class ElementsDialog(QDialog):
    """Edit a shot's element breakdown. Pure UI over an in-memory assembly dict;
    the caller saves it via elements.save_assembly. No network here."""

    def __init__(self, shot_entity, assembly, asset_entities, parent=None,
                 dressings_by_asset=None):
        super().__init__(parent)
        from flumen import elements as E
        self._E = E
        self._asset_entities = list(asset_entities or [])
        self._dressings = dict(dressings_by_asset or {})
        self._assembly = E.normalize(dict(assembly), shot_entity)
        self.setWindowTitle(f"Elements — {shot_entity}")
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            "Assets this shot contains. Layout links each one's rig; lighting will "
            "load its cache (later). The camera is the shot's own published camera."))

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["On", "Kind", "Asset / Camera",
                                              "Look", "Dressing", "Id"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table, 1)

        add_row = QHBoxLayout()
        self.cb_asset = QComboBox()
        self.cb_asset.addItems(self._asset_entities)
        self.cb_asset.currentTextChanged.connect(self._refresh_dressing_choices)
        self.ed_look = QLineEdit()
        self.ed_look.setPlaceholderText("look (optional)")
        # Dressing choice — only meaningful for environments with published
        # dressings; disabled otherwise.
        self.cb_dressing = QComboBox()
        self.cb_dressing.setToolTip("Published set-dressing to load with this "
                                    "environment (optional)")
        b_add = QPushButton("Add asset")
        b_add.clicked.connect(self._add_asset)
        b_cam = QPushButton("Add camera")
        b_cam.clicked.connect(self._add_camera)
        b_rm = QPushButton("Remove selected")
        b_rm.clicked.connect(self._remove_selected)
        for w in (self.cb_asset, self.ed_look, self.cb_dressing, b_add, b_cam):
            add_row.addWidget(w)
        self._refresh_dressing_choices()
        add_row.addStretch(1)
        add_row.addWidget(b_rm)
        root.addLayout(add_row)

        # Shot frame range: start is fixed at 1001, the duration is set here.
        dur_row = QHBoxLayout()
        dur_row.addWidget(QLabel("Shot duration (frames):"))
        self.spin_duration = QSpinBox()
        self.spin_duration.setRange(1, 100000)
        self.spin_duration.setValue(
            int(self._assembly.get("duration") or self._E.DEFAULT_DURATION))
        self.spin_duration.valueChanged.connect(self._update_range_label)
        dur_row.addWidget(self.spin_duration)
        self.lbl_range = QLabel("")
        self.lbl_range.setStyleSheet("color:#9aa;")
        dur_row.addWidget(self.lbl_range)
        dur_row.addStretch(1)
        root.addLayout(dur_row)
        self._update_range_label()

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._commit_enabled_then_accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)
        self._reload()

    def _reload(self):
        els = self._assembly["elements"]
        self.table.setRowCount(len(els))
        for i, e in enumerate(els):
            cb = QCheckBox()
            cb.setChecked(e.get("enabled", True))
            self.table.setCellWidget(i, 0, cb)
            cells = (e["kind"], e["asset"] or "(shot camera)",
                     e.get("look", ""), e.get("dressing", ""), e["id"])
            for j, val in enumerate(cells, start=1):
                self.table.setItem(i, j, QTableWidgetItem(val))

    def _update_range_label(self):
        start = int(self._assembly.get("frame_start") or self._E.DEFAULT_FRAME_START)
        end = start + max(1, self.spin_duration.value()) - 1
        self.lbl_range.setText(f"→ frames {start}–{end}")

    def _refresh_dressing_choices(self):
        names = self._dressings.get(self.cb_asset.currentText().strip()) or []
        self.cb_dressing.clear()
        self.cb_dressing.addItem("")                 # no dressing
        for n in names:
            self.cb_dressing.addItem(n)
        self.cb_dressing.setEnabled(bool(names))

    def _add_asset(self):
        ent = self.cb_asset.currentText().strip()
        if not ent:
            return
        self._E.add_element(self._assembly,
                            self._E.new_element(ent, "asset",
                                                look=self.ed_look.text().strip(),
                                                dressing=self.cb_dressing
                                                .currentText().strip()))
        self.ed_look.clear()
        self._reload()

    def _add_camera(self):
        self._E.add_element(self._assembly, self._E.new_element("", "camera"))
        self._reload()

    def _remove_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return
        self._E.remove_element(self._assembly,
                               self._assembly["elements"][row]["id"])
        self._reload()

    def _commit_enabled_then_accept(self):
        for i, e in enumerate(self._assembly["elements"]):
            w = self.table.cellWidget(i, 0)
            if w is not None:
                e["enabled"] = w.isChecked()
        self._assembly["duration"] = self.spin_duration.value()
        self._assembly.setdefault("frame_start", self._E.DEFAULT_FRAME_START)
        self.accept()

    def assembly(self) -> dict:
        return self._assembly


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


class UserPickerDialog(QDialog):
    """Pick one or more project users (by display name) to assign. Roster-backed,
    so there's no free-text typing of usernames."""

    def __init__(self, roster: list[dict], parent=None, preselected=None):
        super().__init__(parent)
        self.setWindowTitle("Assign to")
        self.resize(320, 380)
        pre = set(preselected or [])
        root = QVBoxLayout(self)
        root.addWidget(QLabel("Assign the selected task(s) to:"))
        self.listw = QListWidget()
        self.listw.setSelectionMode(QAbstractItemView.NoSelection)
        for u in sorted(roster, key=lambda x: (x.get("display") or "").lower()):
            label = u.get("display") or u["username"]
            if u.get("role") == usersmod.SUPERVISOR:
                label += "  (supervisor)"
            it = QListWidgetItem(label)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if u["username"] in pre else Qt.Unchecked)
            it.setData(Qt.UserRole, u["username"])
            self.listw.addItem(it)
        root.addWidget(self.listw, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def selected_usernames(self) -> list[str]:
        out = []
        for i in range(self.listw.count()):
            it = self.listw.item(i)
            if it.checkState() == Qt.Checked:
                out.append(it.data(Qt.UserRole))
        return out


class ManageUsersDialog(QDialog):
    """Edit the curated project roster: display name, role, active. Editing is
    enabled only for supervisors (read-only otherwise). 'Discover' seeds the
    roster from everyone who has signed in / uploaded; the first time it runs it
    makes the running user the first supervisor."""

    ROLE_LABELS = {usersmod.ARTIST: "Artist", usersmod.SUPERVISOR: "Supervisor"}

    def __init__(self, roster: list[dict], me: str, can_edit: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage users")
        self.resize(560, 420)
        self._me = me
        self._can_edit = can_edit
        self.result_users: list[dict] | None = None   # set on save
        self.do_discover = False

        root = QVBoxLayout(self)
        note = ("Project users — assignable in Tasks. Usernames match the SFTP login."
                if can_edit else
                "Read-only — only a supervisor can edit the roster.")
        root.addWidget(QLabel(note))

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Username", "Display name", "Role", "Active"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(
            QAbstractItemView.AllEditTriggers if can_edit else QAbstractItemView.NoEditTriggers)
        root.addWidget(self.table, 1)
        for u in sorted(roster, key=lambda x: (x.get("display") or "").lower()):
            self._add_row(u)

        btns = QHBoxLayout()
        if can_edit:
            b_add = QPushButton("Add user…")
            b_add.clicked.connect(self._add_blank)
            btns.addWidget(b_add)
            b_remove = QPushButton("Remove")
            b_remove.clicked.connect(self._remove_selected)
            btns.addWidget(b_remove)
            b_discover = QPushButton("Discover from server")
            b_discover.setToolTip("Add everyone who has signed in or uploaded to "
                                  "this project.")
            b_discover.clicked.connect(self._on_discover)
            btns.addWidget(b_discover)
        btns.addStretch(1)
        bb = QDialogButtonBox(
            (QDialogButtonBox.Save | QDialogButtonBox.Cancel) if can_edit
            else QDialogButtonBox.Close)
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        if not can_edit:
            bb.button(QDialogButtonBox.Close).clicked.connect(self.reject)
        btns.addWidget(bb)
        root.addLayout(btns)

    def _add_row(self, u: dict):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(u.get("username", "")))
        self.table.setItem(r, 1, QTableWidgetItem(u.get("display") or u.get("username", "")))
        role_cell = QComboBox()
        for key, lbl in self.ROLE_LABELS.items():
            role_cell.addItem(lbl, key)
        role_cell.setCurrentIndex(max(0, list(self.ROLE_LABELS).index(
            u.get("role") if u.get("role") in self.ROLE_LABELS else usersmod.ARTIST)))
        role_cell.setEnabled(self._can_edit)
        self.table.setCellWidget(r, 2, role_cell)
        active = QCheckBox()
        active.setChecked(bool(u.get("active", True)))
        active.setEnabled(self._can_edit)
        self.table.setCellWidget(r, 3, active)
        # username is the identity key — never editable after creation
        self.table.item(r, 0).setFlags(self.table.item(r, 0).flags() & ~Qt.ItemIsEditable)

    def _add_blank(self):
        name, ok = QInputDialog.getText(self, "Add user",
                                        "SFTP username (must match their login):")
        if ok and name.strip():
            self._add_row(usersmod.new_user(name.strip()))

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _on_discover(self):
        # Defer the actual server scan to the caller (it owns the connection);
        # just signal intent and close with save semantics.
        self.do_discover = True
        self.accept()

    def _collect(self) -> list[dict]:
        out = []
        for r in range(self.table.rowCount()):
            name = (self.table.item(r, 0).text() or "").strip()
            if not name:
                continue
            disp = (self.table.item(r, 1).text() or "").strip()
            role = self.table.cellWidget(r, 2).currentData()
            active = self.table.cellWidget(r, 3).isChecked()
            out.append(usersmod.new_user(name, disp, role, active))
        return out

    def _on_save(self):
        if self._can_edit:
            self.result_users = self._collect()
        self.accept()


def _app_icon_path() -> str:
    """The window icon, shipped as data (sys._MEIPASS root when frozen, else the
    packaging/ folder in a source checkout)."""
    name = "flumen.png"
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
