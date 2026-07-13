"""Production tasks stored on the server.

A task is a unit of work = an entity (shot or asset) + a step (department), e.g.
"animate SEQ010/SH0010" or "model characters/hero". Each task is its own JSON
file under 02_pipeline/tasks/<id>.json, so different artists editing different
tasks never conflict.

Lives in flumen (low level, shared) and only needs an sftp object exposing
listdir / read_text / write_text (flumen.sftp.SFTPClient provides these).
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


def steps_for(schema: dict, ttype: str, asset_category: str | None = None) -> list[str]:
    """Department steps from the schema for a task type (those with a work/ folder).
    For assets, `asset_category` applies the per-type template overlay (e.g. only
    environments have a dressing step); None keeps the shared template."""
    if ttype == "asset":
        from . import schema as schema_mod
        tpl = (schema_mod.asset_template_for(schema, asset_category)
               if asset_category else schema.get("asset_template") or {})
    else:
        tpl = schema.get("shot_template") or {}
    return [k for k, v in tpl.items() if isinstance(v, dict) and "work" in v]


def delete_task(sftp, remote_root: str, task_id: str) -> None:
    sftp.remove(tasks_dir(remote_root) + "/" + task_id + ".json")


def get_task(sftp, remote_root: str, task_id: str) -> dict | None:
    return _load_one(sftp, remote_root, task_id)


_VER_RE = re.compile(r"_v(\d+)\.[^.]+$")


def file_version(path: str) -> int | None:
    """Version number from a published file name like '…_v003.blend' -> 3."""
    m = _VER_RE.search(path)
    return int(m.group(1)) if m else None


def published_versions(task: dict) -> set[int]:
    """All version numbers already published for a task (across every file)."""
    out = set()
    for rec in task.get("publishes") or []:
        for rel in rec.get("files") or []:
            v = file_version(rel)
            if v is not None:
                out.add(v)
    return out


def next_version(task: dict, base: str, ext: str = ".blend") -> int:
    """Next publish version for <base>_vNNN<ext>, from the task's publish history
    (the authoritative record). Highest published version + 1 — so versions stay
    monotonic no matter which machine publishes or what's synced locally.

    Counting local files (len+1) is unreliable: a missing/unsynced version makes it
    re-issue an existing number and silently overwrite a publish."""
    pat = re.compile(re.escape(base) + r"_v(\d+)" + re.escape(ext) + r"$")
    highest = 0
    for rec in task.get("publishes") or []:
        for rel in rec.get("files") or []:
            m = pat.search(rel)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


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


def published_looks(task: dict) -> list[dict]:
    """Named looks a surface task has published, latest version of each. A look
    file is '<asset>_surface_<look>_vNNN.blend'; the look name is parsed against the
    task's own asset name so underscores in either name stay unambiguous. Returns
    [{look, version, blend_rel, manifest_rel, time, by, description}] sorted by name.
    Feeds the downstream 'Apply look' dropdown."""
    import os as _os
    asset = (task.get("entity") or "").split("/")[-1]
    pat = re.compile(re.escape(asset) + r"_surface_(.+)_v(\d+)\.blend$")
    best: dict[str, dict] = {}
    for rec in task.get("publishes") or []:
        for rel in rec.get("files") or []:
            m = pat.search(_os.path.basename(rel))
            if not m:
                continue
            look, ver = m.group(1), int(m.group(2))
            cur = best.get(look)
            if cur is None or ver > cur["version"]:
                best[look] = {
                    "look": look, "version": ver, "blend_rel": rel,
                    "manifest_rel": rel[:-len(".blend")] + ".manifest.json",
                    "time": rec.get("time"), "by": rec.get("by"),
                    "description": rec.get("description", ""),
                }
    return [best[k] for k in sorted(best)]


def published_dressings(task: dict) -> list[dict]:
    """Named set-dressings an environment's dressing task has published, latest
    version of each. A dressing file is '<asset>_dressing_<name>_vNNN.blend' with a
    sibling instance manifest. Returns [{dressing, version, blend_rel, manifest_rel,
    time, by, description}] sorted by name. Feeds the shot-breakdown dropdown."""
    import os as _os
    asset = (task.get("entity") or "").split("/")[-1]
    pat = re.compile(re.escape(asset) + r"_dressing_(.+)_v(\d+)\.blend$")
    best: dict[str, dict] = {}
    for rec in task.get("publishes") or []:
        for rel in rec.get("files") or []:
            m = pat.search(_os.path.basename(rel))
            if not m:
                continue
            name, ver = m.group(1), int(m.group(2))
            cur = best.get(name)
            if cur is None or ver > cur["version"]:
                best[name] = {
                    "dressing": name, "version": ver, "blend_rel": rel,
                    "manifest_rel": rel[:-len(".blend")] + ".manifest.json",
                    "time": rec.get("time"), "by": rec.get("by"),
                    "description": rec.get("description", ""),
                }
    return [best[k] for k in sorted(best)]


def publish_task(sftp, remote_root: str, username: str, local_files,
                 task_id: str, status: str = "review",
                 description: str = "", texture_files=None,
                 progress=None) -> list[str] | None:
    """Publish one or more files for a task: upload each into the task's publish/
    folder, record attribution, append a publish-history entry (with the artist's
    description), and advance the task status. Returns the published rel paths,
    or None if the task doesn't exist.

    `texture_files` go into publish/textures/ (subdir preserved) rather than flat —
    so a surface publish's images sit beside the .blend that references them with
    relative `//textures/...` paths, and the look loads anywhere it's synced."""
    import os as _os
    from . import ledger

    if isinstance(local_files, str):
        local_files = [local_files]
    task = get_task(sftp, remote_root, task_id)
    if not task:
        return None
    # Hard guarantee: never overwrite an already-published FILE. Match by full file
    # name (base + version), NOT by bare version number — a surface task carries
    # several looks that version independently, so 'foo_surface_clean_v001.blend'
    # must not be seen as a clash with 'foo_surface_default_v001.blend'. If a client
    # computed a stale version it re-runs to version up.
    published_names = {_os.path.basename(rel)
                       for rec in task.get("publishes") or []
                       for rel in rec.get("files") or []}
    clash = sorted({_os.path.basename(f) for f in local_files} & published_names)
    if clash:
        raise ValueError(
            "file(s) " + ", ".join(clash) +
            f" already published for task {task_id}; refusing to overwrite. "
            "Re-run publish to get the next version.")
    # Resolve every (local file -> publish rel) up front so we can report progress
    # against the grand total of bytes, across the .blend/FBX and any textures.
    def _sz(p):
        try:
            return _os.path.getsize(p)
        except OSError:
            return 0

    jobs: list[tuple[str, str]] = []
    for f in local_files:
        jobs.append((f, task_dir_rel(task) + "/publish/" + _os.path.basename(f)))
    tex_jobs: list[tuple[str, str]] = []
    for f in texture_files or []:
        # Preserve the texture's path under publish/ (e.g. a per-version subfolder
        # 'textures/<base>_vNNN/<tile>.png') so published looks stay immutable —
        # a new publish never overwrites a prior version's textures.
        norm = f.replace("\\", "/")
        sub = (norm.split("/publish/", 1)[1] if "/publish/" in norm
               else "textures/" + _os.path.basename(f))
        tex_jobs.append((f, sub))
    if tex_jobs:
        # The FLAT publish/textures/ pool (model/env sidecar) is shared across
        # versions — skip files the server already has at the same size, so
        # only new/changed textures cost upload time. Look textures live in
        # per-version subfolders and always upload (immutability contract).
        remote_sizes: dict = {}
        try:
            base = remote_root.rstrip("/") + "/" + task_dir_rel(task) + "/publish/textures"
            for e in sftp.listdir(base):
                if not e.get("is_dir"):
                    remote_sizes[e["name"]] = e.get("size")
        except Exception:  # noqa: BLE001 — no sidecar folder yet
            pass
        skipped = 0
        for f, sub in tex_jobs:
            leaf = sub[len("textures/"):] if sub.startswith("textures/") else ""
            if leaf and "/" not in leaf and remote_sizes.get(leaf) == _sz(f):
                skipped += 1
                continue
            jobs.append((f, task_dir_rel(task) + "/publish/" + sub))
        if skipped:
            print(f"{skipped} unchanged texture(s) already on the server — skipped.")
    total = sum(_sz(f) for f, _ in jobs) or 1
    done = [0]   # bytes fully uploaded before the current file (mutable for closure)
    rels = []
    for f, rel in jobs:
        name = _os.path.basename(f)
        cb = None
        if progress:
            def cb(sent, _file_total, _base=done[0], _name=name):
                progress(_base + sent, total, _name)
        sftp.upload(f, remote_root.rstrip("/") + "/" + rel, callback=cb)
        done[0] += _sz(f)
        if progress:
            progress(done[0], total, name)
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

