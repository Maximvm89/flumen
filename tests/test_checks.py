"""Tests for the addon's pre-publish checks (no bpy needed — pure attr reads)."""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "blender_addon"))

from legami_pipeline import checks


def _scene(system="METRIC", scale=1.0):
    return types.SimpleNamespace(
        unit_settings=types.SimpleNamespace(system=system, scale_length=scale))


def _mesh(name, scale=(1.0, 1.0, 1.0), parent=None):
    return types.SimpleNamespace(type="MESH", name=name, scale=scale, parent=parent)


def _empty(name, parent=None):
    return types.SimpleNamespace(type="EMPTY", name=name, scale=(1.0, 1.0, 1.0),
                                 parent=parent)


def _rig():
    """A valid scene: a PUBLISH locator with a mesh parented under it."""
    loc = _empty("PUBLISH")
    geo = _mesh("Body", parent=loc)
    return loc, geo


def test_model_clean_passes():
    loc, geo = _rig()
    issues = checks.run_checks("model", _scene(), [loc, geo])
    assert issues == []
    assert not checks.has_errors(issues)


def test_model_wrong_units_errors():
    loc, geo = _rig()
    issues = checks.run_checks("model", _scene(system="IMPERIAL"), [loc, geo])
    assert checks.has_errors(issues)


def test_model_scale_not_one_warns():
    loc = _empty("PUBLISH")
    geo = _mesh("Body", scale=(2.0, 2.0, 2.0), parent=loc)
    issues = checks.run_checks("model", _scene(), [loc, geo])
    assert not checks.has_errors(issues)             # warning, not error
    assert any(lvl == checks.WARNING for lvl, _ in issues)


def test_model_no_mesh_errors():
    issues = checks.run_checks("model", _scene(), [])
    assert checks.has_errors(issues)


def test_unit_scale_off_errors():
    loc, geo = _rig()
    issues = checks.run_checks("model", _scene(scale=0.01), [loc, geo])
    assert checks.has_errors(issues)


def test_missing_locator_errors():
    issues = checks.check_publish_locator([_mesh("Cube")], "PUBLISH")
    assert checks.has_errors(issues)


def test_empty_locator_errors():
    loc = _empty("PUBLISH")
    issues = checks.check_publish_locator([loc], "PUBLISH")  # nothing parented
    assert checks.has_errors(issues)


def test_populated_locator_ok():
    loc = _empty("PUBLISH")
    geo = _mesh("Body", parent=loc)
    issues = checks.check_publish_locator([loc, geo], "PUBLISH")
    assert issues == []


def test_run_checks_blocks_without_locator():
    # a clean model but NO locator must now fail
    issues = checks.run_checks("model", _scene(), [_mesh("Body")])
    assert checks.has_errors(issues)
