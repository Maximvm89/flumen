"""Shot assembly ("elements" / breakdown): the list of assets + camera a shot
contains, shared across all of the shot's steps (layout / animation / lighting /
comp). One assembly.json per shot, stored beside the shot's folders — NOT a task
JSON, so it never collides with 02_pipeline/tasks/.

Each element resolves to a different REPRESENTATION depending on the step you open
the shot in: layout/animation -> the asset's rig (linked + overridden), lighting ->
the alembic cache (a later round). That mapping lives in DEFAULT_REPRESENTATIONS,
overridable from project_settings.json `assembly.representations`.

Pure helpers up top are unit-testable; the sftp I/O mirrors tasks.py exactly
(sftp.read_text / sftp.write_text, where write_text makedirs the parent).
"""

from __future__ import annotations

import json
import re
import time

SEQ_ROOT = "04_sequences"
ASSEMBLY_NAME = "assembly.json"
KINDS = ("asset", "camera")

# Every shot starts at frame 1001; the duration (default 100 frames) is set per
# shot in the Elements editor. End frame = start + duration - 1.
DEFAULT_FRAME_START = 1001
DEFAULT_DURATION = 100

# Default per-step representation map. Overridable via project_settings.json
# "assembly":{"representations":{...}}. Only the `layout` slice is wired in this
# build; the rest define the seam for the lighting/alembic round.
DEFAULT_REPRESENTATIONS = {
    "layout":    {"source_step": "rig", "fallback_step": "model",
                  "load": "link", "apply_look": False},
    "animation": {"source_step": "rig", "fallback_step": "model",
                  "load": "link", "apply_look": False},
    "lighting":  {"source_step": "cache", "fallback_step": "model",
                  "load": "alembic", "apply_look": True},
    "comp":      None,   # comp consumes renders, not scene elements
}

# Which shot step publishes the shot's own camera (the "camera" element resolves
# to this step's newest publish). Overridable via assembly.camera_step.
DEFAULT_CAMERA_STEP = "layout"


# ---- pure: paths & ids -----------------------------------------------------

def assembly_rel(shot_entity: str) -> str:
    """'SEQ010/SH0010' -> '04_sequences/SEQ010/SH0010/assembly.json'."""
    return f"{SEQ_ROOT}/{shot_entity}/{ASSEMBLY_NAME}"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower()).strip("_")


def element_id_for(seed: str, existing_ids) -> str:
    """A unique instance id within a shot. Base = the seed's leaf name; a second
    instance of the same asset gets _1, _2, … ('frankenstein', 'frankenstein_1')."""
    base = _slug((seed or "element").split("/")[-1]) or "element"
    existing = set(existing_ids or [])
    if base not in existing:
        return base
    n = 1
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


# ---- pure: element / assembly construction ---------------------------------

def new_element(asset_entity: str, kind: str = "asset",
                label: str = "", look: str = "") -> dict:
    """A fresh element dict (id is assigned by add_element). A camera element
    carries no asset entity."""
    if kind not in KINDS:
        raise ValueError(f"unknown element kind: {kind}")
    asset = "" if kind == "camera" else (asset_entity or "")
    label = label or (asset.split("/")[-1] if asset else "camera")
    return {"id": "", "kind": kind, "asset": asset,
            "label": label, "look": look or "", "enabled": True}


def empty_assembly(shot_entity: str) -> dict:
    return {"shot": shot_entity, "frame_start": DEFAULT_FRAME_START,
            "duration": DEFAULT_DURATION, "elements": []}


def frame_range(assembly: dict) -> tuple[int, int]:
    """(start, end) frames for a shot. end = start + duration - 1."""
    start = int(assembly.get("frame_start") or DEFAULT_FRAME_START)
    dur = int(assembly.get("duration") or DEFAULT_DURATION)
    return start, start + max(1, dur) - 1


def add_element(assembly: dict, element: dict) -> dict:
    """Append `element`, assigning a unique id. Mutates + returns assembly."""
    els = assembly.setdefault("elements", [])
    seed = element.get("asset") if element.get("kind") != "camera" else "camera"
    element = dict(element)
    element["id"] = element_id_for(seed, {e.get("id") for e in els})
    els.append(element)
    return assembly


def remove_element(assembly: dict, element_id: str) -> dict:
    """Drop the element with this id. Mutates + returns assembly."""
    assembly["elements"] = [e for e in assembly.get("elements", [])
                            if e.get("id") != element_id]
    return assembly


