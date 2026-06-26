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
    """(task, publish_record) pairs waiting for review: the task is in `status`,
    the record has a turntable, and it hasn't been collected yet."""
    out: list[tuple[dict, dict]] = []
    for task in task_list or []:
        if status and task.get("status") != status:
            continue
        for rec in task.get("publishes") or []:
            if rec.get("turntable") and not rec.get("reviewed"):
                out.append((task, rec))
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
    """Stamp the publish record carrying `turntable_rel` as collected. Returns True
    if a record matched. Mutates `task` in place."""
    for rec in task.get("publishes") or []:
        if rec.get("turntable") == turntable_rel:
            rec["reviewed"] = date_str
            return True
    return False


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
