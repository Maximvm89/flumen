"""The PyInstaller spec must ship every Blender-side script.

`flumen/blender_*.py` are executed by Blender via `--python`, not imported — so
PyInstaller's static analysis never sees them and they only reach the frozen
bundle if listed in the spec's `datas`. A script that is missing there works
perfectly from source and fails only on an artist's installed build, which is
the worst place to find out.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "flumen.spec"


def test_every_blender_script_is_bundled():
    spec = SPEC.read_text()
    scripts = sorted(p.name for p in (ROOT / "flumen").glob("blender_*.py"))
    assert scripts, "no flumen/blender_*.py found — did the layout change?"
    missing = [n for n in scripts if f'"{n}"' not in spec]
    assert not missing, (
        f"not shipped in packaging/flumen.spec datas: {missing}. Add them, or "
        f"the frozen build can't run them.")


def test_flumen_submodules_forced_into_the_bundle():
    """gui.py reaches flumen.turntable through a dynamic __import__(), which
    PyInstaller cannot follow — collect_submodules keeps it in the bundle."""
    spec = SPEC.read_text()
    assert re.search(r"collect_submodules\(\s*[\"']flumen[\"']\s*\)", spec), (
        "packaging/flumen.spec must collect_submodules('flumen') — some modules "
        "are only reached via a dynamic import.")