def normalize(assembly: dict, shot_entity: str = "") -> dict:
    """Coerce a loaded/partial doc to the full shape: backfill missing/duplicate
    ids, drop unknown kinds. Pure — never touches the network."""
    out = empty_assembly(assembly.get("shot") or shot_entity)
    fs = assembly.get("frame_start")
    if isinstance(fs, (int, float)) and fs > 0:
        out["frame_start"] = int(fs)
    du = assembly.get("duration")
    if isinstance(du, (int, float)) and du > 0:
        out["duration"] = int(du)
    for key in ("updated", "updated_by"):       # preserve top-level metadata
        if key in assembly:
            out[key] = assembly[key]
    seen: set[str] = set()
    for raw in assembly.get("elements") or []:
        kind = raw.get("kind", "asset")
        if kind not in KINDS:
            continue
        e = {"id": raw.get("id", ""), "kind": kind,
             "asset": "" if kind == "camera" else raw.get("asset", ""),
             "label": raw.get("label", ""), "look": raw.get("look", ""),
             "enabled": bool(raw.get("enabled", True))}
        if not e["id"] or e["id"] in seen:
            # Keep the original id (or asset) as the collision base so a dup
            # becomes '<id>_1', not the asset's leaf name.
            e["id"] = element_id_for(e["id"] or e["asset"] or "camera", seen)
        seen.add(e["id"])
        out["elements"].append(e)
    return out


# ---- pure: resolution config seam ------------------------------------------

def representations(settings: dict | None) -> dict:
    """Effective step->representation map: DEFAULT_REPRESENTATIONS overlaid by
    project_settings 'assembly.representations'."""
    out = {k: (dict(v) if v else None) for k, v in DEFAULT_REPRESENTATIONS.items()}
    override = ((settings or {}).get("assembly") or {}).get("representations") or {}
    for step, spec in override.items():
        out[step] = dict(spec) if spec else None
    return out


def camera_step(settings: dict | None) -> str:
    return ((settings or {}).get("assembly") or {}).get("camera_step") \
        or DEFAULT_CAMERA_STEP


def resolve_element(element: dict, step: str,
                    settings: dict | None = None) -> dict | None:
    """The representation spec for an element at a given shot step, or None if the
    step consumes no scene elements (e.g. comp)."""
    return representations(settings).get(step)


# ---- pure: task id helper --------------------------------------------------

def rig_task_id(asset_entity: str) -> str:
    """Id of the rig task for an asset entity (the sibling of model_task_id)."""
    from . import tasks
    return tasks.make_id("asset", asset_entity, "rig")


# ---- sftp I/O (mirrors tasks.save_task / _load_one) -------------------------

def load_assembly(sftp, remote_root: str, shot_entity: str) -> dict:
    """Read the shot's assembly.json; an empty assembly if the file is absent."""
    rel = assembly_rel(shot_entity)
    txt = sftp.read_text(remote_root.rstrip("/") + "/" + rel)
    if not txt:
        return empty_assembly(shot_entity)
    try:
        return normalize(json.loads(txt), shot_entity)
    except ValueError:
        return empty_assembly(shot_entity)


def save_assembly(sftp, remote_root: str, shot_entity: str,
                  assembly: dict, actor: str = "") -> dict:
    """Write assembly.json (write_text makedirs the parent). Mirrors save_task."""
    doc = normalize(assembly, shot_entity)
    doc["updated"] = time.time()
    if actor:
        doc["updated_by"] = actor
    sftp.write_text(remote_root.rstrip("/") + "/" + assembly_rel(shot_entity),
                    json.dumps(doc, indent=2))
    return doc


def _newest_publish_for_step(sftp, remote_root, asset_entity, step):
    """Newest published .blend rel for an asset's step, or None."""
    from . import tasks
    t = tasks.get_task(sftp, remote_root,
                       tasks.make_id("asset", asset_entity, step))
    pubs = tasks.published_files(t) if t else []
    return pubs[0]["rel"] if pubs else None


