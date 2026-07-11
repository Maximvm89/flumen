"""Set-dressing scene model (bpy-free, unit-testable).

A dressing scene contains:
  * one environment holder collection `environment__<leaf>` — the linked+overridden
    environment publish, stamped with flumen_env_asset/_step/_blend_rel;
  * one EMPTY `prop_root__<pid>` per placed prop — a plain LOCAL object carrying
    the prop's provenance (flumen_prop_* custom props) and its world transform.
    The prop's published collection is linked+overridden under a `prop__<pid>`
    holder and parented to the empty, so artists move the EMPTY and the transform
    never lives on override data (which cannot be captured reliably).

The publisher turns these into the instance manifest (see flumen/dressing.py —
duplicated minimal helpers here because the add-on cannot import flumen).
"""

from __future__ import annotations

import re

PROP_ROOT_PREFIX = "prop_root__"
PROP_HOLDER_PREFIX = "prop__"
ENV_HOLDER_PREFIX = "environment__"


def normalize_dressing_name(name: str) -> str:
    """Same slug rules as flumen.dressing (and look names)."""
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return slug or "default"


def prop_id_for(seed: str, existing_ids) -> str:
    """A unique prop id from a seed (asset leaf name): 'lantern', 'lantern_2', …"""
    base = re.sub(r"[^a-z0-9_]+", "_", (seed or "prop").strip().lower()).strip("_") \
        or "prop"
    if base not in existing_ids:
        return base
    n = 2
    while f"{base}_{n}" in existing_ids:
        n += 1
    return f"{base}_{n}"


def matrix_to_rows(mat) -> list[list[float]]:
    """4x4 duck-typed matrix -> nested rounded lists (same as flumen.dressing)."""
    return [[round(float(v), 6) for v in row] for row in mat]


def rel_from_local(path: str, project_root: str) -> str:
    """A local mirror path -> project-relative posix path ('' if outside the
    mirror). Tolerates Windows separators on either side."""
    p = (path or "").replace("\\", "/")
    root = (project_root or "").replace("\\", "/").rstrip("/")
    if not (p and root) or not p.startswith(root + "/"):
        return ""
    return p[len(root) + 1:]


def collect_prop_instances(objects) -> list[dict]:
    """Manifest 'props' entries from the scene's prop_root__* empties. Duck-typed:
    each object exposes .name, .matrix_world and dict-style .get for custom props."""
    out = []
    for o in objects:
        name = getattr(o, "name", "")
        if not name.startswith(PROP_ROOT_PREFIX):
            continue
        pid = o.get("flumen_prop_id") or name[len(PROP_ROOT_PREFIX):]
        out.append({
            "id": pid,
            "asset": o.get("flumen_prop_asset", ""),
            "source_step": o.get("flumen_prop_step", "model"),
            "blend_rel": o.get("flumen_prop_blend_rel", ""),
            "collection": o.get("flumen_prop_collection", ""),
            "object": name,
            "matrix_world": matrix_to_rows(o.matrix_world),
        })
    out.sort(key=lambda p: p["id"])
    return out


def collect_environment(collections) -> dict | None:
    """The manifest 'environment' block from the environment__* holder, or None.
    Duck-typed: each collection exposes .name and dict-style .get."""
    for c in collections:
        if getattr(c, "name", "").startswith(ENV_HOLDER_PREFIX):
            return {"asset": c.get("flumen_env_asset", ""),
                    "source_step": c.get("flumen_env_step", "model"),
                    "blend_rel": c.get("flumen_env_blend_rel", "")}
    return None


# Renderable object types collected as local dressing extras. Lights, cameras
# and bare empties stay personal to the artist's scene (empties that parent
# extras geometry ride along via the parent chain at link time).
EXTRAS_TYPES = frozenset({"MESH", "CURVE", "SURFACE", "META", "FONT",
                          "CURVES", "POINTCLOUD", "VOLUME", "GPENCIL"})


def collect_local_extras(objects) -> list:
    """Objects the artist modeled DIRECTLY in the dressing scene — local
    renderable geometry (not linked, not overrides, not managed empties).
    These become part of the dressing: the publisher gathers them into an
    extras collection inside the published .blend and Build shot links it.
    Duck-typed: .type, .name, .library, .override_library, .data."""
    out = []
    for o in objects:
        if getattr(o, "type", "") not in EXTRAS_TYPES:
            continue
        if (getattr(o, "library", None) is not None
                or getattr(o, "override_library", None) is not None
                or getattr(getattr(o, "data", None), "library", None) is not None):
            continue    # linked environment / prop content
        if getattr(o, "name", "").startswith(PROP_ROOT_PREFIX):
            continue
        out.append(o)
    out.sort(key=lambda o: getattr(o, "name", ""))
    return out


def unmanaged_prop_holders(collections, objects) -> list[str]:
    """prop__* holder names that have NO matching prop_root__* empty — typically a
    hand-duplicated override the manifest cannot capture. Publisher WARNs on these."""
    roots = {getattr(o, "name", "")[len(PROP_ROOT_PREFIX):]
             for o in objects if getattr(o, "name", "").startswith(PROP_ROOT_PREFIX)}
    out = []
    for c in collections:
        name = getattr(c, "name", "")
        if name.startswith(PROP_HOLDER_PREFIX) \
                and name[len(PROP_HOLDER_PREFIX):] not in roots:
            out.append(name)
    return sorted(out)
