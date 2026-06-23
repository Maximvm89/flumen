"""Verification tests. Run with: python -m pytest tests/ -q  (from project root)

Covers:
  * schema expansion (parent-before-child ordering, work/publish split)
  * the REAL (non-dry-run) SFTPClient.makedirs/create_all code path, using a
    fake in-memory backend that mimics paramiko's SFTPClient API. This proves
    recursive mkdir + idempotency without needing a live server.
"""

import posixpath
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from animpipe import schema as S
from animpipe.config import SFTPCredentials
from animpipe.sftp import SFTPClient

SCHEMA = yaml.safe_load(open(Path(__file__).parent.parent / "folder_schema.yaml"))
ROOT = "/projects/TST"


# ---- schema -----------------------------------------------------------------
def test_parents_before_children():
    paths = S.project_paths(SCHEMA, ROOT)
    seen = set()
    for p in paths:
        parent = posixpath.dirname(p)
        if parent.startswith(ROOT) and parent != ROOT:
            assert parent in seen, f"{p} listed before its parent {parent}"
        seen.add(p)


def test_asset_has_work_and_publish():
    paths = S.asset_paths(SCHEMA, ROOT, "characters", "hero")
    assert f"{ROOT}/03_assets/characters/hero/model/work" in paths
    assert f"{ROOT}/03_assets/characters/hero/model/publish" in paths


def test_shot_has_departments():
    paths = S.shot_paths(SCHEMA, ROOT, "SEQ010", "SH0010")
    for dept in ("layout", "animation", "lighting", "comp"):
        assert f"{ROOT}/04_sequences/SEQ010/SH0010/{dept}/work" in paths


# ---- real SFTP code path (fake backend) -------------------------------------
class FakeSFTP:
    """Mimics the subset of paramiko.SFTPClient that SFTPClient uses."""
    def __init__(self):
        self.dirs = set()

    def stat(self, path):
        if path not in self.dirs:
            raise IOError("No such file")
        return True

    def mkdir(self, path):
        parent = posixpath.dirname(path)
        if parent and parent not in ("/", "") and parent not in self.dirs:
            raise IOError(f"parent missing: {parent}")  # enforces real ordering
        self.dirs.add(path)

    def close(self):
        pass


def _client_with_fake():
    c = SFTPClient(SFTPCredentials(host="x", port=22, user="x"), dry_run=False)
    c._sftp = FakeSFTP()
    return c


def test_create_all_real_path_and_idempotent():
    c = _client_with_fake()
    paths = S.project_paths(SCHEMA, ROOT)

    created, skipped = c.create_all(paths)
    assert len(created) == len(set(paths))   # everything made
    assert skipped == []                     # nothing pre-existed
    # mkdir never raised "parent missing" => ordering is correct.

    # Second run: idempotent — all skipped, none re-created.
    created2, skipped2 = c.create_all(paths)
    assert created2 == []
    assert len(skipped2) == len(set(paths))
