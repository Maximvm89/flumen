import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# menu_spec is bpy-free by design — load it directly, without importing the
# addon package (whose __init__ needs bpy).
_spec = importlib.util.spec_from_file_location(
    "menu_spec", ROOT / "blender_addon" / "flumen_pipeline" / "menu_spec.py")
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)


def _ops(entries):
    return [e["op"] for e in entries]


def _task(ttype="asset", step="model", entity="characters/panda"):
    return {"type": ttype, "step": step, "entity": entity}


def test_ctx_from_task_and_no_task():
    ctx = M.task_ctx(_task("asset", "dressing", "environments/disco"))
    assert ctx == {"task": True, "type": "asset", "step": "dressing",
                   "category": "environments"}
    assert M.task_ctx(None) == {"task": False, "type": "", "step": "",
                                "category": ""}


def test_no_task_shows_only_general_tools():
    ops = _ops(M.resolve_menu(M.task_ctx(None)))
    assert "flumen.publish" not in ops
    assert "flumen.save_to_task" not in ops
    # asset tools show outside a shot (matches the old hardcoded menu)
    assert "flumen.add_publish_locator" in ops
    assert "flumen.apply_project_settings" in ops
    assert "flumen.show_log" in ops


def test_model_task_menu():
    ops = _ops(M.resolve_menu(M.task_ctx(_task("asset", "model"))))
    assert "flumen.publish" in ops and "flumen.run_checks" in ops
    assert "flumen.apply_look" not in ops          # not on model
    assert "flumen.load_model" not in ops          # surface/rig only
    assert "flumen.build_dressing" not in ops
    assert "flumen.add_publish_locator" in ops


def test_dressing_task_menu():
    ops = _ops(M.resolve_menu(
        M.task_ctx(_task("asset", "dressing", "environments/disco"))))
    assert "flumen.build_dressing" in ops and "flumen.add_prop" in ops
    assert "flumen.apply_look" in ops              # asset, not model
    assert "flumen.load_model" not in ops


def test_shot_layout_menu_hides_asset_tools():
    ops = _ops(M.resolve_menu(M.task_ctx(_task("shot", "layout", "sq010/sh010"))))
    assert "flumen.build_shot" in ops and "flumen.load_animation" in ops
    assert "flumen.add_publish_locator" not in ops
    assert "flumen.preview_turntable" not in ops
    ops_anim = _ops(M.resolve_menu(M.task_ctx(_task("shot", "animation"))))
    assert "flumen.build_shot" not in ops_anim     # layout only
    assert "flumen.load_animation" in ops_anim


def test_settings_hide_removes_action():
    settings = {"menu": {"hide": ["flumen.preview_turntable"]}}
    ops = _ops(M.resolve_menu(M.task_ctx(_task()), settings))
    assert "flumen.preview_turntable" not in ops
    assert "flumen.add_publish_locator" in ops


def test_settings_when_overrides_gate():
    # Re-gate the review camera to dressing tasks only.
    settings = {"menu": {"when": {
        "flumen.add_review_camera": {"task": True, "step": ["dressing"]}}}}
    on_model = _ops(M.resolve_menu(M.task_ctx(_task(step="model")), settings))
    on_dress = _ops(M.resolve_menu(M.task_ctx(_task(step="dressing")), settings))
    assert "flumen.add_review_camera" not in on_model
    assert "flumen.add_review_camera" in on_dress


def test_unknown_ops_in_config_are_ignored():
    settings = {"menu": {"hide": ["flumen.nope"],
                         "when": {"flumen.also_nope": {}}}}
    assert _ops(M.resolve_menu(M.task_ctx(_task()), settings)) == \
        _ops(M.resolve_menu(M.task_ctx(_task())))


def test_category_gate():
    when = {"task": True, "category": ["environments"]}
    assert M.matches(when, M.task_ctx(_task(entity="environments/disco")))
    assert not M.matches(when, M.task_ctx(_task(entity="characters/panda")))
