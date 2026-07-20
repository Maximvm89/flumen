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
    # apply_look: Build shot fetches each element's look (element.look, else
    # 'default') and assigns its materials onto the linked content — shading
    # comes from the LOOK publish at build time, never baked geometry publishes.
    "layout":    {"source_step": "rig", "fallback_step": "model",
                  "load": "link", "apply_look": True},
    "animation": {"source_step": "rig", "fallback_step": "model",
                  "load": "link", "apply_look": True},
    "lighting":  {"source_step": "cache", "fallback_step": "model",
                  "load": "alembic", "apply_look": True},
    "comp":      None,   # comp consumes renders, not scene elements
}

# Which shot step publishes the shot's own camera (the "camera" element resolves
# to this step's newest publish). Overridable via assembly.camera_step.
DEFAULT_CAMERA_STEP = "layout"

# Which steps' published animation a shot step consumes, in precedence order —
# the own step always wins, then upstream. This is what hands the layout's
# camera move + character placements to a fresh animation scene: an animation
# task with no publishes of its own resolves them from layout. Overridable via
# project_settings.json "assembly":{"anim_sources":{...}}; steps not listed
# read only their own publishes.
DEFAULT_ANIM_SOURCES = {
    "animation": ["animation", "layout"],
    "lighting": ["lighting", "animation", "layout"],
}


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
                label: str = "", look: str = "", dressing: str = "") -> dict:
    """A fresh element dict (id is assigned by add_element). A camera element
    carries no asset entity. `dressing` names a published set-dressing to load
    with an environment element (like `look`, resolved to newest at build)."""
    if kind not in KINDS:
        raise ValueError(f"unknown element kind: {kind}")
    asset = "" if kind == "camera" else (asset_entity or "")
    label = label or (asset.split("/")[-1] if asset else "camera")
    return {"id": "", "kind": kind, "asset": asset,
            "label": label, "look": look or "", "dressing": dressing or "",
            "enabled": True}


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
             "dressing": "" if kind == "camera" else raw.get("dressing", ""),
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


def anim_sources(step: str, settings: dict | None = None) -> list[str]:
    """The steps whose published animation `step` consumes, precedence order.
    The step's own publishes always come first (an animator's own versions beat
    the layout they started from), then the configured upstream chain."""
    override = ((settings or {}).get("assembly") or {}).get("anim_sources") or {}
    chain = list(override.get(step) or DEFAULT_ANIM_SOURCES.get(step) or [])
    chain = [s for s in chain if s and s != step]
    return [step] + chain


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
                    "look": e.get("look", ""),
                    "dressing": e.get("dressing", ""),
                    "load": spec.get("load", "link"),
                    "apply_look": bool(spec.get("apply_look", False))})
    return out


def resolved_caches(sftp, remote_root: str, shot_entity: str,
                    step: str = "animation") -> dict:
    """Newest published alembic cache per element for a shot, from the step that
    publishes caches (animation): {element_id: {rel, version, by}}. Drives the
    lighting build — each animated character loads its latest cache."""
    from . import tasks
    t = tasks.get_task(sftp, remote_root,
                       tasks.make_id("shot", shot_entity, step))
    return published_caches(t) if t else {}


def newest_dressing(sftp, remote_root: str, asset_entity: str,
                    dressing_name: str) -> dict | None:
    """The newest published version of a NAMED set-dressing on an environment
    asset: {blend_rel, manifest_rel, version} or None. Resolved at build time so
    a shot always gets the latest layout of the chosen dressing."""
    from . import tasks
    task_id = tasks.make_id("asset", asset_entity, "dressing")
    t = tasks.get_task(sftp, remote_root, task_id)
    if not t:
        return None
    for d in tasks.published_dressings(t):
        if d["dressing"] == dressing_name:
            return {"blend_rel": d["blend_rel"],
                    "manifest_rel": d["manifest_rel"],
                    "version": d["version"]}
    return None


ANIM_BLEND_SUFFIX = "_anim.blend"


def resolved_animation(sftp, remote_root: str, shot_entity: str, step: str,
                       settings: dict | None = None) -> dict | None:
    """The animation to re-apply on Build shot, resolved PER ELEMENT to the newest
    published version that actually contains it: {elements: {id: {blend_rel,
    objects:{obj:action}, version}}} or None. Per-element (not just the single latest
    publish) so an element that wasn't re-published in the most recent version — e.g.
    its animation was unchanged — still resolves from the version that has it.
    The publish list is precedence-ordered across the step's anim_sources chain
    (own step first), so a fresh animation shot inherits the layout's camera move
    and character placements until it publishes versions of its own."""
    anims = published_animations(sftp, remote_root, shot_entity, step, settings)
    elements = {}
    for a in anims:                                  # precedence order
        for eid, objs in (a.get("elements") or {}).items():
            if eid not in elements:
                elements[eid] = {"blend_rel": a["blend_rel"], "objects": objs,
                                 "version": a["version"],
                                 "content": (a.get("contents") or {}).get(eid,
                                                                          "")}
    return {"elements": elements} if elements else None


