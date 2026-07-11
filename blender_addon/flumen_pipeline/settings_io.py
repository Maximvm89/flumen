"""Pure (no-bpy) helpers for locating and loading project_settings.json.

Kept free of `bpy` so it can be unit-tested outside Blender.
"""

import json
import os

SETTINGS_REL = os.path.join("02_pipeline", "project_settings.json")
MENU_REL = os.path.join("02_pipeline", "menu.json")
OCIO_REL = os.path.join("02_pipeline", "ocio", "config.ocio")


def find_project_root(pref_local_root: str | None = None) -> str | None:
    """Resolve the local project root. Priority:
    1. FLUMEN_PROJECT_ROOT env var (set by the launcher)
    2. the addon preference, if provided
    """
    env = os.environ.get("FLUMEN_PROJECT_ROOT")
    if env and os.path.isdir(env):
        return env
    if pref_local_root:
        p = os.path.expanduser(pref_local_root)
        if os.path.isdir(p):
            return p
    # Last resort: env path even if it doesn't exist yet, so callers can report it.
    return env or (os.path.expanduser(pref_local_root) if pref_local_root else None)


def settings_path(root: str) -> str:
    return os.path.join(root, SETTINGS_REL)


def ocio_path(root: str) -> str:
    return os.path.join(root, OCIO_REL)


def menu_path(root: str) -> str:
    return os.path.join(root, MENU_REL)


def load_menu(root: str) -> dict:
    """The project's Flumen-menu config (02_pipeline/menu.json), or {} when the
    project hasn't customized the menu — missing/invalid means built-in
    defaults, never an error."""
    try:
        with open(menu_path(root), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_settings(root: str) -> dict:
    """Load and lightly validate project_settings.json. Raises on missing/invalid."""
    path = settings_path(root)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"project_settings.json not found at {path}. Sync the project first "
            f"(launcher does this automatically)."
        )
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("project_settings.json must be a JSON object")
    return data


def get(data: dict, dotted: str, default=None):
    """Safe nested lookup: get(data, 'render.fps', 24)."""
    cur = data
    for key in dotted.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur
