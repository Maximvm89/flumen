"""Declarative spec for the Flumen menu: which action shows in which context.

No `bpy` — pure data + matching, unit-testable outside Blender. The menu in
ui.py draws whatever resolve_menu() returns.

Each entry: {op, group, when, [text], [icon]}. `when` is the context gate:
  task      True -> only with an active task
  type      / type_not      task type is/isn't in the list ("asset", "shot")
  step      / step_not      task step is/isn't in the list
  category  / category_not  asset category (top folder: "characters",
                            "environments", …) is/isn't in the list
Omitted keys don't constrain. Groups render with a separator between them.

Projects can tune this WITHOUT a release via 02_pipeline/menu.json
(source: pipeline_config/menu.json, applied with `flumen publish-config`):

  {
    "hide": ["flumen.preview_turntable"],
    "when": {"flumen.add_review_camera": {"task": true, "step": ["model"]}}
  }

`hide` removes actions entirely; `when` replaces an action's default gate.
Unknown operator names in the config are ignored (typos can't add actions —
the action set itself stays in code, on purpose).
"""

DEFAULT_MENU = [
    # -- task tools (need an active task) -------------------------------------
    {"op": "flumen.load_model", "icon": "IMPORT", "group": "task",
     "when": {"task": True, "type": ["asset"], "step": ["surface", "rig"]}},
    {"op": "flumen.build_dressing", "text": "Load environment", "icon": "WORLD",
     "group": "task", "when": {"task": True, "step": ["dressing"]}},
    {"op": "flumen.add_prop", "text": "Add prop…",
     "icon": "OUTLINER_OB_GROUP_INSTANCE", "group": "task",
     "when": {"task": True, "step": ["dressing"]}},
    {"op": "flumen.apply_look", "text": "Apply look…", "icon": "MATERIAL",
     "group": "task",
     "when": {"task": True, "type": ["asset"], "step_not": ["model"]}},
    {"op": "flumen.build_shot", "text": "Build shot",
     "icon": "OUTLINER_OB_GROUP_INSTANCE", "group": "task",
     "when": {"task": True, "type": ["shot"], "step": ["layout"]}},
    {"op": "flumen.load_animation", "text": "Load animation…",
     "icon": "ANIM_DATA", "group": "task",
     "when": {"task": True, "type": ["shot"]}},
    {"op": "flumen.add_review_camera", "icon": "VIEW_CAMERA", "group": "task",
     "when": {"task": True}},
    {"op": "flumen.render_review", "icon": "RENDER_STILL", "group": "task",
     "when": {"task": True}},
    {"op": "flumen.save_to_task", "icon": "FILE_TICK", "group": "task",
     "when": {"task": True}},
    {"op": "flumen.run_checks", "icon": "CHECKMARK", "group": "task",
     "when": {"task": True}},
    {"op": "flumen.auto_fix", "icon": "TOOL_SETTINGS", "group": "task",
     "when": {"task": True}},
    {"op": "flumen.publish", "text": "Publish…", "icon": "EXPORT",
     "group": "task", "when": {"task": True}},
    # -- asset/modelling tools (hidden in shots) -------------------------------
    {"op": "flumen.add_publish_locator", "icon": "EMPTY_AXIS", "group": "asset",
     "when": {"type_not": ["shot"]}},
    {"op": "flumen.preview_turntable", "icon": "CAMERA_DATA", "group": "asset",
     "when": {"type_not": ["shot"]}},
    # -- project settings -------------------------------------------------------
    {"op": "flumen.apply_project_settings", "icon": "CHECKMARK",
     "group": "project", "when": {}},
    {"op": "flumen.verify_ocio", "icon": "COLOR", "group": "project",
     "when": {}},
    # -- sync / diagnostics ------------------------------------------------------
    {"op": "flumen.pull_settings", "icon": "IMPORT", "group": "sync",
     "when": {}},
    {"op": "flumen.show_log", "icon": "TEXT", "group": "sync", "when": {}},
]


def task_ctx(task: dict | None) -> dict:
    """The matching context for the active task (or the no-task context)."""
    t = task or {}
    entity = t.get("entity", "") or ""
    return {
        "task": bool(task),
        "type": t.get("type", "") or "",
        "step": t.get("step", "") or "",
        "category": (entity.split("/")[0]
                     if t.get("type") == "asset" and "/" in entity else ""),
    }


def matches(when: dict, ctx: dict) -> bool:
    if when.get("task") and not ctx.get("task"):
        return False
    for key in ("type", "step", "category"):
        allowed = when.get(key)
        if allowed and ctx.get(key) not in allowed:
            return False
        blocked = when.get(key + "_not")
        if blocked and ctx.get(key) in blocked:
            return False
    return True


def resolve_menu(ctx: dict, menu_cfg: dict | None = None) -> list[dict]:
    """The menu entries to draw for `ctx`, in order, after applying the
    project's overrides (the 02_pipeline/menu.json dict)."""
    cfg = menu_cfg or {}
    hide = set(cfg.get("hide") or [])
    when_over = cfg.get("when") or {}
    out = []
    for entry in DEFAULT_MENU:
        if entry["op"] in hide:
            continue
        when = when_over.get(entry["op"], entry["when"])
        if isinstance(when, dict) and matches(when, ctx):
            out.append(entry)
    return out