_ANIM_VER_RE = re.compile(r"_v(\d+)_anim\.blend$")


def anim_version_label(name: str) -> str:
    """'SH0010_layout_v007_anim.blend' -> 'v007'."""
    import os as _os
    m = _ANIM_VER_RE.search(name or "")
    return f"v{int(m.group(1)):03d}" if m else _os.path.splitext(name or "")[0]


def browse_anim_sources(step: str, settings: dict | None = None) -> list[str]:
    """Every step whose published animation the Load-animation PICKER should
    offer: the step's own chain first, then every other step that can carry
    animation (e.g. the animation step's versions while browsing from layout).
    Explicit user choice — the automatic flows (Build shot, publish dedup)
    stay on the directional anim_sources chain."""
    chain = anim_sources(step, settings)
    extra = [s for s, spec in representations(settings).items()
             if spec is not None and s not in chain]
    return chain + extra


def published_animations(sftp, remote_root: str, shot_entity: str, step: str,
                         settings: dict | None = None,
                         sources: list[str] | None = None) -> list[dict]:
    """Every published animation the shot step can consume, in PRECEDENCE order:
    the step's own publishes first (newest first), then each upstream step of its
    anim_sources chain — so an animation task sees the layout's camera move and
    placements until it has versions of its own. A list of {version, step,
    blend_rel, by, description, time, elements:{id:{obj:action}}, hashes}. The
    version label is unique across steps ('v003' for the own step, 'layout v007'
    for upstream); it keys the 'Load animation' picker and feeds the publish
    dialog's changed/unchanged detection. `sources` overrides the step chain
    (browse_anim_sources for the picker's all-steps view)."""
    import json as _json
    from . import tasks
    rr = remote_root.rstrip("/")
    out = []
    for st in (sources if sources is not None
               else anim_sources(step, settings)):
        t = tasks.get_task(sftp, rr, tasks.make_id("shot", shot_entity, st))
        if not t:
            continue
        for p in tasks.published_files(t):             # newest name first
            if not p["name"].endswith(ANIM_BLEND_SUFFIX):
                continue
            blend_rel = p["rel"]
            manifest_rel = blend_rel[: -len(".blend")] + ".manifest.json"
            elements, hashes, contents = {}, {}, {}
            txt = sftp.read_text(rr + "/" + manifest_rel)
            if txt:
                try:
                    m = _json.loads(txt) or {}
                    elements = m.get("elements") or {}
                    hashes = m.get("hashes") or {}
                    contents = m.get("contents") or {}
                except ValueError:
                    elements, hashes, contents = {}, {}, {}
            label = anim_version_label(p["name"])
            if st != step:
                label = f"{st} {label}"
            out.append({"version": label, "step": st,
                        "blend_rel": blend_rel, "by": p.get("by"),
                        "description": p.get("description", ""),
                        "time": p.get("time"),
                        "elements": elements, "hashes": hashes,
                        "contents": contents})
    return out


CACHE_SUFFIX = ".abc"


def cache_dir_rel(shot_entity: str, step: str = "animation") -> str:
    """Where a shot's alembic caches live: the animation step's publish/cache."""
    return f"{SEQ_ROOT}/{shot_entity}/{step}/publish/cache"


def cache_name(element_id: str, version: int) -> str:
    return f"{element_id}_v{version:03d}{CACHE_SUFFIX}"


_CACHE_RE = re.compile(r"^(.+)_v(\d+)\.abc$")


def parse_cache_name(name: str):
    """'skeleton_v003.abc' -> ('skeleton', 3), else None."""
    import os as _os
    m = _CACHE_RE.match(_os.path.basename(name or ""))
    return (m.group(1), int(m.group(2))) if m else None


def published_caches(task: dict) -> dict:
    """Newest published alembic cache per element from a shot task's history:
    {element_id: {version, rel, time, by}}. Fed to the lighting build so each
    animated character resolves to its latest cache, element by element."""
    import os as _os
    best: dict[str, dict] = {}
    for rec in task.get("publishes") or []:
        for rel in rec.get("files") or []:
            if not rel.endswith(CACHE_SUFFIX):
                continue
            parsed = parse_cache_name(_os.path.basename(rel))
            if not parsed:
                continue
            eid, ver = parsed
            cur = best.get(eid)
            if cur is None or ver > cur["version"]:
                best[eid] = {"version": ver, "rel": rel,
                             "time": rec.get("time"), "by": rec.get("by")}
    return best


def next_cache_version(task: dict, element_id: str) -> int:
    """The next cache version number for an element (1 if none yet)."""
    cur = published_caches(task).get(element_id)
    return (cur["version"] + 1) if cur else 1


def latest_anim_hashes(anims: list[dict]) -> dict:
    """The effective published content hash per element, from a
    published_animations() list (precedence order: own step's newest first, then
    upstream). Drives the publish dialog's changed/unchanged detection — an
    animation scene freshly built from layout shows 'unchanged (= layout vNNN)'
    instead of re-publishing identical data."""
    out = {}
    for a in anims:
        for eid, h in (a.get("hashes") or {}).items():
            out.setdefault(eid, h)
    return out
