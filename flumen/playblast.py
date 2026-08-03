"""Shot playblast: render a shot's frame range through its camera headlessly and
publish the video into 07_dailies, attached to the publish record (so it appears in
Dailies review exactly like a model turntable). Reuses the turntable encode/upload
plumbing; the render is a fast Workbench/EEVEE pass over the shot camera.
"""

from __future__ import annotations

import os
import re
import subprocess

PB_DEFAULTS = {
    # EEVEE renders the real materials + textures + lighting, so the playblast
    # matches the artist's shaded viewport. Set BLENDER_WORKBENCH for a fast,
    # flat/shadeless solid pass instead.
    "engine": "BLENDER_EEVEE_NEXT",
    # Workbench-only shading colour (ignored by EEVEE/Cycles): TEXTURE shows the
    # texture maps, MATERIAL shows flat base colours.
    "color": "TEXTURE",
    "resolution_x": 1280,
    "resolution_y": 720,
    "fps": 24,
    "view_transform": "",            # blank = leave the file's view transform
    # Shots with no lights get a camera-parented shadowless key+fill rig so
    # closed sets read instead of rendering black. False = never add lights.
    "auto_light": True,
    # Playblasts are previews: few EEVEE samples and no raytracing (huge
    # speed-up vs the render default of 64), optionally rendered at a fraction
    # of the delivery resolution (50 = half size, ~4x fewer pixels).
    "samples": 16,
    "resolution_percentage": 100,
}


def playblast_settings(project_settings: dict) -> dict:
    s = dict(PB_DEFAULTS)
    s.update((project_settings or {}).get("playblast") or {})
    return s


def delivery_formats(project_settings: dict) -> list[dict]:
    """The project's delivery formats (top-level "formats" block) — e.g. 16:9 +
    9:16 for a dual horizontal/vertical delivery. Each: {name, resolution_x,
    resolution_y}. Empty when the project renders a single format (legacy)."""
    out = []
    for f in (project_settings or {}).get("formats") or []:
        name = str(f.get("name") or "").strip()
        x, y = int(f.get("resolution_x") or 0), int(f.get("resolution_y") or 0)
        if name and x > 0 and y > 0:
            out.append({"name": name, "resolution_x": x, "resolution_y": y})
    return out


def formats_env(formats: list[dict]) -> str:
    """Env encoding for the headless render: '16x9:1920x1080,9x16:1080x1920'."""
    return ",".join(f"{f['name']}:{f['resolution_x']}x{f['resolution_y']}"
                    for f in formats)


