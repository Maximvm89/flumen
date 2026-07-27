"""Publish shot: cmd_publish_shot + render's published-shot resolution and the
exact-version dependency auto-fetch."""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flumen import cli, tasks, render as R
from test_tasks import FakeSrv


class _Srv(FakeSrv):
    """FakeSrv + exists() and a download that materialises a local file (so the
    render's os.path.isfile / de-dup checks behave)."""
    def __init__(self):
        super().__init__()
        self.downloads = []

    def exists(self, p):
        return p in self.files

    def download(self, remote, local):
        self.downloads.append((remote, local))
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "w", encoding="utf-8") as fh:
            fh.write(self.files.get(remote, "") or "")

    def download_dir(self, remote, local):
        self.downloads.append((remote + "/*", local))
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch(monkeypatch, srv, local_root):
    import types as _t
    monkeypatch.setattr(cli, "ProjectConfig", _t.SimpleNamespace(
        load=lambda _c: _t.SimpleNamespace(
            remote_root="/r", resolved_local_root=lambda: local_root)))
    monkeypatch.setattr(cli, "SFTPCredentials", _t.SimpleNamespace(
        from_env=lambda _e: _t.SimpleNamespace(user="marco")))
    monkeypatch.setattr(cli, "SFTPClient", lambda creds: srv)


def _args(**kw):
    import types as _t
    base = dict(config="config.yaml", env=".env")
    base.update(kw)
    return _t.SimpleNamespace(**base)


PUB = "04_sequences/SEQ010/SH0010/lighting/publish/"


# ---- tasks.published_shot_files -------------------------------------------

def test_published_shot_files_filters_kind_and_orders():
    task = tasks.new_task("shot", "SEQ010/SH0010", "lighting")
    task["publishes"] = [
        {"kind": "lights", "by": "a",
         "files": [PUB + "SH0010_lighting_lights_v001.blend"]},
        {"kind": "shot", "by": "b",
         "files": [PUB + "SEQ010_SH0010_lighting_v001.blend",
                   PUB + "SEQ010_SH0010_lighting_v001.deps.json"]},
        {"kind": "shot", "by": "c",
         "files": [PUB + "SEQ010_SH0010_lighting_v002.blend",
                   PUB + "SEQ010_SH0010_lighting_v002.deps.json"]},
    ]
    shots = tasks.published_shot_files(task)
    # newest first, light rig excluded
    assert [s["name"] for s in shots] == ["SEQ010_SH0010_lighting_v002.blend",
                                          "SEQ010_SH0010_lighting_v001.blend"]
    assert shots[0]["deps_rel"].endswith("_v002.deps.json")
    assert all("lights" not in s["name"] for s in shots)


# ---- cmd_publish_shot ------------------------------------------------------

def test_publish_shot_uploads_blend_and_deps(monkeypatch, capsys, tmp_path):
    srv = _Srv()
    t = tasks.save_task(srv, "/r",
                        tasks.new_task("shot", "SEQ010/SH0010", "lighting"))
    blend = tmp_path / "SEQ010_SH0010_lighting_v006.blend"
    blend.write_bytes(b"BLENDER")
    deps = tmp_path / "SEQ010_SH0010_lighting_v006.deps.json"
    deps.write_text(json.dumps({"deps": [{"rel": "x", "kind": "cache"}]}))

    _patch(monkeypatch, srv, str(tmp_path))
    rc = cli.cmd_publish_shot(_args(task=t["id"], local=str(blend),
                                    deps=str(deps), status="review",
                                    description="final"))
    assert rc == 0
    assert "/r/" + PUB + "SEQ010_SH0010_lighting_v006.blend" in srv.files
    assert "/r/" + PUB + "SEQ010_SH0010_lighting_v006.deps.json" in srv.files
    saved = tasks.get_task(srv, "/r", t["id"])
    rec = saved["publishes"][-1]
    assert rec["kind"] == "shot" and rec["by"] == "marco"
    assert saved["status"] == "review"
    shots = tasks.published_shot_files(saved)
    assert shots[0]["name"] == "SEQ010_SH0010_lighting_v006.blend"
    assert shots[0]["deps_rel"].endswith(".deps.json")


