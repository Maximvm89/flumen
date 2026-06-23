"""Production tasks stored on the server.

A task is a unit of work = an entity (shot or asset) + a step (department), e.g.
"animate SEQ010/SH0010" or "model characters/hero". Each task is its own JSON
file under 02_pipeline/tasks/<id>.json, so different artists editing different
tasks never conflict.

Lives in animpipe (low level, shared) and only needs an sftp object exposing
listdir / read_text / write_text (animpipe.sftp.SFTPClient provides these).
"""

from __future__ import annotations

import json
import re
import time

TASKS_DIR_REL = "02_pipeline/tasks"

# Project naming convention (overridable via config.yaml `naming:`).
NAMING_DEFAULTS = {
    "asset_name": r"^[a-z0-9][a-z0-9_]*$",   # lowercase, digits, underscore
    "sequence": r"^[A-Z]{2,}\d{2,}$",         # e.g. SEQ010
    "shot": r"^[A-Z]{2,}\d{2,}$",             # e.g. SH0010
}
NAMING_HINTS = {
    "asset_name": "lowercase letters, numbers, underscore — e.g. hero_armor",
    "sequence": "uppercase letters + digits — e.g. SEQ010",
    "shot": "uppercase letters + digits — e.g. SH0010",
}


def naming_pattern(naming: dict | None, kind: str) -> str:
    return (naming or {}).get(kind) or NAMING_DEFAULTS[kind]


