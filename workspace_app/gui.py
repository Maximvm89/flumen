"""Legami Workspace — PySide6 desktop GUI.

Mirror the project structure locally, point Blender at it, and sync work/publish
files with the FTP with a live size + diff view.

Run:  python -m workspace_app        (from the toolkit folder, venv active)
"""

from __future__ import annotations

import datetime as _dt
import os
import sys

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QGroupBox,
)

from animpipe.config import ProjectConfig, SFTPCredentials
from animpipe.sftp import SFTPClient
from . import core

# status -> (label, background color)
STATUS_STYLE = {
    core.IN_SYNC:      ("In sync",      QColor(225, 245, 230)),
    core.LOCAL_ONLY:   ("Local only",   QColor(220, 235, 255)),
    core.LOCAL_NEWER:  ("Local newer",  QColor(205, 225, 255)),
    core.REMOTE_ONLY:  ("Remote only",  QColor(255, 240, 215)),
    core.REMOTE_NEWER: ("Remote newer", QColor(255, 230, 195)),
    core.SIZE_DIFFERS: ("Size differs", QColor(255, 215, 215)),
}
UPLOAD_STATUSES = {core.LOCAL_ONLY, core.LOCAL_NEWER}
DOWNLOAD_STATUSES = {core.REMOTE_ONLY, core.REMOTE_NEWER}


def _fmt_time(ts):
    if not ts:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