def _overlay_element_info(frames_dir: str, task: dict, version_label: str) -> None:
    """Burn an element breakdown HUD into each playblast frame: every element, the
    step it was loaded from, and the published animation version playing. Reads the
    `_pb_info.json` the render script wrote. Best-effort (no Pillow -> skip)."""
    import glob
    import json as _json

    info_path = os.path.join(frames_dir, "_pb_info.json")
    frames = sorted(glob.glob(os.path.join(frames_dir, "frame_*.png")))
    if not (os.path.isfile(info_path) and frames):
        return
    try:
        elements = (_json.load(open(info_path, encoding="utf-8")) or {}).get(
            "elements") or []
        from PIL import Image, ImageDraw, ImageFont
    except Exception:  # noqa: BLE001
        return

    def _mono(size):
        for name in ("DejaVuSansMono.ttf", "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except Exception:  # noqa: BLE001
                continue
        return ImageFont.load_default()

    title = f"{(task or {}).get('entity', '')}  ·  {version_label}"
    lines = [f"{'ELEMENT':<16}{'STEP':<10}ANIM"]
    for e in elements:
        lines.append(f"{e['id']:<16}{(e['step'] or '-'):<10}{e['anim'] or '-'}")
    font, tfont = _mono(15), _mono(17)
    pad, line_h = 8, 20

    for fp in frames:
        img = Image.open(fp).convert("RGB")
        d = ImageDraw.Draw(img, "RGBA")
        rows = [title] + lines
        fonts = [tfont] + [font] * len(lines)
        w = max(d.textlength(r, font=f) for r, f in zip(rows, fonts)) + pad * 2
        h = line_h * len(rows) + pad * 2
        d.rectangle([6, 6, 6 + w, 6 + h], fill=(0, 0, 0, 150))
        y = 6 + pad
        for r, f in zip(rows, fonts):
            d.text((6 + pad, y), r, font=f, fill=(255, 255, 255, 255))
            y += line_h
        img.save(fp)


def playblast_rel(task: dict, version_label: str, fmt: str = "",
                  kind: str = "playblast") -> str:
    """Where the playblast lands (relative to remote_root / local_root):
    07_dailies/<entity>/<step>/<version_label>_<kind>[_<fmt>].mp4
    `kind` is 'playblast' (default) or 'sweatbox' (Material-Preview review) so
    the two never overwrite each other's dailies clip."""
    suffix = f"_{fmt}" if fmt else ""
    return (f"07_dailies/{task['entity']}/{task['step']}/"
            f"{version_label}_{kind}{suffix}.mp4")


def _open_locally(path: str) -> None:
    """Open a rendered file with the OS default player. Best-effort."""
    import sys as _sys
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
        elif _sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:  # noqa: BLE001 — the file is still on disk
        print(f"could not auto-open {path}: {exc}")


# Sweatbox quality presets — the dialog's Draft/Standard/High dropdown.
# 'standard' is exactly the historical behavior. A project can reshape any of
# them via project_settings.json playblast.sweatbox_presets.<name>.<key>.
SWEATBOX_PRESETS = {
    # 'height' = the LANDSCAPE format's pixel height; every delivery format
    # scales to it with its aspect (and the 16x9/9x16 nesting) intact. 0 keeps
    # the project's delivery resolution untouched.
    # animation-timing checks: fastest readable
    "draft":    {"samples": 16, "height": 720, "resolution_percentage": 100,
                 "raytracing": False, "motion_blur": False},
    # the everyday shading review
    "standard": {"samples": 64, "height": 1080, "resolution_percentage": 100,
                 "raytracing": True, "motion_blur": False},
    # shader/lighting judgement (2K)
    "high":     {"samples": 128, "height": 1440, "resolution_percentage": 100,
                 "raytracing": True, "motion_blur": True},
}


def sweatbox_preset(settings: dict | None, name: str) -> dict:
    """The resolved values for a preset: built-in defaults overlaid with the
    project's playblast.sweatbox_presets.<name> block (partial overrides fine).
    Unknown names resolve to 'standard'."""
    name = (name or "standard").lower()
    base = dict(SWEATBOX_PRESETS.get(name) or SWEATBOX_PRESETS["standard"])
    pb = playblast_settings(settings or {})
    over = (pb.get("sweatbox_presets") or {}).get(name) or {}
    base.update({k: v for k, v in over.items() if k in base})
    return base


def _next_sweatbox_label(sftp, remote_root: str, task: dict,
                         base: str) -> str:
    """'<base>_vNNN' with the next free sweatbox number for this shot/step,
    found by scanning the dailies folder — every sweatbox run becomes its own
    ordered review item instead of overwriting an unversioned clip."""
    dd = f"07_dailies/{task['entity']}/{task['step']}"
    try:
        names = [str(d.get("name", ""))
                 for d in sftp.listdir(remote_root.rstrip("/") + "/" + dd)]
    except Exception:  # noqa: BLE001 — no dailies folder yet
        names = []
    pat = re.compile(r"_v(\d+)_sweatbox", re.IGNORECASE)
    nums = [int(m.group(1))
            for n in names if (m := pat.search(n))]
    return f"{base}_v{max(nums, default=0) + 1:03d}"


def run_playblast(cfg, creds, shot_blend: str, task_id: str,
                  dry_run: bool = False, preview: bool = False,
                  sweatbox: bool = False, label: str = "",
                  only_formats: list | None = None,
                  render_opts: dict | None = None) -> int:
    """Open the published shot .blend headless, render its frame range through the
    scene camera into a PNG sequence, encode an MP4, upload it to 07_dailies and
    attach it to the task's latest publish record. Mirrors run_turntable.

    `preview` renders the SAME clip but keeps it local: the MP4 lands beside the
    shot .blend and opens in the OS video player — nothing uploaded, no review
    record, works offline (the server is only asked, best-effort, for the HUD's
    task info).

    `sweatbox` renders with the viewport's Material-Preview look — a studio HDRI
    lights every shader and the shot's own lights are ignored — at higher EEVEE
    quality, so an animator can judge how shaders read and animate even before
    the shot is lit. It uploads to dailies as a '<label>_sweatbox.mp4' review
    clip, kept separate from the normal '_playblast.mp4'. `label` overrides the
    dailies filename stem (defaults to the shot .blend basename).

    `only_formats` renders just these delivery-format names (e.g. ['9x16']) —
    the Sweatbox's per-format tick boxes. The project's PRIMARY format still
    defines the nesting base, so a narrow format rendered alone is framed
    identically to the same format rendered alongside the wide one."""
    from .sftp import SFTPClient
    from . import tasks
    from .launcher import find_blender, _resolve_ocio
    from .turntable import (_encode_mp4, _cleanup_dir, _meta_fps,
                            _load_project_settings, _bundled_path, record_turntable)

    local_root = cfg.resolved_local_root()
    kind = "sweatbox" if sweatbox else "playblast"
    version_label = ("preview" if preview
                     else label
                     or os.path.splitext(os.path.basename(shot_blend))[0])

    task = None
    if not dry_run:
        try:
            with SFTPClient(creds, dry_run=dry_run) as client:
                task = tasks.get_task(client, cfg.remote_root, task_id)
                # A sweatbox renders PUBLISHED state, not a versioned work
                # file, so its label carries no version of its own ("SH0010",
                # or worse the build file's stem). Number the clips per shot
                # instead: scan the dailies folder and take the next free
                # v###, so every run is a distinct, ordered review item.
                if sweatbox and not preview and task:
                    version_label = _next_sweatbox_label(
                        client, cfg.remote_root, task,
                        label or task.get("entity", "").split("/")[-1]
                        or "shot")
        except Exception as exc:  # noqa: BLE001 — preview renders offline
            if not preview:
                raise
            print(f"(preview) server unreachable — rendering without task "
                  f"info: {exc}")
        if not task and not preview:
            print(f"error: task not found: {task_id}")
            return 1

    settings = _load_project_settings(local_root)
    pb = playblast_settings(settings)
    # Dual-delivery projects render every format (e.g. 16:9 + 9:16). A single
    # unnamed format keeps the legacy one-clip behavior/naming.
    formats = delivery_formats(settings) or [
        {"name": "", "resolution_x": pb["resolution_x"],
         "resolution_y": pb["resolution_y"]}]
    # The project's FIRST format is the nesting base (a narrower format renders
    # as a centred slice of it). Capture it before filtering so ticking only
    # 9x16 still yields the same framing it has in a full dual-format render.
    base_fmt = formats[0]
    if only_formats:
        keep = [f for f in formats if f["name"] in only_formats]
        if keep:
            formats = keep
        else:
            print(f"warning: none of {only_formats} match the project's "
                  f"delivery formats — rendering all.")
    t = task or {"entity": "?", "step": "?"}

    def _out_local(fmt_name: str) -> str:
        if preview:
            suffix = f"_{fmt_name}" if fmt_name else ""
            return os.path.join(os.path.dirname(os.path.abspath(shot_blend)),
                                f"{kind}_preview{suffix}.mp4")
        frel = playblast_rel(t, version_label, fmt_name, kind)
        return os.path.join(local_root, *frel.split("/"))

    # Sweatbox quality: preset values (Draft/Standard/High, project-tunable)
    # plus the dialog's per-run advanced overrides. The preset 'height' scales
    # every delivery format from the LANDSCAPE base — aspect and the 16x9/9x16
    # nesting survive because everything scales by one factor.
    ro = dict(render_opts or {})
    preset = sweatbox_preset(settings, ro.get("preset", "standard")) \
        if sweatbox else None
    if preset:
        h = int(preset.get("height") or 0)
        if h > 0 and base_fmt["resolution_y"] > 0:
            k = h / base_fmt["resolution_y"]

            def _scaled(f):
                return dict(f,
                            resolution_x=max(2, round(f["resolution_x"] * k
                                                      / 2) * 2),
                            resolution_y=max(2, round(f["resolution_y"] * k
                                                      / 2) * 2))
            base_fmt = _scaled(base_fmt)
            formats = [_scaled(f) for f in formats]

    out_local = _out_local(formats[0]["name"])

    if dry_run:
        for f in formats:
            dest = (_out_local(f["name"]) if preview
                    else "publish -> " + playblast_rel(t, version_label,
                                                       f["name"], kind))
            print(f"(dry-run) would playblast {shot_blend} "
                  f"[{f['name'] or 'default'} {f['resolution_x']}x"
                  f"{f['resolution_y']}]\n          {dest}")
        return 0

    blender = find_blender(cfg.blender_path)
    if not blender:
        print("error: Blender not found for playblast render.")
        return 1

    frames_dir = os.path.join(os.path.dirname(out_local),
                              f"_pb_frames_{version_label}")
    env = os.environ.copy()
    ocio = _resolve_ocio(local_root)
    if ocio:
        env["BLENDER_OCIO"] = ocio
    env.update({
        "FLUMEN_PB_FRAMES_DIR": frames_dir,
        "FLUMEN_PB_RESX": str(formats[0]["resolution_x"]),
        "FLUMEN_PB_RESY": str(formats[0]["resolution_y"]),
        "FLUMEN_PB_COLOR": str(pb.get("color", "TEXTURE")),
        "FLUMEN_PB_VIEW": str(pb.get("view_transform", "")),
        "FLUMEN_PB_AUTOLIGHT": "0" if pb.get("auto_light") is False else "1",
        # Sweatbox forces EEVEE (never Workbench) + Material-Preview HDRI, and a
        # higher sample count for cleaner shading than the fast daily playblast.
        "FLUMEN_PB_ENGINE": ("BLENDER_EEVEE_NEXT" if sweatbox
                             else str(pb["engine"])),
        "FLUMEN_PB_SWEATBOX": "1" if sweatbox else "0",
        "FLUMEN_PB_SAMPLES": str(pb.get("sweatbox_samples", 64) if sweatbox
                                 else pb.get("samples", 16)),
        "FLUMEN_PB_RESPCT": str(pb.get("resolution_percentage", 100)),
        # Nesting base = the project's primary format, independent of which
        # formats were ticked (see only_formats).
        "FLUMEN_PB_BASE": (f"{base_fmt['resolution_x']}x"
                           f"{base_fmt['resolution_y']}"),
    })
    if preset:
        env["FLUMEN_PB_SAMPLES"] = str(preset["samples"])
        env["FLUMEN_PB_RESPCT"] = str(preset["resolution_percentage"])
        env["FLUMEN_PB_RT"] = "1" if preset["raytracing"] else "0"
        env["FLUMEN_PB_MBLUR"] = "1" if preset["motion_blur"] else "0"
        if ro.get("hdri"):
            env["FLUMEN_PB_SWEATBOX_HDRI"] = str(ro["hdri"])
        if ro.get("hdri_strength"):
            env["FLUMEN_PB_SWEATBOX_STRENGTH"] = str(ro["hdri_strength"])
        if ro.get("cull") is not None:
            env["FLUMEN_PB_CULL"] = "1" if ro["cull"] else "0"
    if len(formats) > 1 or formats[0]["name"]:
        env["FLUMEN_PB_FORMATS"] = formats_env(formats)

    script = _bundled_path("blender_playblast.py")
    print("Rendering playblast frames…")
    # --factory-startup: the render needs NOTHING from the user's Blender —
    # the script reads FLUMEN_PB_* env and OCIO comes via BLENDER_OCIO. The
    # artist's add-on stack otherwise loads into the headless render, where
    # it's pure risk: HardOps dies on GPU calls in background mode, and a
    # sweatbox render on Windows crashed outright (0xC0000409) with the full
    # stack loaded. Third-party addons belong in interactive sessions only.
    subprocess.run([blender, "--background", "--factory-startup",
                    shot_blend, "--python", script],
                   env=env, check=True)

    # One Blender session rendered every format; encode + upload each.
    from . import syncsketch
    outputs = []      # (fmt_name, rel, local_path)
    fps = _meta_fps(frames_dir, pb["fps"])
    for f in formats:
        fdir = (os.path.join(frames_dir, f["name"]) if f["name"] else frames_dir)
        if not os.path.isdir(fdir):
            print(f"error: no frames rendered for format "
                  f"'{f['name'] or 'default'}'.")
            continue
        # The element-breakdown HUD only fits the wide format: on a portrait
        # (9:16) frame it covers half the picture. Reviewers read the
        # breakdown off the 16:9 clip; the vertical delivery stays clean.
        if int(f["resolution_x"]) >= int(f["resolution_y"]):
            _overlay_element_info(fdir, task, version_label)
        frel = playblast_rel(t, version_label, f["name"], kind)
        flocal = _out_local(f["name"])
        print(f"Encoding MP4 -> {flocal}")
        if _encode_mp4(fdir, flocal, fps) and os.path.isfile(flocal):
            outputs.append((f["name"], frel, flocal))
    _cleanup_dir(frames_dir)
    if not outputs:
        print("error: playblast encode produced no file.")
        return 1

    if preview:
        for _name, _frel, flocal in outputs:
            print(f"preview rendered -> {flocal}")
        _open_locally(outputs[0][2])
        print("preview opened — nothing uploaded.")
        return 0

    with SFTPClient(creds) as client:
        rr = cfg.remote_root.rstrip("/")
        for _name, frel, flocal in outputs:
            client.upload(flocal, rr + "/" + frel)
        # Every format is its own Dailies review item (16:9 + 9:16 both show);
        # they share one review status — approving the shot approves both.
        if sweatbox:
            # A sweatbox is NOT a publish's review clip — it's rendered from
            # published state by whoever wants to look at it. Attaching it to
            # publishes[-1] (as record_turntable does) shows THAT publish's
            # artist in Dailies instead of the person who ran the sweatbox.
            from .turntable import record_sweatbox
            record_sweatbox(client, cfg.remote_root, task_id,
                            [frel for _n, frel, _l in outputs], creds.user)
        else:
            record_turntable(client, cfg.remote_root, task_id, outputs[0][1],
                             creds.user,
                             extra_rels=[frel for _n, frel, _l in outputs[1:]])
        for _name, frel, flocal in outputs:
            syncsketch.announce_media(client, cfg.remote_root, flocal,
                                      os.path.basename(frel))
    for _name, frel, _local in outputs:
        print(f"published playblast -> {cfg.remote_root}/{frel}")
    return 0
