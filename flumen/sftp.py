"""Thin SFTP wrapper around Paramiko with recursive mkdir.

Supports a `dry_run` mode that performs no network I/O — used so the CLI can
preview exactly what it would create.
"""

from __future__ import annotations

import os
import posixpath
import queue
import stat as stat_mod
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

from .config import SFTPCredentials


def _local_is_current(local_path: str, remote_size: int,
                      remote_mtime: float) -> bool:
    """True when local_path already matches the remote file's size + mtime, so the
    transfer can be skipped. The downloaders preserve the remote mtime locally, and
    published files are versioned (a given path's bytes never change once written),
    so a size + mtime match means identical content. The 2s tolerance covers
    filesystems with coarse mtime granularity (e.g. FAT)."""
    try:
        st = os.stat(local_path)
    except OSError:
        return False
    return st.st_size == remote_size and abs(st.st_mtime - remote_mtime) <= 2


def parallel_walk(list_dir: "Callable[[str], list[dict]]", root: str,
                  workers: int = 8) -> list[dict]:
    """Breadth-first walk that lists directories concurrently.

    `list_dir(path)` returns raw entries: dicts with name, is_dir, size, mtime.
    Returns flat list of dicts with rel (relative to root), is_dir, size, mtime.
    Pure of any SFTP specifics, so it's unit-testable with a fake list_dir.
    """
    results: list[dict] = []
    rlock = threading.Lock()
    state_lock = threading.Condition()
    outstanding = [0]
    ex = ThreadPoolExecutor(max_workers=max(1, workers))

    def submit(path: str, rel: str):
        with state_lock:
            outstanding[0] += 1
        ex.submit(task, path, rel)

    def task(path: str, rel: str):
        try:
            local = []
            subs = []
            for e in list_dir(path):
                erel = f"{rel}/{e['name']}" if rel else e["name"]
                row = {k: v for k, v in e.items() if k != "name"}
                row["rel"] = erel
                local.append(row)
                if e["is_dir"]:
                    subs.append((f"{path}/{e['name']}", erel))
            with rlock:
                results.extend(local)
            for sp, sr in subs:
                submit(sp, sr)
        finally:
            with state_lock:
                outstanding[0] -= 1
                if outstanding[0] == 0:
                    state_lock.notify_all()

    submit(root, "")
    with state_lock:
        while outstanding[0] > 0:
            state_lock.wait()
    ex.shutdown(wait=True)
    return results


def _close_quietly(sock) -> None:
    try:
        sock.close()
    except OSError:
        pass


