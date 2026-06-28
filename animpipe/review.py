"""Dailies review model: every published turntable is a review item with a status
(to_review / reviewed / approved) you set in the Workspace app. State lives on the
publish record (publishes[*].review_status) — server-backed, no third-party service.

Pure helpers here are unit-testable; the GUI and CLI drive load/set/export.
"""

from __future__ import annotations

import datetime
import os

REVIEWS_BASE = "07_dailies/_reviews"

REVIEW_STATUSES = ["to_review", "reviewed", "approved"]
REVIEW_LABELS = {
    "to_review": "To review",
    "reviewed": "Reviewed",
    "approved": "Approved",
}
DEFAULT_STATUS = "to_review"


def today_str() -> str:
    """Local date as YYYY-MM-DD. Single source so tests can monkeypatch it."""
    return datetime.date.today().isoformat()


def review_dir_rel(date_str: str) -> str:
    """Folder (relative to remote_root / local_root) for an export."""
    return f"{REVIEWS_BASE}/{date_str}"


def version_from_turntable(turntable_rel: str) -> str:
    """'…/frankenstein_model_v003_turntable.mp4' -> 'frankenstein_model_v003'.
    Also strips the '_playblast' suffix used for shot playblasts."""
    base = os.path.splitext(os.path.basename(turntable_rel or ""))[0]
    for suf in ("_turntable", "_playblast"):
        if base.endswith(suf):
            return base[: -len(suf)]
    return base


def clip_name(rec: dict) -> str:
    """The turntable's own basename (unique, e.g. 'frankenstein_model_v003_turntable.mp4')."""
    return os.path.basename(rec.get("turntable", ""))


def review_status(rec: dict) -> str:
    """A publish record's review status, defaulting to 'to_review'."""
    s = rec.get("review_status")
    return s if s in REVIEW_STATUSES else DEFAULT_STATUS


def item_date(rec: dict) -> str:
    """The daily's publish date (YYYY-MM-DD) for grouping, or '' if unknown."""
    t = rec.get("time")
    if not t:
        return ""
    try:
        return datetime.date.fromtimestamp(float(t)).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


_SHOT_STEPS = ("layout", "animation", "lighting", "comp")


def _review_kind(step: str) -> str:
    if step == "surface":
        return "look"
    if step in _SHOT_STEPS:
        return "shot"
    return "model"


def review_items(task_list: list[dict],
                 statuses: list[str] | set[str] | None = None) -> list[dict]:
    """Every publish carrying a turntable, across all tasks, as a review item.
    Optionally filtered to `statuses`. Sorted newest date first, then entity."""
    out: list[dict] = []
    for task in task_list or []:
        for rec in task.get("publishes") or []:
            if not rec.get("turntable"):
                continue
            st = review_status(rec)
            if statuses is not None and st not in statuses:
                continue
            out.append({
                "task_id": task.get("id", ""),
                "entity": task.get("entity", ""),
                "step": task.get("step", ""),
                "version": version_from_turntable(rec.get("turntable", "")),
                "clip": clip_name(rec),
                "source": rec.get("turntable", ""),
                "sheet": rec.get("sheet", ""),       # texture/UV sheet (looks)
                "kind": _review_kind(task.get("step", "")),
                "by": rec.get("by", ""),
                "description": rec.get("description", ""),
                "time": rec.get("time"),
                "date": item_date(rec),
                "status": st,
                "task_status": task.get("status", ""),
            })
    out.sort(key=lambda i: (i["date"], i["entity"], i["version"]), reverse=True)
    return out


def matches_query(item: dict, query: str) -> bool:
    """Case-insensitive search across a review item's fields. Every
    whitespace-separated term must appear somewhere (entity, step, version,
    artist, status, description, date)."""
    q = (query or "").strip().lower()
    if not q:
        return True
    hay = " ".join([
        item.get("entity", ""), item.get("step", ""), item.get("version", ""),
        item.get("by", ""), item.get("status", ""),
        REVIEW_LABELS.get(item.get("status", ""), ""),
        item.get("description", ""), item.get("date", ""),
    ]).lower()
    return all(term in hay for term in q.split())


