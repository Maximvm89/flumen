"""Tests for flumen.plan — pure planning math (no server, fixed dates)."""

import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flumen import plan as P

MON = datetime.date(2026, 7, 13)          # a Monday
CFG = P.planning_config({"planning": {"deadline": "2026-09-01"}})


def _t(step, ttype="asset", status="todo", assignees=None, est=None, due=""):
    t = {"id": f"x-{step}", "type": ttype, "entity": "characters/frank",
         "step": step, "status": status, "assignees": assignees or []}
    if est is not None:
        t["estimate_days"] = est
    if due:
        t["due"] = due
    return t


def test_workdays_between_skips_weekends():
    fri = datetime.date(2026, 7, 17)
    mon_next = datetime.date(2026, 7, 20)
    assert P.workdays_between(MON, fri) == 4          # tue..fri
    assert P.workdays_between(fri, mon_next) == 1     # only monday
    assert P.workdays_between(MON, MON) == 0


def test_add_workdays_rounds_up_and_skips_weekends():
    assert P.add_workdays(MON, 1) == datetime.date(2026, 7, 14)
    assert P.add_workdays(MON, 0.5) == datetime.date(2026, 7, 14)
    assert P.add_workdays(MON, 5) == datetime.date(2026, 7, 20)  # over the weekend


def test_estimates_task_beats_step_default():
    assert P.estimate_of(_t("model"), CFG) == 3.0                 # step default
    assert P.estimate_of(_t("model", est=1.5), CFG) == 1.5        # own estimate
    assert P.estimate_of(_t("weird_step"), CFG) == 2.0            # global default


def test_health_states():
    assert P.health(_t("model", status="done"), MON, CFG) == P.HEALTH_DONE
    assert P.health(_t("model"), MON, CFG) == P.HEALTH_UNPLANNED
    assert P.health(_t("model", due="2026-07-10"), MON, CFG) == P.HEALTH_LATE
    assert P.health(_t("model", due="2026-07-15"), MON, CFG) == P.HEALTH_DUE_SOON
    assert P.health(_t("model", due="2026-08-20"), MON, CFG) == P.HEALTH_ON_TRACK


def test_plan_summary_capacity_and_fit():
    roster = [{"username": "a", "active": True},
              {"username": "b", "active": True},
              {"username": "ghost", "active": False}]
    tasks = [_t("model", assignees=["a"], est=4),
             _t("rig", assignees=["a"], est=2),
             _t("layout", ttype="shot", assignees=["b"], est=1),
             _t("surface", est=2),                      # unassigned
             _t("dressing", status="done", est=99)]     # done: ignored
    s = P.plan_summary(tasks, roster, MON, CFG)
    assert s["deadline"] == "2026-09-01"
    assert s["workdays_left"] == P.workdays_between(MON, datetime.date(2026, 9, 1))
    assert s["remaining_days"] == 9.0
    assert s["unassigned_days"] == 2.0
    assert s["per_artist"]["a"]["remaining"] == 6.0
    assert s["per_artist"]["a"]["tasks"] == 2
    assert s["capacity_days"] == 2 * s["workdays_left"]   # two active artists
    assert s["fits"] is True


def test_propose_schedule_pipeline_order_and_warnings():
    tasks = [_t("dressing", assignees=["a"], est=2),
             _t("model", assignees=["a"], est=3),
             _t("surface", assignees=["a"], est=2),
             _t("layout", ttype="shot", est=1)]          # unassigned -> warning
    proposal, warns = P.propose_schedule(tasks, MON, CFG)
    d_model = datetime.date.fromisoformat(proposal["x-model"])
    d_surface = datetime.date.fromisoformat(proposal["x-surface"])
    d_dressing = datetime.date.fromisoformat(proposal["x-dressing"])
    assert d_model < d_surface < d_dressing               # pipeline order
    assert d_model == P.add_workdays(MON, 3)
    assert any("unassigned" in w for w in warns)


def test_propose_schedule_overflow_warns_past_deadline():
    cfg = P.planning_config({"planning": {"deadline": "2026-07-16"}})
    tasks = [_t("model", assignees=["a"], est=10)]
    proposal, warns = P.propose_schedule(tasks, MON, cfg)
    assert proposal["x-model"] > "2026-07-16"
    assert any("past the deadline" in w for w in warns)


def test_availability_scales_pace():
    cfg = P.planning_config({"planning": {"deadline": "2026-09-01",
                                          "availability": {"half": 2.5}}})
    tasks = [_t("model", assignees=["half"], est=2)]
    proposal, _ = P.propose_schedule(tasks, MON, cfg)
    # 2 days of work at half pace -> 4 workdays out
    assert proposal["x-model"] == P.add_workdays(MON, 4).isoformat()


