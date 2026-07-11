"""Auto-upload review media to SyncSketch.

Every reviewable artifact — model turntable, look review, shot playblast,
review still — lands in a per-day review named "Dailies YYYY-MM-DD" inside
the configured SyncSketch project: one playlist per dailies session, items
named by their version label.

Config lives server-side in 02_pipeline/notifications.json (the same
gitignored file as the email/Discord channels — secrets stay off git and off
artists' machines):

  "syncsketch": {
    "username": "you@studio.com",       # SyncSketch account login
    "api_key": "…",
    "project": "Legami",                 # SyncSketch project name (must exist)
    "enabled": true
  }

Best-effort everywhere: SyncSketch being down, misconfigured, or the package
missing must never fail a publish — log and move on.
"""

from __future__ import annotations

import datetime
import os


def day_review_name(when: datetime.date | None = None) -> str:
    """One review (playlist) per dailies day."""
    return f"Dailies {(when or datetime.date.today()).isoformat()}"


def _api(cfg: dict):
    from syncsketch import SyncSketchAPI    # lazy: optional dependency
    return SyncSketchAPI(cfg["username"], cfg["api_key"])


def _find_project_id(api, name: str):
    data = api.get_projects() or {}
    for p in data.get("objects") or []:
        if (p.get("name") or "").strip().lower() == name.strip().lower():
            return p.get("id")
    return None


def _find_or_create_review(api, project_id, name: str):
    data = api.get_reviews_by_project_id(project_id) or {}
    for r in data.get("objects") or []:
        if r.get("name") == name:
            return r.get("id")
    made = api.create_review(project_id, name,
                             description="Flumen dailies (auto-uploaded)") or {}
    return made.get("id")


def announce_media(sftp, remote_root: str, local_path: str,
                   item_name: str) -> bool:
    """Upload one media file into today's dailies review. Returns True when the
    upload happened; False (with a log line) on anything else. Never raises."""
    from . import notify
    try:
        cfg = ((notify.load_notify_config(sftp, remote_root) or {})
               .get("syncsketch") or {})
        if not cfg or cfg.get("enabled") is False:
            return False
        if not (cfg.get("username") and cfg.get("api_key")
                and cfg.get("project")):
            print("[Flumen] syncsketch: config incomplete (needs username, "
                  "api_key, project) — skipped.")
            return False
        if not os.path.isfile(local_path):
            print(f"[Flumen] syncsketch: file not found: {local_path} — skipped.")
            return False
        api = _api(cfg)
        pid = _find_project_id(api, str(cfg["project"]))
        if pid is None:
            print(f"[Flumen] syncsketch: project '{cfg['project']}' not found "
                  f"— skipped.")
            return False
        review = day_review_name()
        rid = _find_or_create_review(api, pid, review)
        if rid is None:
            print(f"[Flumen] syncsketch: could not open review '{review}' "
                  f"— skipped.")
            return False
        api.add_media(rid, local_path, file_name=item_name)
        print(f"[Flumen] syncsketch: uploaded '{item_name}' -> '{review}'.")
        return True
    except Exception as exc:  # noqa: BLE001 — never fail the publish over review sync
        print("[Flumen] syncsketch upload failed:", exc)
        return False
