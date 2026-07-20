"""Tests for flumen.cli commands that need server interaction (faked)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flumen import cli, tasks
from test_tasks import FakeSrv


class _DownloadSrv(FakeSrv):
    """FakeSrv + a download that just records the (remote, local) pair."""
    def __init__(self):
        super().__init__()
        self.downloads = []

    def download(self, remote, local):
        self.downloads.append((remote, local))

    def download_dir(self, remote, local):
        self.downloads.append((remote + "/*", local))
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch(monkeypatch, srv, remote_root="/r", local_root="/local"):
    import types as _t
    monkeypatch.setattr(cli, "ProjectConfig",
                        _t.SimpleNamespace(load=lambda _c: _t.SimpleNamespace(
                            remote_root=remote_root,
                            resolved_local_root=lambda: local_root)))
    monkeypatch.setattr(cli, "SFTPCredentials",
                        _t.SimpleNamespace(from_env=lambda _e: _t.SimpleNamespace(
                            user="marco")))
    monkeypatch.setattr(cli, "SFTPClient", lambda creds: srv)


def _args(**kw):
    import types as _t
    base = dict(config="config.yaml", env=".env")
    base.update(kw)
    return _t.SimpleNamespace(**base)


def test_fetch_publish_resolves_newest_via_step(monkeypatch, capsys, tmp_path):
    srv = _DownloadSrv()
    # a model task with two published versions
    mt = tasks.save_task(srv, "/r", tasks.new_task("asset", "characters/hero", "model"))
    tasks.publish_task(srv, "/r", "marco", ["/tmp/hero_model_v001.blend"], mt["id"])
    tasks.publish_task(srv, "/r", "marco", ["/tmp/hero_model_v002.blend"], mt["id"])
    # the SURFACE task asks for its model sibling via --step model
    st = tasks.save_task(srv, "/r",
                         tasks.new_task("asset", "characters/hero", "surface"))

    _patch(monkeypatch, srv, local_root=str(tmp_path))
    rc = cli.cmd_fetch_publish(_args(task=st["id"], step="model", ext=".blend",
                                     into=None))
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()[-1]
    # downloaded the NEWEST model publish to the local mirror, and printed its path
    remote, local = [d for d in srv.downloads if d[0].endswith(".blend")][-1]
    assert remote.endswith("hero_model_v002.blend")
    assert local == out and local.endswith("hero_model_v002.blend")


def _surface_with_looks(srv):
    st = tasks.save_task(srv, "/r",
                         tasks.new_task("asset", "characters/frankenstein", "surface"))
    pub = "03_assets/characters/frankenstein/surface/publish/"
    st["publishes"] = [
        {"files": [pub + "frankenstein_surface_default_v001.blend",
                   pub + "frankenstein_surface_default_v001.manifest.json"],
         "time": 1, "by": "marco"},
        {"files": [pub + "frankenstein_surface_default_v002.blend",
                   pub + "frankenstein_surface_default_v002.manifest.json"],
         "time": 2, "by": "marco"},
    ]
    tasks.save_task(srv, "/r", st)
    return st


def test_list_looks_json(monkeypatch, capsys):
    import json
    srv = _DownloadSrv()
    st = _surface_with_looks(srv)
    _patch(monkeypatch, srv)
    rc = cli.cmd_list_looks(_args(task=st["id"]))
    assert rc == 0
    looks = json.loads(capsys.readouterr().out.strip())
    assert [(l["look"], l["version"]) for l in looks] == [("default", 2)]


def test_fetch_look_downloads_blend_manifest_and_textures(monkeypatch, capsys, tmp_path):
    srv = _DownloadSrv()
    st = _surface_with_looks(srv)
    _patch(monkeypatch, srv, local_root=str(tmp_path))
    rc = cli.cmd_fetch_look(_args(task=st["id"], look="default"))
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert out.endswith("frankenstein_surface_default_v002.blend")  # newest
    fetched = [r for r, _ in srv.downloads]
    assert any(r.endswith("frankenstein_surface_default_v002.blend") for r in fetched)
    assert any(r.endswith(".manifest.json") for r in fetched)
    assert any(r.endswith("/textures/*") for r in fetched)          # textures dir


def test_fetch_look_unknown_name_errors(monkeypatch, capsys, tmp_path):
    srv = _DownloadSrv()
    st = _surface_with_looks(srv)
    _patch(monkeypatch, srv, local_root=str(tmp_path))
    rc = cli.cmd_fetch_look(_args(task=st["id"], look="nope"))
    assert rc == 1
    assert "no look" in capsys.readouterr().err


def test_fetch_publish_errors_when_no_publish(monkeypatch, capsys):
    srv = _DownloadSrv()
    mt = tasks.save_task(srv, "/r", tasks.new_task("asset", "characters/hero", "model"))
    st = tasks.save_task(srv, "/r",
                         tasks.new_task("asset", "characters/hero", "surface"))
    _patch(monkeypatch, srv)
    rc = cli.cmd_fetch_publish(_args(task=st["id"], step="model", ext=".blend",
                                     into=None))
    assert rc == 1
    assert "no published" in capsys.readouterr().err


# ---- assembly / resolve-assembly -------------------------------------------

def test_assembly_add_then_list(monkeypatch, capsys):
    import json
    srv = _DownloadSrv()
    _patch(monkeypatch, srv)
    rc = cli.cmd_assembly_add(_args(shot="SEQ010/SH0010",
                                    asset="characters/frankenstein",
                                    camera=False, label="", look=""))
    assert rc == 0
    rc = cli.cmd_assembly_add(_args(shot="SEQ010/SH0010", asset="",
                                    camera=True, label="", look=""))
    assert rc == 0
    capsys.readouterr()  # drain
    rc = cli.cmd_assembly_list(_args(shot="SEQ010/SH0010"))
    assert rc == 0
    els = json.loads(capsys.readouterr().out)
    assert [e["id"] for e in els] == ["frankenstein", "camera"]
    assert els[1]["kind"] == "camera"


def test_assembly_add_requires_asset_or_camera(monkeypatch, capsys):
    srv = _DownloadSrv()
    _patch(monkeypatch, srv)
    rc = cli.cmd_assembly_add(_args(shot="SEQ010/SH0010", asset="",
                                    camera=False, label="", look=""))
    assert rc == 1
    assert "--asset required" in capsys.readouterr().err


def test_assembly_remove(monkeypatch, capsys):
    srv = _DownloadSrv()
    _patch(monkeypatch, srv)
    cli.cmd_assembly_add(_args(shot="SEQ010/SH0010",
                               asset="characters/frankenstein",
                               camera=False, label="", look=""))
    capsys.readouterr()
    rc = cli.cmd_assembly_remove(_args(shot="SEQ010/SH0010", id="frankenstein"))
    assert rc == 0
    rc = cli.cmd_assembly_remove(_args(shot="SEQ010/SH0010", id="nope"))
    assert rc == 1
    assert "no element" in capsys.readouterr().err


def test_resolve_assembly_downloads_and_prints_json(monkeypatch, capsys, tmp_path):
    import json
    from flumen import turntable, elements as E
    monkeypatch.setattr(turntable, "_load_project_settings", lambda _r: {})
    srv = _DownloadSrv()
    ent = "characters/frankenstein"
    tasks.save_task(srv, "/r", tasks.new_task("asset", ent, "model"))
    tasks.publish_task(srv, "/r", "marco", ["/tmp/frankenstein_model_v001.blend"],
                       tasks.make_id("asset", ent, "model"))
    shot = "SEQ010/SH0010"
    shot_task = tasks.save_task(srv, "/r", tasks.new_task("shot", shot, "layout"))
    asm = E.empty_assembly(shot)
    E.add_element(asm, E.new_element(ent))
    E.save_assembly(srv, "/r", shot, asm)

    _patch(monkeypatch, srv, local_root=str(tmp_path))
    rc = cli.cmd_resolve_assembly(_args(task=shot_task["id"], shot=None, step=None, list=False, only=[], pick=[]))
    assert rc == 0
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert res["frame_start"] == 1001 and res["frame_end"] == 1100  # defaults
    out = res["elements"]
    assert len(out) == 1
    el = out[0]
    assert el["id"] == "frankenstein" and el["source_step"] == "model"
    assert el["collection"] == "frankenstein"
    assert el["blend_local"].endswith("frankenstein_model_v001.blend")
    # downloaded the resolved publish + its sidecar textures folder
    assert any(r.endswith("frankenstein_model_v001.blend")
               for r, _ in srv.downloads)
    assert any(r.endswith("/publish/textures/*") for r, _ in srv.downloads)


def test_resolve_assembly_rejects_non_shot_task(monkeypatch, capsys, tmp_path):
    from flumen import turntable
    monkeypatch.setattr(turntable, "_load_project_settings", lambda _r: {})
    srv = _DownloadSrv()
    at = tasks.save_task(srv, "/r",
                         tasks.new_task("asset", "characters/frankenstein", "model"))
    _patch(monkeypatch, srv, local_root=str(tmp_path))
    rc = cli.cmd_resolve_assembly(_args(task=at["id"], shot=None, step=None, list=False, only=[], pick=[]))
    assert rc == 1
    assert "not a shot task" in capsys.readouterr().err


def test_resolve_assembly_list_does_not_download(monkeypatch, capsys, tmp_path):
    import json
    from flumen import turntable, elements as E
    monkeypatch.setattr(turntable, "_load_project_settings", lambda _r: {})
    srv = _DownloadSrv()
    ent = "characters/frankenstein"
    tasks.save_task(srv, "/r", tasks.new_task("asset", ent, "model"))
    tasks.publish_task(srv, "/r", "marco", ["/tmp/frankenstein_model_v001.blend"],
                       tasks.make_id("asset", ent, "model"))
    shot = "SEQ010/SH0010"
    shot_task = tasks.save_task(srv, "/r", tasks.new_task("shot", shot, "layout"))
    asm = E.empty_assembly(shot)
    E.add_element(asm, E.new_element(ent))
    E.add_element(asm, E.new_element("", "camera"))   # create_rig, no publish
    E.save_assembly(srv, "/r", shot, asm)

    _patch(monkeypatch, srv, local_root=str(tmp_path))
    rc = cli.cmd_resolve_assembly(_args(task=shot_task["id"], shot=None, step=None,
                                        list=True, only=[], pick=[]))
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["elements"]
    assert {e["id"] for e in out} == {"frankenstein", "camera"}     # both listed
    assert all(e["blend_local"] == "" for e in out)                 # nothing fetched
    assert srv.downloads == []
    # blend_rel must ride even in --list: the Build-shot dialog compares it
    # against the loaded library to flag 'new model/rig' updates.
    fr = next(e for e in out if e["id"] == "frankenstein")
    assert fr["blend_rel"].endswith("frankenstein_model_v001.blend")
    cam = next(e for e in out if e["kind"] == "camera")
    assert cam["load"] == "create_rig" and cam["camera_name"] == "SEQ010_SH0010"


def test_resolve_assembly_only_fetches_chosen(monkeypatch, capsys, tmp_path):
    import json
    from flumen import turntable, elements as E
    monkeypatch.setattr(turntable, "_load_project_settings", lambda _r: {})
    srv = _DownloadSrv()
    for nm in ("characters/frankenstein", "props/box"):
        tasks.save_task(srv, "/r", tasks.new_task("asset", nm, "model"))
        base = nm.split("/")[-1]
        tasks.publish_task(srv, "/r", "marco", [f"/tmp/{base}_model_v001.blend"],
                           tasks.make_id("asset", nm, "model"))
    shot = "SEQ010/SH0010"
    shot_task = tasks.save_task(srv, "/r", tasks.new_task("shot", shot, "layout"))
    asm = E.empty_assembly(shot)
    E.add_element(asm, E.new_element("characters/frankenstein"))
    E.add_element(asm, E.new_element("props/box"))
    E.save_assembly(srv, "/r", shot, asm)

    _patch(monkeypatch, srv, local_root=str(tmp_path))
    rc = cli.cmd_resolve_assembly(_args(task=shot_task["id"], shot=None, step=None,
                                        list=False, only=["box"], pick=[]))
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["elements"]
    assert [e["id"] for e in out] == ["box"]                        # filtered
    blends = [r for r, _ in srv.downloads if r.endswith(".blend")]
    assert len(blends) == 1                                         # only box
    assert srv.downloads[0][0].endswith("box_model_v001.blend")     # only box fetched


def test_assembly_set_range(monkeypatch, capsys):
    from flumen import elements as E
    srv = _DownloadSrv()
    _patch(monkeypatch, srv)
    rc = cli.cmd_assembly_set_range(_args(shot="SEQ010/SH0010", duration=240, start=0))
    assert rc == 0
    assert "1001-1240" in capsys.readouterr().out
    asm = E.load_assembly(srv, "/r", "SEQ010/SH0010")
    assert asm["frame_start"] == 1001 and asm["duration"] == 240
    assert E.frame_range(asm) == (1001, 1240)


def test_resolve_assembly_includes_animation(monkeypatch, capsys, tmp_path):
    import json
    from flumen import turntable, elements as E
    monkeypatch.setattr(turntable, "_load_project_settings", lambda _r: {})
    srv = _DownloadSrv()
    ent = "characters/frankenstein"
    tasks.save_task(srv, "/r", tasks.new_task("asset", ent, "model"))
    tasks.publish_task(srv, "/r", "marco", ["/tmp/frankenstein_model_v001.blend"],
                       tasks.make_id("asset", ent, "model"))
    shot = "SEQ010/SH0010"
    lt = tasks.make_id("shot", shot, "layout")
    st = tasks.save_task(srv, "/r", tasks.new_task("shot", shot, "layout"))
    asm = E.empty_assembly(shot); E.add_element(asm, E.new_element(ent))
    E.save_assembly(srv, "/r", shot, asm)
    pub = "04_sequences/SEQ010/SH0010/layout/publish"
    t = tasks.get_task(srv, "/r", lt)
    t["publishes"] = [{"files": [pub + "/SH0010_layout_v001.blend",
                                 pub + "/SH0010_layout_v001_anim.blend",
                                 pub + "/SH0010_layout_v001_anim.manifest.json"],
                       "time": 1, "by": "marco"}]
    tasks.save_task(srv, "/r", t)
    srv.files["/r/" + pub + "/SH0010_layout_v001_anim.manifest.json"] = \
        '{"version":1,"elements":{"frankenstein":{"frank_rig":"A"}}}'

    _patch(monkeypatch, srv, local_root=str(tmp_path))
    rc = cli.cmd_resolve_assembly(_args(task=st["id"], shot=None, step=None,
                                        list=False, only=[], pick=[]))
    assert rc == 0
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    fr = res["anim"]["elements"]["frankenstein"]
    assert fr["objects"] == {"frank_rig": "A"}
    assert fr["blend_local"].endswith("SH0010_layout_v001_anim.blend")
    assert any(r.endswith("SH0010_layout_v001_anim.blend") for r, _ in srv.downloads)

    # --list previews the anim METADATA (dialog shows versions) but must not
    # download the blends — no blend_local, no new downloads.
    srv.downloads.clear()
    rc = cli.cmd_resolve_assembly(_args(task=st["id"], shot=None, step=None,
                                        list=True, only=[], pick=[]))
    res2 = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    fr2 = res2["anim"]["elements"]["frankenstein"]
    assert fr2["version"] == "v001" and "blend_local" not in fr2
    assert srv.downloads == []


def test_list_animations_downloads_and_prints(monkeypatch, capsys, tmp_path):
    import json
    from flumen import elements as E
    srv = _DownloadSrv()
    shot = "SEQ010/SH0010"
    lt = tasks.make_id("shot", shot, "layout")
    st = tasks.save_task(srv, "/r", tasks.new_task("shot", shot, "layout"))
    pub = "04_sequences/SEQ010/SH0010/layout/publish/anim"
    t = tasks.get_task(srv, "/r", lt)
    t["publishes"] = [{"files": [pub + "/SH0010_layout_v005_anim.blend",
                                 pub + "/SH0010_layout_v005_anim.manifest.json"],
                       "time": 5, "by": "marco", "description": "take 5"}]
    tasks.save_task(srv, "/r", t)
    srv.files["/r/" + pub + "/SH0010_layout_v005_anim.manifest.json"] = \
        ('{"version":5,"elements":{"camera":{"SEQ010_SH0010":"CamA"}},'
         '"hashes":{"camera":"h5"}}')

    _patch(monkeypatch, srv, local_root=str(tmp_path))
    rc = cli.cmd_list_animations(_args(task=st["id"], shot=None, step=None,
                                       no_fetch=False))
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out[0]["version"] == "v005"
    assert out[0]["elements"] == {"camera": {"SEQ010_SH0010": "CamA"}}
    assert out[0]["hashes"] == {"camera": "h5"}
    assert out[0]["blend_local"].endswith("SH0010_layout_v005_anim.blend")
    assert any(r.endswith("SH0010_layout_v005_anim.blend") for r, _ in srv.downloads)

    # --no-fetch: metadata + hashes only, no downloads, no blend_local
    srv.downloads.clear()
    rc = cli.cmd_list_animations(_args(task=st["id"], shot=None, step=None,
                                       no_fetch=True))
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out[0]["hashes"] == {"camera": "h5"} and "blend_local" not in out[0]
    assert srv.downloads == []


def test_list_dressings_json(monkeypatch, capsys):
    import json
    srv = _DownloadSrv()
    dt = tasks.save_task(srv, "/r",
                         tasks.new_task("asset", "environments/market_square",
                                        "dressing"))
    pub = "03_assets/environments/market_square/dressing/publish/"
    dt["publishes"] = [
        {"files": [pub + "market_square_dressing_night_market_v001.blend",
                   pub + "market_square_dressing_night_market_v001.manifest.json"],
         "time": 1, "by": "leo"},
        {"files": [pub + "market_square_dressing_night_market_v002.blend"],
         "time": 2, "by": "leo"},
    ]
    tasks.save_task(srv, "/r", dt)
    _patch(monkeypatch, srv)
    rc = cli.cmd_list_dressings(_args(task=dt["id"]))
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert [(d["dressing"], d["version"]) for d in out] == [("night_market", 2)]
    assert cli.cmd_list_dressings(_args(task="nope")) == 1     # missing task


def test_list_asset_publishes_json(monkeypatch, capsys):
    import json
    srv = _DownloadSrv()
    # published model -> listed
    mt = tasks.save_task(srv, "/r",
                         tasks.new_task("asset", "props/lantern", "model"))
    mt["publishes"] = [{"files": [
        "03_assets/props/lantern/model/publish/lantern_model_v002.blend"],
        "time": 1}]
    tasks.save_task(srv, "/r", mt)
    # model task with no publishes -> skipped
    tasks.save_task(srv, "/r", tasks.new_task("asset", "props/crate", "model"))
    # surface task -> skipped (wrong step)
    st = tasks.save_task(srv, "/r",
                         tasks.new_task("asset", "props/lantern", "surface"))
    st["publishes"] = [{"files": [
        "03_assets/props/lantern/surface/publish/lantern_surface_default_v001.blend"],
        "time": 1}]
    tasks.save_task(srv, "/r", st)
    _patch(monkeypatch, srv)
    rc = cli.cmd_list_asset_publishes(_args(step="model"))
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out == [{"entity": "props/lantern", "step": "model",
                    "blend_rel": "03_assets/props/lantern/model/publish/"
                                 "lantern_model_v002.blend"}]


def _seed_env_with_dressing(srv):
    """Env asset with a model publish + a dressing task with a v002 manifest."""
    import json as _json
    env = "environments/market_square"
    tasks.save_task(srv, "/r", tasks.new_task("asset", env, "model"))
    tasks.publish_task(srv, "/r", "marco", ["/tmp/market_square_model_v004.blend"],
                       tasks.make_id("asset", env, "model"))
    dt = tasks.save_task(srv, "/r", tasks.new_task("asset", env, "dressing"))
    pub = "03_assets/environments/market_square/dressing/publish/"
    dt["publishes"] = [
        {"files": [pub + "market_square_dressing_night_market_v002.blend",
                   pub + "market_square_dressing_night_market_v002.manifest.json"],
         "time": 2, "by": "leo"}]
    tasks.save_task(srv, "/r", dt)
    manifest = {"dressing": "night_market", "version": 2,
                "environment": {"asset": env, "source_step": "model",
                                "blend_rel": "x/m_v004.blend"},
                "props": [
                    {"id": "lantern", "asset": "props/lantern",
                     "source_step": "model",
                     "blend_rel": "03_assets/props/lantern/model/publish/"
                                  "lantern_model_v002.blend",
                     "collection": "lantern", "object": "prop_root__lantern",
                     "matrix_world": [[1, 0, 0, 2.5], [0, 1, 0, 0],
                                      [0, 0, 1, 0], [0, 0, 0, 1]]},
                    {"id": "lantern_2", "asset": "props/lantern",
                     "source_step": "model",
                     "blend_rel": "03_assets/props/lantern/model/publish/"
                                  "lantern_model_v002.blend",
                     "collection": "lantern", "object": "prop_root__lantern_2",
                     "matrix_world": [[1, 0, 0, -2.5], [0, 1, 0, 0],
                                      [0, 0, 1, 0], [0, 0, 0, 1]]}],
                "extras": {
                    "collection": "market_square_dressing_night_market_extras",
                    "count": 3}}
    srv.write_text("/r/" + pub + "market_square_dressing_night_market_v002.manifest.json",
                   _json.dumps(manifest))
    return env


def test_resolve_assembly_inlines_dressing_props(monkeypatch, capsys, tmp_path):
    import json
    from flumen import turntable, elements as E
    monkeypatch.setattr(turntable, "_load_project_settings", lambda _r: {})
    srv = _DownloadSrv()
    env = _seed_env_with_dressing(srv)
    shot = "SEQ010/SH0010"
    shot_task = tasks.save_task(srv, "/r", tasks.new_task("shot", shot, "layout"))
    asm = E.empty_assembly(shot)
    E.add_element(asm, E.new_element(env, dressing="night_market"))
    E.save_assembly(srv, "/r", shot, asm)

    _patch(monkeypatch, srv, local_root=str(tmp_path))
    rc = cli.cmd_resolve_assembly(_args(task=shot_task["id"], shot=None, step=None,
                                        list=False, only=[], pick=[]))
    assert rc == 0
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    el = res["elements"][0]
    d = el["dressing"]
    assert d["name"] == "night_market" and d["version"] == 2
    assert [p["id"] for p in d["props"]] == ["lantern", "lantern_2"]
    assert d["props"][0]["blend_local"].endswith("lantern_model_v002.blend")
    assert d["props"][0]["matrix_world"][0][3] == 2.5
    # the shared prop blend is downloaded once (deduped), not per instance
    lantern_dls = [r for r, _ in srv.downloads
                   if r.endswith("lantern_model_v002.blend")]
    assert len(lantern_dls) == 1
    # local extras: the dressing .blend itself is fetched and the collection
    # named, so Build shot can link the in-scene modeled geometry
    ex = d["extras"]
    assert ex["collection"] == "market_square_dressing_night_market_extras"
    assert ex["blend_local"].endswith(
        "market_square_dressing_night_market_v002.blend")
    assert any(r.endswith("market_square_dressing_night_market_v002.blend")
               for r, _ in srv.downloads)

    # --list embeds only name+version, no prop downloads
    srv.downloads.clear()
    rc = cli.cmd_resolve_assembly(_args(task=shot_task["id"], shot=None, step=None,
                                        list=True, only=[], pick=[]))
    assert rc == 0
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    d = res["elements"][0]["dressing"]
    assert d == {"name": "night_market", "version": 2}
    assert not srv.downloads


def test_resolve_assembly_missing_dressing_warns_not_fatal(monkeypatch, capsys,
                                                           tmp_path):
    import json
    from flumen import turntable, elements as E
    monkeypatch.setattr(turntable, "_load_project_settings", lambda _r: {})
    srv = _DownloadSrv()
    env = "environments/market_square"
    tasks.save_task(srv, "/r", tasks.new_task("asset", env, "model"))
    tasks.publish_task(srv, "/r", "marco", ["/tmp/market_square_model_v004.blend"],
                       tasks.make_id("asset", env, "model"))
    shot = "SEQ010/SH0010"
    shot_task = tasks.save_task(srv, "/r", tasks.new_task("shot", shot, "layout"))
    asm = E.empty_assembly(shot)
    E.add_element(asm, E.new_element(env, dressing="never_published"))
    E.save_assembly(srv, "/r", shot, asm)

    _patch(monkeypatch, srv, local_root=str(tmp_path))
    rc = cli.cmd_resolve_assembly(_args(task=shot_task["id"], shot=None, step=None,
                                        list=False, only=[], pick=[]))
    assert rc == 0                                        # never fatal
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    el = res["elements"][0]
    assert el["blend_local"]                              # env still resolves
    assert "never_published" in el["dressing_error"]
    assert "dressing" not in el or not isinstance(el.get("dressing"), dict)


def test_review_still_uploads_and_notifies(monkeypatch, capsys, tmp_path):
    from flumen import notify
    srv = _DownloadSrv()
    srv.uploads = []
    srv.upload = lambda local, remote, callback=None: srv.uploads.append(remote)
    t = tasks.save_task(srv, "/r",
                        tasks.new_task("asset", "environments/disco", "model"))
    img = tmp_path / "disco_model_review_20260702_140000.png"
    img.write_bytes(b"png")
    announced = []
    monkeypatch.setattr(notify, "announce_dailies",
                        lambda c, rr, task, rec, media, actor:
                        announced.append(media) or True)
    _patch(monkeypatch, srv)
    rc = cli.cmd_review_still(_args(task=t["id"], file=str(img)))
    assert rc == 0
    assert srv.uploads == ["/r/07_dailies/environments/disco/model/"
                           "disco_model_review_20260702_140000.png"]
    assert announced == [["07_dailies/environments/disco/model/"
                          "disco_model_review_20260702_140000.png"]]
    out = capsys.readouterr().out
    assert "review still ->" in out
    # The still is recorded on the task so the Dailies tab lists it.
    saved = tasks.get_task(srv, "/r", t["id"])
    assert [s["file"] for s in saved["stills"]] == [
        "07_dailies/environments/disco/model/"
        "disco_model_review_20260702_140000.png"]
    assert saved["stills"][0]["review_status"] == "to_review"
    # missing file / task -> clean errors
    assert cli.cmd_review_still(_args(task=t["id"], file="/nope.png")) == 1
    assert cli.cmd_review_still(_args(task="nope", file=str(img))) == 1


def test_fetch_publish_pulls_sidecar_textures(monkeypatch, capsys, tmp_path):
    srv = _DownloadSrv()
    t = tasks.save_task(srv, "/r",
                        tasks.new_task("asset", "environments/house", "model"))
    rel = tasks.task_dir_rel(t) + "/publish/house_model_v002.blend"
    t["publishes"] = [{"files": [rel], "time": 1, "by": "m"}]
    tasks.save_task(srv, "/r", t)
    _patch(monkeypatch, srv)
    rc = cli.cmd_fetch_publish(_args(task=t["id"], step="", ext=".blend",
                                     into=str(tmp_path)))
    assert rc == 0
    tex = [(r, l) for r, l in srv.downloads
           if r.endswith("/publish/textures/*")]
    assert tex and tex[0][1] == str(tmp_path / "textures")


class _ConnSrv(FakeSrv):
    """FakeSrv + exists()/context manager, for test-connection."""
    def __init__(self, existing=(".",)):
        super().__init__()
        self.existing = set(existing)

    def exists(self, p):
        return p in self.existing

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_conn(monkeypatch, srv, remote_root="/r"):
    import types as _t
    monkeypatch.setattr(cli, "ProjectConfig",
                        _t.SimpleNamespace(load=lambda _c: _t.SimpleNamespace(
                            remote_root=remote_root)))
    monkeypatch.setattr(cli, "SFTPCredentials",
                        _t.SimpleNamespace(from_env=lambda _e: _t.SimpleNamespace(
                            user="marco", host="ftp.example.com", port=22)))
    monkeypatch.setattr(cli, "SFTPClient",
                        lambda creds, dry_run=False: srv)


def test_test_connection_ok_checks_remote_root(monkeypatch, capsys):
    _patch_conn(monkeypatch, _ConnSrv(existing=(".", "/r")))
    assert cli.cmd_test_connection(_args()) == 0
    out = capsys.readouterr().out
    assert "Connection OK" in out and "marco@ftp.example.com" in out


def test_test_connection_fails_when_remote_root_missing(monkeypatch, capsys):
    # Logged in fine, but the project folder isn't on this server -> a publish
    # would go nowhere. Must fail loudly, not report success.
    _patch_conn(monkeypatch, _ConnSrv(existing=(".",)))
    assert cli.cmd_test_connection(_args()) == 1
    err = capsys.readouterr().err
    assert "remote_root" in err and "/r" in err


def test_test_connection_no_config_is_credentials_only(monkeypatch, capsys):
    # No config.yaml here (e.g. bare credentials test): connection alone passes.
    import types as _t
    _patch_conn(monkeypatch, _ConnSrv(existing=(".",)))

    def _boom(_c):
        raise FileNotFoundError("no config.yaml")
    monkeypatch.setattr(cli, "ProjectConfig", _t.SimpleNamespace(load=_boom))
    assert cli.cmd_test_connection(_args()) == 0
    assert "Connection OK" in capsys.readouterr().out


def test_resolve_assembly_list_includes_anim_metadata(monkeypatch, capsys,
                                                      tmp_path):
    # --list feeds the Build-shot dialog: it must say which anim version each
    # element would get (so the artist can see the scene is behind) WITHOUT
    # downloading anything.
    import json
    from flumen import turntable, elements as E
    monkeypatch.setattr(turntable, "_load_project_settings", lambda _r: {})
    srv = _DownloadSrv()
    ent = "characters/frankenstein"
    tasks.save_task(srv, "/r", tasks.new_task("asset", ent, "model"))
    tasks.publish_task(srv, "/r", "marco", ["/tmp/frankenstein_model_v001.blend"],
                       tasks.make_id("asset", ent, "model"))
    shot = "SEQ010/SH0010"
    lt = tasks.make_id("shot", shot, "layout")
    st = tasks.save_task(srv, "/r", tasks.new_task("shot", shot, "layout"))
    asm = E.empty_assembly(shot)
    E.add_element(asm, E.new_element(ent))
    E.save_assembly(srv, "/r", shot, asm)
    pub = "04_sequences/SEQ010/SH0010/layout/publish"
    t = tasks.get_task(srv, "/r", lt)
    t["publishes"] = [{"files": [pub + "/SH0010_layout_v001.blend",
                                 pub + "/anim/SH0010_layout_v001_anim.blend",
                                 pub + "/anim/SH0010_layout_v001_anim.manifest.json"],
                       "time": 1, "by": "marco"}]
    tasks.save_task(srv, "/r", t)
    srv.files["/r/" + pub + "/anim/SH0010_layout_v001_anim.manifest.json"] = \
        '{"version":1,"elements":{"frankenstein":{"frank_rig":"A"}}}'

    _patch(monkeypatch, srv, local_root=str(tmp_path))
    rc = cli.cmd_resolve_assembly(_args(task=st["id"], shot=None, step=None,
                                        list=True, only=[], pick=[]))
    assert rc == 0
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    fr = res["anim"]["elements"]["frankenstein"]
    assert fr["version"] == "v001" and fr["objects"] == {"frank_rig": "A"}
    assert "blend_local" not in fr                       # metadata only
    assert srv.downloads == []                           # nothing fetched


def test_resolve_assembly_resolves_looks_at_build(monkeypatch, capsys,
                                                  tmp_path):
    # Build-time looks: every asset element resolves its look (element.look,
    # else 'default') from the asset's surface publishes — metadata in --list,
    # fetched for a real build. Shading never depends on the geometry publish.
    import json
    from flumen import turntable, elements as E
    monkeypatch.setattr(turntable, "_load_project_settings", lambda _r: {})
    srv = _DownloadSrv()
    ent = "characters/frankenstein"
    tasks.save_task(srv, "/r", tasks.new_task("asset", ent, "model"))
    tasks.publish_task(srv, "/r", "marco", ["/tmp/frankenstein_model_v001.blend"],
                       tasks.make_id("asset", ent, "model"))
    st = tasks.save_task(srv, "/r", tasks.new_task("asset", ent, "surface"))
    pub = "03_assets/characters/frankenstein/surface/publish"
    st["publishes"] = [{"files": [pub + "/frankenstein_surface_default_v002.blend",
                                  pub + "/frankenstein_surface_default_v002"
                                        ".manifest.json"],
                        "time": 1, "by": "marco"}]
    tasks.save_task(srv, "/r", st)
    shot = "SEQ010/SH0010"
    shot_task = tasks.save_task(srv, "/r", tasks.new_task("shot", shot, "layout"))
    asm = E.empty_assembly(shot)
    E.add_element(asm, E.new_element(ent))
    E.save_assembly(srv, "/r", shot, asm)

    _patch(monkeypatch, srv, local_root=str(tmp_path))
    # --list: metadata only, nothing downloaded
    rc = cli.cmd_resolve_assembly(_args(task=shot_task["id"], shot=None,
                                        step=None, list=True, only=[], pick=[]))
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    fr = out["elements"][0]
    assert fr["look_data"] == {"name": "default", "version": 2}
    assert srv.downloads == []
    # real build: the look blend + manifest download
    rc = cli.cmd_resolve_assembly(_args(task=shot_task["id"], shot=None,
                                        step=None, list=False, only=[],
                                        pick=[]))
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    ld = out["elements"][0]["look_data"]
    assert ld["blend_local"].endswith("frankenstein_surface_default_v002.blend")
    got = [r for r, _l in srv.downloads]
    assert any(r.endswith("_v002.blend") for r in got)
    assert any(r.endswith(".manifest.json") for r in got)


def test_publish_cache_and_lighting_resolves_it(monkeypatch, capsys, tmp_path):
    # the caching tool -> lighting build round-trip, end to end (faked server).
    import json
    from flumen import turntable, elements as E
    monkeypatch.setattr(turntable, "_load_project_settings", lambda _r: {})
    srv = _DownloadSrv()
    ent = "characters/skeleton"
    for st in ("rig", "model"):
        tasks.save_task(srv, "/r", tasks.new_task("asset", ent, st))
        tasks.publish_task(srv, "/r", "m", [f"/tmp/skeleton_{st}_v001.blend"],
                           tasks.make_id("asset", ent, st))
    shot = "SEQ010/SH0010"
    tasks.save_task(srv, "/r", tasks.new_task("shot", shot, "animation"))
    tasks.save_task(srv, "/r", tasks.new_task("shot", shot, "lighting"))
    asm = E.empty_assembly(shot)
    E.add_element(asm, E.new_element(ent))
    E.save_assembly(srv, "/r", shot, asm)

    # publish a cache for skeleton from the animation task
    abc = tmp_path / "skeleton.abc"
    abc.write_bytes(b"ABC-fake")
    anim_id = tasks.make_id("shot", shot, "animation")
    _patch(monkeypatch, srv, local_root=str(tmp_path))
    rc = cli.cmd_publish_cache(_args(task=anim_id,
                                     cache=[f"skeleton={abc}"], description=""))
    assert rc == 0
    out = capsys.readouterr().out
    assert "cached skeleton v001" in out

    # lighting resolve picks up the cache
    lit_id = tasks.make_id("shot", shot, "lighting")
    rc = cli.cmd_resolve_assembly(_args(task=lit_id, shot=None, step=None,
                                        list=False, only=[], pick=[]))
    assert rc == 0
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    sk = next(e for e in res["elements"] if e["id"] == "skeleton")
    assert sk["load"] == "alembic"
    assert sk["cache_rel"].endswith("skeleton_v001.abc")
    assert sk["cache_local"].endswith("skeleton_v001.abc")
    assert sk["cache_version"] == 1
