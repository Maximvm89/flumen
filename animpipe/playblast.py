"""Shot playblast: render a shot's frame range through its camera headlessly and
publish the video into 07_dailies, attached to the publish record (so it appears in
Dailies review exactly like a model turntable). Reuses the turntable encode/upload
plumbing; the render is a fast Workbench/EEVEE pass over the shot camera.
"""

from __future__ import annotations

import os
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
}


def playblast_settings(project_settings: dict) -> dict:
    s = dict(PB_DEFAULTS)
    s.update((project_settings or {}).get("playblast") or {})
    return s


def playblast_rel(task: dict, version_label: str) -> str:
    """Where the playblast lands (relative to remote_root / local_root):
    07_dailies/<entity>/<step>/<version_label>_playblast.mp4"""
    return f"07_dailies/{task['entity']}/{task['step']}/{version_label}_playblast.mp4"


def run_playblast(cfg, creds, shot_blend: str, task_id: str,
                  dry_run: bool = False) -> int:
    """Open the published shot .blend headless, render its frame range through the
    scene camera into a PNG sequence, encode an MP4, upload it to 07_dailies and
    attach it to the task's latest publish record. Mirrors run_turntable."""
    from .sftp import SFTPClient
    from . import tasks
    from .launcher import find_blender, _resolve_ocio
    from .turntable import (_encode_mp4, _cleanup_dir, _meta_fps,
                            _load_project_settings, _bundled_path, record_turntable)

    local_root = cfg.resolved_local_root()
    version_label = os.path.splitext(os.path.basename(shot_blend))[0]

    task = None
    if not dry_run:
        with SFTPClient(creds, dry_run=dry_run) as client:
            task = tasks.get_task(client, cfg.remote_root, task_id)
        if not task:
            print(f"error: task not found: {task_id}")
            return 1

    pb = playblast_settings(_load_project_settings(local_root))
    rel = playblast_rel(task or {"entity": "?", "step": "?"}, version_label)
    out_local = os.path.join(local_root, *rel.split("/"))

    if dry_run:
        print(f"(dry-run) would playblast {shot_blend}\n"
              f"          -> {out_local}\n          publish -> {rel}")
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
        "LEGAMI_PB_FRAMES_DIR": frames_dir,
        "LEGAMI_PB_RESX": str(pb["resolution_x"]),
        "LEGAMI_PB_RESY": str(pb["resolution_y"]),
        "LEGAMI_PB_ENGINE": str(pb["engine"]),
        "LEGAMI_PB_COLOR": str(pb.get("color", "TEXTURE")),
        "LEGAMI_PB_VIEW": str(pb.get("view_transform", "")),
    })

    script = _bundled_path("blender_playblast.py")
    print("Rendering playblast frames…")
    subprocess.run([blender, "--background", shot_blend, "--python", script],
                   env=env, check=True)

    fps = _meta_fps(frames_dir, pb["fps"])
    print(f"Encoding MP4 -> {out_local}")
    ok = _encode_mp4(frames_dir, out_local, fps)
    _cleanup_dir(frames_dir)
    if not ok or not os.path.isfile(out_local):
        print("error: playblast encode produced no file.")
        return 1

    with SFTPClient(creds) as client:
        client.upload(out_local, cfg.remote_root.rstrip("/") + "/" + rel)
        record_turntable(client, cfg.remote_root, task_id, rel, creds.user)
    print(f"published playblast -> {cfg.remote_root}/{rel}")
    return 0
