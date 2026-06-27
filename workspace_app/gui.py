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
from PySide6.QtGui import QBrush, QColor, QAction
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
        QTimer.singleShot(250, self._refresh_review)

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
        b_new = QPushButton("New task…")
        b_new.clicked.connect(self._new_task)
        b_gen = QPushButton("Generate from structure")
        b_gen.clicked.connect(self._generate_tasks)
        b_reload = QPushButton("Refresh")
        b_reload.clicked.connect(self._load_tasks)
        controls.addWidget(b_new)
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

        top = QHBoxLayout()
        top.addWidget(QLabel("Review date:"))
        self.ed_review_date = QLineEdit(reviewmod.today_str())
        self.ed_review_date.setMaximumWidth(120)
        self.ed_review_date.editingFinished.connect(self._refresh_review)
        top.addWidget(self.ed_review_date)
        self.b_review_build = QPushButton("Build / Update Review")
        self.b_review_build.setToolTip(
            "Collect the turntables of tasks in 'review' status into this day's "
            "folder, and flag them as collected.")
        self.b_review_build.clicked.connect(self._build_review)
        top.addWidget(self.b_review_build)
        top.addStretch(1)
        self.b_review_refresh = QPushButton("Refresh")
        self.b_review_refresh.clicked.connect(self._refresh_review)
        top.addWidget(self.b_review_refresh)
        dl.addLayout(top)

        self.lbl_review = QLabel("—")
        self.lbl_review.setStyleSheet("color:#666;")
        dl.addWidget(self.lbl_review)

        self.review_list = QTableWidget(0, 1)
        self.review_list.setHorizontalHeaderLabels(["Clip"])
        self.review_list.horizontalHeader().setStretchLastSection(True)
        self.review_list.verticalHeader().setVisible(False)
        self.review_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.review_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.review_list.itemDoubleClicked.connect(lambda *_: self._copy_review_clip())
        dl.addWidget(self.review_list, 1)

        ops = QHBoxLayout()
        self.b_review_view = QPushButton("Open Review Sheet")
        self.b_review_view.setToolTip("Open index.html — all the day's clips in one "
                                      "page to scrub through.")
        self.b_review_view.clicked.connect(self._open_review_index)
        self.b_review_open = QPushButton("Open Folder")
        self.b_review_open.setToolTip("Open the review folder in Finder/Explorer to "
                                      "drag clips into SyncSketch.")
        self.b_review_open.clicked.connect(self._open_review_folder)
        self.b_review_copy = QPushButton("Copy Clip to Clipboard")
        self.b_review_copy.setToolTip("Copy the selected clip so SyncSketch ▸ MEDIA "
                                      "▸ Upload from ▸ Clipboard can paste it.")
        self.b_review_copy.clicked.connect(self._copy_review_clip)
        ops.addWidget(self.b_review_view)
        ops.addWidget(self.b_review_open)
        ops.addWidget(self.b_review_copy)
        ops.addStretch(1)
        dl.addLayout(ops)
        return page

    def _open_review_index(self):
        import webbrowser
        folder = self._review_local_dir()
        idx = os.path.join(folder, "index.html") if folder else None
        if idx and os.path.isfile(idx):
            webbrowser.open("file://" + idx)
        else:
            QMessageBox.information(
                self, "No review sheet",
                "No index.html for this date yet — click 'Build / Update Review' "
                "first (or sync the review folder down).")

    def _review_local_dir(self) -> str | None:
        if not self.cfg:
            return None
        date_str = self.ed_review_date.text().strip() or reviewmod.today_str()
        return os.path.join(self.cfg.resolved_local_root(),
                            *reviewmod.review_dir_rel(date_str).split("/"))

    def _refresh_review(self):
        import glob
        self.review_list.setRowCount(0)
        folder = self._review_local_dir()
        if not folder:
            self.lbl_review.setText("Sign in to use dailies review.")
            return
        clips = sorted(glob.glob(os.path.join(folder, "*.mp4")))
        self.review_list.setRowCount(len(clips))
        for i, c in enumerate(clips):
            item = QTableWidgetItem(os.path.basename(c))
            item.setData(Qt.UserRole, c)
            self.review_list.setItem(i, 0, item)
        if clips:
            self.lbl_review.setText(f"{len(clips)} clip(s) in {folder}")
        elif os.path.isdir(folder):
            self.lbl_review.setText(f"No clips yet in {folder}")
        else:
            self.lbl_review.setText("No review built for this date yet — "
                                    "click 'Build / Update Review'.")

    def _build_review(self):
        if not self.cfg:
            return
        date_str = self.ed_review_date.text().strip() or reviewmod.today_str()
        user = self._creds_user_safe()
        remote, pname = self.cfg.remote_root, self.cfg.name
        local_root = self.cfg.resolved_local_root()
        self.b_review_build.setEnabled(False)
        self.status.showMessage(f"Building review {date_str}…")

        def work():
            return self._conn_do(lambda c: reviewmod.build_review_session(
                c, remote_root=remote, project_name=pname, local_root=local_root,
                username=user, date_str=date_str))

        def done(res):
            self.b_review_build.setEnabled(True)
            n_new = len(res.get("collected") or [])
            if n_new:
                self.status.showMessage(
                    f"Review {date_str}: added {n_new} clip(s) "
                    f"({res.get('count', 0)} total).")
            else:
                self.status.showMessage(
                    f"Review {date_str}: nothing new waiting for review.")
            self._refresh_review()

        job = Job(work)
        self._jobs.append(job)

        def _fail(m, j=job):
            if j in self._jobs:
                self._jobs.remove(j)
            self.b_review_build.setEnabled(True)
            self._on_error(m)

        job.done.connect(lambda r, j=job: (
            self._jobs.remove(j) if j in self._jobs else None, done(r)))
        job.failed.connect(_fail)
        job.start()

    def _open_review_folder(self):
        folder = self._review_local_dir()
        if folder and os.path.isdir(folder):
            clipboardmod.reveal(folder)
        else:
            QMessageBox.information(self, "No folder",
                                    "Build the review first (no local folder yet).")

    def _selected_review_clip(self) -> str | None:
        rows = self.review_list.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.review_list.item(rows[0].row(), 0)
        return item.data(Qt.UserRole) if item else None

    def _copy_review_clip(self):
        clip = self._selected_review_clip()
        if not clip:
            QMessageBox.information(self, "No clip selected",
                                    "Select a clip in the list first.")
            return
        if clipboardmod.copy_file(clip):
            self.status.showMessage(
                f"Copied {os.path.basename(clip)} — paste in SyncSketch "
                "(MEDIA ▸ Upload from ▸ Clipboard).")
        else:
            self.status.showMessage("Could not copy to clipboard on this platform.")

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
            cells = [
                t.get("type", ""), t.get("entity", ""), t.get("step", ""),
                tasksmod.STATUS_LABELS.get(t.get("status", ""), t.get("status", "")),
                ", ".join(t.get("assignees") or []),
                t.get("updated_by", ""),
            ]
            for j, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if j == 0:
                    item.setData(Qt.UserRole, t)
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
        if blend_rel is None:
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
        cfg = self.cfg
        creds = self._creds()

        def work():
            # Pull the chosen published file down so Blender opens the real version.
            if blend_rel:
                self._conn_do(lambda c: c.download(
                    core.remote_path_for(remote_root, blend_rel), open_file))
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

    def _new_task(self):
        if not self.cfg:
            return
        schema = self.cfg.schema
        spec = {
            "asset_categories": tasksmod.asset_categories(schema),
            "asset_steps": tasksmod.steps_for(schema, "asset"),
            "shot_steps": tasksmod.steps_for(schema, "shot"),
            "naming": self.cfg.naming or {},
        }
        dlg = NewTaskDialog(spec, self)
        if dlg.exec() != QDialog.Accepted:
            return
        data = dlg.values()
        remote = self.cfg.remote_root
        actor = self._creds_user_safe()
        task = tasksmod.new_task(data["type"], data["entity"], data["step"],
                                 title=data["title"] or None)

        def work():
            return self._conn_do(lambda c: tasksmod.save_task(c, remote, task, actor))

        self._busy_buttons(True)
        self._spawn(work, lambda _r: (self._busy_buttons(False), self._load_tasks()),
                    busy_msg="Creating task…")

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


