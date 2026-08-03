"""Publish a lighting shot's full work file as the render ground truth.

`FLUMEN_OT_publish_shot` saves a new work version, captures the exact external
files the scene links (libraries + Alembic caches + textures) into a sidecar
`*.deps.json`, and publishes both to the task's publish/ folder (kind 'shot').
The toolkit render opens the newest such publish and auto-fetches any missing
dependency at the exact version the shot uses.

Extracted like lights.py; registration flows through operators.CLASSES.
"""

import json
import os
import subprocess

import bpy

from ._common import active_task, _toolkit_cmd, _no_window, _publog
from . import dressing as dressing_mod


def _collect_dependencies():
    """Every external file the scene links, as project-relative rels tagged with a
    kind: linked libraries, Alembic CacheFiles, and unpacked external images.
    Paths outside the project mirror are dropped (nothing on the server to fetch
    them from). Deduped, so a library used by many objects is listed once."""
    root = os.environ.get("FLUMEN_PROJECT_ROOT", "")
    deps, seen = [], set()

    def add(path, kind):
        rel = dressing_mod.rel_from_local(bpy.path.abspath(path or ""), root)
        if rel and rel not in seen:
            seen.add(rel)
            deps.append({"rel": rel, "kind": kind})

    for lib in bpy.data.libraries:
        add(lib.filepath, "library")
    for cf in getattr(bpy.data, "cache_files", []):       # Alembic .abc caches
        add(cf.filepath, "cache")
    for img in bpy.data.images:
        if (img.source in {"FILE", "SEQUENCE", "TILED"}
                and not img.packed_file and img.library is None):
            add(img.filepath, "texture")
    return deps


class FLUMEN_OT_publish_shot(bpy.types.Operator):
    bl_idname = "flumen.publish_shot"
    bl_label = "Publish shot"
    bl_description = ("Save a new work version and publish the whole .blend as the "
                      "render ground truth — the exact file the final render "
                      "opens, with the caches/libraries it uses recorded so a "
                      "render machine can fetch any it's missing")

    def invoke(self, context, event):
        task = active_task()
        if not task or task.get("type") != "shot" or not task.get("work_dir"):
            self.report({"ERROR"}, "Open a lighting shot task from the Workspace "
                                   "app (its work folder is where the version is "
                                   "saved).")
            return {"CANCELLED"}
        if not bpy.data.filepath:
            self.report({"ERROR"}, "Save into the task first (Flumen ▸ Save into "
                                   "task) so linked paths are relative.")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(
            self, width=380, title="Publish shot", confirm_text="Publish")

    def draw(self, context):
        col = self.layout.column()
        col.prop(context.window_manager, "flumen_publish_desc", text="Description")
        col.label(text="Saves a new work version, then publishes the whole scene "
                       "as the render ground truth.", icon="EXPORT")

    def execute(self, context):
        from .operators import _save_work_version   # local: avoid import cycle
        task = active_task()
        if not task:
            return {"CANCELLED"}
        path = _save_work_version(task)
        if not path:
            self.report({"ERROR"}, "Could not save the work file — see the "
                                   "pipeline log.")
            return {"CANCELLED"}
        deps = _collect_dependencies()
        deps_path = path[:-6] + ".deps.json"           # sibling of the .blend
        try:
            with open(deps_path, "w", encoding="utf-8") as fh:
                json.dump({"blend": os.path.basename(path),
                           "shot": task.get("entity", ""),
                           "deps": deps}, fh, indent=2)
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"Could not write the dependency manifest: {exc}")
            return {"CANCELLED"}
        desc = context.window_manager.flumen_publish_desc
        cmd, td = _toolkit_cmd(["publish-shot", "--task", task["id"],
                                "--local", path, "--deps", deps_path,
                                "--status", "review", "--description", desc])
        if cmd is None:
            self.report({"WARNING"}, f"Saved {os.path.basename(path)}, but the "
                        f"toolkit wasn't found to publish it.")
            return {"FINISHED"}
        context.window_manager.flumen_publish_desc = ""
        p = subprocess.run(cmd, cwd=td, encoding="utf-8", errors="replace", capture_output=True,
                           **_no_window())
        for line in ((p.stdout or "") + (p.stderr or "")).splitlines():
            _publog("  " + line, echo=False)
        if p.returncode != 0:
            self.report({"ERROR"}, "Shot publish failed — see the pipeline log.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Published {os.path.basename(path)} "
                    f"({len(deps)} dependency ref(s)); task → Review.")
        return {"FINISHED"}


CLASSES = (FLUMEN_OT_publish_shot,)
