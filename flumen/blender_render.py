"""Headless FINAL render of a lighting work file, driven by env vars from
flumen.render.

Opens the lighter's saved scene (passed as the .blend), applies the project's
final render settings (engine/samples/resolution) with optional per-shot
overrides, renders the frame range through the shot camera to a PNG sequence,
and emits FLUMEN_PROGRESS lines the app's bar tracks. The caller encodes the
review MP4 and uploads.
"""

import os
import sys

import bpy


def _env(name, default=""):
    return os.environ.get(name, default)


def _fmt(sec):
    sec = int(sec)
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def _install_progress(scene):
    total = max(1, scene.frame_end - scene.frame_start + 1)
    st = {"start": None, "frame_start": None, "done": 0}

    def _pre(*_a):
        import time
        st["frame_start"] = time.monotonic()
        if st["start"] is None:
            st["start"] = st["frame_start"]
        i = scene.frame_current - scene.frame_start + 1
        # A "starting" line so there's feedback BEFORE the (slow) frame finishes.
        avg = ((time.monotonic() - st["start"]) / st["done"]) if st["done"] else 0
        eta = avg * (total - st["done"])
        pct = int(100 * st["done"] / total)
        tail = f"  ~{_fmt(eta)} left" if st["done"] else ""
        print(f"FLUMEN_PROGRESS {pct} {eta:.0f} rendering frame "
              f"{scene.frame_current}", flush=True)
        print(f"[render] frame {i}/{total} (#{scene.frame_current}) "
              f"rendering…{tail}", flush=True)

    def _post(*_a):
        import time
        st["done"] += 1
        dur = time.monotonic() - st["frame_start"] if st["frame_start"] else 0
        avg = (time.monotonic() - st["start"]) / st["done"]
        eta = avg * (total - st["done"])
        pct = int(100 * st["done"] / total)
        print(f"FLUMEN_PROGRESS {pct} {eta:.0f} rendered frame "
              f"{scene.frame_current}", flush=True)
        print(f"[render] frame {st['done']}/{total} done in {_fmt(dur)}"
              + (f"  ·  {pct}%  ·  ~{_fmt(eta)} left"
                 if st["done"] < total else "  ·  100% complete"),
              flush=True)

    bpy.app.handlers.render_pre.append(_pre)
    bpy.app.handlers.render_post.append(_post)


def main():
    scene = bpy.context.scene
    frames_dir = _env("FLUMEN_RENDER_FRAMES_DIR")
    if not frames_dir:
        print("[render] no frames dir; aborting.")
        return
    os.makedirs(frames_dir, exist_ok=True)

    if scene.camera is None:
        scene.camera = next((o for o in scene.objects if o.type == "CAMERA"), None)
    if scene.camera is None:
        print("[render] no camera in the scene; nothing to render.")
        return

    # Missing linked deps (caches/rigs/env not synced) -> the render would be a
    # void. Warn loudly; the caller treats a non-zero exit as failure.
    missing = [lib.filepath for lib in bpy.data.libraries
               if not os.path.isfile(bpy.path.abspath(lib.filepath))]
    if missing:
        print("[render] ERROR: missing linked libraries — sync the shot's "
              "publishes first:")
        for m in missing:
            print("   ", m)
        sys.exit(3)

    r = scene.render
    # --- project final render settings, with per-shot overrides -------------
    # Engine enum names differ by Blender version (EEVEE is BLENDER_EEVEE in
    # some builds, BLENDER_EEVEE_NEXT in others) — try the aliases.
    want = _env("FLUMEN_RENDER_ENGINE", "BLENDER_EEVEE")
    aliases = ([want] if "CYCLES" in want
               else ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"])
    for eng in aliases + ["CYCLES"]:
        try:
            r.engine = eng
            break
        except (TypeError, ValueError):
            continue
    resx = _env("FLUMEN_RENDER_RESX")
    resy = _env("FLUMEN_RENDER_RESY")
    if resx and resy:
        r.resolution_x, r.resolution_y = int(resx), int(resy)
    try:
        r.resolution_percentage = max(1, min(100,
                                             int(_env("FLUMEN_RENDER_RESPCT",
                                                      "100"))))
    except ValueError:
        r.resolution_percentage = 100
    samples = _env("FLUMEN_RENDER_SAMPLES")
    if samples:
        try:
            if r.engine == "CYCLES":
                scene.cycles.samples = int(samples)
                scene.cycles.use_denoising = _env("FLUMEN_RENDER_DENOISE",
                                                  "1") != "0"
                dev = _env("FLUMEN_RENDER_DEVICE")
                if dev:
                    scene.cycles.device = dev
            else:
                scene.eevee.taa_render_samples = int(samples)
        except Exception as exc:  # noqa: BLE001
            print("[render] could not set samples:", exc)
    # EEVEE raytracing (the finals' engine) — required for the eye-shader
    # switch and screen-space reflections/refraction. EEVEE renders on the GPU.
    if r.engine != "CYCLES" and _env("FLUMEN_RENDER_RAYTRACING", "") in ("0", "1"):
        try:
            scene.eevee.use_raytracing = _env("FLUMEN_RENDER_RAYTRACING") == "1"
        except Exception as exc:  # noqa: BLE001
            print("[render] could not set raytracing:", exc)
    fps = _env("FLUMEN_RENDER_FPS")
    if fps:
        try:
            r.fps = int(fps)
        except ValueError:
            pass
    if _env("FLUMEN_RENDER_FILM_TRANSPARENT", "") in ("0", "1"):
        r.film_transparent = _env("FLUMEN_RENDER_FILM_TRANSPARENT") == "1"

    # Frame range: the work file's own, unless overridden.
    fs, fe = _env("FLUMEN_RENDER_START"), _env("FLUMEN_RENDER_END")
    if fs and fe:
        scene.frame_start, scene.frame_end = int(fs), int(fe)

    # PNG sequence output (a review MP4 is encoded from these by the caller).
    if hasattr(r.image_settings, "media_type"):
        r.image_settings.media_type = "IMAGE"
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    r.image_settings.color_depth = _env("FLUMEN_RENDER_DEPTH", "16")
    r.filepath = os.path.join(frames_dir, "frame_")
    r.use_file_extension = True
    r.use_overwrite = True

    _install_progress(scene)
    nframes = scene.frame_end - scene.frame_start + 1
    print(f"[render] {r.engine} {r.resolution_x}x{r.resolution_y} "
          f"@ {r.resolution_percentage}% · {nframes} frame(s) "
          f"{scene.frame_start}-{scene.frame_end} · cam={scene.camera.name}",
          flush=True)
    print(f"[render] starting — final {r.engine} render on the GPU. Per-frame "
          f"progress follows.", flush=True)
    bpy.ops.render.render(animation=True)
    print("[render] done.", flush=True)


main()
