"""Thin SFTP wrapper around Paramiko with recursive mkdir.

Supports a `dry_run` mode that performs no network I/O — used so the CLI can
preview exactly what it would create.
"""

from __future__ import annotations

import os
import posixpath
import stat as stat_mod
from typing import Iterable

from .config import SFTPCredentials


class SFTPClient:
    def __init__(self, creds: SFTPCredentials, dry_run: bool = False):
        self.creds = creds
        self.dry_run = dry_run
        self._transport = None
        self._sftp = None
        self._known_dirs: set[str] = set()

    # -- connection management ------------------------------------------------
    def __enter__(self) -> "SFTPClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        if self.dry_run:
            return
        import paramiko  # imported lazily so dry-run needs no paramiko install

        pkey = None
        if self.creds.key_file:
            pkey = paramiko.PKey.from_path(self.creds.key_file) if hasattr(
                paramiko.PKey, "from_path"
            ) else paramiko.RSAKey.from_private_key_file(
                self.creds.key_file, password=self.creds.key_passphrase
            )

        self._transport = paramiko.Transport((self.creds.host, self.creds.port))
        self._transport.connect(
            username=self.creds.user,
            password=self.creds.password if not pkey else None,
            pkey=pkey,
        )
        self._sftp = paramiko.SFTPClient.from_transport(self._transport)

    def close(self) -> None:
        if self._sftp:
            self._sftp.close()
        if self._transport:
            self._transport.close()
        self._sftp = self._transport = None

    # -- operations -----------------------------------------------------------
    def exists(self, path: str) -> bool:
        if self.dry_run:
            return path in self._known_dirs
        try:
            self._sftp.stat(path)
            return True
        except IOError:
            return False

    def makedirs(self, path: str) -> bool:
        """Create `path` and any missing parents. Returns True if it created it,
        False if it already existed."""
        path = posixpath.normpath(path)
        if self.exists(path):
            self._known_dirs.add(path)
            return False

        # Build parents first.
        parent = posixpath.dirname(path)
        if parent and parent not in ("/", "") and not self.exists(parent):
            self.makedirs(parent)

        if self.dry_run:
            self._known_dirs.add(path)
            return True

        self._sftp.mkdir(path)
        self._known_dirs.add(path)
        return True

    def put(self, local_path: str, remote_path: str) -> None:
        """Upload a local file to `remote_path`, creating parent dirs first."""
        parent = posixpath.dirname(remote_path)
        if parent:
            self.makedirs(parent)
        if self.dry_run:
            return
        self._sftp.put(local_path, remote_path)

    def download_file(self, remote_path: str, local_path: str) -> None:
        """Download a single file, creating local parent dirs."""
        import os
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        if self.dry_run:
            return
        self._sftp.get(remote_path, local_path)

    def download_dir(self, remote_dir: str, local_dir: str) -> int:
        """Recursively download remote_dir into local_dir. Returns file count."""
        import os
        import stat as _stat
        os.makedirs(local_dir, exist_ok=True)
        if self.dry_run:
            print(f"  [DRY-RUN] would sync {remote_dir} -> {local_dir}")
            return 0
        count = 0
        for entry in self._sftp.listdir_attr(remote_dir):
            rpath = posixpath.join(remote_dir, entry.filename)
            lpath = os.path.join(local_dir, entry.filename)
            if _stat.S_ISDIR(entry.st_mode):
                count += self.download_dir(rpath, lpath)
            else:
                self._sftp.get(rpath, lpath)
                count += 1
        return count

    def walk_remote(self, remote_root: str) -> list[dict]:
        """Recursively list remote_root. Returns dicts with rel (path relative to
        remote_root), is_dir, size, mtime. Empty in dry-run."""
        if self.dry_run:
            return []
        results: list[dict] = []

        def _walk(rdir: str, rel: str) -> None:
            try:
                entries = self._sftp.listdir_attr(rdir)
            except IOError:
                return  # missing dir -> nothing
            for e in entries:
                erel = f"{rel}/{e.filename}" if rel else e.filename
                is_dir = stat_mod.S_ISDIR(e.st_mode)
                results.append({"rel": erel, "is_dir": is_dir,
                                "size": int(e.st_size or 0),
                                "mtime": float(e.st_mtime or 0)})
                if is_dir:
                    _walk(posixpath.join(rdir, e.filename), erel)

        _walk(remote_root, "")
        return results

    def upload(self, local_path: str, remote_path: str) -> None:
        """Upload a file, creating parents and preserving mtime (clean diffs)."""
        parent = posixpath.dirname(remote_path)
        if parent:
            self.makedirs(parent)
        if self.dry_run:
            return
        self._sftp.put(local_path, remote_path)
        st = os.stat(local_path)
        try:
            self._sftp.utime(remote_path, (st.st_atime, st.st_mtime))
        except IOError:
            pass  # some servers disallow utime; diff will just show remote newer

    def download(self, remote_path: str, local_path: str) -> None:
        """Download a file, creating local parents and preserving mtime."""
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        if self.dry_run:
            return
        self._sftp.get(remote_path, local_path)
        st = self._sftp.stat(remote_path)
        if st.st_mtime:
            os.utime(local_path, (st.st_atime or st.st_mtime, st.st_mtime))

    def create_all(self, paths: Iterable[str]) -> tuple[list[str], list[str]]:
        """Create every path. Returns (created, skipped_existing)."""
        created, skipped = [], []
        for p in paths:
            if self.makedirs(p):
                created.append(p)
            else:
                skipped.append(p)
        return created, skipped
