"""Declarative spec for the Flumen menu — context-first.

No `bpy` — pure data + resolution, unit-testable outside Blender. The menu in
ui.py draws whatever resolve_menu() returns.

Every context (task type + step, optionally asset category) has an ORDERED
list of actions; "---" draws a separator. Projects control the whole thing —
content, order, separators — via 02_pipeline/menu.json (source:
pipeline_config/menu.json, applied with `flumen publish-config`):

  {
    "menus": {
      "no_task":       ["flumen.add_publish_locator", "---", "flumen.show_log"],
      "asset:model":   ["flumen.save_to_task", "flumen.publish", "---", ...],
      "asset:model:environments": [...],     // category-specific variant
      "shot:layout":   [...],
      "shot:*":        [...]                 // any shot step without its own list
    }
  }

Context keys, most specific wins:
  "<type>:<step>:<category>"  e.g. "asset:model:environments"
  "<type>:<step>"             e.g. "shot:layout"
  "<type>:*"                  any step of that type
  "no_task"                   Blender opened without a task

A context key present in menu.json fully replaces the built-in list for that
context; contexts not in the file fall back to the built-in defaults below.
Only operators known to the add-on (the ACTIONS registry) are honored —
unknown names are skipped, so a typo can never break the menu. Labels and
icons live here, not in the config.
"""

SEPARATOR = "---"

# Every operator the menu may show: op -> {text (optional; default = the
# operator's own label), icon}.
ACTIONS = {
    "flumen.load_model": {"icon": "IMPORT"},
    "flumen.build_dressing": {"text": "Load environment", "icon": "WORLD"},
    "flumen.add_prop": {"text": "Add prop…",
                        "icon": "OUTLINER_OB_GROUP_INSTANCE"},
    "flumen.apply_look": {"text": "Apply look…", "icon": "MATERIAL"},
    "flumen.build_shot": {"text": "Build shot",
                          "icon": "OUTLINER_OB_GROUP_INSTANCE"},
    "flumen.load_animation": {"text": "Load animation…", "icon": "ANIM_DATA"},
    "flumen.cache_shot": {"text": "Cache shot (Alembic)…", "icon": "FILE_CACHE"},
    "flumen.add_lights": {"text": "Add LIGHTS collection", "icon": "OUTLINER_COLLECTION"},
    "flumen.load_lights": {"text": "Load lights from another shot…",
                           "icon": "LIGHT"},
    "flumen.publish_lights": {"text": "Publish lights", "icon": "OUTLINER_OB_LIGHT"},
    "flumen.cycle_format": {"text": "Preview format (16:9 ⇄ 9:16)",
                            "icon": "ARROW_LEFTRIGHT"},
    "flumen.preview_playblast": {"text": "Preview playblast",
                                 "icon": "PLAY"},
    "flumen.render_turntable": {"text": "Render turntable",
                                "icon": "RENDER_ANIMATION"},
    "flumen.add_review_camera": {"icon": "VIEW_CAMERA"},
    "flumen.render_review": {"icon": "RENDER_STILL"},
    "flumen.save_to_task": {"icon": "FILE_TICK"},
    "flumen.run_checks": {"icon": "CHECKMARK"},
    "flumen.auto_fix": {"icon": "TOOL_SETTINGS"},
    "flumen.publish": {"text": "Publish…", "icon": "EXPORT"},
    "flumen.add_publish_locator": {"icon": "EMPTY_AXIS"},
    "flumen.add_publish_collection": {"icon": "OUTLINER_COLLECTION"},
    "flumen.preview_turntable": {"icon": "CAMERA_DATA"},
    "flumen.apply_project_settings": {"icon": "CHECKMARK"},
    "flumen.verify_ocio": {"icon": "COLOR"},
    "flumen.pull_settings": {"icon": "IMPORT"},
    "flumen.test_connection": {"icon": "URL"},
    "flumen.show_log": {"icon": "TEXT"},
}

