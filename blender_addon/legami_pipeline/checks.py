"""Pre-publish sanity checks.

No `bpy` import — functions read plain attributes off the scene/objects, so they
are unit-testable outside Blender. Each returns a list of (level, message) where
level is "error" (blocks publish) or "warning" (allowed, just flagged).
"""

ERROR = "error"
WARNING = "warning"


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


def check_model(scene, objects):
    issues = check_units(scene)
    meshes = [o for o in objects if getattr(o, "type", "") == "MESH"]
    if not meshes:
        issues.append((ERROR, "No mesh objects found to publish."))
    for o in meshes:
        scale = tuple(round(float(v), 4) for v in getattr(o, "scale", (1.0, 1.0, 1.0)))
        if scale != (1.0, 1.0, 1.0):
            issues.append((WARNING,
                           f"'{getattr(o, 'name', '?')}' has unapplied scale "
                           f"{scale} — apply it (Ctrl+A ▸ Scale) for clean "
                           f"transforms in Maya."))
    return issues


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


def check_publish_locator(objects, locator_name):
    """The publish locator must exist and contain geometry — this is what tells
    the pipeline exactly what to export/render."""
    loc = None
    for o in objects:
        name = getattr(o, "name", "")
        if name == locator_name or name.split(".")[0] == locator_name:
            loc = o
            break
    if loc is None:
        return [(ERROR, f"No '{locator_name}' locator found. Add one "
                        f"(Legami menu ▸ Add Publish Locator) and parent your "
                        f"asset geometry under it.")]
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
               ttype=None, frame_start=1001):
    """Dispatch by task type/step. Asset publishes need a populated publish locator;
    a shot publish is checked for a camera + the right frame range instead."""
    if ttype == "shot":
        return check_shot(scene, objects, frame_start)
    if step == "model":
        issues = check_model(scene, objects)
    elif step == "surface":
        issues = check_units(scene)
        issues += check_surface(objects, locator, textures)
    else:
        issues = check_units(scene)
    issues += check_publish_locator(objects, locator)
    return issues


def has_errors(issues):
    return any(level == ERROR for level, _ in issues)
