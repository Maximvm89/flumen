"""Tests for shot_media.py — lighting-shot local sync + cleanup."""

import json
import os

from flumen import shot_media


# ---- cleanup: pure local ----------------------------------------------------

def _write(path, nbytes=4):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * nbytes)


def test_cleanup_plan_keeps_newest_cache_and_texture_versions(tmp_path):
    root = str(tmp_path)
    cd = os.path.join(root, "04_sequences", "SEQ010", "SH0010",
                      "animation", "publish", "cache")
    _write(os.path.join(cd, "gatto_mummia_v001.abc"), 10)
    _write(os.path.join(cd, "gatto_mummia_v006.abc"), 10)
    _write(os.path.join(cd, "gatto_mummia_v006.vis.json"), 2)
    _write(os.path.join(cd, "orso_v012.abc"), 10)          # single -> stays
    _write(os.path.join(cd, "orso_v011.abc"), 10)
    _write(os.path.join(cd, "orso_v011.vis.json"), 2)      # sidecar rides along
    tex = os.path.join(root, "03_assets", "characters", "gatto_mummia",
                       "surface", "publish", "textures")
    _write(os.path.join(tex, "gatto_mummia_surface_default_v005", "a.png"), 7)
    _write(os.path.join(tex, "gatto_mummia_surface_default_v006", "a.png"), 7)

    plan = shot_media.cleanup_plan(root, "SEQ010/SH0010",
                                   ["characters/gatto_mummia"])
    names = sorted(os.path.basename(p) for p, _ in plan["victims"])
    assert names == ["gatto_mummia_surface_default_v005",
                     "gatto_mummia_v001.abc",
                     "orso_v011.abc", "orso_v011.vis.json"]
    assert plan["total_bytes"] == 10 + 10 + 2 + 7
    # newest versions untouched by apply
    n, freed = shot_media.cleanup_apply(plan, log=lambda *a: None)
    assert (n, freed) == (4, 29)
    assert os.path.isfile(os.path.join(cd, "gatto_mummia_v006.abc"))
    assert os.path.isfile(os.path.join(cd, "orso_v012.abc"))
    assert os.path.isdir(os.path.join(tex,
                                      "gatto_mummia_surface_default_v006"))
    assert not os.path.isdir(os.path.join(tex,
                                          "gatto_mummia_surface_default_v005"))


def test_cleanup_plan_empty_when_nothing_local(tmp_path):
    plan = shot_media.cleanup_plan(str(tmp_path), "SEQ010/SH0010",
                                   ["characters/gatto_mummia"])
    assert plan == {"victims": [], "total_bytes": 0}


# ---- sync: fake client ------------------------------------------------------

class _FakeClient:
    """Server with one recorded cache + one published look; records downloads."""

    def __init__(self, files):
        self.files = files              # remote path -> bytes
        self.downloaded, self.dirs = [], []

    def exists(self, path):
        return path in self.files or any(k.startswith(path + "/")
                                         for k in self.files)

    def download(self, remote, local):
        self.downloaded.append(remote)
        _write(local, len(self.files.get(remote, b"")))

    def download_dir(self, remote, local):
        self.dirs.append(remote)
        return 1

    def read_text(self, path):
        return self.files.get(path, b"").decode()

    def fetch_stats(self):
        return {"downloaded": len(self.downloaded) + len(self.dirs),
                "skipped": 0, "bytes": 123}


def test_sync_shot_media_pulls_newest_cache_look_and_textures(tmp_path):
    rr = "/srv/Legami"
    shot = "SEQ010/SH0010"
    cache_rel = "04_sequences/SEQ010/SH0010/animation/publish/cache"
    look_rel = ("03_assets/characters/gatto_mummia/surface/publish/"
                "gatto_mummia_surface_default_v006.blend")
    anim_task = {"type": "shot",
                 "entity": shot, "step": "animation", "publishes": [
                     {"kind": "cache",
                      "files": [f"{cache_rel}/gatto_mummia_v001.abc"]},
                     {"kind": "cache",
                      "files": [f"{cache_rel}/gatto_mummia_v006.abc",
                                f"{cache_rel}/gatto_mummia_v006.vis.json"]}]}
    surf_task = {"type": "asset", "entity": "characters/gatto_mummia",
                 "step": "surface",
                 "publishes": [{"kind": "look", "files": [look_rel]}]}
    assembly = {"shot": shot, "elements": [
        {"id": "gatto_mummia", "kind": "asset",
         "asset": "characters/gatto_mummia"}]}

    from flumen import tasks as T
    files = {
        f"{rr}/{cache_rel}/gatto_mummia_v006.abc": b"abc-data",
        f"{rr}/{cache_rel}/gatto_mummia_v006.vis.json": b"{}",
        f"{rr}/{look_rel}": b"blend",
        f"{rr}/{look_rel[:-len('.blend')]}.manifest.json": b"{}",
        f"{rr}/03_assets/characters/gatto_mummia/surface/publish/textures/"
        f"gatto_mummia_surface_default_v006/a.png": b"png",
        T.tasks_dir(rr) + "/" + T.make_id("shot", shot, "animation")
        + ".json": json.dumps(anim_task).encode(),
        T.tasks_dir(rr) + "/"
        + T.make_id("asset", "characters/gatto_mummia", "surface")
        + ".json": json.dumps(surf_task).encode(),
        f"{rr}/04_sequences/SEQ010/SH0010/assembly.json":
            json.dumps(assembly).encode(),
    }
    client = _FakeClient(files)
    res = shot_media.sync_shot_media(client, rr, str(tmp_path), shot,
                                     log=lambda *a: None)
    assert res["caches"] == 1 and res["looks"] == 1
    # newest cache + sidecar, look blend + manifest — and ONLY the newest
    # texture folder, not the whole textures tree
    assert f"{rr}/{cache_rel}/gatto_mummia_v006.abc" in client.downloaded
    assert f"{rr}/{cache_rel}/gatto_mummia_v006.vis.json" in client.downloaded
    assert f"{rr}/{look_rel}" in client.downloaded
    assert client.dirs == [f"{rr}/03_assets/characters/gatto_mummia/surface/"
                           f"publish/textures/gatto_mummia_surface_default_v006"]
    assert not any("v001" in d for d in client.downloaded)