# pull_settings stays in ACTIONS (a project can re-add it via menu.json) but is
# not in any default menu — config refreshes at every launch anyway.
_TAIL = [SEPARATOR, "flumen.apply_project_settings", "flumen.verify_ocio",
         SEPARATOR, "flumen.test_connection", "flumen.show_log"]
_TASK_CORE = ["flumen.add_review_camera", "flumen.render_review",
              "flumen.save_to_task", "flumen.run_checks", "flumen.auto_fix",
              "flumen.publish"]
_ASSET_TOOLS = [SEPARATOR, "flumen.add_publish_locator",
                "flumen.preview_turntable"]

DEFAULT_MENUS = {
    # No task: only the general project tools (asset tools live on asset steps).
    "no_task": _TAIL[1:],
    # Assets. Environments variants: no turntable (envs never render one).
    "asset:model": ["flumen.apply_look"] + _TASK_CORE + _ASSET_TOOLS + _TAIL,
    "asset:model:environments": (_TASK_CORE
                                 + [SEPARATOR, "flumen.add_publish_collection"]
                                 + _TAIL),
    "asset:surface": (["flumen.load_model", "flumen.apply_look",
                       "flumen.render_turntable", SEPARATOR]
                      + _TASK_CORE + _ASSET_TOOLS + _TAIL),
    "asset:rig": (["flumen.load_model", "flumen.apply_look", SEPARATOR]
                  + _TASK_CORE + _ASSET_TOOLS + _TAIL),
    # Dressing scenes are linked content: no locator, no turntable, no looks.
    "asset:dressing": (["flumen.build_dressing", "flumen.add_prop", SEPARATOR]
                       + _TASK_CORE + _TAIL),
    # Unlisted asset steps (a project may add its own): generic asset menu.
    "asset:*": (["flumen.apply_look", SEPARATOR]
                + _TASK_CORE + _ASSET_TOOLS + _TAIL),
    # Shots: Build shot resolves per step (rigs now, caches when they land).
    "shot:*": (["flumen.build_shot", "flumen.load_animation",
                "flumen.cycle_format", "flumen.preview_playblast", SEPARATOR]
               + _TASK_CORE + _TAIL),
}


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


def context_keys(ctx: dict) -> list[str]:
    """Menu keys to try for `ctx`, most specific first."""
    if not ctx.get("task"):
        return ["no_task"]
    ttype, step = ctx.get("type", ""), ctx.get("step", "")
    keys = []
    if ctx.get("category"):
        keys.append(f"{ttype}:{step}:{ctx['category']}")
    keys.append(f"{ttype}:{step}")
    keys.append(f"{ttype}:*")
    return keys


def _menu_list(ctx: dict, menu_cfg: dict | None) -> list[str]:
    keys = context_keys(ctx)
    cfg_menus = (menu_cfg or {}).get("menus")
    if isinstance(cfg_menus, dict):
        for key in keys:
            if isinstance(cfg_menus.get(key), list):
                return cfg_menus[key]
    for key in keys:
        if key in DEFAULT_MENUS:
            return DEFAULT_MENUS[key]
    return []


def resolve_menu(ctx: dict, menu_cfg: dict | None = None) -> list[dict]:
    """The entries to draw for `ctx`, in order: {"op", "text", "icon"} for an
    action, {"sep": True} for a separator. Unknown ops are skipped; leading,
    trailing and doubled separators are collapsed."""
    out = []
    for item in _menu_list(ctx, menu_cfg):
        if item == SEPARATOR:
            if out and not out[-1].get("sep"):
                out.append({"sep": True})
            continue
        spec = ACTIONS.get(item)
        if spec is None:
            continue    # unknown operator (typo / other version) — skip
        out.append({"op": item, "text": spec.get("text", ""),
                    "icon": spec.get("icon", "")})
    while out and out[-1].get("sep"):
        out.pop()
    return out


def matches(when: dict, ctx: dict) -> bool:
    """Gate matcher (kept for panel polls): task/type/step/category with _not
    variants, as in the previous gate-based config."""
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
