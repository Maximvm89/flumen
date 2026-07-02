"""Dailies email notifications: config load, mail body content, gating, and the
record_* hooks announcing without ever breaking the publish path."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flumen import notify, tasks, turntable
from test_tasks import FakeSrv

REMOTE = "/shared/Show"


def _cfg(srv, enabled=True, recipients=("sup@studio.com",)):
    srv.write_text(REMOTE + "/02_pipeline/notifications.json", json.dumps({
        "dailies_email": {"enabled": enabled, "recipients": list(recipients),
                          "smtp": {"host": "smtp.studio.com"}}}))


def test_load_notify_config_missing_or_broken_is_empty():
    srv = FakeSrv()
    assert notify.load_notify_config(srv, REMOTE) == {}
    srv.write_text(REMOTE + "/02_pipeline/notifications.json", "{not json")
    assert notify.load_notify_config(srv, REMOTE) == {}


def test_dailies_email_carries_all_info():
    task = {"id": "asset-characters_panda-model", "entity": "characters/panda",
            "step": "model", "type": "asset", "status": "review"}
    rec = {"description": "tighter silhouette", "review_status": "to_review",
           "files": ["03_assets/characters/panda/model/publish/panda_model_v010.blend",
                     "03_assets/characters/panda/model/publish/panda_model_v010.fbx"]}
    media = ["07_dailies/characters/panda/model/panda_model_v010_turntable.mp4"]
    subject, body = notify.dailies_email(task, rec, media, REMOTE + "/", "leo")
    assert subject == ("[Flumen] Dailies: characters/panda · model · "
                       "panda_model_v010 — by leo")
    # every piece of info lands in the body, with full FTP paths
    for expected in [
        "characters/panda", "model", "asset",
        "asset-characters_panda-model", "review",
        "tighter silhouette", "to_review", "leo",
        REMOTE + "/07_dailies/characters/panda/model/panda_model_v010_turntable.mp4",
        REMOTE + "/03_assets/characters/panda/model/publish/panda_model_v010.blend",
        REMOTE + "/03_assets/characters/panda/model/publish/panda_model_v010.fbx",
    ]:
        assert expected in body, f"missing from body: {expected}"


def test_announce_gated_on_enabled_and_recipients(monkeypatch):
    srv = FakeSrv()
    sent = []
    monkeypatch.setattr(notify, "send_email",
                        lambda smtp, to, subj, body: sent.append(to) or True)
    task = {"entity": "e", "step": "s"}
    # no config at all
    assert notify.announce_dailies(srv, REMOTE, task, {}, ["x.mp4"], "leo") is False
    # disabled
    _cfg(srv, enabled=False)
    assert notify.announce_dailies(srv, REMOTE, task, {}, ["x.mp4"], "leo") is False
    # enabled but nobody listed
    _cfg(srv, recipients=())
    assert notify.announce_dailies(srv, REMOTE, task, {}, ["x.mp4"], "leo") is False
    assert sent == []
    # enabled + recipients -> sends
    _cfg(srv)
    assert notify.announce_dailies(srv, REMOTE, task, {}, ["x.mp4"], "leo") is True
    assert sent == [["sup@studio.com"]]


def test_record_turntable_announces(monkeypatch):
    srv = FakeSrv()
    _cfg(srv)
    calls = []
    monkeypatch.setattr(notify, "send_email",
                        lambda smtp, to, subj, body: calls.append((subj, body)) or True)
    t = tasks.save_task(srv, REMOTE, tasks.new_task("asset", "characters/panda", "model"))
    tasks.publish_task(srv, REMOTE, "leo", ["/tmp/panda_model_v001.blend"], t["id"],
                       description="first pass")
    rel = "07_dailies/characters/panda/model/panda_model_v001_turntable.mp4"
    turntable.record_turntable(srv, REMOTE, t["id"], rel, "leo")
    assert len(calls) == 1
    subj, body = calls[0]
    assert "panda_model_v001" in subj and "leo" in subj
    assert REMOTE + "/" + rel in body
    assert "first pass" in body                       # publish description included
    assert "panda_model_v001.blend" in body           # source file included


def test_record_review_media_announces_and_failures_never_raise(monkeypatch):
    srv = FakeSrv()
    _cfg(srv)
    t = tasks.save_task(srv, REMOTE, tasks.new_task("asset", "characters/panda", "surface"))
    blend = "03_assets/characters/panda/surface/publish/panda_surface_default_v001.blend"
    t["publishes"] = [{"files": [blend], "time": 1}]
    tasks.save_task(srv, REMOTE, t)

    # a blown-up mailer must not break the record path
    def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(notify, "send_email", boom)
    ok = turntable.record_review_media(
        srv, REMOTE, t["id"], blend, "leo",
        turntable="07_dailies/characters/panda/surface/x_turntable.mp4",
        sheet="07_dailies/characters/panda/surface/x_textures.png")
    assert ok is True                                  # record still landed
    reloaded = tasks.get_task(srv, REMOTE, t["id"])
    assert reloaded["publishes"][0]["turntable"].endswith("_turntable.mp4")
