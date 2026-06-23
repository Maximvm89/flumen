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


def run_checks(step, scene, objects):
    """Dispatch by task step. Unknown steps still get the unit check."""
    if step == "model":
        return check_model(scene, objects)
    return check_units(scene)


def has_errors(issues):
    return any(level == ERROR for level, _ in issues)
