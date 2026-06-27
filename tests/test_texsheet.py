"""Tests for animpipe.texsheet pure helpers (no Pillow needed)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from animpipe import texsheet as T


def test_parse_texture_name():
    assert T.parse_texture_name("Frank_v007_BaseColor.1001.png") == ("BaseColor", 1001)
    assert T.parse_texture_name("hero_Roughness.1002.exr") == ("Roughness", 1002)
    assert T.parse_texture_name("plain_Normal.png") == ("Normal", None)


def test_group_tiles_sorted():
    files = ["a_BaseColor.1003.png", "a_BaseColor.1001.png", "a_BaseColor.1002.png",
             "a_Roughness.1001.png"]
    g = T.group_tiles(files)
    assert [t for t, _ in g["BaseColor"]] == [1001, 1002, 1003]      # sorted
    assert list(g) == ["BaseColor", "Roughness"] or "Roughness" in g
    assert len(g["Roughness"]) == 1


def test_ordered_maps_canonical_first():
    maps = {"Roughness", "BaseColor", "ZCustom", "Normal"}
    # known maps in canonical order, unknowns appended alphabetically
    assert T.ordered_maps(maps) == ["BaseColor", "Roughness", "Normal", "ZCustom"]


def test_label_text():
    e = {"width": 2048, "height": 2048, "colorspace": "sRGB"}
    assert T.label_text("BaseColor", e) == "BaseColor   2048x2048   sRGB"
    assert T.label_text("Roughness", None) == "Roughness"


def test_sheet_dims_scales_with_rows_and_cols():
    w1, h1 = T.sheet_dims(2, 3)
    w2, h2 = T.sheet_dims(4, 3)
    assert w1 == w2 and h2 > h1            # more rows -> taller, same width
    w3, _ = T.sheet_dims(2, 5)
    assert w3 > w1                          # more columns -> wider


def test_is_color_map():
    assert T.is_color_map("BaseColor") and not T.is_color_map("Roughness")