def set_status_on_task(task: dict, turntable_rel: str, status: str) -> bool:
    """Set review_status on every publish record carrying `turntable_rel`. If the
    status is 'approved', also complete the task (status -> done). Mutates `task`;
    returns True if any record matched."""
    if status not in REVIEW_STATUSES:
        raise ValueError(f"unknown review status: {status}")
    hit = False
    for rec in task.get("publishes") or []:
        if rec.get("turntable") == turntable_rel:
            rec["review_status"] = status
            hit = True
    if hit and status == "approved":
        task["status"] = "done"
    return hit


def delete_review(sftp, remote_root: str, item: dict,
                  local_root: str | None = None) -> bool:
    """Delete a review's media — the turntable mp4 and texture sheet — from the
    server (and the local mirror if `local_root` is given), and clear the
    turntable/sheet fields from the publish record so it stops being a review item.
    The published look/model itself is NOT touched. Returns True if the record was
    found and updated."""
    import os
    from . import tasks
    remote = remote_root.rstrip("/")
    for rel in (item.get("source"), item.get("sheet")):
        if not rel:
            continue
        try:
            sftp.remove(remote + "/" + rel)
        except Exception:  # noqa: BLE001 — already gone is fine
            pass
        if local_root:
            lp = os.path.join(local_root, *rel.split("/"))
            if os.path.isfile(lp):
                try:
                    os.remove(lp)
                except OSError:
                    pass
    task = tasks.get_task(sftp, remote_root, item.get("task_id", ""))
    if not task:
        return False
    hit = False
    for rec in task.get("publishes") or []:
        if rec.get("turntable") == item.get("source"):
            rec.pop("turntable", None)
            rec.pop("sheet", None)
            hit = True
    if hit:
        tasks.save_task(sftp, remote_root, task)
    return hit


def set_review_status(sftp, remote_root: str, task_id: str, turntable_rel: str,
                      status: str, username: str) -> bool:
    """Load the task, set the item's review status (approved completes the task),
    save. Mirrors turntable.record_turntable."""
    from . import tasks
    task = tasks.get_task(sftp, remote_root, task_id)
    if not task:
        return False
    if not set_status_on_task(task, turntable_rel, status):
        return False
    tasks.save_task(sftp, remote_root, task, actor=username)
    return True


# ---- export (folder + clickable index.html) --------------------------------

def manifest_entry(item: dict) -> dict:
    e = {k: item.get(k) for k in
         ("task_id", "entity", "step", "version", "clip", "source", "by",
          "description", "time", "status", "kind")}
    if item.get("sheet"):
        e["sheet"] = os.path.basename(item["sheet"])   # copied beside the clip
    return e


def build_manifest(items: list[dict], date_str: str) -> dict:
    ordered = sorted(items, key=lambda e: (e.get("entity", ""), e.get("step", "")))
    return {"date": date_str, "count": len(ordered),
            "clips": [manifest_entry(i) for i in ordered]}


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_index_html(manifest: dict) -> str:
    """A self-contained review sheet: one <video> per clip with entity·step·version,
    artist, status and notes. Lives next to the mp4s so a supe opens it in a browser."""
    date_str = manifest.get("date", "")
    rows = []
    for c in manifest.get("clips", []):
        title = f"{c.get('entity','')} · {c.get('step','')} · {c.get('version','')}"
        status = REVIEW_LABELS.get(c.get("status", ""), c.get("status", ""))
        meta = f"{status}  ·  by {c.get('by','') or '—'}"
        if c.get("description"):
            meta += f" — {c['description']}"
        sheet_html = (f'\n    <br><img src="{_esc(c["sheet"])}" width="640" '
                      f'style="margin-top:8px;border-radius:6px">'
                      if c.get("sheet") else "")
        rows.append(
            f'  <figure>\n'
            f'    <figcaption><b>{_esc(title)}</b><br><small>{_esc(meta)}</small>'
            f'</figcaption>\n'
            f'    <video controls preload="metadata" width="640" '
            f'src="{_esc(c.get("clip",""))}"></video>{sheet_html}\n'
            f'  </figure>')
    body = "\n".join(rows) or "  <p>No clips in this review.</p>"
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">\n"
        f"<title>Dailies review — {_esc(date_str)}</title>\n"
        "<style>body{background:#1e1e1e;color:#ddd;font-family:sans-serif;"
        "margin:24px}figure{display:inline-block;margin:0 18px 24px 0;"
        "vertical-align:top}figcaption{margin-bottom:6px}small{color:#9aa}"
        "video{background:#000;border-radius:6px}h1{font-weight:500}</style>\n"
        f"</head><body>\n<h1>Dailies review — {_esc(date_str)} "
        f"({manifest.get('count', 0)} clip(s))</h1>\n{body}\n</body></html>\n")


