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


def run_checks(step, scene, objects, locator="PUBLISH"):
    """Dispatch by task step. Every publish must have a populated publish locator."""
    if step == "model":
        issues = check_model(scene, objects)
    else:
        issues = check_units(scene)
    issues += check_publish_locator(objects, locator)
    return issues


def has_errors(issues):
    return any(level == ERROR for level, _ in issues)
