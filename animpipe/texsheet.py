"""Texture/UV contact sheet for a look review.

Lays out a look's published texture tiles — one row per map type, UDIM tiles
left→right, each labeled with resolution + colorspace — plus the UV layout, so a
reviewer can confirm the maps and UVs at a glance (right tiles, no missing tile,
correct colorspace). The drawing uses Pillow; the layout/parsing logic is pure and
unit-tested.
"""

from __future__ import annotations

import os
import re

_TILE_RE = re.compile(r"\.(\d{4})\.[^.]+$")

# Row order so sheets are consistent regardless of file enumeration order.
MAP_ORDER = ["BaseColor", "Diffuse", "Albedo", "Color", "Roughness", "Rough",
             "Metalness", "Metallic", "Metal", "Specular", "Normal", "Bump",
             "Height", "Displacement", "Disp", "Alpha", "Opacity", "Emission",
             "Emissive", "AO", "Occlusion"]
_COLOR_MAPS = {"BaseColor", "Diffuse", "Albedo", "Color"}


def parse_texture_name(filename: str):
    """(map_type, tile) from a published tile name, e.g.
    'Frank_v007_BaseColor.1001.png' -> ('BaseColor', 1001). No tile -> (stem, None)."""
    base = os.path.basename(filename)
    m = _TILE_RE.search(base)
    if m:
        tile = int(m.group(1))
        stem = base[: m.start()]
    else:
        tile = None
        stem = os.path.splitext(base)[0]
    mtype = stem.rsplit("_", 1)[-1] if "_" in stem else stem
    return mtype, tile


def group_tiles(filenames) -> dict:
    """{map_type: [(tile, filename), …]} with tiles sorted ascending."""
    groups: dict[str, list] = {}
    for f in filenames:
        mt, tile = parse_texture_name(f)
        groups.setdefault(mt, []).append((tile if tile is not None else 0, f))
    for mt in groups:
        groups[mt].sort()
    return groups


def ordered_maps(maps) -> list[str]:
    maps = set(maps)
    known = [m for m in MAP_ORDER if m in maps]
    rest = sorted(m for m in maps if m not in MAP_ORDER)
    return known + rest


def is_color_map(map_type: str) -> bool:
    return map_type in _COLOR_MAPS


def label_text(map_type: str, entry: dict | None) -> str:
    """Caption for a map row: 'BaseColor   2048x2048   sRGB'."""
    if not entry:
        return map_type
    res = f"{entry.get('width', '?')}x{entry.get('height', '?')}"
    cs = entry.get("colorspace", "") or ""
    return f"{map_type}   {res}   {cs}".strip()


def sheet_dims(n_rows: int, max_cols: int, cell: int = 480, label_h: int = 26,
               pad: int = 14, header: int = 46):
    """Overall (width, height) for n_rows map rows of up to max_cols tiles."""
    max_cols = max(max_cols, 1)
    width = pad + max_cols * (cell + pad)
    height = header + max(n_rows, 1) * (label_h + cell + pad) + pad
    return max(width, 640), height


# ---- Pillow composite (thin; logic above is what's tested) -----------------

def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _thumb(path, cell):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((cell, cell))
    return im


def _draw_uv_panel(draw, x0, y0, width, height, uv_json):
    """Draw the UV wireframe (edge segments from the look-review render) spanning the
    used UDIM tiles, with faint tile boundaries. Returns the number of tiles wide."""
    import json
    import math
    try:
        data = json.load(open(uv_json))
    except Exception:  # noqa: BLE001
        return 1
    segs = data.get("segments", [])
    tiles_u = max(1, math.ceil(data.get("max_u", 1.0) - 1e-6))
    tiles_v = max(1, math.ceil(data.get("max_v", 1.0) - 1e-6))
    cell_w = width / tiles_u
    cell_h = cell_w                      # square tiles
    panel_h = min(height, cell_h * tiles_v)
    draw.rectangle([x0, y0, x0 + width, y0 + panel_h], fill=(12, 12, 14))

    def px(u, v):
        return (x0 + u * cell_w, y0 + panel_h - v * cell_h)

    for tu in range(tiles_u + 1):       # tile grid
        gx = x0 + tu * cell_w
        draw.line([gx, y0, gx, y0 + panel_h], fill=(55, 55, 60), width=1)
    for tv in range(tiles_v + 1):
        gy = y0 + panel_h - tv * cell_h
        draw.line([x0, gy, x0 + width, gy], fill=(55, 55, 60), width=1)
    for a in range(tiles_u):            # UDIM numbers
        draw.text((x0 + a * cell_w + 4, y0 + 4), str(1001 + a), fill=(120, 120, 130))
    for s in segs:
        draw.line([px(s[0], s[1]), px(s[2], s[3])], fill=(90, 200, 255), width=1)
    return tiles_u


def build_sheet(tiles_dir: str, tex_entries: list, out_path: str, *,
                uv_segments: str | None = None, title: str = "",
                cell: int = 480) -> str:
    """Compose the contact sheet PNG. tex_entries are the look manifest's texture
    rows ({name,width,height,colorspace,…}); falls back to listing tiles_dir.
    uv_segments is the UV-wireframe JSON from the look-review render."""
    from PIL import Image, ImageDraw

    files = [e["name"] for e in tex_entries] if tex_entries else sorted(
        f for f in os.listdir(tiles_dir) if _TILE_RE.search(f))
    groups = group_tiles(files)
    maps = ordered_maps(groups.keys())
    max_cols = max((len(groups[m]) for m in maps), default=1)
    has_uv = bool(uv_segments and os.path.isfile(uv_segments))
    rows = len(maps) + (1 if has_uv else 0)
    pad, label_h, header = 14, 26, 46
    W, H = sheet_dims(rows, max_cols, cell, label_h, pad, header)

    img = Image.new("RGB", (W, H), (24, 24, 26))
    draw = ImageDraw.Draw(img)
    f, bf = _font(15), _font(22)
    by_name = {e["name"]: e for e in tex_entries}
    draw.text((pad, pad + 4), title or "Texture / UV sheet",
              fill=(235, 235, 235), font=bf)

    y = header
    for m in maps:
        tiles = groups[m]
        draw.text((pad, y), label_text(m, by_name.get(tiles[0][1])),
                  fill=(200, 200, 210), font=f)
        ty = y + label_h
        for col, (tile, fname) in enumerate(tiles):
            x = pad + col * (cell + pad)
            try:
                im = _thumb(os.path.join(tiles_dir, fname), cell)
                img.paste(im, (x, ty))
            except Exception:  # noqa: BLE001
                draw.rectangle([x, ty, x + cell, ty + cell], outline=(90, 60, 60))
            draw.text((x + 5, ty + 4), str(tile), fill=(255, 235, 60), font=f)
        y = ty + cell + pad

    if has_uv:
        draw.text((pad, y), "UV Layout", fill=(200, 200, 210), font=f)
        span = max_cols * cell + (max_cols - 1) * pad
        _draw_uv_panel(draw, pad, y + label_h, span, cell, uv_segments)

    img.save(out_path)
    return out_path
