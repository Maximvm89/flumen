"""SyncSketch integration: push dailies (turntables, playblasts) to a SyncSketch
review for annotation/approval.

Design notes:
- One shared studio service account. Its secret (login + api_key) lives on the
  server at 02_pipeline/syncsketch.json and is pulled into ~/.legami/cache at
  sign-in; artists configure nothing.
- Non-secret settings (enabled, account/project ids, name templates) live in the
  "syncsketch" block of project_settings.json, shipped to every artist.
- Reviews are resolved by name (find-or-create) so the per-department layout
  ("<project> — <step>") appears automatically; each daily is a new item.
- Pure helpers (settings, templating, secret loading, the pending-upload filter)
  are unit-testable with no network; the SDK client is only touched at upload time.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .config import CACHED_SYNCSKETCH

SECRET_REL = "02_pipeline/syncsketch.json"
SYNCSKETCH_HOST = "https://www.syncsketch.com"


@dataclass
class SyncSketchSettings:
    enabled: bool = False
    account_id: int = 0
    project_id: int = 0
    review_name_template: str = "{project} — {step}"
    item_name_template: str = "{entity}  {version_label}"

    @classmethod
    def from_project_settings(cls, project_settings: dict | None) -> "SyncSketchSettings":
        blk = (project_settings or {}).get("syncsketch") or {}
        d = cls()
        return cls(
            enabled=bool(blk.get("enabled", d.enabled)),
            account_id=int(blk.get("account_id", d.account_id) or 0),
            project_id=int(blk.get("project_id", d.project_id) or 0),
            review_name_template=str(blk.get("review_name_template")
                                     or d.review_name_template),
            item_name_template=str(blk.get("item_name_template")
                                   or d.item_name_template),
        )

    def configured(self) -> bool:
        """True when the feature is on AND a SyncSketch project is set."""
        return bool(self.enabled and self.project_id)


class _SafeDict(dict):
    """format_map helper: leave unknown {placeholders} untouched instead of raising."""
    def __missing__(self, key):  # noqa: D401
        return "{" + key + "}"


def render_name(template: str, *, project: str = "", step: str = "",
                entity: str = "", version_label: str = "", date: str = "") -> str:
    ctx = _SafeDict(project=project, step=step, entity=entity,
                    version_label=version_label, date=date)
    return (template or "").format_map(ctx).strip()


def load_secret() -> tuple[str, str] | None:
    """Return (login, api_key) for the shared service account, or None.

    Precedence mirrors SFTPCredentials.from_env: explicit env vars first (handy for
    dev/CI), then the cached secret the app pulled from the server at sign-in.
    """
    login = os.environ.get("SYNCSKETCH_LOGIN")
    key = os.environ.get("SYNCSKETCH_API_KEY")
    if login and key:
        return login, key
    if os.path.isfile(CACHED_SYNCSKETCH):
        try:
            with open(CACHED_SYNCSKETCH, encoding="utf-8") as fh:
                d = json.load(fh)
            login = d.get("login") or d.get("email")
            key = d.get("api_key") or d.get("apiKey")
            if login and key:
                return login, key
        except (ValueError, OSError):
            return None
    return None


def review_url(review: dict) -> str:
    """Public review link from a SyncSketch review dict (uuid-based), best-effort."""
    uuid = (review or {}).get("uuid")
    if uuid:
        return f"https://syncsketch.com/sketch/{uuid}/"
    rid = (review or {}).get("id")
    return f"https://syncsketch.com/pro/#/project//review/{rid}/" if rid else ""


class SyncSketchClient:
    """Thin wrapper over the official `syncsketch` SDK. Context manager so callers
    read uniformly (`with SyncSketchClient(...) as ss:`)."""

    def __init__(self, login: str, api_key: str):
        from syncsketch import SyncSketchAPI
        self.api = SyncSketchAPI(login, api_key, host=SYNCSKETCH_HOST,
                                 use_header_auth=True)

    def __enter__(self) -> "SyncSketchClient":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def find_or_create_review(self, project_id: int, name: str) -> dict:
        """Return the review dict named `name` under the project, creating it if
        absent. Per-department reviews thus appear on first upload."""
        existing = self.api.get_reviews_by_project_id(project_id) or {}
        for rev in existing.get("objects", []) or []:
            if (rev.get("name") or "") == name:
                return rev
        return self.api.create_review(project_id, name) or {}

    def add_media(self, review_id: int, filepath: str, item_name: str,
                  artist_name: str) -> dict:
        return self.api.add_media(review_id, filepath, artist_name=artist_name,
                                  file_name=item_name) or {}


def upload_daily(settings: SyncSketchSettings, *, project_name: str,
                 video_local: str, task: dict, version_label: str,
                 username: str, dry_run: bool = False) -> str | None:
    """Upload one dailies video to the right SyncSketch review. Returns the review
    URL on success, or None. Raises on misconfiguration/SDK errors (callers that
    want best-effort behaviour use try_upload_daily)."""
    if not settings.configured():
        return None
    step = task.get("step", "")
    entity = task.get("entity", "")
    review_name = render_name(settings.review_name_template,
                              project=project_name, step=step, entity=entity,
                              version_label=version_label)
    item_name = render_name(settings.item_name_template,
                            project=project_name, step=step, entity=entity,
                            version_label=version_label)
    if dry_run:
        print(f"(dry-run) would upload '{item_name}' to SyncSketch review "
              f"'{review_name}' (project {settings.project_id})")
        return None

    secret = load_secret()
    if not secret:
        raise RuntimeError("SyncSketch secret not found — run 'animpipe "
                           "syncsketch-setup' and re-sign-in.")
    login, api_key = secret
    with SyncSketchClient(login, api_key) as ss:
        review = ss.find_or_create_review(settings.project_id, review_name)
        rid = review.get("id")
        if not rid:
            raise RuntimeError(f"could not resolve SyncSketch review '{review_name}'")
        ss.add_media(rid, video_local, item_name=os.path.basename(video_local),
                     artist_name=username)
    return review_url(review)


def try_upload_daily(settings: SyncSketchSettings, **kwargs) -> str | None:
    """Best-effort upload: never raises, prints a warning on failure. Used by the
    turntable so a SyncSketch hiccup never fails the render/publish."""
    if not settings.configured():
        return None
    try:
        return upload_daily(settings, **kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: SyncSketch upload skipped ({exc})")
        return None


def record_review_url(sftp, remote_root: str, task_id: str, url: str,
                      username: str) -> str | None:
    """Attach the SyncSketch review URL to the task's most recent publish entry,
    so the batch sync knows it's already uploaded. Mirrors record_turntable."""
    from . import tasks
    task = tasks.get_task(sftp, remote_root, task_id)
    if not task:
        return None
    if task.get("publishes"):
        task["publishes"][-1]["syncsketch_url"] = url
    tasks.save_task(sftp, remote_root, task, actor=username)
    return url


def pending_uploads(task_list: list[dict]) -> list[tuple[dict, dict]]:
    """(task, publish_record) pairs for dailies that have a turntable but no
    SyncSketch upload yet. Drives the `syncsketch-sync` backfill."""
    out: list[tuple[dict, dict]] = []
    for task in task_list or []:
        for rec in task.get("publishes") or []:
            if rec.get("turntable") and not rec.get("syncsketch_url"):
                out.append((task, rec))
    return out
