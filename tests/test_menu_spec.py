import importlib.util
import json
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
    return [e["op"] for e in entries if not e.get("sep")]


def _shape(entries):
    return [("|" if e.get("sep") else e["op"]) for e in entries]


def _task(ttype="asset", step="model", entity="characters/panda"):
    return {"type": ttype, "step": step, "entity": entity}


def test_context_keys_most_specific_first():
    assert M.context_keys(M.task_ctx(None)) == ["no_task"]
    assert M.context_keys(M.task_ctx(_task("asset", "model",
                                           "environments/disco"))) == \
        ["asset:model:environments", "asset:model", "asset:*"]
    assert M.context_keys(M.task_ctx(_task("shot", "layout", "sq010/sh010"))) == \
        ["shot:layout", "shot:*"]


def test_no_task_menu_is_general_tools_only():
    ops = _ops(M.resolve_menu(M.task_ctx(None)))
    assert "flumen.publish" not in ops
    assert "flumen.add_publish_locator" not in ops   # asset tools need a task
    assert "flumen.preview_turntable" not in ops
    assert ops[0] == "flumen.apply_project_settings"
    assert "flumen.show_log" in ops


def test_order_and_separators_preserved():
    cfg = {"menus": {"asset:model": [
        "flumen.publish", "---", "flumen.show_log"]}}
    shape = _shape(M.resolve_menu(M.task_ctx(_task()), cfg))
    assert shape == ["flumen.publish", "|", "flumen.show_log"]


def test_separators_collapsed_at_edges_and_doubles():
    cfg = {"menus": {"asset:model": [
        "---", "flumen.publish", "---", "---", "flumen.show_log", "---"]}}
    shape = _shape(M.resolve_menu(M.task_ctx(_task()), cfg))
    assert shape == ["flumen.publish", "|", "flumen.show_log"]


def test_unknown_ops_skipped():
    cfg = {"menus": {"asset:model": ["flumen.nope", "flumen.publish"]}}
    assert _ops(M.resolve_menu(M.task_ctx(_task()), cfg)) == ["flumen.publish"]


def test_category_variant_wins_over_step():
    ctx_env = M.task_ctx(_task("asset", "model", "environments/disco"))
    ctx_char = M.task_ctx(_task("asset", "model", "characters/panda"))
    # defaults: environments model has no turntable preview, characters does
    assert "flumen.preview_turntable" not in _ops(M.resolve_menu(ctx_env))
    assert "flumen.preview_turntable" in _ops(M.resolve_menu(ctx_char))
    # a config key for the category variant overrides only that variant
    cfg = {"menus": {"asset:model:environments": ["flumen.publish"]}}
    assert _ops(M.resolve_menu(ctx_env, cfg)) == ["flumen.publish"]
    assert "flumen.run_checks" in _ops(M.resolve_menu(ctx_char, cfg))


def test_wildcard_fallback_for_unlisted_steps():
    ops = _ops(M.resolve_menu(M.task_ctx(_task("asset", "groom"))))
    assert "flumen.publish" in ops                 # asset:* generic menu
    ops_shot = _ops(M.resolve_menu(M.task_ctx(_task("shot", "lighting"))))
    assert "flumen.build_shot" in ops_shot         # shot:* covers every step


def test_dressing_menu():
    ops = _ops(M.resolve_menu(
        M.task_ctx(_task("asset", "dressing", "environments/disco"))))
    assert ops[0] == "flumen.build_dressing" and ops[1] == "flumen.add_prop"
    assert "flumen.apply_look" not in ops
    assert "flumen.add_publish_locator" not in ops
    assert "flumen.preview_turntable" not in ops


def test_every_action_in_registry_and_labels_stay_in_code():
    for menu in M.DEFAULT_MENUS.values():
        for item in menu:
            assert item == M.SEPARATOR or item in M.ACTIONS, item
    e = next(x for x in M.resolve_menu(M.task_ctx(_task("asset", "dressing",
                                                        "environments/disco")))
             if x.get("op") == "flumen.build_dressing")
    assert e["text"] == "Load environment" and e["icon"] == "WORLD"


def test_shipped_menu_json_reproduces_defaults():
    # pipeline_config/menu.json spells out every context explicitly — published
    # unedited it must not change the menu anywhere (shape AND order).
    cfg = json.load(open(ROOT / "pipeline_config" / "menu.json"))
    for task in (None,
                 _task("asset", "model"), _task("asset", "surface"),
                 _task("asset", "rig"),
                 _task("asset", "model", "environments/disco"),
                 _task("asset", "surface", "environments/disco"),
                 _task("asset", "dressing", "environments/disco"),
                 _task("shot", "layout", "sq010/sh010"),
                 _task("shot", "animation", "sq010/sh010"),
                 _task("shot", "lighting", "sq010/sh010"),
                 _task("shot", "comp", "sq010/sh010")):
        ctx = M.task_ctx(task)
        assert _shape(M.resolve_menu(ctx, cfg)) == \
            _shape(M.resolve_menu(ctx)), ctx


def test_matches_kept_for_panel_polls():
    gate = {"type_not": ["shot"], "category_not": ["environments"]}
    assert M.matches(gate, M.task_ctx(_task()))
    assert not M.matches(gate, M.task_ctx(_task("shot", "layout")))
    assert not M.matches(gate, M.task_ctx(_task(entity="environments/disco")))
