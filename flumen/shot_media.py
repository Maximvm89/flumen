"""Local media upkeep for a lighting shot: pull the newest cache + look
textures into the local mirror, and sweep the superseded versions off disk.

Sync is the download side of what Build shot resolves — run it before a
lighting session (or a BRQ render) so every cache and texture the build will
want is already local at its newest version. Cleanup is the inverse: caches
and per-version texture folders pile up (a shot's cache dir alone reaches
tens of GB), and only the newest version of each is ever used by a build, so
everything older can go — it stays on the server and re-downloads on demand.
"""

from __future__ import annotations

import os
import re

from . import elements as E
from . import tasks as T


def _asset_elements(assembly: dict) -> list[dict]:
    return [el for el in (assembly.get("elements") or [])
            if el.get("kind", "asset") == "asset" and el.get("asset")]


def sync_shot_media(client, remote_root: str, local_root: str,
                    shot_entity: str, log=print) -> dict:
    """Download the newest published cache per element (.abc + .vis.json) and
    each asset's chosen look at its newest version (blend + manifest + its
    texture folder) into the local mirror. Unchanged files are skipped by the
    client (size+mtime), so a re-run is nearly free. Returns
    {caches, looks, downloaded, skipped, bytes}."""
    rr = remote_root.rstrip("/")
    n_caches = n_looks = 0

    caches = E.resolved_caches(client, rr, shot_entity)
    for eid, c in sorted(caches.items()):
        rel = c.get("rel") or ""
        if not rel:
            continue
        if not client.exists(rr + "/" + rel):
            log(f"  {eid}: cache v{c.get('version', 0):03d} recorded but not "
                f"on the server — skipped")
            continue
        log(f"  {eid}: cache v{c.get('version', 0):03d}")
        client.download(rr + "/" + rel,
                        os.path.join(local_root, *rel.split("/")))
        vis = E.cache_vis_rel(rel)
        if client.exists(rr + "/" + vis):
            client.download(rr + "/" + vis,
                            os.path.join(local_root, *vis.split("/")))
        n_caches += 1

    assembly = E.load_assembly(client, rr, shot_entity)
    seen_assets = set()
    for el in _asset_elements(assembly):
        asset, lname = el["asset"], el.get("look") or "default"
        if (asset, lname) in seen_assets:
            continue
        seen_assets.add((asset, lname))
        stask = T.get_task(client, rr, T.make_id("asset", asset, "surface"))
        sel = next((l for l in (T.published_looks(stask) if stask else [])
                    if l["look"] == lname), None)
        if sel is None:
            continue                       # no look published yet — nothing to sync
        log(f"  {asset}: look '{lname}' v{sel['version']:03d} + textures")
        for rel in (sel["blend_rel"], sel["manifest_rel"]):
            client.download(rr + "/" + rel,
                            os.path.join(local_root, *rel.split("/")))
        # Textures ride in a per-version folder named after the blend stem —
        # fetching only the newest stem's folder IS the "latest textures".
        stem = os.path.basename(sel["blend_rel"])[:-len(".blend")]
        tex_rel = sel["blend_rel"].rsplit("/", 1)[0] + "/textures/" + stem
        if client.exists(rr + "/" + tex_rel):
            client.download_dir(rr + "/" + tex_rel,
                                os.path.join(local_root, *tex_rel.split("/")))
        n_looks += 1

    stats = client.fetch_stats()
    return {"caches": n_caches, "looks": n_looks,
            "downloaded": stats["downloaded"], "skipped": stats["skipped"],
            "bytes": stats["bytes"]}


_TEX_VER_RE = re.compile(r"^(.+)_v(\d+)$")


def _dir_size(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def cleanup_plan(local_root: str, shot_entity: str,
                 asset_entities: list[str]) -> dict:
    """What 'Clean up unused data' would delete, WITHOUT deleting: superseded
    LOCAL versions of the shot's caches and of the given assets' texture
    folders (the newest local version of each always survives — it's the one
    every build uses; the server keeps all versions regardless). Returns
    {victims: [(abs_path, bytes)], total_bytes} — victims may be files (.abc,
    .vis.json) or whole texture version folders."""
    victims: list[tuple[str, int]] = []

    cache_dir = os.path.join(local_root,
                             *E.cache_dir_rel(shot_entity).split("/"))
    if os.path.isdir(cache_dir):
        best: dict[str, int] = {}
        for n in os.listdir(cache_dir):
            parsed = E.parse_cache_name(n)
            if parsed:
                best[parsed[0]] = max(best.get(parsed[0], 0), parsed[1])
        for n in sorted(os.listdir(cache_dir)):
            parsed = E.parse_cache_name(n)
            if not parsed or parsed[1] >= best[parsed[0]]:
                continue
            for name in (n, E.cache_vis_rel(n)):
                p = os.path.join(cache_dir, name)
                if os.path.isfile(p):
                    victims.append((p, os.path.getsize(p)))

    for asset in sorted(set(asset_entities)):
        tex_root = os.path.join(local_root, "03_assets", *asset.split("/"),
                                "surface", "publish", "textures")
        if not os.path.isdir(tex_root):
            continue
        best = {}
        folders = []
        for n in os.listdir(tex_root):
            m = _TEX_VER_RE.match(n)
            if m and os.path.isdir(os.path.join(tex_root, n)):
                folders.append((m.group(1), int(m.group(2)), n))
                best[m.group(1)] = max(best.get(m.group(1), 0),
                                       int(m.group(2)))
        for base, ver, n in sorted(folders):
            if ver < best[base]:
                p = os.path.join(tex_root, n)
                victims.append((p, _dir_size(p)))

    return {"victims": victims, "total_bytes": sum(b for _, b in victims)}


def cleanup_apply(plan: dict, log=print) -> tuple[int, int]:
    """Delete everything a cleanup_plan listed. Returns (items, bytes)."""
    import shutil
    n = freed = 0
    for path, size in plan.get("victims") or []:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            n += 1
            freed += size
            log(f"  removed {path} ({size / 1e6:.0f} MB)")
        except OSError as exc:
            log(f"  could not remove {path}: {exc}")
    return n, freed