def test_dependencies_cross_artist():
    """A surface by artist b waits for the model by artist a, even when b is idle."""
    tasks = [_t("model", assignees=["a"], est=4),
             {"id": "x-surface", "type": "asset", "entity": "characters/frank",
              "step": "surface", "status": "todo", "assignees": ["b"],
              "estimate_days": 2}]
    proposal, _ = P.propose_schedule(tasks, MON, CFG)
    d_model = datetime.date.fromisoformat(proposal["x-model"])
    d_surface = datetime.date.fromisoformat(proposal["x-surface"])
    assert d_model == P.add_workdays(MON, 4)
    assert d_surface == P.add_workdays(MON, 6)      # starts AFTER the model


def test_dependencies_shot_chain_with_elements():
    def shot(step, tid):
        return {"id": tid, "type": "shot", "entity": "SEQ010/SH0010",
                "step": step, "status": "todo", "assignees": ["anim"],
                "estimate_days": 1}
    rig = {"id": "rig-frank", "type": "asset", "entity": "characters/frank",
           "step": "rig", "status": "todo", "assignees": ["rigger"],
           "estimate_days": 5}
    model = {"id": "model-frank", "type": "asset", "entity": "characters/frank",
             "step": "model", "status": "done", "assignees": ["rigger"]}
    tasks = [model, rig, shot("layout", "s-layout"),
             shot("animation", "s-anim"), shot("lighting", "s-light")]
    elements = {"SEQ010/SH0010": ["characters/frank"]}
    proposal, warns = P.propose_schedule(tasks, MON, CFG, shot_elements=elements)
    d_rig = datetime.date.fromisoformat(proposal["rig-frank"])
    d_layout = datetime.date.fromisoformat(proposal["s-layout"])
    d_anim = datetime.date.fromisoformat(proposal["s-anim"])
    d_light = datetime.date.fromisoformat(proposal["s-light"])
    assert d_rig <= d_layout < d_anim < d_light      # rig gates the whole chain
    assert d_layout == P.add_workdays(MON, 6)        # rig 5d, then layout 1d
    assert not warns                                 # done model doesn't warn


def test_done_prerequisite_does_not_block():
    tasks = [_t("model", status="done", assignees=["a"]),
             _t("surface", assignees=["a"], est=2)]
    proposal, warns = P.propose_schedule(tasks, MON, CFG)
    assert "x-model" not in proposal
    assert proposal["x-surface"] == P.add_workdays(MON, 2).isoformat()
    assert not warns


def test_unscheduled_prerequisite_warns():
    tasks = [_t("model", est=3),                     # unassigned
             _t("surface", assignees=["a"], est=2)]
    proposal, warns = P.propose_schedule(tasks, MON, CFG)
    assert "x-surface" in proposal
    assert any("unassigned (not scheduled)" in w for w in warns)
    assert any("unreliable" in w for w in warns)


def test_sub_workdays_inverse_of_add():
    for n in (1, 3, 7, 10):
        assert P.sub_workdays(P.add_workdays(MON, n), n) == MON


def test_reflow_pushes_dependent_forward():
    """Dragging a model later pushes its surface (kept order, no overlap)."""
    model = _t("model", assignees=["a"], est=3,
               due=P.add_workdays(MON, 10).isoformat())      # dragged late
    surface = {"id": "x-surface", "type": "asset", "entity": "characters/frank",
               "step": "surface", "status": "todo", "assignees": ["b"],
               "estimate_days": 2, "due": P.add_workdays(MON, 5).isoformat()}
    out = P.reflow([model, surface], MON, CFG)
    assert out["x-model"] == P.add_workdays(MON, 10).isoformat()  # kept
    assert out["x-surface"] == P.add_workdays(MON, 12).isoformat()  # pushed


def test_reflow_keeps_untouched_positions():
    a = _t("model", assignees=["a"], est=3, due=P.add_workdays(MON, 3).isoformat())
    b = {"id": "x-rig", "type": "asset", "entity": "characters/frank",
         "step": "rig", "status": "todo", "assignees": ["a"],
         "estimate_days": 2, "due": P.add_workdays(MON, 8).isoformat()}
    out = P.reflow([a, b], MON, CFG)
    assert out["x-model"] == P.add_workdays(MON, 3).isoformat()
    assert out["x-rig"] == P.add_workdays(MON, 8).isoformat()   # gap preserved


def test_reflow_same_artist_overlap_slides():
    """Two tasks dragged onto each other: the later-due one slides after."""
    t1 = _t("model", assignees=["a"], est=3, due=P.add_workdays(MON, 3).isoformat())
    t2 = {"id": "x-model2", "type": "asset", "entity": "characters/orso",
          "step": "model", "status": "todo", "assignees": ["a"],
          "estimate_days": 3, "due": P.add_workdays(MON, 4).isoformat()}
    out = P.reflow([t1, t2], MON, CFG)
    assert out["x-model"] == P.add_workdays(MON, 3).isoformat()
    assert out["x-model2"] == P.add_workdays(MON, 6).isoformat()  # after t1
