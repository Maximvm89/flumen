"""Turntable orchestration: render a model turntable headlessly and publish the
video into 07_dailies. Pure helpers are unit-testable; run_turntable drives the
headless Blender render + upload.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def _bundled_path(name: str) -> str:
    """Path to a file shipped alongside this module. When frozen by PyInstaller
    the source isn't on disk, so resolve under the bundle's animpipe/ data dir;
    otherwise resolve next to this file."""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "animpipe", name)
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
}


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
    """Attach the turntable to the task's most recent publish entry + ledger."""
    from . import tasks, ledger
    task = tasks.get_task(sftp, remote_root, task_id)
    if not task:
        return None
    if task.get("publishes"):
        task["publishes"][-1]["turntable"] = rel
    tasks.save_task(sftp, remote_root, task, actor=username)
    ledger.record_uploads(sftp, remote_root, username, [rel])
    return rel


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
                  dry_run: bool = False, preview: bool = False) -> int:
    """Render a turntable from model_path and publish it to 07_dailies. With
    preview=True, open Blender interactively through the turntable camera (no
    render, no publish) so framing/scale can be dialed in."""
    from .sftp import SFTPClient
    from . import tasks
    from .launcher import find_blender, _resolve_ocio

    local_root = cfg.resolved_local_root()
    version_label = os.path.splitext(os.path.basename(model_path))[0]  # panda_model_v003

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
        "LEGAMI_TT_OUTPUT": out_local,
        "LEGAMI_TT_FRAMES_DIR": frames_dir,
        "LEGAMI_TT_FRAMES": str(settings["frames"]),
        "LEGAMI_TT_RESX": str(settings["resolution_x"]),
        "LEGAMI_TT_RESY": str(settings["resolution_y"]),
        "LEGAMI_TT_FPS": str(settings["fps"]),
        "LEGAMI_TT_ENGINE": str(settings["engine"]),
        "LEGAMI_TT_VIEW": str(settings.get("view_transform", "")),
    })

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
        env["LEGAMI_TT_MODEL"] = model_path
        env["LEGAMI_TT_CONTROL"] = str(settings.get("template_control", ""))
        env["LEGAMI_TT_REMOVE"] = "||".join(settings.get("template_remove") or [])
        env["LEGAMI_TT_GROUND"] = str(settings.get("template_ground", ""))
        env["LEGAMI_TT_FIT"] = str(settings.get("template_fit", ""))
        env["LEGAMI_TT_FIT_SCALE"] = str(settings.get("template_fit_scale", 1.0))
        env["LEGAMI_TT_FIT_MODE"] = str(settings.get("template_fit_mode", "box"))
        env["LEGAMI_TT_STAMP"] = "1" if settings.get("stamp", True) else "0"
        env["LEGAMI_TT_LOCATOR"] = locator

    script = _bundled_path("blender_turntable.py")
    if preview:
        # Interactive: launch Blender with a window (no --background), set up the
        # framing, and leave it open. Blocks until the artist closes Blender.
        env["LEGAMI_TT_PREVIEW"] = "1"
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
        record_turntable(client, cfg.remote_root, task_id, rel, creds.user)
    print(f"published turntable -> {cfg.remote_root}/{rel}")
    return 0
