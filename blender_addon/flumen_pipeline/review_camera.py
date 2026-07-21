"""Review camera: drop a quick shot camera and render a review of the current
work (playblast-style), independent of building the shot. Add-review-camera,
render-review and cycle-format operators plus their helpers."""

import json
import os
import subprocess

import bpy

from . import settings_io
from .build_shot import (
    _named_holder)
from ._common import (
    _no_window, _pref_local_root, _toolkit_cmd, active_task)


# --- review camera --------------------------------------------------------------
# A local dolly rig for framing review renders. Lives in its own collection and is
# EXCLUDED from every publish: the model post-process strips it (outside the
# PUBLISH locator), and dressing/shot publishes unlink it around the save.
REVIEW_CAM_COLL = "review_camera"

def _unlink_review_camera(context):
    """Temporarily remove the review-camera collection from the scene (and hand
    the scene camera back to a non-review camera) so a saved publish copy never
    carries it. Returns a restore() callable."""
    coll = bpy.data.collections.get(REVIEW_CAM_COLL)
    if coll is None:
        return lambda: None
    sc = context.scene.collection
    scene = context.scene
    was_linked = coll.name in sc.children
    prior_cam = scene.camera
    review_cams = {o for o in coll.all_objects if o.type == "CAMERA"}
    if scene.camera in review_cams:
        scene.camera = next((o for o in scene.objects
                             if o.type == "CAMERA" and o not in review_cams), None)
    if was_linked:
        try:
            sc.children.unlink(coll)
        except Exception:  # noqa: BLE001
            pass

    def restore():
        if was_linked:
            try:
                sc.children.link(coll)
            except Exception:  # noqa: BLE001
                pass
        scene.camera = prior_cam
    return restore

def _add_review_headlight(context, holder, cam):
    """A shadowless sun parented to the review camera — it lights whatever the
    camera looks at, like a DCC viewport headlight. No shadows means walls and
    ceilings can't block it (interiors light up too), and a sun adds ~zero render
    cost. Only added when the scene has no render-enabled lights of its own
    (emissive materials don't count — an unlit set renders black without this).
    Returns True if the headlight was added."""
    if cam is None:
        return False
    if any(o.name.startswith("REVIEW_headlight") for o in holder.all_objects):
        return False
    review = set(holder.all_objects)
    for o in context.scene.objects:
        if o.type == "LIGHT" and o not in review and not o.hide_render:
            return False
    data = bpy.data.lights.new("REVIEW_headlight", "SUN")
    data.energy = 3.0        # W/m² — tweak the light if the still is too hot/dim
    try:
        data.use_shadow = False
    except AttributeError:   # older Blender: shadow flag lives elsewhere; harmless
        pass
    light = bpy.data.objects.new("REVIEW_headlight", data)
    holder.objects.link(light)
    light.parent = cam       # identity transform: shines exactly where cam looks
    return True

def _viewport_matrix(context):
    """World matrix of the user's current 3D-viewport view, or None. Lets the
    review camera spawn already framed on what the artist is looking at."""
    try:
        for area in context.window.screen.areas:
            if area.type == "VIEW_3D":
                r3d = area.spaces.active.region_3d
                if r3d is not None:
                    return r3d.view_matrix.inverted()
    except Exception:  # noqa: BLE001
        pass
    return None

class FLUMEN_OT_add_review_camera(bpy.types.Operator):
    bl_idname = "flumen.add_review_camera"
    bl_label = "Add review camera"
    bl_description = ("Add a free camera for review renders, framed on your "
                      "current view (plus a camera headlight if the scene has "
                      "no lights). A plain camera — move it with G/R or lock it "
                      "to the view (N ▸ View ▸ Camera to View). Never published "
                      "at any stage; use 'Render review still' when framed")

    def execute(self, context):
        existing = bpy.data.collections.get(REVIEW_CAM_COLL)
        if existing is not None:
            cam = next((o for o in existing.all_objects if o.type == "CAMERA"),
                       None)
            if _add_review_headlight(context, existing, cam):
                self.report({"INFO"}, "Review camera already there — added a "
                                      "headlight (the scene has no lights).")
            else:
                self.report({"INFO"}, "Review camera already in the scene.")
            return {"FINISHED"}
        holder = _named_holder(context, REVIEW_CAM_COLL)
        # A plain, unconstrained camera on purpose: a rigged camera fights
        # 'Lock Camera to View' (constraints snap it back every update) and a
        # review still needs no animatable rig.
        data = bpy.data.cameras.new("REVIEW_Camera")
        cam = bpy.data.objects.new("REVIEW_Camera", data)
        holder.objects.link(cam)
        view = _viewport_matrix(context)
        if view is not None:
            cam.matrix_world = view
        context.scene.camera = cam
        lit = _add_review_headlight(context, holder, cam)
        self.report({"INFO"}, "Review camera added on your current view"
                              + (", headlight on — the scene has no lights"
                                 if lit else "")
                              + " — tweak framing, then 'Render review still'.")
        return {"FINISHED"}

