"""Email notifications for dailies: whenever review media lands (model turntable,
look review, shot playblast), everyone in the recipient list gets a mail with the
full story — what was published, by whom, from which files, and where it all
lives on the FTP.

Configured server-side in <remote_root>/02_pipeline/notifications.json so the TD
edits one file for the whole team:

    {
      "dailies_email": {
        "enabled": true,
        "recipients": ["supervisor@studio.com", "lead@studio.com"],
        "smtp": {
          "host": "smtp.studio.com", "port": 587,
          "user": "pipeline@studio.com",
          "password": "…",              // or "password_env": "FLUMEN_SMTP_PASSWORD"
          "starttls": true,
          "from": "Flumen <pipeline@studio.com>"
        }
      },
      "dailies_discord": {
        "enabled": true,
        "webhook": "https://discord.com/api/webhooks/…"
      }
    }

Either block is optional — Discord needs no credentials at all (a channel
webhook URL is enough), email wants an SMTP account whose password lives in
this server-side file (no per-machine setup) or in .env via password_env.

Sending is best-effort and never raises into the publish/render path: a broken
mail server must not break a publish. Pure builders here are unit-testable; only
send_email touches the network.
"""

from __future__ import annotations

import datetime
import json
import os

NOTIFY_FILE_REL = "02_pipeline/notifications.json"


def notify_file(remote_root: str) -> str:
    return remote_root.rstrip("/") + "/" + NOTIFY_FILE_REL


def load_notify_config(sftp, remote_root: str) -> dict:
    """The team notification config from the server, or {} if absent/broken."""
    try:
        txt = sftp.read_text(notify_file(remote_root))
    except Exception:  # noqa: BLE001
        return {}
    if not txt:
        return {}
    try:
        return json.loads(txt) or {}
    except ValueError:
        return {}


def _smtp_password(smtp_cfg: dict) -> str:
    """Inline password, or indirected through an env var (password_env) so creds
    can live in .env instead of the shared config."""
    if smtp_cfg.get("password"):
        return str(smtp_cfg["password"])
    env = smtp_cfg.get("password_env")
    return os.environ.get(env, "") if env else ""


def dailies_email(task: dict, rec: dict, media_rels: list[str],
                  remote_root: str, actor: str) -> tuple[str, str]:
    """Build (subject, body) for a dailies drop. `rec` is the publish record the
    media was attached to; `media_rels` the just-landed clip/sheet rel paths."""
    root = remote_root.rstrip("/")
    entity = task.get("entity", "?")
    step = task.get("step", "?")
    first = media_rels[0] if media_rels else ""
    version = os.path.splitext(os.path.basename(first))[0]
    for suffix in ("_turntable", "_playblast", "_textures"):
        version = version.replace(suffix, "")
    when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    subject = f"[Flumen] Dailies: {entity} · {step} · {version} — by {actor}"

    lines = [
        f"New dailies item from {actor}",
        "",
        f"  Project root:  {root}",
        f"  Entity:        {entity}",
        f"  Step:          {step}",
        f"  Type:          {task.get('type', '?')}",
        f"  Task:          {task.get('id', '?')}   (status: {task.get('status', '?')})",
        f"  Version:       {version}",
        f"  When:          {when}",
    ]
    if rec.get("description"):
        lines.append(f"  Description:   {rec['description']}")
    if rec.get("review_status"):
        lines.append(f"  Review status: {rec['review_status']}")
    lines.append("")
    lines.append("Review media:")
    for rel in media_rels:
        lines.append(f"  {root}/{rel}")
    files = rec.get("files") or []
    if files:
        lines.append("")
        lines.append("Published from:")
        for rel in files:
            lines.append(f"  {root}/{rel}")
    lines += ["", "— Flumen"]
    return subject, "\n".join(lines)


def send_email(smtp_cfg: dict, recipients: list[str], subject: str,
               body: str, timeout: float = 15.0) -> bool:
    """Send via SMTP (STARTTLS by default). Returns True on success; never raises."""
    import smtplib
    from email.message import EmailMessage

    host = smtp_cfg.get("host")
    if not (host and recipients):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_cfg.get("from") or smtp_cfg.get("user") or "flumen@localhost"
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(smtp_cfg.get("port", 587)),
                          timeout=timeout) as s:
            if smtp_cfg.get("starttls", True):
                s.starttls()
            user, pw = smtp_cfg.get("user"), _smtp_password(smtp_cfg)
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001 — notification must never break a publish
        print(f"[Flumen] dailies mail failed: {exc}")
        return False


def dailies_discord_payload(task: dict, rec: dict, media_rels: list[str],
                            remote_root: str, actor: str) -> dict:
    """A Discord webhook embed with the same info as the email."""
    subject, body = dailies_email(task, rec, media_rels, remote_root, actor)
    return {
        "username": "Flumen Dailies",
        "embeds": [{
            "title": subject.replace("[Flumen] ", ""),
            "description": body if len(body) <= 3900 else body[:3900] + "\n…",
            "color": 9109504,
        }],
    }


def send_discord(webhook: str, payload: dict, timeout: float = 15.0) -> bool:
    """POST a webhook payload (stdlib only). Returns True on 2xx; never raises."""
    import urllib.request
    if not webhook:
        return False
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": "flumen-notify"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # noqa: BLE001 — notification must never break a publish
        print(f"[Flumen] dailies Discord post failed: {exc}")
        return False


def announce_dailies(sftp, remote_root: str, task: dict, rec: dict,
                     media_rels: list[str], actor: str) -> bool:
    """Load the team config and announce the dailies drop on every configured
    channel (email and/or Discord). Best-effort; never raises. Returns True if
    at least one channel accepted it."""
    try:
        cfg = load_notify_config(sftp, remote_root) or {}
        sent = False
        mail = cfg.get("dailies_email") or {}
        if mail.get("enabled") and mail.get("recipients"):
            subject, body = dailies_email(task, rec, media_rels, remote_root, actor)
            sent |= send_email(mail.get("smtp") or {}, list(mail["recipients"]),
                               subject, body)
        disc = cfg.get("dailies_discord") or {}
        if disc.get("enabled") and disc.get("webhook"):
            sent |= send_discord(
                disc["webhook"],
                dailies_discord_payload(task, rec, media_rels, remote_root, actor))
        return sent
    except Exception as exc:  # noqa: BLE001
        print(f"[Flumen] dailies notification failed: {exc}")
        return False
