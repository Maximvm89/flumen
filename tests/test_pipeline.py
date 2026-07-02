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

from flumen import schema as S
from flumen.config import SFTPCredentials
from flumen.sftp import SFTPClient

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


def test_asset_template_overlay_per_type():
    # environments gain the dressing step from asset_templates; others don't.
    env_tpl = S.asset_template_for(SCHEMA, "environments")
    assert "dressing" in env_tpl and "model" in env_tpl     # additive overlay
    assert "rig" not in env_tpl                             # null -> removed
    char_tpl = S.asset_template_for(SCHEMA, "characters")
    assert "dressing" not in char_tpl and "rig" in char_tpl  # others keep rig
    # no asset_templates key at all -> shared template unchanged
    assert S.asset_template_for({"asset_template": {"model": {}}}, "environments") \
        == {"model": {}}
    # explicit null removal in a minimal schema
    assert S.asset_template_for(
        {"asset_template": {"model": {}, "rig": {}},
         "asset_templates": {"environments": {"rig": None}}},
        "environments") == {"model": {}}


def test_environment_asset_paths_include_dressing():
    paths = S.asset_paths(SCHEMA, ROOT, "environments", "market_square")
    assert f"{ROOT}/03_assets/environments/market_square/dressing/work" in paths
    assert f"{ROOT}/03_assets/environments/market_square/dressing/publish" in paths
    assert not any("/rig/" in p for p in paths)             # rig removed for envs
    char = S.asset_paths(SCHEMA, ROOT, "characters", "hero")
    assert not any("/dressing/" in p for p in char)
    assert any("/rig/" in p for p in char)                  # characters keep rig


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


def test_parallel_walk_correctness():
    from flumen.sftp import parallel_walk
    # fake remote filesystem: path -> entries
    fs = {
        "/r": [{"name": "a", "is_dir": True, "size": 0, "mtime": 0},
               {"name": "f0.txt", "is_dir": False, "size": 5, "mtime": 1}],
        "/r/a": [{"name": "b", "is_dir": True, "size": 0, "mtime": 0},
                 {"name": "f1.txt", "is_dir": False, "size": 6, "mtime": 2}],
        "/r/a/b": [{"name": "f2.txt", "is_dir": False, "size": 7, "mtime": 3}],
    }
    rows = parallel_walk(lambda p: fs.get(p, []), "/r", workers=4)
    rels = {r["rel"] for r in rows}
    assert rels == {"a", "f0.txt", "a/b", "a/f1.txt", "a/b/f2.txt"}
    sizes = {r["rel"]: r["size"] for r in rows}
    assert sizes["a/b/f2.txt"] == 7
    assert sum(1 for r in rows if r["is_dir"]) == 2  # a, a/b


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