def test_publish_shot_never_overwrites(monkeypatch, capsys, tmp_path):
    srv = _Srv()
    t = tasks.save_task(srv, "/r",
                        tasks.new_task("shot", "SEQ010/SH0010", "lighting"))
    blend = tmp_path / "SEQ010_SH0010_lighting_v006.blend"
    blend.write_bytes(b"BLENDER")
    _patch(monkeypatch, srv, str(tmp_path))
    assert cli.cmd_publish_shot(_args(task=t["id"], local=str(blend), deps="",
                                      status="", description="")) == 0
    # re-publishing the same-named file is refused (never clobber a publish)
    rc = cli.cmd_publish_shot(_args(task=t["id"], local=str(blend), deps="",
                                    status="", description=""))
    assert rc == 1
    assert "already published" in capsys.readouterr().err


# ---- render: published-only + exact-version dependency fetch ---------------

def test_published_shot_blend_newest_downloaded(tmp_path):
    import types as _t
    srv = _Srv()
    task = tasks.new_task("shot", "SEQ010/SH0010", "lighting")
    task["publishes"] = [{"kind": "shot", "by": "a", "files": [
        PUB + "SEQ010_SH0010_lighting_v002.blend",
        PUB + "SEQ010_SH0010_lighting_v002.deps.json"]}]
    srv.files["/r/" + PUB + "SEQ010_SH0010_lighting_v002.blend"] = "<blend>"
    cfg = _t.SimpleNamespace(remote_root="/r")
    blend, deps_rel = R._published_shot_blend(srv, cfg, task, str(tmp_path))
    assert blend and blend.endswith("SEQ010_SH0010_lighting_v002.blend")
    assert os.path.isfile(blend)                      # fetched into the mirror
    assert deps_rel.endswith("_v002.deps.json")


def test_published_shot_blend_none_when_unpublished(tmp_path):
    import types as _t
    srv = _Srv()
    cfg = _t.SimpleNamespace(remote_root="/r")
    task = tasks.new_task("shot", "SEQ010/SH0010", "lighting")
    assert R._published_shot_blend(srv, cfg, task, str(tmp_path)) == (None, "")


def test_ensure_dependencies_fetches_present_flags_gone(tmp_path):
    import types as _t
    srv = _Srv()
    cache_a = "04_sequences/SEQ010/SH0010/animation/publish/cache/skeleton_v003.abc"
    cache_b = "04_sequences/SEQ010/SH0010/animation/publish/cache/orso_v002.abc"
    srv.files["/r/" + cache_a] = "<abc>"              # on the server
    # cache_b deliberately absent — the exact version is gone
    deps_rel = PUB + "SEQ010_SH0010_lighting_v002.deps.json"
    srv.files["/r/" + deps_rel] = json.dumps({"deps": [
        {"rel": cache_a, "kind": "cache"},
        {"rel": cache_b, "kind": "cache"}]})
    cfg = _t.SimpleNamespace(remote_root="/r")
    fetched, missing = R._ensure_dependencies(srv, cfg, deps_rel, str(tmp_path))
    assert fetched == [cache_a]
    assert missing == [cache_b]


def test_ensure_dependencies_skips_already_mirrored(tmp_path):
    import types as _t
    srv = _Srv()
    rel = "04_sequences/SEQ010/SH0010/animation/publish/cache/skeleton_v003.abc"
    # already present locally
    local = tmp_path / Path(rel)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("<abc>")
    srv.files["/r/" + rel] = "<abc>"
    deps_rel = PUB + "x.deps.json"
    srv.files["/r/" + deps_rel] = json.dumps({"deps": [{"rel": rel, "kind": "cache"}]})
    cfg = _t.SimpleNamespace(remote_root="/r")
    fetched, missing = R._ensure_dependencies(srv, cfg, deps_rel, str(tmp_path))
    assert fetched == [] and missing == []            # nothing to do
    assert not srv.downloads