def validate_name(naming: dict | None, kind: str, value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return False
    return bool(re.match(naming_pattern(naming, kind), value))


def asset_categories(schema: dict) -> list[str]:
    return list((schema.get("root", {}).get("03_assets") or {}).keys())


def steps_for(schema: dict, ttype: str) -> list[str]:
    """Department steps from the schema for a task type (those with a work/ folder)."""
    key = "asset_template" if ttype == "asset" else "shot_template"
    tpl = schema.get(key) or {}
    return [k for k, v in tpl.items() if isinstance(v, dict) and "work" in v]


def delete_task(sftp, remote_root: str, task_id: str) -> None:
    sftp.remove(tasks_dir(remote_root) + "/" + task_id + ".json")


def get_task(sftp, remote_root: str, task_id: str) -> dict | None:
    return _load_one(sftp, remote_root, task_id)


def published_files(task: dict, ext: str = ".blend") -> list[dict]:
    """Published files of a given type from the task's history, newest first.
    Each entry: {rel, name, time, by, description}."""
    import os as _os
    out = []
    for rec in task.get("publishes") or []:
        for rel in rec.get("files") or []:
            if rel.endswith(ext):
                out.append({"rel": rel, "name": _os.path.basename(rel),
                            "time": rec.get("time"), "by": rec.get("by"),
                            "description": rec.get("description", "")})
    out.sort(key=lambda r: r["name"], reverse=True)  # _v003 before _v002
    return out


def publish_task(sftp, remote_root: str, username: str, local_files,
                 task_id: str, status: str = "review",
                 description: str = "") -> list[str] | None:
    """Publish one or more files for a task: upload each into the task's publish/
    folder, record attribution, append a publish-history entry (with the artist's
    description), and advance the task status. Returns the published rel paths,
    or None if the task doesn't exist."""
    import os as _os
    from . import ledger

    if isinstance(local_files, str):
        local_files = [local_files]
    task = get_task(sftp, remote_root, task_id)
    if not task:
        return None
    rels = []
    for f in local_files:
        rel = task_dir_rel(task) + "/publish/" + _os.path.basename(f)
        sftp.upload(f, remote_root.rstrip("/") + "/" + rel)
        rels.append(rel)
    ledger.record_uploads(sftp, remote_root, username, rels)

    # Append to the task's publish history and advance status in one save.
    record = {"time": time.time(), "by": username,
              "description": description, "files": rels, "status": status}
    task["publishes"] = (task.get("publishes") or []) + [record]
    if status:
        task["status"] = status
    save_task(sftp, remote_root, task, actor=username)
    return rels

STATUSES = ["todo", "in_progress", "review", "done"]
STATUS_LABELS = {
    "todo": "To do",
    "in_progress": "In progress",
    "review": "Review",
    "done": "Done",
}


def tasks_dir(remote_root: str) -> str:
    return remote_root.rstrip("/") + "/" + TASKS_DIR_REL


def make_id(ttype: str, entity: str, step: str) -> str:
    raw = f"{ttype}-{entity}-{step}".lower()
    return re.sub(r"[^a-z0-9._-]+", "_", raw)


def task_dir_rel(task: dict) -> str:
    """Folder (relative to remote_root / local_root) for a task's step, e.g.
    '03_assets/characters/hero/model' or '04_sequences/SEQ010/SH0010/animation'."""
    top = "03_assets" if task.get("type") == "asset" else "04_sequences"
    return f"{top}/{task['entity']}/{task['step']}"


def task_work_rel(task: dict) -> str:
    return task_dir_rel(task) + "/work"


def new_task(ttype: str, entity: str, step: str, title: str | None = None,
             assignees: list[str] | None = None, status: str = "todo") -> dict:
    return {
        "id": make_id(ttype, entity, step),
        "type": ttype,                       # "shot" | "asset" | "other"
        "entity": entity,                    # e.g. "SEQ010/SH0010" or "characters/hero"
        "step": step,                        # e.g. "animation", "model"
        "title": title or f"{entity} — {step}",
        "assignees": assignees or [],
        "status": status if status in STATUSES else "todo",
        "publishes": [],          # history: {time, by, description, files, status}
        "updated": time.time(),
        "updated_by": "",
    }


def matches_query(task: dict, query: str) -> bool:
    """Case-insensitive search across a task's fields. Every whitespace-separated
    term must appear somewhere (entity, step, assignees, status, type, title)."""
    q = (query or "").strip().lower()
    if not q:
        return True
    hay = " ".join([
        task.get("type", ""), task.get("entity", ""), task.get("step", ""),
        task.get("status", ""), STATUS_LABELS.get(task.get("status", ""), ""),
        " ".join(task.get("assignees") or []),
        task.get("updated_by", ""), task.get("title", ""),
    ]).lower()
    return all(term in hay for term in q.split())


def load_tasks(sftp, remote_root: str) -> list[dict]:
    out = []
    d = tasks_dir(remote_root)
    for e in sftp.listdir(d):
        if e["is_dir"] or not e["name"].endswith(".json"):
            continue
        txt = sftp.read_text(d + "/" + e["name"])
        if not txt:
            continue
        try:
            out.append(json.loads(txt))
        except ValueError:
            continue
    return out


def save_task(sftp, remote_root: str, task: dict, actor: str = "") -> dict:
    task = dict(task)
    task["updated"] = time.time()
    if actor:
        task["updated_by"] = actor
    sftp.write_text(tasks_dir(remote_root) + "/" + task["id"] + ".json",
                    json.dumps(task, indent=2))
    return task


def _load_one(sftp, remote_root: str, task_id: str) -> dict | None:
    txt = sftp.read_text(tasks_dir(remote_root) + "/" + task_id + ".json")
    if not txt:
        return None
    try:
        return json.loads(txt)
    except ValueError:
        return None


def assign(sftp, remote_root: str, task_id: str, username: str,
           add: bool = True, actor: str = "") -> dict | None:
    """Add or remove an assignee on a task."""
    t = _load_one(sftp, remote_root, task_id)
    if not t:
        return None
    a = set(t.get("assignees") or [])
    if add:
        a.add(username)
    else:
        a.discard(username)
    t["assignees"] = sorted(a)
    return save_task(sftp, remote_root, t, actor or username)


def set_status(sftp, remote_root: str, task_id: str, status: str,
               actor: str = "") -> dict | None:
    if status not in STATUSES:
        return None
    t = _load_one(sftp, remote_root, task_id)
    if not t:
        return None
    t["status"] = status
    return save_task(sftp, remote_root, t, actor)


def build_catalog(root) -> dict:
    """Catalog of valid entities and steps that actually exist in the project:
    {"shot": {entity: [steps]}, "asset": {entity: [steps]}}. Used to populate
    the New-task dropdowns so no invalid entity/step can be entered."""
    cat: dict[str, dict[str, set]] = {"shot": {}, "asset": {}}
    for t in generate_from_tree(root):
        if t["type"] in cat:
            cat[t["type"]].setdefault(t["entity"], set()).add(t["step"])
    return {ttype: {e: sorted(steps) for e, steps in ents.items()}
            for ttype, ents in cat.items()}


def generate_from_tree(root) -> list[dict]:
    """Discover tasks from a merged tree (duck-typed: .children, .rel, .is_dir).

    A 'step' is any folder that contains a 'work' child; its parent path is the
    entity, the top-level folder decides shot vs asset.
    """
    found: list[dict] = []

    def walk(node):
        for child in node.children.values():
            if not child.is_dir:
                continue
            if "work" in child.children:           # this folder is a dept/step
                parts = child.rel.split("/")
                step = parts[-1]
                top = parts[0]
                ttype = ("shot" if top.startswith("04")
                         else "asset" if top.startswith("03") else "other")
                entity = "/".join(parts[1:-1]) or top
                found.append(new_task(ttype, entity, step))
            walk(child)

    walk(root)
    return found
