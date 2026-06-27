"""Lookdev environment helpers — choosing the HDRI a look review renders under.

Pure path/selection logic, no bpy and no network, so it's unit-testable. The
artist drops professional .exr/.hdr files into 05_library/hdri; a project default
is configured in project_settings, and a publish can override it per look.
"""

from __future__ import annotations

import os

HDRI_DIR_REL = "05_library/hdri"
HDRI_EXTS = (".exr", ".hdr")


def hdri_dir(local_root: str) -> str:
    return os.path.join(local_root, *HDRI_DIR_REL.split("/"))


def list_hdris(local_root: str) -> list[str]:
    """Names of the HDRI files available under 05_library/hdri (sorted). Names, not
    paths, so they're stable to show in a dropdown and store on a publish."""
    d = hdri_dir(local_root)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d)
                  if os.path.splitext(f)[1].lower() in HDRI_EXTS)


def project_default(project_settings: dict) -> str:
    """The configured default HDRI name, or '' if none."""
    return ((project_settings or {}).get("turntable") or {}).get("lookdev_hdri") or ""


def resolve_hdri(project_settings: dict, name: str | None,
                 local_root: str) -> str | None:
    """Absolute path to the HDRI to light a look review with: the explicitly chosen
    `name`, else the project default, else None (caller falls back to a neutral
    world). Only returns a path that actually exists on disk."""
    name = (name or "").strip()
    if name.lower() == "none":          # explicit "neutral" choice — skip the default
        return None
    candidate = name or project_default(project_settings)
    if not candidate:
        return None
    # Accept a bare name (under the hdri dir) or a path relative to local_root.
    for path in (os.path.join(hdri_dir(local_root), candidate),
                 os.path.join(local_root, *candidate.split("/"))):
        if os.path.isfile(path):
            return path
    return None
