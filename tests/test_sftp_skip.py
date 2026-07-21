"""SFTPClient skip-if-unchanged downloads: an already-present file with a matching
size + mtime must not be re-transferred (the build/render speed-up)."""

import os

from flumen.sftp import SFTPClient, _local_is_current
from flumen.config import SFTPCredentials


class _Attr:
    def __init__(self, size, mtime, isdir=False):
        self.st_size = size
        self.st_mtime = mtime
        self.st_atime = mtime
        self.st_mode = 0o040000 if isdir else 0o100644


class _Entry(_Attr):
    def __init__(self, name, size, mtime, isdir=False):
        super().__init__(size, mtime, isdir)
        self.filename = name


class RecordingSFTP:
    """A minimal fake paramiko SFTP: serves a dict of remote files and records
    every actual get() so a test can assert transfers happened or were skipped."""
    def __init__(self, files, tree=None):
        self.files = files            # remote -> (size, mtime, content bytes)
        self.tree = tree or {}        # remote dir -> [_Entry, ...]
        self.gets = []

    def stat(self, path):
        s, m, _ = self.files[path]
        return _Attr(s, m)

    def get(self, remote, local):
        self.gets.append(remote)
        _s, _m, content = self.files[remote]
        with open(local, "wb") as fh:
            fh.write(content)

    def listdir_attr(self, path):
        return self.tree[path]

    def close(self):
        pass


def _client(files, tree=None):
    c = SFTPClient(SFTPCredentials(host="x", port=22, user="x"), dry_run=False)
    c._sftp = RecordingSFTP(files, tree)
    return c


def test_local_is_current_matches_size_and_mtime(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abcd")
    os.utime(p, (1000, 1000))
    assert _local_is_current(str(p), 4, 1000)          # exact
    assert _local_is_current(str(p), 4, 1001)          # within 2s tolerance
    assert not _local_is_current(str(p), 5, 1000)      # size differs
    assert not _local_is_current(str(p), 4, 1010)      # mtime differs
    assert not _local_is_current(str(tmp_path / "missing"), 4, 1000)


def test_download_skips_when_unchanged(tmp_path):
    content = b"hello world"
    remote = "/r/pub/model_v001.blend"
    c = _client({remote: (len(content), 1_700_000_000, content)})
    local = str(tmp_path / "model_v001.blend")

    c.download(remote, local)                    # absent -> transfers
    assert c._sftp.gets == [remote]
    assert open(local, "rb").read() == content

    c.download(remote, local)                    # identical -> skipped
    assert c._sftp.gets == [remote]              # no second get

    # remote replaced with different bytes (size + mtime change) -> re-download
    c._sftp.files[remote] = (len(content) + 1, 1_700_000_500, content + b"!")
    c.download(remote, local)
    assert c._sftp.gets == [remote, remote]


def test_download_dir_skips_unchanged_files(tmp_path):
    files = {
        "/r/tex/a.png": (3, 111, b"aaa"),
        "/r/tex/b.png": (4, 222, b"bbbb"),
    }
    tree = {"/r/tex": [_Entry("a.png", 3, 111), _Entry("b.png", 4, 222)]}
    c = _client(files, tree)
    dest = str(tmp_path / "tex")

    n = c.download_dir("/r/tex", dest)           # first sync: both transfer
    assert n == 2
    assert sorted(c._sftp.gets) == ["/r/tex/a.png", "/r/tex/b.png"]

    c._sftp.gets.clear()
    n = c.download_dir("/r/tex", dest)           # second sync: both skipped
    assert n == 2                                # still counts them as present
    assert c._sftp.gets == []                    # nothing re-fetched

    # one file changes on the server -> only that one re-downloads
    files["/r/tex/b.png"] = (5, 999, b"bbbbb")
    tree["/r/tex"] = [_Entry("a.png", 3, 111), _Entry("b.png", 5, 999)]
    c.download_dir("/r/tex", dest)
    assert c._sftp.gets == ["/r/tex/b.png"]
