"""Pre-publish sanity checks.

No `bpy` import — functions read plain attributes off the scene/objects, so they
are unit-testable outside Blender. Each returns a list of (level, message) where
level is "error" (blocks publish) or "warning" (allowed, just flagged).
"""

ERROR = "error"
WARNING = "warning"

# Pre-publish scene profiler (environments): WARN-only budgets. Overridden by
# the project_settings "profiling" block; omit a key there to skip that check.
DEFAULT_PROFILING = {
    "apply_to_categories": ["environments"],
    "apply_to_steps": ["model", "dressing"],
    "max_polycount": 2_000_000,
    "max_objects": 1500,
    "max_textures": 200,
    "max_texture_size": 8192,
    "max_texture_memory_mb": 8192,
    "heavy_modifiers": ["SUBSURF", "MULTIRES", "BOOLEAN", "NODES", "OCEAN"],
}


def profile_thresholds(settings: dict | None) -> dict:
    """Effective profiler config: defaults overlaid by project_settings."""
    out = dict(DEFAULT_PROFILING)
    out.update((settings or {}).get("profiling") or {})
    return out


def _mb(n_bytes: float) -> float:
    return n_bytes / (1024 * 1024)


def texture_memory_bytes(tex: dict) -> int:
    """Estimated GPU memory of one texture: w*h*channels*(4 if float else 1)."""
    w = int(tex.get("width", 0))
    h = int(tex.get("height", 0))
    ch = int(tex.get("channels", 4)) or 4
    return w * h * ch * (4 if tex.get("is_float") else 1)


def check_profile(stats: dict, thresholds: dict) -> list:
    """Profile a scene against the budgets — every issue a WARNING (the profiler
    informs, it never blocks). `stats` (collected bpy-side):
    {poly_count, object_count, textures: [{name, width, height, channels,
    is_float}], heavy_modifiers: [(obj_name, mod_type)]}."""
    if not stats:
        return []
    t = thresholds or DEFAULT_PROFILING
    issues = []

    limit = t.get("max_polycount")
    if limit and stats.get("poly_count", 0) > limit:
        issues.append((WARNING, f"Heavy geometry: {stats['poly_count']:,} polys "
                                f"— over the {limit:,} budget. Consider decimating "
                                f"or instancing."))
    limit = t.get("max_objects")
    if limit and stats.get("object_count", 0) > limit:
        issues.append((WARNING, f"{stats['object_count']:,} objects — over the "
                                f"{limit:,} budget. Consider joining static meshes."))
    textures = stats.get("textures") or []
    limit = t.get("max_textures")
    if limit and len(textures) > limit:
        issues.append((WARNING, f"{len(textures)} textures — over the {limit} "
                                f"budget."))
    limit = t.get("max_texture_size")
    if limit:
        over = [x for x in textures
                if max(int(x.get("width", 0)), int(x.get("height", 0))) > limit]
        for x in over[:5]:
            issues.append((WARNING, f"Texture '{x.get('name', '?')}' is "
                                    f"{x.get('width')}x{x.get('height')} — over "
                                    f"{limit}px."))
        if len(over) > 5:
            issues.append((WARNING, f"…and {len(over) - 5} more texture(s) over "
                                    f"{limit}px."))
    limit = t.get("max_texture_memory_mb")
    if limit:
        total_mb = _mb(sum(texture_memory_bytes(x) for x in textures))
        if total_mb > limit:
            issues.append((WARNING, f"Estimated texture memory {total_mb:,.0f} MB "
                                    f"— over the {limit:,} MB budget."))
    heavy = stats.get("heavy_modifiers") or []
    if heavy:
        sample = ", ".join(f"{o} ({m})" for o, m in heavy[:3])
        issues.append((WARNING, f"{len(heavy)} heavy modifier(s) unapplied — "
                                f"render cost multiplies downstream: {sample}"
                                + ("…" if len(heavy) > 3 else "")))
    return issues


def check_units(scene):
    """Scene must be metric / meters so it lands at the right scale in Maya."""
    issues = []
    us = getattr(scene, "unit_settings", None)
    system = getattr(us, "system", "")
    if system != "METRIC":
        issues.append((ERROR, f"Unit system is '{system or 'NONE'}', expected "
                              f"METRIC (meters) for Maya compatibility."))
        return issues
    scale = float(getattr(us, "scale_length", 1.0))
    if abs(scale - 1.0) > 1e-6:
        issues.append((ERROR, f"Unit scale is {scale}, expected 1.0 "
                              f"(1 Blender unit = 1 meter)."))
    return issues