# In-flight review render: {out, task_id, prior} while the F12-style render job
# runs; the render_complete/render_cancel handlers below consume it. The render
# is INVOKE_DEFAULT so the normal Render window (progress bar, Esc to cancel)
# shows — a blocking exec render would freeze the UI with no feedback.
_REVIEW_PENDING = None

def _review_render_finish(cancelled):
    """One-shot epilogue for the review render: restore camera/output settings,
    then (on success) hand the PNG to `flumen review-still` in the background."""
    global _REVIEW_PENDING
    pending, _REVIEW_PENDING = _REVIEW_PENDING, None
    for handlers, fn in ((bpy.app.handlers.render_complete, _on_review_complete),
                         (bpy.app.handlers.render_cancel, _on_review_cancel)):
        try:
            handlers.remove(fn)
        except ValueError:
            pass
    if pending is None:
        return
    scene = bpy.context.scene
    r = scene.render
    out = pending["out"]
    if not cancelled and not os.path.isfile(out):
        # write_still can lag the completion handler — save the result ourselves.
        try:
            bpy.data.images["Render Result"].save_render(filepath=out)
        except Exception as exc:  # noqa: BLE001
            print("[Flumen] review: could not save the render:", exc)
    cam_p, fp_p, fmt_p, media_p = pending["prior"]
    scene.camera, r.filepath = cam_p, fp_p
    if media_p is not None:               # restore media BEFORE format — the
        try:                              # format enum is filtered by it
            r.image_settings.media_type = media_p
        except (AttributeError, TypeError):
            pass
    try:
        r.image_settings.file_format = fmt_p
    except TypeError:
        pass
    if cancelled:
        print("[Flumen] review render cancelled — nothing uploaded.")
        return
    if not os.path.isfile(out):
        print("[Flumen] review render produced no image — nothing uploaded.")
        return
    cmd, td = _toolkit_cmd(["review-still", "--task", pending["task_id"],
                            "--file", out])
    if cmd is None:
        print(f"[Flumen] review rendered to {out}, but the toolkit isn't "
              f"available to upload.")
        return
    try:
        subprocess.Popen(cmd, cwd=td, **_no_window())
        print(f"[Flumen] review: uploading {os.path.basename(out)} "
              f"to dailies in background.")
    except Exception as exc:  # noqa: BLE001
        print("[Flumen] review: upload failed to start:", exc)

def _on_review_complete(scene, *args):
    _review_render_finish(cancelled=False)

def _on_review_cancel(scene, *args):
    _review_render_finish(cancelled=True)

class FLUMEN_OT_render_review(bpy.types.Operator):
    bl_idname = "flumen.render_review"
    bl_label = "Render review still"
    bl_description = ("Render the current frame through the review camera "
                      "(progress in the render window) and upload it to "
                      "07_dailies (with the usual notification)")

    def execute(self, context):
        import tempfile
        import time as _time
        global _REVIEW_PENDING
        task = active_task()
        if not task:
            self.report({"ERROR"}, "No active task — open from the Workspace app.")
            return {"CANCELLED"}
        coll = bpy.data.collections.get(REVIEW_CAM_COLL)
        cam = next((o for o in coll.all_objects if o.type == "CAMERA"),
                   None) if coll else None
        if cam is None:
            self.report({"ERROR"}, "No review camera — run 'Add review camera' "
                                   "first.")
            return {"CANCELLED"}
        if _REVIEW_PENDING is not None:
            self.report({"WARNING"}, "A review render is already in progress.")
            return {"CANCELLED"}

        leaf = task.get("entity", "review").split("/")[-1]
        stamp = _time.strftime("%Y%m%d_%H%M%S")
        out = os.path.join(tempfile.gettempdir(),
                           f"{leaf}_{task.get('step', '')}_review_{stamp}.png")
        scene = context.scene
        r = scene.render
        prior = (scene.camera, r.filepath, r.image_settings.file_format,
                 getattr(r.image_settings, "media_type", None))
        scene.camera = cam
        # media_type filters the file_format enum (Blender 4.4+/5.x): a scene
        # set to VIDEO output only offers FFMPEG until flipped back to IMAGE.
        try:
            r.image_settings.media_type = "IMAGE"
        except (AttributeError, TypeError):
            pass
        r.image_settings.file_format = "PNG"
        r.filepath = out
        _REVIEW_PENDING = {"out": out, "task_id": task["id"], "prior": prior}
        bpy.app.handlers.render_complete.append(_on_review_complete)
        bpy.app.handlers.render_cancel.append(_on_review_cancel)
        try:
            res = bpy.ops.render.render("INVOKE_DEFAULT", write_still=True)
        except Exception as exc:  # noqa: BLE001
            _review_render_finish(cancelled=True)
            self.report({"ERROR"}, f"Could not start the render: {exc}")
            return {"CANCELLED"}
        if "CANCELLED" in res:
            _review_render_finish(cancelled=True)
            self.report({"ERROR"}, "Could not start the render.")
            return {"CANCELLED"}
        self.report({"INFO"}, "Rendering review still — watch the render window; "
                              "the upload starts when it finishes.")
        return {"FINISHED"}

