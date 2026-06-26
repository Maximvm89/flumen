"""Dailies review builder: collect the turntables waiting for review into a dated
folder on the server, write a clickable review sheet, and flag each as collected.

No third-party service — everything lives on our own SFTP. The "collected?" state
is stamped on the task record (publishes[*].reviewed = <date>) so reruns only pick
up new dailies. Pure helpers here are unit-testable; the CLI (cmd_build_review)
drives the download/upload/record.
"""

from __future__ import annotations

import datetime
import os

REVIEWS_BASE = "07_dailies/_reviews"


def today_str() -> str:
    """Local date as YYYY-MM-DD. Single source so tests can monkeypatch it."""
    return datetime.date.today().isoformat()


def review_dir_rel(date_str: str) -> str:
    """Folder (relative to remote_root / local_root) for a review session."""
    return f"{REVIEWS_BASE}/{date_str}"


def version_from_turntable(turntable_rel: str) -> str:
    """'…/frankenstein_model_v003_turntable.mp4' -> 'frankenstein_model_v003'."""
    base = os.path.splitext(os.path.basename(turntable_rel or ""))[0]
    return base[: -len("_turntable")] if base.endswith("_turntable") else base


def clip_name(rec: dict) -> str:
    """Destination filename in the review folder — the turntable's own basename
    (already unique, e.g. 'frankenstein_model_v003_turntable.mp4')."""
    return os.path.basename(rec.get("turntable", ""))


def collectable(task_list: list[dict], status: str = "review") -> list[tuple[dict, dict]]:
    """(task, publish_record) pairs waiting for review — one per task: the NEWEST
    publish that carries a turntable, included only if it hasn't been collected yet.

    Reviewing the latest version is the point; older/superseded turntables (and a
    turntable re-recorded on more than one publish) are intentionally skipped."""
    out: list[tuple[dict, dict]] = []
    for task in task_list or []:
        if status and task.get("status") != status:
            continue
        latest = None  # publishes are append-ordered, so the last match is newest
        for rec in task.get("publishes") or []:
            if rec.get("turntable"):
                latest = rec
        if latest is not None and not latest.get("reviewed"):
            out.append((task, latest))
    return out


def manifest_entry(task: dict, rec: dict) -> dict:
    return {
        "task_id": task.get("id", ""),
        "entity": task.get("entity", ""),
        "step": task.get("step", ""),
        "version": version_from_turntable(rec.get("turntable", "")),
        "clip": clip_name(rec),
        "source": rec.get("turntable", ""),
        "by": rec.get("by", ""),
        "description": rec.get("description", ""),
        "time": rec.get("time"),
    }


def merge_clips(existing: list[dict], new: list[dict]) -> list[dict]:
    """Union of clip entries, de-duplicated by clip filename (existing kept first).
    Lets a day's review accumulate across multiple build-review runs instead of
    each run overwriting the manifest with only its own batch."""
    out = list(existing or [])
    seen = {c.get("clip") for c in out}
    for c in new or []:
        if c.get("clip") not in seen:
            out.append(c)
            seen.add(c.get("clip"))
    return out