def check_model(scene, objects, max_listed=5):
    issues = check_units(scene)
    meshes = [o for o in objects if getattr(o, "type", "") == "MESH"]
    if not meshes:
        issues.append((ERROR, "No mesh objects found to publish."))
    # Aggregate unapplied-scale offenders: list a few by name, summarize the rest
    # (an environment can have hundreds — a per-object wall of text helps nobody).
    unapplied = []
    for o in meshes:
        scale = tuple(round(float(v), 4) for v in getattr(o, "scale", (1.0, 1.0, 1.0)))
        if scale != (1.0, 1.0, 1.0):
            unapplied.append(getattr(o, "name", "?"))
    if unapplied:
        sample = ", ".join(f"'{n}'" for n in unapplied[:max_listed])
        more = len(unapplied) - max_listed
        issues.append((WARNING,
                       f"{len(unapplied)} mesh(es) with unapplied scale — select "
                       f"them and apply (Ctrl+A ▸ Scale) for clean transforms in "
                       f"Maya: {sample}" + (f" …and {more} more" if more > 0 else "")))
    return issues


def fixable_scale_objects(objects):
    """Triage unapplied-scale meshes for the auto-fixer (pure, duck-typed):
    (fixable, shared, animated, linked). Shared-mesh instances are NOT fixable —
    applying scale bakes into the mesh data and would deform every other
    instance. Keyframed objects are left alone — their scale may be intentional.
    Linked/override objects (shot and dressing elements) can't be edited at all —
    transform_apply on library data only errors."""
    fixable, shared, animated, linked = [], [], [], []
    for o in objects:
        if getattr(o, "type", "") != "MESH":
            continue
        scale = tuple(round(float(v), 4) for v in getattr(o, "scale", (1, 1, 1)))
        if scale == (1.0, 1.0, 1.0):
            continue
        if (getattr(o, "library", None) is not None
                or getattr(o, "override_library", None) is not None
                or getattr(getattr(o, "data", None), "library", None) is not None):
            linked.append(o)
        elif getattr(getattr(o, "data", None), "users", 1) > 1:
            shared.append(o)
        elif getattr(o, "animation_data", None) is not None:
            animated.append(o)
        else:
            fixable.append(o)
    return fixable, shared, animated, linked


def _descendants(objects, root):
    """All descendants of root among `objects` (pure; uses .parent references)."""
    children = {}
    for o in objects:
        p = getattr(o, "parent", None)
        if p is not None:
            children.setdefault(id(p), []).append(o)
    out, stack = [], [root]
    while stack:
        cur = stack.pop()
        for c in children.get(id(cur), []):
            out.append(c)
            stack.append(c)
    return out


def _find_publish_collection(collections, locator_name):
    for c in collections or []:
        name = getattr(c, "name", "")
        if name == locator_name or name.split(".")[0] == locator_name:
            return c
    return None


def check_publish_locator(objects, locator_name, collections=None):
    """The publish root must exist and contain geometry — this is what tells
    the pipeline exactly what to export/render. Two equivalent forms:
    an EMPTY named '<locator>' with the asset parented under it (characters,
    props), or a COLLECTION named '<locator>' holding the asset's collections
    (environments — big sets are organized in collections, not parent chains).
    The empty wins when both exist (back-compat)."""
    loc = None
    for o in objects:
        name = getattr(o, "name", "")
        if name == locator_name or name.split(".")[0] == locator_name:
            loc = o
            break
    if loc is None:
        coll = _find_publish_collection(collections, locator_name)
        if coll is not None:
            meshes = [o for o in getattr(coll, "all_objects", []) or []
                      if getattr(o, "type", "") == "MESH"]
            if not meshes:
                return [(ERROR, f"'{locator_name}' collection is empty — move "
                                f"your asset's collections inside it before "
                                f"publishing.")]
            return []
        return [(ERROR, f"No '{locator_name}' publish root found. Either add "
                        f"the empty (Flumen menu ▸ Add Publish Locator) and "
                        f"parent your geometry under it, or create a collection "
                        f"named '{locator_name}' and put your asset's "
                        f"collections inside it.")]
    meshes = [o for o in _descendants(objects, loc)
              if getattr(o, "type", "") == "MESH"]
    if not meshes:
        return [(ERROR, f"'{locator_name}' locator is empty — parent your asset "
                        f"geometry under it before publishing.")]
    return []