class FLUMEN_OT_cycle_format(bpy.types.Operator):
    bl_idname = "flumen.cycle_format"
    bl_label = "Preview format"
    bl_description = ("Cycle the scene resolution through the project's "
                      "delivery formats (e.g. 16:9 ⇄ 9:16) so you can compose "
                      "the shot for every crop through the same camera")

    def execute(self, context):
        try:
            root = settings_io.find_project_root(_pref_local_root())
            settings = settings_io.load_settings(root) if root else {}
        except Exception:  # noqa: BLE001
            settings = {}
        formats = [f for f in (settings.get("formats") or [])
                   if f.get("name") and f.get("resolution_x")
                   and f.get("resolution_y")]
        if len(formats) < 2:
            self.report({"INFO"}, "No delivery formats configured for this "
                                  "project (project_settings 'formats').")
            return {"CANCELLED"}
        r = context.scene.render
        idx = next((i for i, f in enumerate(formats)
                    if (int(f["resolution_x"]), int(f["resolution_y"]))
                    == (r.resolution_x, r.resolution_y)), -1)
        nxt = formats[(idx + 1) % len(formats)]
        r.resolution_x = int(nxt["resolution_x"])
        r.resolution_y = int(nxt["resolution_y"])
        # Match the render's nesting (see blender_playblast): a format narrower
        # than the primary previews as the centered slice of it — same vertical
        # FOV — by locking the camera's vertical sensor size. The original
        # sensor state is stashed on the camera data and restored on the
        # primary, where a safe-area box marks the narrowest format's crop.
        cam = context.scene.camera.data if context.scene.camera else None
        bx, by = (int(formats[0]["resolution_x"]),
                  int(formats[0]["resolution_y"]))
        if cam is not None and bx >= by:
            if "flumen_sensor_fit" not in cam:
                cam["flumen_sensor_fit"] = cam.sensor_fit
                cam["flumen_sensor_height"] = cam.sensor_height
            narrower = (int(nxt["resolution_x"]) / int(nxt["resolution_y"])
                        < bx / by - 1e-6)
            if narrower and cam["flumen_sensor_fit"] != "VERTICAL":
                cam.sensor_fit = "VERTICAL"
                cam.sensor_height = cam.sensor_width * (by / bx)
            else:
                cam.sensor_fit = str(cam["flumen_sensor_fit"])
                cam.sensor_height = float(cam["flumen_sensor_height"])
            # Crop guide on the primary: dashed box where the narrowest
            # format's slice sits inside this frame.
            slices = [f for f in formats
                      if int(f["resolution_x"]) / int(f["resolution_y"])
                      < bx / by - 1e-6]
            if slices and nxt is formats[0]:
                fx = min(int(f["resolution_x"]) / int(f["resolution_y"])
                         for f in slices) * by / bx
                try:
                    cam.show_safe_areas = True
                    context.scene.safe_areas.title = (1.0 - fx, 0.0)
                except Exception:  # noqa: BLE001
                    pass
        self.report({"INFO"}, f"Previewing {nxt['name']} "
                              f"({r.resolution_x}x{r.resolution_y}) — the "
                              f"playblast renders every format regardless.")
        return {"FINISHED"}