def write_review_folder(sftp, *, remote_root: str, local_root: str,
                        items: list[dict], date_str: str, username: str,
                        log=None) -> dict:
    """Export the given review items to 07_dailies/_reviews/<date>/: copy each clip
    in (downloading from the server if not local), and write a clickable index.html
    + _review.json. Returns {date, count, folder_rel, folder_local}."""
    import json
    import shutil
    from . import ledger

    def _log(m):
        if log:
            log(m)

    review_rel = review_dir_rel(date_str)
    review_local = os.path.join(local_root, *review_rel.split("/"))
    result = {"date": date_str, "count": 0,
              "folder_rel": review_rel, "folder_local": review_local}
    if not items:
        return result

    os.makedirs(review_local, exist_ok=True)
    # Start fresh so the folder holds ONLY this export — otherwise it accumulates
    # clips from earlier exports (and the SyncSketch drag folder fills with extras).
    for f in os.listdir(review_local):
        fp = os.path.join(review_local, f)
        if os.path.isfile(fp):
            try:
                os.remove(fp)
            except OSError:
                pass
    try:
        for e in sftp.listdir(remote_root.rstrip("/") + "/" + review_rel):
            if not e.get("is_dir"):
                try:
                    sftp.remove(remote_root.rstrip("/") + "/" + review_rel
                                + "/" + e["name"])
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001 — folder may not exist yet
        pass
    uploaded = []
    for item in items:
        src_rel = item["source"]
        clip = item["clip"]
        src_local = os.path.join(local_root, *src_rel.split("/"))
        if not os.path.isfile(src_local):
            try:
                sftp.download(remote_root.rstrip("/") + "/" + src_rel, src_local)
            except Exception as exc:  # noqa: BLE001
                _log(f"warning: could not fetch {src_rel} ({exc}); skipping.")
                continue
        dest_local = os.path.join(review_local, clip)
        if os.path.abspath(dest_local) != os.path.abspath(src_local):
            shutil.copy2(src_local, dest_local)
        sftp.upload(dest_local, remote_root.rstrip("/") + "/" + review_rel + "/" + clip)
        uploaded.append(review_rel + "/" + clip)
        _log(f"  {item.get('entity')}  ->  {clip}")

        # A look item also carries a texture/UV sheet — bring it along.
        sheet_rel = item.get("sheet")
        if sheet_rel:
            sheet_name = os.path.basename(sheet_rel)
            sheet_src = os.path.join(local_root, *sheet_rel.split("/"))
            if not os.path.isfile(sheet_src):
                try:
                    sftp.download(remote_root.rstrip("/") + "/" + sheet_rel, sheet_src)
                except Exception:  # noqa: BLE001
                    sheet_src = None
            if sheet_src and os.path.isfile(sheet_src):
                sheet_dest = os.path.join(review_local, sheet_name)
                if os.path.abspath(sheet_dest) != os.path.abspath(sheet_src):
                    shutil.copy2(sheet_src, sheet_dest)
                sftp.upload(sheet_dest,
                            remote_root.rstrip("/") + "/" + review_rel + "/" + sheet_name)
                uploaded.append(review_rel + "/" + sheet_name)

    manifest = build_manifest(items, date_str)
    man_local = os.path.join(review_local, "_review.json")
    idx_local = os.path.join(review_local, "index.html")
    with open(man_local, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    with open(idx_local, "w", encoding="utf-8") as fh:
        fh.write(render_index_html(manifest))
    for local, name in ((man_local, "_review.json"), (idx_local, "index.html")):
        sftp.upload(local, remote_root.rstrip("/") + "/" + review_rel + "/" + name)
        uploaded.append(review_rel + "/" + name)
    ledger.record_uploads(sftp, remote_root, username, uploaded)

    result["count"] = manifest["count"]
    return result
