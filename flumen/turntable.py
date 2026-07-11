"""Turntable orchestration: render a model turntable headlessly and publish the
video into 07_dailies. Pure helpers are unit-testable; run_turntable drives the
headless Blender render + upload.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def _bundled_path(name: str) -> str:
    """Path to a file shipped alongside this module. When frozen by PyInstaller
    the source isn't on disk, so resolve under the bundle's flumen/ data dir;
    otherwise resolve next to this file."""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "flumen", name)
    return os.path.join(os.path.dirname(__file__), name)


DEFAULTS = {
    "engine": "EEVEE",
    "resolution_x": 1280,
    "resolution_y": 720,
    "frames": 120,
    "fps": 24,
    "view_transform": "ACES 1.0 - SDR Video",
    # Optional: use an existing turntable .blend instead of the auto-built rig.
    "template": "",            # path relative to project root, e.g. dev/.../tt.blend
    "template_control": "",    # name of the empty/control to parent the model under
    "template_remove": [],     # object names to delete from the template (placeholders)
    "template_ground": "",     # object to rest the model on (uses its top surface)
    "template_fit": "",        # object whose volume the asset is scaled to fit (framing)
    "template_fit_scale": 1.0, # zoom knob: <1 = pull back/margin, >1 = fill more
    "template_fit_mode": "box",# box (whole bbox) | height (fill vertically) | width
    "stamp": True,             # burn the asset's real size + applied scale into the frames
    # Orientation: models are authored facing -Y (Blender's front view); the
    # template render yaws the asset so that front faces the camera at frame 1.
    "face_camera": True,
    "front_offset_deg": 0.0,   # extra yaw if a project's front convention differs
}


def face_camera_yaw(front_xy, to_cam_xy) -> float:
    """Signed Z rotation (radians) turning the asset's horizontal front
    direction onto the direction toward the camera. Zero when either vector is
    degenerate (camera straight above, no front)."""
    import math
    fx, fy = front_xy
    tx, ty = to_cam_xy
    if (abs(fx) < 1e-9 and abs(fy) < 1e-9) or (abs(tx) < 1e-9 and abs(ty) < 1e-9):
        return 0.0
    return math.atan2(fx * ty - fy * tx, fx * tx + fy * ty)


def turntable_settings(project_settings: dict) -> dict:
    s = dict(DEFAULTS)
    s.update((project_settings or {}).get("turntable") or {})
    return s


def dailies_rel(task: dict, version_label: str) -> str:
    """Where the turntable lands (relative to remote_root / local_root):
    07_dailies/<entity>/<step>/<version_label>_turntable.mp4"""
    return f"07_dailies/{task['entity']}/{task['step']}/{version_label}_turntable.mp4"


def record_turntable(sftp, remote_root: str, task_id: str, rel: str,
                     username: str) -> str | None:
    """Attach the turntable to the task's most recent publish entry + ledger,
    and mail the dailies notification (if the team configured one)."""
    from . import tasks, ledger, notify
    task = tasks.get_task(sftp, remote_root, task_id)
    if not task:
        return None
    rec = {}
    if task.get("publishes"):
        rec = task["publishes"][-1]
        rec["turntable"] = rel
    tasks.save_task(sftp, remote_root, task, actor=username)
    ledger.record_uploads(sftp, remote_root, username, [rel])
    notify.announce_dailies(sftp, remote_root, task, rec, [rel], username)
    return rel


def record_still(sftp, remote_root: str, task_id: str, rel: str,
                 username: str) -> str | None:
    """Attach a review still to the task as its OWN review item (task['stills']),
    independent of publish records — a still can be rendered at any point, not
    just after a publish. Also ledger + dailies notification."""
    from . import tasks, ledger, notify
    task = tasks.get_task(sftp, remote_root, task_id)
    if not task:
        return None
    rec = {"file": rel, "by": username, "time": time.time(),
           "review_status": "to_review"}
    task.setdefault("stills", []).append(rec)
    tasks.save_task(sftp, remote_root, task, actor=username)
    ledger.record_uploads(sftp, remote_root, username, [rel])
    notify.announce_dailies(sftp, remote_root, task, rec, [rel], username)
    return rel


def look_dailies_rel(entity: str, version_label: str, suffix: str) -> str:
    """Where a look-review artifact lands:
    07_dailies/<entity>/surface/<version_label>_<suffix>."""
    return f"07_dailies/{entity}/surface/{version_label}_{suffix}"


def record_review_media(sftp, remote_root: str, task_id: str, blend_rel: str,
                        username: str, *, turntable: str | None = None,
                        sheet: str | None = None) -> bool:
    """Attach review media (turntable mp4 and/or texture sheet) to the publish
    record that produced `blend_rel` — matched by file, NOT 'latest', since several
    looks may publish between renders. Returns True if a record matched."""
    from . import tasks, ledger, notify
    task = tasks.get_task(sftp, remote_root, task_id)
    if not task:
        return False
    hit = False
    matched = {}
    for rec in task.get("publishes") or []:
        if blend_rel in (rec.get("files") or []):
            if turntable:
                rec["turntable"] = turntable
            if sheet:
                rec["sheet"] = sheet
            matched = rec
            hit = True
    if hit:
        tasks.save_task(sftp, remote_root, task, actor=username)
        media = [r for r in (turntable, sheet) if r]
        ledger.record_uploads(sftp, remote_root, username, media)
        notify.announce_dailies(sftp, remote_root, task, matched, media, username)
    return hit


def _export_uv_only(cfg, model_path: str, uv_out: str) -> int:
    """Open the model headless and dump its UV wireframe (no render) — the fast path
    for a sheet‑only review."""
    from .launcher import find_blender, _resolve_ocio
    blender = find_blender(cfg.blender_path)
    if not blender:
        print("error: Blender not found for UV export.")
        return 1
    local_root = cfg.resolved_local_root()
    settings = turntable_settings(_load_project_settings(local_root))
    env = os.environ.copy()
    ocio = _resolve_ocio(local_root)
    if ocio:
        env["BLENDER_OCIO"] = ocio
    env["FLUMEN_TT_UV_ONLY"] = "1"
    env["FLUMEN_LR_UV_OUT"] = uv_out
    env["FLUMEN_TT_LOCATOR"] = (_load_project_settings(local_root).get("publish")
                                or {}).get("locator") or "PUBLISH"
    script = _bundled_path("blender_turntable.py")
    try:
        subprocess.run([blender, "--background", model_path, "--python", script],
                       env=env, check=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print("error: UV export failed:", exc)
        return 1


def run_look_review(cfg, creds, *, task_id: str, entity: str, base: str,
                    version: int, model_path: str, look_blend: str,
                    manifest_path: str, blend_rel: str, hdri: str | None = None,
                    sheet_only: bool = False, dry_run: bool = False) -> int:
    """Render a shaded look turntable + texture/UV sheet and publish both into
    07_dailies, attached to the look's publish record. The turntable REUSES the
    model turntable pipeline (run_turntable) — same template, framing and lighting —
    just with the look applied to the model. The sheet is built from the published
    tiles + the UV wireframe the render dumps. With sheet_only=True it skips the
    (slow) turntable and just regenerates the texture/UV sheet."""
    import tempfile
    from .sftp import SFTPClient
    from . import texsheet

    local_root = cfg.resolved_local_root()
    version_label = f"{base}_v{version:03d}"
    tt_rel = dailies_rel({"entity": entity, "step": "surface"}, version_label)
    sheet_rel = look_dailies_rel(entity, version_label, "textures.png")
    sheet_local = os.path.join(local_root, *sheet_rel.split("/"))
    tiles_dir = os.path.join(os.path.dirname(look_blend), "textures", version_label)
    uv_json = os.path.join(tempfile.gettempdir(), f"_lr_uv_{version_label}.json")

    if dry_run:
        what = "texture/UV sheet only" if sheet_only else "turntable + sheet"
        print(f"(dry-run) would render look review ({what}) of {base} v{version:03d}\n"
              f"          sheet -> {sheet_rel}"
              + ("" if sheet_only else f"\n          turntable -> {tt_rel}"))
        return 0

    # 1) Shaded turntable via the SAME template/rig as model turntables — unless
    # we only want the sheet, in which case do a quick UV export (no render).
    if sheet_only:
        if _export_uv_only(cfg, model_path, uv_json) != 0:
            print("warning: UV export failed; sheet will have no UV panel.")
    else:
        rc = run_turntable(cfg, creds, model_path, task_id,
                           version_label=version_label, look_blend=look_blend,
                           manifest_path=manifest_path, uv_out=uv_json,
                           record_blend_rel=blend_rel)
        if rc:
            return rc

    # 2) Texture/UV sheet from the published tiles + the UV wireframe.
    entries = []
    if os.path.isfile(manifest_path):
        try:
            entries = json.load(open(manifest_path)).get("textures", [])
        except ValueError:
            entries = []
    try:
        os.makedirs(os.path.dirname(sheet_local), exist_ok=True)
        texsheet.build_sheet(tiles_dir, entries, sheet_local, uv_segments=uv_json,
                             title=f"{entity} · {base} · v{version:03d}")
    except Exception as exc:  # noqa: BLE001
        print(f"warning: texture sheet failed ({exc}); turntable published without it.")
        return 0

    with SFTPClient(creds) as client:
        client.upload(sheet_local, cfg.remote_root.rstrip("/") + "/" + sheet_rel)
        record_review_media(client, cfg.remote_root, task_id, blend_rel, creds.user,
                            sheet=sheet_rel)
    print(f"published look texture sheet -> {sheet_rel}")
    return 0


def _ffmpeg_exe() -> str:
    """Bundled ffmpeg (via imageio-ffmpeg) if available, else system ffmpeg."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def _encode_mp4(frames_dir: str, out_mp4: str, fps) -> bool:
    """Encode frame_####.png in frames_dir into an H.264 MP4."""
    import glob
    import re
    frames = sorted(glob.glob(os.path.join(frames_dir, "frame_*.png")))
    if not frames:
        print("error: no rendered frames found to encode.")
        return False
    start = re.findall(r"(\d+)", os.path.basename(frames[0]))[-1]
    cmd = [_ffmpeg_exe(), "-y", "-framerate", str(fps),
           "-start_number", str(int(start)),
           "-i", os.path.join(frames_dir, "frame_%04d.png"),
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", out_mp4]
    try:
        subprocess.run(cmd, check=True)
        return os.path.isfile(out_mp4)
    except Exception as exc:  # noqa: BLE001
        print("error: ffmpeg encode failed:", exc)
        return False


def _cleanup_dir(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _meta_fps(frames_dir: str, default) -> int:
    """fps the render actually used (templates define their own), via the meta
    sidecar the render script writes; falls back to the project default."""
    meta = os.path.join(frames_dir, "_tt_meta.json")
    try:
        with open(meta, encoding="utf-8") as fh:
            return int(json.load(fh).get("fps", default))
    except Exception:  # noqa: BLE001
        return int(default)


def _load_project_settings(local_root: str) -> dict:
    path = os.path.join(local_root, "02_pipeline", "project_settings.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except ValueError:
            return {}
    return {}


def run_turntable(cfg, creds, model_path: str, task_id: str,
                  dry_run: bool = False, preview: bool = False, *,
                  version_label: str | None = None, look_blend: str | None = None,
                  manifest_path: str | None = None, uv_out: str | None = None,
                  record_blend_rel: str | None = None) -> int:
    """Render a turntable from model_path and publish it to 07_dailies. Reused for
    look reviews: pass look_blend + manifest_path to shade the model with a published
    look first (same template rig), version_label to name the daily after the look,
    uv_out to dump the UV wireframe, and record_blend_rel to attach the turntable to a
    specific publish record. preview=True opens Blender interactively (no render)."""
    from .sftp import SFTPClient
    from . import tasks
    from .launcher import find_blender, _resolve_ocio

    local_root = cfg.resolved_local_root()
    version_label = version_label or os.path.splitext(os.path.basename(model_path))[0]

    need_task = not dry_run and not preview  # preview doesn't touch the task/server
    with SFTPClient(creds, dry_run=dry_run) as client:
        task = tasks.get_task(client, cfg.remote_root, task_id) if need_task else None
    if need_task and not task:
        print(f"error: task not found: {task_id}")
        return 1

    project_settings = _load_project_settings(local_root)
    settings = turntable_settings(project_settings)
    locator = (project_settings.get("publish") or {}).get("locator") or "PUBLISH"
    rel = dailies_rel(task or {"entity": "?", "step": "?"}, version_label)
    out_local = os.path.join(local_root, *rel.split("/"))
    template_rel = (settings.get("template") or "").strip()

    if dry_run:
        mode = f"template '{template_rel}'" if template_rel else "auto-built rig"
        print(f"(dry-run) would render turntable of {model_path} via {mode}\n"
              f"          -> {out_local}\n          publish -> {rel}")
        return 0

    blender = find_blender(cfg.blender_path)
    if not blender:
        print("error: Blender not found for turntable render.")
        return 1

    frames_dir = os.path.join(os.path.dirname(out_local),
                              f"_tt_frames_{version_label}")
    env = os.environ.copy()
    ocio = _resolve_ocio(local_root)
    if ocio:
        env["BLENDER_OCIO"] = ocio
    env.update({
        "FLUMEN_TT_OUTPUT": out_local,
        "FLUMEN_TT_FRAMES_DIR": frames_dir,
        "FLUMEN_TT_FRAMES": str(settings["frames"]),
        "FLUMEN_TT_RESX": str(settings["resolution_x"]),
        "FLUMEN_TT_RESY": str(settings["resolution_y"]),
        "FLUMEN_TT_FPS": str(settings["fps"]),
        "FLUMEN_TT_ENGINE": str(settings["engine"]),
        "FLUMEN_TT_VIEW": str(settings.get("view_transform", "")),
    })
    # Look-review extras: shade the model with the look + dump UVs (the template
    # script applies these after appending the model).
    if look_blend:
        env["FLUMEN_LR_LOOK"] = look_blend
        env["FLUMEN_LR_MANIFEST"] = manifest_path or ""
    if uv_out:
        env["FLUMEN_LR_UV_OUT"] = uv_out

    # Template mode: open the artist's turntable .blend and append the model into
    # it, parented under a named control. Otherwise open the model and auto-rig.
    blend_to_open = model_path
    if template_rel:
        template_local = os.path.join(local_root, *template_rel.split("/"))
        try:
            with SFTPClient(creds) as client:  # fetch the latest template
                client.download(cfg.remote_root.rstrip("/") + "/" + template_rel,
                                template_local)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: could not fetch template ({exc}); using local copy.")
        if not os.path.isfile(template_local):
            print(f"error: turntable template not found: {template_local}")
            return 1
        blend_to_open = template_local
        env["FLUMEN_TT_MODEL"] = model_path
        env["FLUMEN_TT_CONTROL"] = str(settings.get("template_control", ""))
        env["FLUMEN_TT_REMOVE"] = "||".join(settings.get("template_remove") or [])
        env["FLUMEN_TT_GROUND"] = str(settings.get("template_ground", ""))
        env["FLUMEN_TT_FIT"] = str(settings.get("template_fit", ""))
        env["FLUMEN_TT_FIT_SCALE"] = str(settings.get("template_fit_scale", 1.0))
        env["FLUMEN_TT_FIT_MODE"] = str(settings.get("template_fit_mode", "box"))
        env["FLUMEN_TT_FACE_CAMERA"] = ("1" if settings.get("face_camera", True)
                                        else "0")
        env["FLUMEN_TT_FRONT_OFFSET"] = str(settings.get("front_offset_deg", 0.0))
        env["FLUMEN_TT_STAMP"] = "1" if settings.get("stamp", True) else "0"
        env["FLUMEN_TT_LOCATOR"] = locator

    script = _bundled_path("blender_turntable.py")
    if preview:
        # Interactive: launch Blender with a window (no --background), set up the
        # framing, and leave it open. Blocks until the artist closes Blender.
        env["FLUMEN_TT_PREVIEW"] = "1"
        print("Opening turntable preview — look through the camera; close Blender "
              "when done. Tweak template_fit_scale / template_fit_mode to adjust.")
        subprocess.run([blender, blend_to_open, "--python", script],
                       env=env, check=True)
        return 0

    print(f"Rendering turntable frames ({'template' if template_rel else 'auto'})…")
    subprocess.run([blender, "--background", blend_to_open, "--python", script],
                   env=env, check=True)

    fps = _meta_fps(frames_dir, settings["fps"])
    print(f"Encoding MP4 -> {out_local}")
    ok = _encode_mp4(frames_dir, out_local, fps)
    _cleanup_dir(frames_dir)
    if not ok or not os.path.isfile(out_local):
        print("error: turntable encode produced no file.")
        return 1

    with SFTPClient(creds) as client:
        client.upload(out_local, cfg.remote_root.rstrip("/") + "/" + rel)
        if record_blend_rel:    # look review: attach to the exact look's record
            record_review_media(client, cfg.remote_root, task_id, record_blend_rel,
                                creds.user, turntable=rel)
        else:
            record_turntable(client, cfg.remote_root, task_id, rel, creds.user)
    print(f"published turntable -> {cfg.remote_root}/{rel}")
    return 0
