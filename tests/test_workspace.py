"""Tests for workspace_app.core (no Qt, no real FTP)."""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workspace_app import core


class FakeSFTP:
    """Provides walk_remote() like animpipe.sftp.SFTPClient."""
    def __init__(self, entries):
        self._entries = entries

    def walk_remote(self, remote_root):
        return self._entries


def _dir(rel):
    return {"rel": rel, "is_dir": True, "size": 0, "mtime": 0.0}


def _file(rel, size, mtime):
    return {"rel": rel, "is_dir": False, "size": size, "mtime": mtime}


def test_mirror_structure_creates_dirs_only(tmp_path):
    entries = [
        _dir("03_assets"),
        _dir("03_assets/characters"),
        _dir("03_assets/characters/hero"),
        _dir("03_assets/characters/hero/model"),
        _dir("03_assets/characters/hero/model/work"),
        _file("03_assets/characters/hero/model/work/hero.blend", 100, 1.0),  # ignored
    ]
    sftp = FakeSFTP(entries)
    created = core.mirror_structure(sftp, "/shared/Legami", str(tmp_path))
    assert (tmp_path / "03_assets/characters/hero/model/work").is_dir()
    # the file entry must NOT create anything
    assert not (tmp_path / "03_assets/characters/hero/model/work/hero.blend").exists()
    assert len(created) == 5


def test_in_tracked_area():
    assert core.in_tracked_area("a/b/work/x.blend")
    assert core.in_tracked_area("seq/shot/comp/publish/v001.exr")
    assert not core.in_tracked_area("03_assets/characters/hero/model/foo.txt")


def test_diff_statuses(tmp_path):
    now = time.time()
    # local files
    work = tmp_path / "shotA" / "work"
    work.mkdir(parents=True)
    (work / "same.blend").write_bytes(b"x" * 10)
    (work / "local_only.blend").write_bytes(b"y" * 5)
    (work / "local_newer.blend").write_bytes(b"z" * 8)
    (work / "size_diff.blend").write_bytes(b"a" * 20)
    os.utime(work / "same.blend", (now, now))
    os.utime(work / "local_newer.blend", (now + 100, now + 100))
    os.utime(work / "size_diff.blend", (now, now))

    entries = [
        _dir("shotA"), _dir("shotA/work"),
        _file("shotA/work/same.blend", 10, now),
        _file("shotA/work/local_newer.blend", 8, now),          # remote older
        _file("shotA/work/remote_only.blend", 3, now),
        _file("shotA/work/size_diff.blend", 999, now),          # different size
        _file("shotA/work/remote_newer.blend", 4, now + 100),   # not local
        _file("ignored/foo.txt", 1, now),                       # outside area
    ]
    # add a local 'remote_newer' with older mtime to trigger REMOTE_NEWER
    (work / "remote_newer.blend").write_bytes(b"b" * 4)
    os.utime(work / "remote_newer.blend", (now - 100, now - 100))

    rows = {r.rel: r.status for r in core.diff(FakeSFTP(entries), "/r", str(tmp_path))}
    assert rows["shotA/work/same.blend"] == core.IN_SYNC
    assert rows["shotA/work/local_only.blend"] == core.LOCAL_ONLY
    assert rows["shotA/work/remote_only.blend"] == core.REMOTE_ONLY
    assert rows["shotA/work/local_newer.blend"] == core.LOCAL_NEWER
    assert rows["shotA/work/remote_newer.blend"] == core.REMOTE_NEWER
    assert rows["shotA/work/size_diff.blend"] == core.SIZE_DIFFERS
    assert "ignored/foo.txt" not in rows  # outside tracked areas


def test_total_size_and_human():
    files = {"a": (1024, 0.0), "b": (2048, 0.0)}
    assert core.local_total_size(files) == 3072
    assert core.human_size(0) == "0 B"
    assert core.human_size(1536).endswith("KB")
    assert core.human_size(None) == "—"


def test_set_local_root_preserves_comments(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "project:\n"
        '  name: "X"\n'
        "  # a comment\n"
        '  remote_root: "/shared/Legami"\n'
        "schema: f.yaml\n"
    )
    core.set_local_root_in_config(str(cfg), "/Users/me/Legami/LEGAMI")
    text = cfg.read_text()
    assert "# a comment" in text                       # comments preserved
    assert 'local_root: "/Users/me/Legami/LEGAMI"' in text
    # idempotent: running again replaces, not duplicates
    core.set_local_root_in_config(str(cfg), "/new/path")
    assert text.count("local_root:") == 1 or cfg.read_text().count("local_root:") == 1
