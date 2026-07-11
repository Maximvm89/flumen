import datetime
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flumen import syncsketch as SS
from test_tasks import FakeSrv


class FakeAPI:
    last = None

    def __init__(self, auth, key):
        FakeAPI.last = self
        self.auth, self.key = auth, key
        self.projects = {"objects": [{"id": 7, "name": "Legami"}]}
        self.reviews = {"objects": []}
        self.created, self.uploads = [], []
        self.fail_upload = False

    def get_projects(self):
        return self.projects

    def get_reviews_by_project_id(self, pid):
        return self.reviews

    def create_review(self, pid, name, description=""):
        self.created.append((pid, name))
        return {"id": 42, "name": name}

    def add_media(self, rid, path, artist_name="", file_name="", **kw):
        if self.fail_upload:
            raise RuntimeError("boom")
        self.uploads.append((rid, path, file_name))


def _fake_pkg(monkeypatch):
    mod = types.ModuleType("syncsketch")
    mod.SyncSketchAPI = FakeAPI
    monkeypatch.setitem(sys.modules, "syncsketch", mod)
    FakeAPI.last = None


def _srv(cfg):
    s = FakeSrv()
    s.files["/r/02_pipeline/notifications.json"] = json.dumps(
        {"syncsketch": cfg} if cfg is not None else {})
    return s


GOOD = {"enabled": True, "username": "marco", "api_key": "k",
        "project": "Legami"}


def test_day_review_name():
    assert SS.day_review_name(datetime.date(2026, 7, 15)) == "Dailies 2026-07-15"


def test_upload_creates_todays_review(monkeypatch, tmp_path):
    _fake_pkg(monkeypatch)
    media = tmp_path / "disco_model_v014_turntable.mp4"
    media.write_bytes(b"mp4")
    ok = SS.announce_media(_srv(GOOD), "/r", str(media),
                           "disco_model_v014_turntable.mp4")
    assert ok is True
    api = FakeAPI.last
    assert api.created == [(7, SS.day_review_name())]
    assert api.uploads == [(42, str(media), "disco_model_v014_turntable.mp4")]


def test_upload_reuses_existing_review(monkeypatch, tmp_path):
    _fake_pkg(monkeypatch)
    media = tmp_path / "x.mp4"
    media.write_bytes(b"m")
    srv = _srv(GOOD)

    class Reusing(FakeAPI):
        def __init__(self, a, k):
            super().__init__(a, k)
            self.reviews = {"objects": [{"id": 9, "name": SS.day_review_name()}]}
    sys.modules["syncsketch"].SyncSketchAPI = Reusing
    assert SS.announce_media(srv, "/r", str(media), "x.mp4") is True
    api = FakeAPI.last
    assert api.created == []
    assert api.uploads == [(9, str(media), "x.mp4")]


def test_disabled_or_missing_config_is_noop(monkeypatch, tmp_path):
    _fake_pkg(monkeypatch)
    media = tmp_path / "x.mp4"
    media.write_bytes(b"m")
    assert SS.announce_media(_srv(None), "/r", str(media), "x") is False
    assert SS.announce_media(_srv({**GOOD, "enabled": False}), "/r",
                             str(media), "x") is False
    assert SS.announce_media(_srv({"username": "m"}), "/r",
                             str(media), "x") is False   # incomplete
    assert FakeAPI.last is None                          # API never constructed


def test_unknown_project_skips(monkeypatch, tmp_path):
    _fake_pkg(monkeypatch)
    media = tmp_path / "x.mp4"
    media.write_bytes(b"m")
    cfg = {**GOOD, "project": "OtherShow"}
    assert SS.announce_media(_srv(cfg), "/r", str(media), "x") is False
    assert FakeAPI.last.uploads == []


def test_missing_file_skips(monkeypatch):
    _fake_pkg(monkeypatch)
    assert SS.announce_media(_srv(GOOD), "/r", "/nope/x.mp4", "x") is False


def test_upload_error_never_raises(monkeypatch, tmp_path):
    _fake_pkg(monkeypatch)
    media = tmp_path / "x.mp4"
    media.write_bytes(b"m")

    class Failing(FakeAPI):
        def __init__(self, a, k):
            super().__init__(a, k)
            self.fail_upload = True
    sys.modules["syncsketch"].SyncSketchAPI = Failing
    assert SS.announce_media(_srv(GOOD), "/r", str(media), "x") is False