def _has_material(obj):
    """True if a mesh has at least one real material assigned (via slots)."""
    for slot in getattr(obj, "material_slots", []) or []:
        if getattr(slot, "material", None) is not None:
            return True
    # Fallback for objects exposing data.materials directly.
    for m in getattr(getattr(obj, "data", None), "materials", []) or []:
        if m is not None:
            return True
    return False


def check_surface(objects, locator_name, textures):
    """Surface (shading/texturing) publish gate. The safety-critical rule: no
    published look may carry a dead texture path, so every used image must resolve
    to a file on disk (or be packed). Also flags geometry under the locator with no
    material — an empty look. `textures` is a list of records duck-typed with
    `.name` and `.is_missing` (the operator builds these from bpy.data.images so
    this stays bpy-free and testable)."""
    issues = []
    for tex in textures or []:
        if getattr(tex, "is_missing", False):
            issues.append((ERROR,
                           f"Texture '{getattr(tex, 'name', '?')}' has no file on "
                           f"disk — fix or reload it; publishing would ship a dead "
                           f"texture path."))
    # Material coverage on the geometry that actually gets published.
    loc = None
    for o in objects:
        name = getattr(o, "name", "")
        if name == locator_name or name.split(".")[0] == locator_name:
            loc = o
            break
    meshes = ([o for o in _descendants(objects, loc)
               if getattr(o, "type", "") == "MESH"] if loc is not None
              else [o for o in objects if getattr(o, "type", "") == "MESH"])
    for o in meshes:
        if not _has_material(o):
            issues.append((ERROR,
                           f"'{getattr(o, 'name', '?')}' has no material assigned "
                           f"— a surface publish needs a look on every mesh."))
    return issues


def check_shot(scene, objects, frame_start_expected=1001):
    """Shot (layout/anim) publish gate: a camera must be set and the timeline must
    be the shot's frame range (starting at 1001). No publish locator — a shot is the
    assembled scene, not a single asset under a locator."""
    issues = check_units(scene)
    if getattr(scene, "camera", None) is None:
        issues.append((ERROR, "No active scene camera — Build shot creates the shot "
                              "camera; set one before publishing."))
    fs = int(getattr(scene, "frame_start", 0) or 0)
    fe = int(getattr(scene, "frame_end", 0) or 0)
    if fe <= fs:
        issues.append((ERROR, f"Frame range is empty ({fs}-{fe}). Run Build shot to "
                              f"set the shot's frame range."))
    elif fs != frame_start_expected:
        issues.append((WARNING, f"Frame start is {fs}, expected {frame_start_expected}"
                               f" — run Build shot to align the timeline."))
    return issues


def run_checks(step, scene, objects, locator="PUBLISH", textures=None,
               ttype=None, frame_start=1001, profile_stats=None, profiling=None,
               collections=None):
    """Dispatch by task type/step. Asset publishes need a populated publish root
    (locator empty or collection); a shot publish is checked for a camera + the
    right frame range instead. With `profile_stats` (heavy steps like
    environments), the WARN-only profiler report is appended."""
    if ttype == "shot":
        return check_shot(scene, objects, frame_start)
    if step == "dressing":
        # A dressing scene has no PUBLISH locator — everything in it is linked.
        # Its gate (an environment holder must exist) lives in the publish
        # operator, which owns the collection data this module never sees.
        issues = check_units(scene)
    elif step == "model":
        issues = check_model(scene, objects)
        issues += check_publish_locator(objects, locator, collections)
    elif step == "surface":
        issues = check_units(scene)
        issues += check_surface(objects, locator, textures)
        issues += check_publish_locator(objects, locator, collections)
    else:
        issues = check_units(scene)
        issues += check_publish_locator(objects, locator, collections)
    if profile_stats is not None:
        issues += check_profile(profile_stats, profiling)
    return issues


def has_errors(issues):
    return any(level == ERROR for level, _ in issues)
