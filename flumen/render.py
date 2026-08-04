"""Final shot render: open the lighting task's newest PUBLISHED shot file headless,
apply the project's final render settings (with optional per-shot overrides),
render a PNG sequence into 06_renders, encode a review MP4 into 07_dailies, and
record it on the task.

The lighter runs 'Publish shot' to save + publish the whole scene as the render
ground truth; this renders exactly that published file (never an unpublished work
file), auto-fetching any linked cache/library the render machine is missing at the
exact versions the shot uses. Reuses the turntable/playblast encode + record
plumbing.
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


def _published_shot_blend(client, cfg, task, local_root: str):
    """The newest PUBLISHED shot .blend (kind 'shot') for this lighting task — the
    render ground truth — fetched into the local mirror. Returns (blend_local,
    deps_rel), or (None, '') if nothing has been published (render is
    published-only: the lighter must run 'Publish shot' first)."""
    from . import tasks as T
    shots = T.published_shot_files(task)
    if not shots:
        return None, ""
    top = shots[0]
    rr = cfg.remote_root.rstrip("/")
    dest = os.path.join(local_root, *top["rel"].split("/"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    client.download(rr + "/" + top["rel"], dest)
    return dest, top.get("deps_rel") or ""


def _ensure_dependencies(client, cfg, deps_rel: str, local_root: str):
    """Download any dependency the published shot references (linked library,
    alembic cache, texture) that is missing from the local mirror, at the EXACT
    rel the lighting file used. Returns (fetched, missing): rels pulled now, and
    rels not found on the server (the exact version is gone — render can't be
    faithful, so the caller flags these and aborts)."""
    import json
    from .cli import _fetch_sidecar_textures
    rr = cfg.remote_root.rstrip("/")
    fetched, missing = [], []
    if not deps_rel:
        return fetched, missing
    txt = client.read_text(rr + "/" + deps_rel)
    try:
        manifest = json.loads(txt) if txt else {}
    except ValueError:
        manifest = {}
    tex_seen: set = set()
    for d in manifest.get("deps") or []:
        rel = d.get("rel") or ""
        if not rel:
            continue
        local = os.path.join(local_root, *rel.split("/"))
        if os.path.isfile(local):
            continue                                 # already mirrored
        if not client.exists(rr + "/" + rel):
            missing.append(rel)                      # exact version gone
            continue
        os.makedirs(os.path.dirname(local), exist_ok=True)
        client.download(rr + "/" + rel, local)
        fetched.append(rel)
        # Linked publishes reference //textures/* — pull the sidecar images too,
        # else a fresh render machine shows pink where a cache/env was fetched.
        if d.get("kind") == "library" and rel.endswith(".blend"):
            _fetch_sidecar_textures(client, rr, rel, os.path.dirname(local),
                                    tex_seen)
    return fetched, missing


def queue_dir(local_root: str) -> str:
    """The central folder of queue-ready .blend files for an external render
    manager (BRQ): drag its contents into the queue in one go."""
    return os.path.join(local_root, "06_renders", "_brq_queue")


def run_render(cfg, creds, task_id: str, samples: int | None = None,
               respct: int | None = None, start: int | None = None,
               end: int | None = None, dry_run: bool = False,
               prep_queue: bool = False, blend_override: str = "") -> int:
    """Render the newest PUBLISHED lighting shot — or, with `prep_queue`, STOP
    short of rendering and save a queue-ready copy (project render settings,
    output path and frame range baked in, library paths absolute) into
    queue_dir() for an external render manager (BRQ renders the file as-is
    with the file's own settings). `blend_override` preps a specific .blend
    (the last sweatbox build, any file) instead of the lighting publish; its
    missing linked libraries are fetched first."""
    from .sftp import SFTPClient
    from . import tasks as T
    from .launcher import find_blender, _resolve_ocio
    from .turntable import (_encode_mp4, _meta_fps, _load_project_settings,
                            _bundled_path, record_turntable)

    local_root = cfg.resolved_local_root()

    with SFTPClient(creds, dry_run=dry_run) as client:
        # Refresh project_settings.json from the server first — the render
        # settings (engine EEVEE vs Cycles, samples, raytracing) are the single
        # source of truth, and the local mirror only syncs on a Blender launch.
        if not dry_run:
            try:
                ps_rel = "02_pipeline/project_settings.json"
                client.download(cfg.remote_root.rstrip("/") + "/" + ps_rel,
                                os.path.join(local_root, *ps_rel.split("/")))
            except Exception:  # noqa: BLE001 — fall back to the local copy
                pass
        task = T.get_task(client, cfg.remote_root, task_id) if not dry_run else \
            {"entity": "?", "step": "lighting", "id": task_id}
        if not task or task.get("type", "shot") != "shot":
            print(f"error: not a shot task: {task_id}")
            return 1
        entity = task["entity"]
        blend, missing_deps = None, []
        if blend_override:
            # Prep an explicit file (last sweatbox build, any .blend): not a
            # publish, so no deps manifest — scan the file itself and fetch
            # whatever the local mirror lacks (same preflight as opening).
            blend = os.path.abspath(os.path.expanduser(blend_override))
            if not os.path.isfile(blend):
                print(f"error: no such file: {blend}")
                return 1
            if not dry_run:
                from . import blend_deps
                _, failed = blend_deps.fetch_missing_libraries(
                    client, cfg.remote_root, local_root, blend, log=print)
                missing_deps = failed
        elif not dry_run:
            blend, deps_rel = _published_shot_blend(client, cfg, task, local_root)
            if blend:
                fetched, missing_deps = _ensure_dependencies(
                    client, cfg, deps_rel, local_root)
                if fetched:
                    print(f"fetched {len(fetched)} missing dependency file(s) "
                          f"for the render (exact versions the shot uses).")

    settings = _load_project_settings(local_root)
    rnd = _project_render(settings)
    if not dry_run and not blend:
        print("error: no PUBLISHED shot to render — run 'Publish shot' from the "
              "lighting task first. Render uses the published file as ground "
              "truth, not the work file.")
        return 1
    if not dry_run and missing_deps:
        print("error: the published shot references dependency file(s) that are "
              "no longer on the server (exact versions gone):")
        for rel in missing_deps:
            print(f"  missing: {rel}")
        print("re-cache the missing element(s) and Publish shot again, then retry.")
        return 1

    frames_rel = render_frames_rel(entity)
    frames_dir = os.path.join(local_root, *frames_rel.split("/"))
    video_rel = render_video_rel(entity)
    video_local = os.path.join(local_root, *video_rel.split("/"))

    if dry_run:
        what = ("prep for the render queue" if prep_queue else "render")
        print(f"(dry-run) would {what} {entity} "
              + (os.path.basename(blend_override) if blend_override
                 else "newest PUBLISHED lighting shot"))
        print(f"          PNG sequence -> {frames_rel}")
        if not prep_queue:
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
    if prep_queue:
        # Stamp everything the render would use INTO a copy and stop — an
        # external render manager (BRQ) renders that file as-is.
        stem = os.path.splitext(os.path.basename(blend))[0]
        pre = entity.replace("/", "_")
        name = stem if stem.startswith((pre, entity.split("/")[-1])) \
            else f"{pre}_{stem}"          # don't double an entity-named stem
        prep_out = os.path.join(queue_dir(local_root), f"{name}_brq.blend")
        env["FLUMEN_PREP_OUT"] = prep_out
        print(f"Preparing {entity} for the render queue …")
        rc = subprocess.call([blender, "--background", "--factory-startup",
                              blend, "--python", script], env=env)
        if rc != 0 or not os.path.isfile(prep_out):
            print(f"error: prep produced no file (Blender exit {rc}).")
            return 1
        print(f"queue-ready -> {prep_out}")
        print(f"frames will land in -> {frames_rel}")
        return 0
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

    total_mb = sum(os.path.getsize(f) for f in frames) / 1e6
    print(f"Uploading {len(frames)} frame(s) ({total_mb:.0f} MB) + review "
          f"video to the server …", flush=True)
    with SFTPClient(creds) as client:
        rr = cfg.remote_root.rstrip("/")
        for i, f in enumerate(frames, 1):
            rel = frames_rel + "/" + os.path.basename(f)
            client.upload(f, rr + "/" + rel)
            if i % 10 == 0 or i == len(frames):
                print(f"  uploaded {i}/{len(frames)} frames", flush=True)
        if made and os.path.isfile(video_local):
            client.upload(video_local, rr + "/" + video_rel)
            record_turntable(client, cfg.remote_root, task_id, video_rel,
                             creds.user)
            print("  uploaded review video", flush=True)
    print(f"published render -> {cfg.remote_root}/{frames_rel}")
    if made:
        print(f"review video    -> {cfg.remote_root}/{video_rel}")
    return 0