def resolved_elements(sftp, remote_root: str, shot_entity: str, step: str,
                      settings: dict | None = None,
                      picks: dict | None = None) -> list[dict]:
    """For each ENABLED element, find the publish to bring in for `step` and return
    [{id,label,kind,asset,blend_rel,source_step,available_steps,look,load,apply_look}].
    `available_steps` are the asset's geometry steps that actually have a published
    .blend (e.g. ['model'] now, ['rig','model'] once a rig publishes); the chosen
    step defaults to the first but can be overridden per element via `picks`
    ({element_id: step}). Elements with no publish are skipped. Drives
    `resolve-assembly`."""
    picks = picks or {}
    from . import tasks
    assembly = load_assembly(sftp, remote_root, shot_entity)
    out: list[dict] = []
    for e in assembly.get("elements", []):
        if not e.get("enabled", True):
            continue
        spec = resolve_element(e, step, settings)
        if spec is None:
            continue

        if e["kind"] == "camera":
            # The shot's OWN camera = a fresh Dolly camera rig named after the shot.
            # Its animation comes back via the published anim Actions (re-applied on
            # Build shot), exactly like the character rigs.
            out.append({"id": e["id"], "label": e["label"], "kind": "camera",
                        "asset": "", "blend_rel": "",
                        "source_step": camera_step(settings), "available_steps": [],
                        "look": "", "load": "create_rig", "apply_look": False,
                        "camera_name": shot_entity.replace("/", "_")})
            continue

        # asset element: the geometry steps with a publish, in preference order
        # (source_step e.g. rig, then fallback model). The chosen step defaults to
        # the first available but can be overridden via picks[id].
        candidates = [spec["source_step"]]
        if spec.get("fallback_step") and spec["fallback_step"] not in candidates:
            candidates.append(spec["fallback_step"])
        rels = {}
        for st in candidates:
            rel = _newest_publish_for_step(sftp, remote_root, e["asset"], st)
            if rel:
                rels[st] = rel
        avail = [st for st in candidates if st in rels]
        if not avail:
            continue
        chosen = picks.get(e["id"])
        if chosen not in rels:
            chosen = avail[0]
        out.append({"id": e["id"], "label": e["label"], "kind": "asset",
                    "asset": e["asset"], "blend_rel": rels[chosen],
                    "source_step": chosen, "available_steps": avail,
                    "look": e.get("look", ""), "load": spec.get("load", "link"),
                    "apply_look": bool(spec.get("apply_look", False))})
    return out


ANIM_BLEND_SUFFIX = "_anim.blend"


def resolved_animation(sftp, remote_root: str, shot_entity: str,
                       step: str) -> dict | None:
    """The animation to re-apply on Build shot, resolved PER ELEMENT to the newest
    published version that actually contains it: {elements: {id: {blend_rel,
    objects:{obj:action}, version}}} or None. Per-element (not just the single latest
    publish) so an element that wasn't re-published in the most recent version — e.g.
    its animation was unchanged — still resolves from the version that has it."""
    anims = published_animations(sftp, remote_root, shot_entity, step)  # newest first
    elements = {}
    for a in anims:
        for eid, objs in (a.get("elements") or {}).items():
            if eid not in elements:
                elements[eid] = {"blend_rel": a["blend_rel"], "objects": objs,
                                 "version": a["version"]}
    return {"elements": elements} if elements else None


_ANIM_VER_RE = re.compile(r"_v(\d+)_anim\.blend$")


def anim_version_label(name: str) -> str:
    """'SH0010_layout_v007_anim.blend' -> 'v007'."""
    import os as _os
    m = _ANIM_VER_RE.search(name or "")
    return f"v{int(m.group(1)):03d}" if m else _os.path.splitext(name or "")[0]


def published_animations(sftp, remote_root: str, shot_entity: str,
                         step: str) -> list[dict]:
    """Every published animation for the shot step (newest first): a list of
    {version, blend_rel, by, description, time, elements:{id:{obj:action}}}. Feeds
    the 'Load animation' picker so the artist can choose a version per element."""
    import json as _json
    from . import tasks
    t = tasks.get_task(sftp, remote_root, tasks.make_id("shot", shot_entity, step))
    if not t:
        return []
    rr = remote_root.rstrip("/")
    out = []
    for p in tasks.published_files(t):                 # newest name first
        if not p["name"].endswith(ANIM_BLEND_SUFFIX):
            continue
        blend_rel = p["rel"]
        manifest_rel = blend_rel[: -len(".blend")] + ".manifest.json"
        elements, hashes = {}, {}
        txt = sftp.read_text(rr + "/" + manifest_rel)
        if txt:
            try:
                m = _json.loads(txt) or {}
                elements = m.get("elements") or {}
                hashes = m.get("hashes") or {}
            except ValueError:
                elements, hashes = {}, {}
        out.append({"version": anim_version_label(p["name"]),
                    "blend_rel": blend_rel, "by": p.get("by"),
                    "description": p.get("description", ""), "time": p.get("time"),
                    "elements": elements, "hashes": hashes})
    return out


def latest_anim_hashes(anims: list[dict]) -> dict:
    """The newest published content hash per element, from a published_animations()
    list (newest first). Drives the publish dialog's changed/unchanged detection."""
    out = {}
    for a in anims:
        for eid, h in (a.get("hashes") or {}).items():
            out.setdefault(eid, h)
    return out