class Job(QThread):
    """Run a callable off the UI thread."""
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
        self.setWindowTitle("Legami Workspace")
        self.resize(1000, 640)
        self.config_path = config_path
        self.cfg: ProjectConfig | None = None
        self.rows: list[core.DiffRow] = []
        self._job: Job | None = None

        self._build_ui()
        self._load_config()

    # ---- UI construction ----------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        # Connection / paths
        box = QGroupBox("Project")
        grid = QGridLayout(box)
        self.lbl_project = QLabel("—")
        grid.addWidget(QLabel("Project:"), 0, 0)
        grid.addWidget(self.lbl_project, 0, 1, 1, 3)

        grid.addWidget(QLabel("Config file:"), 1, 0)
        self.ed_config = QLineEdit(self.config_path)
        grid.addWidget(self.ed_config, 1, 1, 1, 2)
        b_cfg = QPushButton("Browse…")
        b_cfg.clicked.connect(self._pick_config)
        grid.addWidget(b_cfg, 1, 3)

        grid.addWidget(QLabel("Local folder:"), 2, 0)
        self.ed_local = QLineEdit()
        grid.addWidget(self.ed_local, 2, 1, 1, 2)
        b_local = QPushButton("Browse…")
        b_local.clicked.connect(self._pick_local)
        grid.addWidget(b_local, 2, 3)

        grid.addWidget(QLabel("FTP password:"), 3, 0)
        self.ed_pass = QLineEdit()
        self.ed_pass.setEchoMode(QLineEdit.Password)
        self.ed_pass.setPlaceholderText("(from .env if blank)")
        grid.addWidget(self.ed_pass, 3, 1, 1, 2)
        outer.addWidget(box)

        # Actions
        actions = QHBoxLayout()
        self.b_mirror = QPushButton("Create Local Structure")
        self.b_mirror.clicked.connect(self._on_mirror)
        self.b_configure = QPushButton("Configure Blender → this folder")
        self.b_configure.clicked.connect(self._on_configure)
        self.b_refresh = QPushButton("Refresh / Diff")
        self.b_refresh.clicked.connect(self._on_refresh)
        actions.addWidget(self.b_mirror)
        actions.addWidget(self.b_configure)
        actions.addStretch(1)
        actions.addWidget(self.b_refresh)
        outer.addLayout(actions)

        # Table
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Status", "File (work/ & publish/)", "Local", "Remote",
             "Local modified", "Remote modified"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        outer.addWidget(self.table, 1)

        # Transfer buttons
        transfer = QHBoxLayout()
        self.b_download = QPushButton("⬇ Download selected")
        self.b_download.clicked.connect(lambda: self._on_transfer("download", selected=True))
        self.b_upload = QPushButton("⬆ Upload selected")
        self.b_upload.clicked.connect(lambda: self._on_transfer("upload", selected=True))
        self.b_download_all = QPushButton("⬇ Download all remote-newer")
        self.b_download_all.clicked.connect(lambda: self._on_transfer("download", selected=False))
        self.b_upload_all = QPushButton("⬆ Upload all local-newer")
        self.b_upload_all.clicked.connect(lambda: self._on_transfer("upload", selected=False))
        for b in (self.b_download, self.b_upload, self.b_download_all, self.b_upload_all):
            transfer.addWidget(b)
        outer.addLayout(transfer)

        self.status = self.statusBar()
        self.status.showMessage("Ready.")

    # ---- config / creds -----------------------------------------------------
    def _load_config(self):
        path = self.ed_config.text().strip()
        if not os.path.isfile(path):
            self.lbl_project.setText("config.yaml not found — Browse to it.")
            return
        try:
            self.cfg = ProjectConfig.load(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Config error", str(exc))
            return
        self.config_path = path
        self.lbl_project.setText(f"{self.cfg.name} [{self.cfg.code}]  →  {self.cfg.remote_root}")
        if not self.ed_local.text().strip():
            self.ed_local.setText(self.cfg.resolved_local_root())

    def _creds(self) -> SFTPCredentials:
        creds = SFTPCredentials.from_env(".env")
        pw = self.ed_pass.text().strip()
        if pw:
            creds.password = pw
        return creds

    def _busy(self, on: bool, msg=""):
        for b in (self.b_mirror, self.b_configure, self.b_refresh, self.b_download,
                  self.b_upload, self.b_download_all, self.b_upload_all):
            b.setEnabled(not on)
        if msg:
            self.status.showMessage(msg)

    def _run(self, fn, on_done):
        self._busy(True, "Working…")
        self._job = Job(fn)
        self._job.done.connect(lambda r: (self._busy(False), on_done(r)))
        self._job.failed.connect(self._on_error)
        self._job.start()

    def _on_error(self, msg):
        self._busy(False)
        self.status.showMessage("Error.")
        QMessageBox.critical(self, "Error", msg)

    # ---- pickers ------------------------------------------------------------
    def _pick_config(self):
        p, _ = QFileDialog.getOpenFileName(self, "Select config.yaml", "",
                                           "YAML (*.yaml *.yml)")
        if p:
            self.ed_config.setText(p)
            self._load_config()

    def _pick_local(self):
        p = QFileDialog.getExistingDirectory(self, "Select local project folder")
        if p:
            self.ed_local.setText(p)

    # ---- actions ------------------------------------------------------------
    def _on_mirror(self):
        if not self.cfg:
            return
        local = self.ed_local.text().strip()
        creds = self._creds()
        remote = self.cfg.remote_root

        def work():
            with SFTPClient(creds) as c:
                return core.mirror_structure(c, remote, local)

        def done(created):
            self.status.showMessage(f"Created {len(created)} folder(s) under {local}")
            QMessageBox.information(self, "Structure created",
                                    f"Mirrored the project structure: "
                                    f"{len(created)} new folder(s).")
        self._run(work, done)

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
            f"Saved local folder to config.yaml.\n\nThe launcher and Blender addon "
            f"will now use:\n{local}\n\nFiles will save into this structure.")
        self.status.showMessage("Configured. Relaunch Blender via the launcher.")

    def _on_refresh(self):
        if not self.cfg:
            return
        local = self.ed_local.text().strip()
        creds = self._creds()
        remote = self.cfg.remote_root

        def work():
            with SFTPClient(creds) as c:
                return core.diff(c, remote, local)
        self._run(work, self._populate)

    def _populate(self, rows: list[core.DiffRow]):
        self.rows = rows
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            label, color = STATUS_STYLE.get(r.status, (r.status, QColor(255, 255, 255)))
            cells = [
                label, r.rel,
                core.human_size(r.local_size), core.human_size(r.remote_size),
                _fmt_time(r.local_mtime), _fmt_time(r.remote_mtime),
            ]
            for j, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setBackground(QBrush(color))
                if j == 0:
                    item.setData(Qt.UserRole, r)
                self.table.setItem(i, j, item)
        self.table.setSortingEnabled(True)
        self._update_status_bar()

    def _update_status_bar(self):
        counts = core.summarize(self.rows)
        local_files = {r.rel: (r.local_size or 0, r.local_mtime or 0)
                       for r in self.rows if r.local_size is not None}
        total = core.local_total_size(local_files)
        parts = [f"Local: {len(local_files)} files, {core.human_size(total)}"]
        for status, (label, _c) in STATUS_STYLE.items():
            if counts.get(status):
                parts.append(f"{label}: {counts[status]}")
        self.status.showMessage("   |   ".join(parts))

    def _selected_rows(self) -> list[core.DiffRow]:
        out = []
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            r = item.data(Qt.UserRole)
            if r:
                out.append(r)
        return out

    def _on_transfer(self, direction: str, selected: bool):
        if not self.cfg:
            return
        local_root = self.ed_local.text().strip()
        remote_root = self.cfg.remote_root
        creds = self._creds()

        if selected:
            chosen = self._selected_rows()
        elif direction == "download":
            chosen = [r for r in self.rows if r.status in DOWNLOAD_STATUSES]
        else:
            chosen = [r for r in self.rows if r.status in UPLOAD_STATUSES]

        if not chosen:
            QMessageBox.information(self, "Nothing to do",
                                    "No matching files for this action.")
            return

        verb = "Upload" if direction == "upload" else "Download"
        if QMessageBox.question(self, f"{verb} {len(chosen)} file(s)?",
                                f"{verb} {len(chosen)} file(s)?") != QMessageBox.Yes:
            return

        def work():
            n = 0
            with SFTPClient(creds) as c:
                for r in chosen:
                    rp = core.remote_path_for(remote_root, r.rel)
                    lp = core.local_path_for(local_root, r.rel)
                    if direction == "upload":
                        c.upload(lp, rp)
                    else:
                        c.download(rp, lp)
                    n += 1
            return n

        def done(n):
            self.status.showMessage(f"{verb}ed {n} file(s). Refreshing…")
            self._on_refresh()
        self._run(work, done)


def main():
    app = QApplication(sys.argv)
    config = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    win = MainWindow(config)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