def build_manifest(entries: list[dict], date_str: str) -> dict:
    ordered = sorted(entries, key=lambda e: (e.get("entity", ""), e.get("step", "")))
    return {"date": date_str, "count": len(ordered), "clips": ordered}


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_index_html(manifest: dict) -> str:
    """A self-contained review sheet: one <video> per clip with entity·step·version,
    artist and notes. Lives next to the mp4s so a supe just opens it in a browser."""
    date_str = manifest.get("date", "")
    rows = []
    for c in manifest.get("clips", []):
        title = f"{c.get('entity','')} · {c.get('step','')} · {c.get('version','')}"
        meta = f"by {c.get('by','') or '—'}"
        if c.get("description"):
            meta += f" — {c['description']}"
        rows.append(
            f'  <figure>\n'
            f'    <figcaption><b>{_esc(title)}</b><br><small>{_esc(meta)}</small>'
            f'</figcaption>\n'
            f'    <video controls preload="metadata" width="640" '
            f'src="{_esc(c.get("clip",""))}"></video>\n'
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


def mark_reviewed(task: dict, turntable_rel: str, date_str: str) -> bool:
    """Stamp EVERY publish record carrying `turntable_rel` as collected. Returns
    True if any matched. Mutates `task` in place.

    All matching records are stamped (not just the first) because the same
    turntable can be recorded on more than one publish; otherwise the record that
    `collectable` selects (the newest) could stay unstamped and re-collect forever."""
    hit = False
    for rec in task.get("publishes") or []:
        if rec.get("turntable") == turntable_rel:
            rec["reviewed"] = date_str
            hit = True
    return hit


def clear_reviewed(task: dict, date_str: str | None = None) -> int:
    """Remove the `reviewed` stamp from publish records (all, or only those
    collected on `date_str`). Returns how many were cleared. Mutates in place.
    Used by reset-review to let a day's batch be rebuilt from scratch."""
    n = 0
    for rec in task.get("publishes") or []:
        if "reviewed" in rec and (date_str is None or rec.get("reviewed") == date_str):
            del rec["reviewed"]
            n += 1
    return n


def record_collected(sftp, remote_root: str, task_id: str, turntable_rel: str,
                     date_str: str, username: str) -> bool:
    """Load the task, stamp the matching publish reviewed=<date>, save. Mirrors
    turntable.record_turntable."""
    from . import tasks
    task = tasks.get_task(sftp, remote_root, task_id)
    if not task:
        return False
    if not mark_reviewed(task, turntable_rel, date_str):
        return False
    tasks.save_task(sftp, remote_root, task, actor=username)
    return True


def build_review_session(sftp, *, remote_root: str, project_name: str,
                         local_root: str, username: str, date_str: str,
                         status: str = "review", log=None) -> dict:
    """Collect the turntables waiting for review into the dated folder, copy them
    in locally, upload them + a cumulative index.html/_review.json, and stamp each
    publish reviewed=<date>. Shared by the CLI and the Workspace app so both behave
    identically. `log` (optional callable) gets a line per collected clip.

    Returns {date, count (total in the session), collected (new clip names this
    run), folder_rel, folder_local}.
    """
    import glob  # noqa: F401 — kept for symmetry with callers that may glob after
    import json
    import shutil

    from . import tasks, ledger

    def _log(msg):
        if log:
            log(msg)

    review_rel = review_dir_rel(date_str)
    review_local = os.path.join(local_root, *review_rel.split("/"))
    result = {"date": date_str, "count": 0, "collected": [],
              "folder_rel": review_rel, "folder_local": review_local}

    waiting = collectable(tasks.load_tasks(sftp, remote_root), status=status)
    if not waiting:
        return result

    os.makedirs(review_local, exist_ok=True)
    entries, uploaded = [], []
    for task, rec in waiting:
        src_rel = rec["turntable"]
        clip = clip_name(rec)
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
        dest_rel = review_rel + "/" + clip
        sftp.upload(dest_local, remote_root.rstrip("/") + "/" + dest_rel)
        uploaded.append(dest_rel)
        entries.append(manifest_entry(task, rec))
        record_collected(sftp, remote_root, task["id"], src_rel, date_str, username)
        _log(f"  {task.get('id')}  ->  {clip}")

    # Cumulative: merge into any manifest already in the folder.
    existing = []
    prev = sftp.read_text(remote_root.rstrip("/") + "/" + review_rel + "/_review.json")
    if prev:
        try:
            existing = json.loads(prev).get("clips", [])
        except ValueError:
            pass
    manifest = build_manifest(merge_clips(existing, entries), date_str)

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
    result["collected"] = [e["clip"] for e in entries]
    return result