class NewTaskDialog(QDialog):
    """Type and Step are dropdowns (from the schema); the entity is typed and
    validated against the project naming convention so names stay consistent."""
    def __init__(self, spec: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New task")
        self.spec = spec
        self.naming = spec.get("naming") or {}
        root = QVBoxLayout(self)

        top = QFormLayout()
        self.cb_type = QComboBox()
        self.cb_type.addItems(["shot", "asset"])
        self.cb_type.currentTextChanged.connect(self._on_type)
        top.addRow("Type", self.cb_type)
        root.addLayout(top)

        # Asset inputs: category dropdown + typed name
        self.asset_box = QWidget()
        af = QFormLayout(self.asset_box)
        af.setContentsMargins(0, 0, 0, 0)
        self.cb_category = QComboBox()
        self.cb_category.addItems(spec.get("asset_categories") or [])
        self.ed_asset_name = QLineEdit()
        self.ed_asset_name.setPlaceholderText(tasksmod.NAMING_HINTS["asset_name"])
        af.addRow("Category", self.cb_category)
        af.addRow("Asset name", self.ed_asset_name)
        root.addWidget(self.asset_box)

        # Shot inputs: sequence + shot codes
        self.shot_box = QWidget()
        sf = QFormLayout(self.shot_box)
        sf.setContentsMargins(0, 0, 0, 0)
        self.ed_seq = QLineEdit()
        self.ed_seq.setPlaceholderText(tasksmod.NAMING_HINTS["sequence"])
        self.ed_shot = QLineEdit()
        self.ed_shot.setPlaceholderText(tasksmod.NAMING_HINTS["shot"])
        sf.addRow("Sequence", self.ed_seq)
        sf.addRow("Shot", self.ed_shot)
        root.addWidget(self.shot_box)

        bottom = QFormLayout()
        self.cb_step = QComboBox()
        self.ed_title = QLineEdit()
        self.ed_title.setPlaceholderText("(optional)")
        bottom.addRow("Step", self.cb_step)
        bottom.addRow("Title", self.ed_title)
        root.addLayout(bottom)

        self.lbl_err = QLabel("")
        self.lbl_err.setStyleSheet("color:#A32D2D;")
        self.lbl_err.setWordWrap(True)
        root.addWidget(self.lbl_err)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._on_type(self.cb_type.currentText())

    def _on_type(self, ttype):
        is_asset = ttype == "asset"
        self.asset_box.setVisible(is_asset)
        self.shot_box.setVisible(not is_asset)
        self.cb_step.clear()
        self.cb_step.addItems(self.spec.get(
            "asset_steps" if is_asset else "shot_steps") or [])
        self.adjustSize()

    def _on_ok(self):
        ttype = self.cb_type.currentText()
        if ttype == "asset":
            name = self.ed_asset_name.text().strip()
            if not self.cb_category.currentText():
                return self._err("Pick an asset category.")
            if not tasksmod.validate_name(self.naming, "asset_name", name):
                return self._err("Asset name invalid: "
                                 + tasksmod.NAMING_HINTS["asset_name"])
            self._entity = f"{self.cb_category.currentText()}/{name}"
        else:
            seq = self.ed_seq.text().strip()
            shot = self.ed_shot.text().strip()
            if not tasksmod.validate_name(self.naming, "sequence", seq):
                return self._err("Sequence invalid: "
                                 + tasksmod.NAMING_HINTS["sequence"])
            if not tasksmod.validate_name(self.naming, "shot", shot):
                return self._err("Shot invalid: " + tasksmod.NAMING_HINTS["shot"])
            self._entity = f"{seq}/{shot}"
        if not self.cb_step.currentText():
            return self._err("Pick a step.")
        self.accept()

    def _err(self, msg):
        self.lbl_err.setText(msg)

    def values(self) -> dict:
        return {
            "type": self.cb_type.currentText(),
            "entity": self._entity,
            "step": self.cb_step.currentText(),
            "title": self.ed_title.text().strip(),
        }


class BugReportDialog(QDialog):
    """Collect a bug report (title + description) and choose what to attach.
    Mirrors NewTaskDialog. The screenshot was grabbed before this opened."""

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


def main():
    applog.setup_logging()
    app = QApplication(sys.argv)
    config = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    win = MainWindow(config)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
