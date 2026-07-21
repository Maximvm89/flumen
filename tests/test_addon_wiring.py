"""Automated wiring gate for the Blender add-on package.

The rest of test_addon.py imports individual modules; this one imports the WHOLE
`flumen_pipeline` package fresh under a fake bpy and asserts it wires up. It is
the safety net that lets operators.py be split into submodules without smoke-
testing every dropdown in Blender: a broken re-export, a circular import, or a
dropped CLASSES entry fails HERE, not in the app.

A pure code-move extraction that keeps this green cannot have broken the add-on's
load path or registration surface — only genuine Blender-runtime behavior (which
a move doesn't change) is out of scope.
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "blender_addon"))


def _install_rich_bpy():
    """A fake bpy complete enough to import prefs + operators + ui + __init__."""
    bpy = types.ModuleType("bpy")

    class _Noop:
        @staticmethod
        def append(*a, **k):
            pass

        @staticmethod
        def remove(*a, **k):
            pass

        @staticmethod
        def register(*a, **k):
            pass

    def _prop(*a, **k):
        return None

    bpy.types = types.SimpleNamespace(
        Operator=object, Panel=object, AddonPreferences=object, Menu=object,
        PropertyGroup=object, WindowManager=type("WindowManager", (), {}),
        TOPBAR_MT_editor_menus=_Noop)
    bpy.props = types.SimpleNamespace(
        BoolProperty=_prop, StringProperty=_prop, IntProperty=_prop,
        FloatProperty=_prop, EnumProperty=_prop, CollectionProperty=_prop,
        PointerProperty=_prop)
    bpy.utils = types.SimpleNamespace(
        user_resource=lambda *a, **k: "/tmp/flumen_modules",
        register_class=lambda c: None, unregister_class=lambda c: None)
    bpy.app = types.SimpleNamespace(
        handlers=types.SimpleNamespace(), timers=_Noop)
    bpy.context = types.SimpleNamespace()
    bpy.data = types.SimpleNamespace(scenes=[])
    sys.modules["bpy"] = bpy


def _fresh_import():
    """Re-import the package from scratch so circular-import / missing-symbol
    regressions actually surface on every run."""
    _install_rich_bpy()
    for name in list(sys.modules):
        if name == "flumen_pipeline" or name.startswith("flumen_pipeline."):
            del sys.modules[name]
    import flumen_pipeline as pkg
    return pkg


def test_package_imports_and_registration_surface_intact():
    pkg = _fresh_import()
    ops = sys.modules["flumen_pipeline.operators"]

    # Every registered class is actually a class (register_class would need one).
    assert ops.CLASSES, "operators.CLASSES is empty"
    non_classes = [c for c in ops.CLASSES if not isinstance(c, type)]
    assert non_classes == [], f"non-class entries in CLASSES: {non_classes}"

    # No duplicate registrations (a copy-paste during a split would show here).
    assert len(ops.CLASSES) == len(set(ops.CLASSES)), "duplicate CLASSES entries"

    # __init__ composes prefs + ops + ui; it must resolve.
    assert len(pkg._ALL_CLASSES) == len(ops.CLASSES) + 1 + len(
        sys.modules["flumen_pipeline.ui"].CLASSES)


def test_init_referenced_operators_symbols_exist():
    """__init__.py reaches into operators for these by name (startup hooks +
    WindowManager property definitions). A split must keep them re-exported."""
    _fresh_import()
    ops = sys.modules["flumen_pipeline.operators"]
    for name in ("scaffold_empty_scene", "scaffold_surface_scene",
                 "apply_project_color", "enable_project_addons",
                 "look_name_search", "lookdev_hdri_items", "dressing_name_search",
                 "FLUMEN_AssemblyItem", "FLUMEN_AnimItem", "FLUMEN_PublishItem"):
        assert hasattr(ops, name), f"operators.{name} is missing (broken re-export)"


def test_operator_bl_idnames_unique():
    """Two operators sharing a bl_idname (a real risk when moving classes between
    modules) silently shadow each other in Blender — catch it here."""
    _fresh_import()
    ops = sys.modules["flumen_pipeline.operators"]
    idnames = [c.bl_idname for c in ops.CLASSES if hasattr(c, "bl_idname")]
    dupes = {i for i in idnames if idnames.count(i) > 1}
    assert not dupes, f"duplicate bl_idname(s): {dupes}"