STATUSES = ["todo", "in_progress", "review", "done", "omitted"]
STATUS_LABELS = {
    "todo": "To do",
    "in_progress": "In progress",
    "review": "Review",
    "done": "Done",
    # Cut from the production: stays on record but drops out of task lists'
    # defaults, the plan and the schedule. Set it back to To do to reinstate.
    "omitted": "Omitted",
}


def tasks_dir(remote_root: str) -> str:
    return remote_root.rstrip("/") + "/" + TASKS_DIR_REL


def make_id(ttype: str, entity: str, step: str) -> str:
    raw = f"{ttype}-{entity}-{step}".lower()
    return re.sub(r"[^a-z0-9._-]+", "_", raw)


def model_task_id(asset_entity: str) -> str:
    """Id of the model task for an asset entity (the upstream a surface/rig task
    shades or rigs). Deterministic sibling of the current task — same entity, the
    'model' step. Used to fetch the published model to load into a surface file."""
    return make_id("asset", asset_entity, "model")


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
    d = tasks_dir(remote_root)
    paths = [d + "/" + e["name"] for e in sftp.listdir(d)
             if not e["is_dir"] and e["name"].endswith(".json")]
    # Dozens of small files on a remote server: read them concurrently when the
    # client supports it (SFTPClient.read_many) — the load is latency-bound.
    reader = getattr(sftp, "read_many", None)
    if callable(reader):
        by_path = reader(paths)
        texts = [by_path.get(p) for p in paths]
    else:
        texts = [sftp.read_text(p) for p in paths]
    out = []
    for txt in texts:
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


