"""Tests for animpipe.syncsketch pure helpers + task recording (no network)."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from animpipe import syncsketch as ss, tasks
from test_tasks import FakeSrv  # reuse the in-memory fake


def test_settings_defaults_and_override():
    d = ss.SyncSketchSettings.from_project_settings({})
    assert d.enabled is False and d.project_id == 0
    assert d.review_name_template == "{project} — {step}"
    assert not d.configured()  # disabled + no project

    s = ss.SyncSketchSettings.from_project_settings({
        "syncsketch": {"enabled": True, "account_id": 5, "project_id": 42,
                       "item_name_template": "{version_label}"}})
    assert s.enabled and s.account_id == 5 and s.project_id == 42
    assert s.item_name_template == "{version_label}"
    assert s.review_name_template == "{project} — {step}"  # default kept
    assert s.configured()


def test_configured_requires_project_id():
    s = ss.SyncSketchSettings.from_project_settings(
        {"syncsketch": {"enabled": True, "project_id": 0}})
    assert not s.configured()  # enabled but no project => not ready


def test_render_name_department_and_item():
    review = ss.render_name("{project} — {step}", project="Legami", step="model")
    assert review == "Legami — model"
    item = ss.render_name("{entity}  {version_label}",
                          entity="characters/frankenstein",
                          version_label="frankenstein_model_v003")
    assert item == "characters/frankenstein  frankenstein_model_v003"


def test_render_name_unknown_placeholder_is_safe():
    # Unknown keys are left intact rather than raising KeyError.
    assert ss.render_name("{step} {bogus}", step="rig") == "rig {bogus}"


def test_load_secret_env_precedence(monkeypatch, tmp_path):
    cached = tmp_path / "syncsketch.json"
    cached.write_text(json.dumps({"login": "file@x.com", "api_key": "FILEKEY"}))
    monkeypatch.setattr(ss, "CACHED_SYNCSKETCH", str(cached))
    monkeypatch.setenv("SYNCSKETCH_LOGIN", "env@x.com")
    monkeypatch.setenv("SYNCSKETCH_API_KEY", "ENVKEY")
    assert ss.load_secret() == ("env@x.com", "ENVKEY")


def test_load_secret_from_cache_file(monkeypatch, tmp_path):
    cached = tmp_path / "syncsketch.json"
    cached.write_text(json.dumps({"login": "file@x.com", "api_key": "FILEKEY"}))
    monkeypatch.setattr(ss, "CACHED_SYNCSKETCH", str(cached))
    monkeypatch.delenv("SYNCSKETCH_LOGIN", raising=False)
    monkeypatch.delenv("SYNCSKETCH_API_KEY", raising=False)
    assert ss.load_secret() == ("file@x.com", "FILEKEY")


def test_load_secret_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "CACHED_SYNCSKETCH", str(tmp_path / "nope.json"))
    monkeypatch.delenv("SYNCSKETCH_LOGIN", raising=False)
    monkeypatch.delenv("SYNCSKETCH_API_KEY", raising=False)
    assert ss.load_secret() is None


def test_review_url_from_uuid():
    assert ss.review_url({"uuid": "abc123"}) == "https://syncsketch.com/sketch/abc123/"
    assert ss.review_url({}) == ""


def test_pending_uploads_filters_already_uploaded():
    task = tasks.new_task("asset", "characters/panda", "model")
    task["publishes"] = [
        {"turntable": "07_dailies/a.mp4"},                       # needs upload
        {"turntable": "07_dailies/b.mp4", "syncsketch_url": "x"},  # already done
        {"files": ["x.blend"]},                                  # no turntable
    ]
    pending = ss.pending_uploads([task])
    assert len(pending) == 1
    assert pending[0][1]["turntable"] == "07_dailies/a.mp4"


def test_upload_daily_uses_client_and_resolves_review(monkeypatch):
    """upload_daily finds/creates the review, uploads, returns the review URL —
    with the SDK client fully mocked."""
    calls = {}

    class FakeClient:
        def __init__(self, login, api_key):
            calls["auth"] = (login, api_key)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def find_or_create_review(self, project_id, name):
            calls["review_name"] = name
            calls["project_id"] = project_id
            return {"id": 99, "uuid": "rev-uuid", "name": name}

        def add_media(self, review_id, filepath, item_name, artist_name):
            calls["media"] = (review_id, filepath, item_name, artist_name)
            return {"id": 7}

    monkeypatch.setattr(ss, "SyncSketchClient", FakeClient)
    monkeypatch.setattr(ss, "load_secret", lambda: ("svc@x.com", "KEY"))

    settings = ss.SyncSketchSettings.from_project_settings(
        {"syncsketch": {"enabled": True, "project_id": 42}})
    task = tasks.new_task("asset", "characters/panda", "model")
    url = ss.upload_daily(settings, project_name="Legami",
                          video_local="/tmp/panda_model_v003_turntable.mp4",
                          task=task, version_label="panda_model_v003",
                          username="marco")
    assert url == "https://syncsketch.com/sketch/rev-uuid/"
    assert calls["review_name"] == "Legami — model"
    assert calls["project_id"] == 42
    assert calls["media"][0] == 99 and calls["media"][3] == "marco"


def test_upload_daily_skips_when_not_configured():
    settings = ss.SyncSketchSettings()  # disabled
    assert ss.upload_daily(settings, project_name="L", video_local="/tmp/x.mp4",
                           task={}, version_label="x", username="m") is None


def test_upload_daily_dry_run_does_not_touch_sdk(monkeypatch):
    def boom():
        raise AssertionError("load_secret must not be called in dry-run")
    monkeypatch.setattr(ss, "load_secret", boom)
    settings = ss.SyncSketchSettings.from_project_settings(
        {"syncsketch": {"enabled": True, "project_id": 42}})
    assert ss.upload_daily(settings, project_name="L", video_local="/tmp/x.mp4",
                           task=tasks.new_task("asset", "a", "model"),
                           version_label="x", username="m", dry_run=True) is None


def test_try_upload_daily_swallows_errors(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(ss, "upload_daily", boom)
    settings = ss.SyncSketchSettings.from_project_settings(
        {"syncsketch": {"enabled": True, "project_id": 42}})
    assert ss.try_upload_daily(settings, project_name="L", video_local="/tmp/x.mp4",
                               task={}, version_label="x", username="m") is None
    assert "SyncSketch upload skipped" in capsys.readouterr().out


def test_record_review_url_attaches_to_last_publish():
    s = FakeSrv()
    t = tasks.save_task(s, "/r", tasks.new_task("asset", "characters/panda", "model"))
    tasks.publish_task(s, "/r", "marco", ["/tmp/panda_model_v001.blend"], t["id"])
    ss.record_review_url(s, "/r", t["id"], "https://syncsketch.com/sketch/u/", "marco")
    reloaded = tasks.get_task(s, "/r", t["id"])
    assert reloaded["publishes"][-1]["syncsketch_url"] == "https://syncsketch.com/sketch/u/"
