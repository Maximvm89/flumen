"""Final shot render: open the lighting task's latest saved work file headless,
apply the project's final render settings (with optional per-shot overrides),
render a PNG sequence into 06_renders, encode a review MP4 into 07_dailies, and
record it on the task.

The lighter assembles + lights the shot and saves a work file; this renders
exactly that. Reuses the turntable/playblast encode + record plumbing.
"""

from __future__ import annotations

import glob
import os
import subprocess


def _project_render(settings: dict) -> dict:
    return (settings or {}).get("render") or {}


def render_frames_rel(entity: str) -> str:
    return f"06_renders/{entity}/lighting"


def render_video_rel(entity: str) -> str:
    leaf = entity.split("/")[-1]
    return f"07_dailies/{entity}/lighting/{leaf}_lighting_render.mp4"


def _latest_work_blend(client, cfg, task, local_root: str) -> str | None:
    """The newest .blend in the lighting task's work folder — local if present,
    otherwise fetched from the server (work files sync there)."""
    from . import tasks as T
    work_rel = T.task_work_rel(task)
    work_dir = os.path.join(local_root, *work_rel.split("/"))
    local = sorted(glob.glob(os.path.join(work_dir, "*.blend")), reverse=True)
    if local:
        return local[0]
    rr = cfg.remote_root.rstrip("/")
    names = sorted((e["name"] for e in client.listdir(rr + "/" + work_rel)
                    if e["name"].endswith(".blend")), reverse=True)
    if not names:
        return None
    os.makedirs(work_dir, exist_ok=True)
    dest = os.path.join(work_dir, names[0])
    client.download(rr + "/" + work_rel + "/" + names[0], dest)
    return dest


def run_render(cfg, creds, task_id: str, samples: int | None = None,
               respct: int | None = None, start: int | None = None,
               end: int | None = None, dry_run: bool = False) -> int:
    from .sftp import SFTPClient
    from . import tasks as T
    from .launcher import find_blender, _resolve_ocio
    from .turntable import (_encode_mp4, _meta_fps, _load_project_settings,
                            _bundled_path, record_turntable)

    local_root = cfg.resolved_local_root()
    settings = _load_project_settings(local_root)
    rnd = _project_render(settings)

    with SFTPClient(creds, dry_run=dry_run) as client:
        task = T.get_task(client, cfg.remote_root, task_id) if not dry_run else \
            {"entity": "?", "step": "lighting", "id": task_id}
        if not task or task.get("type", "shot") != "shot":
            print(f"error: not a shot task: {task_id}")
            return 1
        entity = task["entity"]
        blend = None if dry_run else _latest_work_blend(client, cfg, task,
                                                        local_root)
    if not dry_run and not blend:
        print("error: no lighting work file to render — the lighter must save "
              "the shot into the task first.")
        return 1

    frames_rel = render_frames_rel(entity)
    frames_dir = os.path.join(local_root, *frames_rel.split("/"))
    video_rel = render_video_rel(entity)
    video_local = os.path.join(local_root, *video_rel.split("/"))

    if dry_run:
        print(f"(dry-run) would render {entity} lighting work file")
        print(f"          PNG sequence -> {frames_rel}")
        print(f"          review video -> {video_rel}")
        return 0

    blender = find_blender(cfg.blender_path)
    if not blender:
        print("error: Blender not found for the render.")
        return 1

    env = os.environ.copy()
    ocio = _resolve_ocio(local_root)
    if ocio:
        env["BLENDER_OCIO"] = ocio
    engine = str(rnd.get("engine", "BLENDER_EEVEE"))
    cyc = rnd.get("cycles") or {}
    eev = rnd.get("eevee") or {}
    # Samples: override wins, else the engine's own project block.
    proj_samples = (cyc.get("samples") if "CYCLES" in engine
                    else eev.get("taa_render_samples"))
    env.update({
        "FLUMEN_RENDER_FRAMES_DIR": frames_dir,
        "FLUMEN_RENDER_ENGINE": engine,
        "FLUMEN_RENDER_RESX": str(rnd.get("resolution_x", "")),
        "FLUMEN_RENDER_RESY": str(rnd.get("resolution_y", "")),
        "FLUMEN_RENDER_RESPCT": str(respct if respct is not None
                                    else rnd.get("resolution_percentage", 100)),
        "FLUMEN_RENDER_SAMPLES": str(samples if samples is not None
                                     else (proj_samples or "")),
        "FLUMEN_RENDER_DENOISE": "0" if cyc.get("use_denoising") is False else "1",
        "FLUMEN_RENDER_DEVICE": str(cyc.get("device", "")),
        # EEVEE raytracing — the finals' engine; the eye-shader switch needs it.
        "FLUMEN_RENDER_RAYTRACING":
            "1" if eev.get("use_raytracing") else "0",
        "FLUMEN_RENDER_FPS": str(rnd.get("fps", "")),
        "FLUMEN_RENDER_FILM_TRANSPARENT":
            "1" if rnd.get("film_transparent") else "0",
        "FLUMEN_RENDER_DEPTH": str((settings.get("output") or {})
                                   .get("color_depth", "16")),
    })
    if start is not None and end is not None:
        env["FLUMEN_RENDER_START"], env["FLUMEN_RENDER_END"] = str(start), str(end)

    script = _bundled_path("blender_render.py")
    os.makedirs(frames_dir, exist_ok=True)
    print(f"Rendering {entity} lighting … (this is a FINAL render — it can be "
          f"slow)")
    rc = subprocess.call([blender, "--background", blend, "--python", script],
                         env=env)
    frames = sorted(glob.glob(os.path.join(frames_dir, "frame_*.png")))
    if rc != 0 or not frames:
        print(f"error: render produced no frames (Blender exit {rc}).")
        return 1
    print(f"Rendered {len(frames)} frame(s) -> {frames_rel}")

    fps = rnd.get("fps", 24)
    os.makedirs(os.path.dirname(video_local), exist_ok=True)
    made = _encode_mp4(frames_dir, video_local, fps)

    with SFTPClient(creds) as client:
        rr = cfg.remote_root.rstrip("/")
        for f in frames:
            rel = frames_rel + "/" + os.path.basename(f)
            client.upload(f, rr + "/" + rel)
        if made and os.path.isfile(video_local):
            client.upload(video_local, rr + "/" + video_rel)
            record_turntable(client, cfg.remote_root, task_id, video_rel,
                             creds.user)
    print(f"published render -> {cfg.remote_root}/{frames_rel}")
    if made:
        print(f"review video    -> {cfg.remote_root}/{video_rel}")
    return 0