def _owner_from_attr(entry) -> str:
    """Extract the owner name from an SFTP entry's `ls -l` style longname.
    e.g. '-rw-r--r-- 1 marco.parisi staff 1234 ... name' -> 'marco.parisi'.
    Returns '' if the server doesn't expose a name."""
    longname = getattr(entry, "longname", "") or ""
    parts = longname.split()
    owner = parts[2] if len(parts) >= 3 else ""
    # numeric uid (no name resolution on server) isn't useful -> drop it
    return "" if owner.isdigit() else owner


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
        import socket

        pkey = None
        if self.creds.key_file:
            pkey = paramiko.PKey.from_path(self.creds.key_file) if hasattr(
                paramiko.PKey, "from_path"
            ) else paramiko.RSAKey.from_private_key_file(
                self.creds.key_file, password=self.creds.key_passphrase
            )

        # Open the TCP socket ourselves with a bounded timeout: a firewalled or
        # unreachable host must fail in seconds with a clear message, not hang
        # a publish indefinitely with no output (paramiko's own connect has no
        # timeout). Paramiko's packetizer copes with the recv timeout staying
        # on the socket, so long transfers are unaffected.
        timeout = float(os.environ.get("FLUMEN_CONNECT_TIMEOUT", "20"))
        where = f"{self.creds.host}:{self.creds.port}"
        try:
            sock = socket.create_connection((self.creds.host, self.creds.port),
                                            timeout=timeout)
        except OSError as exc:
            raise ConnectionError(
                f"cannot reach {where} ({exc}) — check your network/VPN and "
                f"the host/port in .env") from exc
        try:
            self._transport = paramiko.Transport(sock)
            self._transport.connect(
                username=self.creds.user,
                password=self.creds.password if not pkey else None,
                pkey=pkey,
            )
        except paramiko.AuthenticationException as exc:
            self.close()
            _close_quietly(sock)
            raise ConnectionError(
                f"login failed for user '{self.creds.user}' on {where} — "
                f"check the credentials in .env") from exc
        except paramiko.SSHException as exc:
            self.close()
            _close_quietly(sock)
            raise ConnectionError(
                f"SSH handshake with {where} failed: {exc}") from exc
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
        """Download a single file, creating local parent dirs. Skips (and preserves
        mtime) when an identical copy is already local — see download()."""
        import os
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        if self.dry_run:
            return
        st = self._sftp.stat(remote_path)
        if _local_is_current(local_path, int(st.st_size or 0),
                             float(st.st_mtime or 0)):
            return
        self._sftp.get(remote_path, local_path)
        if st.st_mtime:
            os.utime(local_path, (st.st_atime or st.st_mtime, st.st_mtime))

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
                # Skip files already present + unchanged; preserve mtime so the
                # next sync recognises them (listdir_attr already gave us stat,
                # so no extra round-trip).
                if not _local_is_current(lpath, int(entry.st_size or 0),
                                         float(entry.st_mtime or 0)):
                    self._sftp.get(rpath, lpath)
                    if entry.st_mtime:
                        os.utime(lpath, (entry.st_atime or entry.st_mtime,
                                         entry.st_mtime))
                count += 1
        return count

    def walk_remote(self, remote_root: str, workers: int = 8) -> list[dict]:
        """Recursively list remote_root, listing folders CONCURRENTLY over several
        SFTP channels (much faster than serial — the scan is latency-bound).
        Returns dicts with rel, is_dir, size, mtime. Empty in dry-run."""
        if self.dry_run:
            return []

        import paramiko

        # A pool of independent SFTP channels on the same connection — paramiko
        # channels aren't thread-safe, so each worker borrows its own.
        chan_pool: "queue.Queue" = queue.Queue()
        opened = []
        try:
            for _ in range(max(1, workers)):
                try:
                    ch = paramiko.SFTPClient.from_transport(self._transport)
                except Exception:  # noqa: BLE001 — fall back to fewer channels
                    break
                opened.append(ch)
                chan_pool.put(ch)
            if not opened:  # couldn't open extra channels -> use the main one
                opened.append(self._sftp)
                chan_pool.put(self._sftp)

            def list_dir(path: str) -> list[dict]:
                ch = chan_pool.get()
                try:
                    entries = ch.listdir_attr(path)
                except IOError:
                    return []
                finally:
                    chan_pool.put(ch)
                return [{"name": e.filename,
                         "is_dir": stat_mod.S_ISDIR(e.st_mode),
                         "size": int(e.st_size or 0),
                         "mtime": float(e.st_mtime or 0),
                         "owner": _owner_from_attr(e)} for e in entries]

            return parallel_walk(list_dir, remote_root, workers=len(opened))
        finally:
            for ch in opened:
                if ch is not self._sftp:
                    try:
                        ch.close()
                    except Exception:  # noqa: BLE001
                        pass

    def listdir(self, remote_dir: str) -> list[dict]:
        """List ONE remote directory (non-recursive). Returns dicts with name,
        is_dir, size, mtime. Empty if missing or in dry-run."""
        if self.dry_run:
            return []
        try:
            entries = self._sftp.listdir_attr(remote_dir)
        except IOError:
            return []
        return [{"name": e.filename,
                 "is_dir": stat_mod.S_ISDIR(e.st_mode),
                 "size": int(e.st_size or 0),
                 "mtime": float(e.st_mtime or 0),
                 "owner": _owner_from_attr(e)} for e in entries]

    def remove(self, remote_path: str) -> None:
        """Delete a remote file. Silent if missing or in dry-run."""
        if self.dry_run:
            return
        try:
            self._sftp.remove(remote_path)
        except IOError:
            pass

    def read_many(self, paths: list[str], workers: int = 8) -> dict:
        """Read several small remote text files CONCURRENTLY over extra SFTP
        channels (latency-bound, same trick as walk_remote). Returns
        {path: text | None (missing/unreadable)}."""
        import paramiko
        from concurrent.futures import ThreadPoolExecutor
        paths = list(paths)
        if not paths or self.dry_run:
            return {p: None for p in paths}
        chan_pool: "queue.Queue" = queue.Queue()
        opened = []
        try:
            for _ in range(max(1, min(workers, len(paths)))):
                try:
                    ch = paramiko.SFTPClient.from_transport(self._transport)
                except Exception:  # noqa: BLE001 — fewer channels, still works
                    break
                opened.append(ch)
                chan_pool.put(ch)
            if not opened:
                opened.append(self._sftp)
                chan_pool.put(self._sftp)

            def read_one(p):
                ch = chan_pool.get()
                try:
                    with ch.open(p, "r") as fh:
                        data = fh.read()
                    return p, (data.decode("utf-8")
                               if isinstance(data, bytes) else data)
                except IOError:
                    return p, None
                finally:
                    chan_pool.put(ch)

            with ThreadPoolExecutor(max_workers=len(opened)) as ex:
                return dict(ex.map(read_one, paths))
        finally:
            for ch in opened:
                if ch is not self._sftp:
                    try:
                        ch.close()
                    except Exception:  # noqa: BLE001
                        pass

    def read_text(self, remote_path: str) -> str | None:
        """Read a small remote text file (e.g. a ledger). None if missing."""
        if self.dry_run:
            return None
        try:
            with self._sftp.open(remote_path, "r") as fh:
                data = fh.read()
        except IOError:
            return None
        return data.decode("utf-8") if isinstance(data, bytes) else data

    def write_text(self, remote_path: str, text: str) -> None:
        """Write a small remote text file, creating parent dirs."""
        parent = posixpath.dirname(remote_path)
        if parent:
            self.makedirs(parent)
        if self.dry_run:
            return
        with self._sftp.open(remote_path, "w") as fh:
            fh.write(text.encode("utf-8"))

    def upload(self, local_path: str, remote_path: str, callback=None) -> None:
        """Upload a file ATOMICALLY, creating parents and preserving mtime
        (clean diffs): the bytes go to '<name>.part' and are renamed into place
        only once the transfer completed and the server confirmed the size —
        a connection that dies mid-file can never leave a truncated file under
        the real name for someone to download.
        `callback(bytes_so_far, total_bytes)` (paramiko's put callback) fires during
        transfer so callers can show progress."""
        parent = posixpath.dirname(remote_path)
        if parent:
            self.makedirs(parent)
        if self.dry_run:
            return
        part = remote_path + ".part"
        # put(confirm=True) stats the uploaded file and raises if the size
        # doesn't match what we sent — the rename below only happens for
        # complete, verified transfers.
        self._sftp.put(local_path, part, callback=callback, confirm=True)
        try:
            # POSIX rename overwrites the target atomically; plain SFTP rename
            # fails if the target exists on many servers, so drop it first.
            self._sftp.posix_rename(part, remote_path)
        except (AttributeError, IOError):
            try:
                self._sftp.remove(remote_path)
            except IOError:
                pass
            self._sftp.rename(part, remote_path)
        st = os.stat(local_path)
        try:
            self._sftp.utime(remote_path, (st.st_atime, st.st_mtime))
        except IOError:
            pass  # some servers disallow utime; diff will just show remote newer

    def download(self, remote_path: str, local_path: str) -> None:
        """Download a file, creating local parents and preserving mtime.

        Skips the transfer when an identical copy (same size + mtime) is already
        in the local mirror — a big speed-up for re-builds and re-renders, since
        published files never change once written. One stat replaces a full
        re-download of an unchanged file."""
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        if self.dry_run:
            return
        st = self._sftp.stat(remote_path)
        if _local_is_current(local_path, int(st.st_size or 0),
                             float(st.st_mtime or 0)):
            return
        self._sftp.get(remote_path, local_path)
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