def set_plan(sftp, remote_root: str, task_id: str,
             estimate_days: float | None = None, due: str | None = None,
             actor: str = "") -> dict | None:
    """Update a task's planning fields. None leaves a field untouched; an empty
    string / 0 clears it. `due` is 'YYYY-MM-DD'."""
    t = _load_one(sftp, remote_root, task_id)
    if not t:
        return None
    if estimate_days is not None:
        try:
            v = float(estimate_days)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            t["estimate_days"] = v
        else:
            t.pop("estimate_days", None)
    if due is not None:
        if str(due).strip():
            t["due"] = str(due).strip()
        else:
            t.pop("due", None)
    return save_task(sftp, remote_root, t, actor)


def set_status(sftp, remote_root: str, task_id: str, status: str,
               actor: str = "") -> dict | None:
    if status not in STATUSES:
        return None
    t = _load_one(sftp, remote_root, task_id)
    if not t:
        return None
    t["status"] = status
    return save_task(sftp, remote_root, t, actor)


def sequences_from_tasks(task_list: list[dict]) -> list[str]:
    """Sorted, unique sequence codes from shot-type tasks (the '<seq>/<shot>'
    entity's first segment). Feeds the New Shot dialog's sequence dropdown."""
    seqs = set()
    for t in task_list or []:
        if t.get("type") == "shot":
            seq = (t.get("entity") or "").split("/")[0]
            if seq:
                seqs.add(seq)
    return sorted(seqs)


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
