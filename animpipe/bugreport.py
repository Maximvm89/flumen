"""Build a pre-filled GitHub New-Issue URL for the Workspace app's bug reporter.

Pure + Qt-free so it's unit-testable. The app opens the returned URL in the
browser; the reporter reviews and clicks Submit (no token shipped). GitHub can't
take file attachments via a URL, so the full log + screenshot are written to a
local folder for the reporter to drag in; we inline a short log tail in the body.
"""

from __future__ import annotations

import urllib.parse

GITHUB_REPO = "Maximvm89/legami-pipeline"
# GitHub accepts long URLs but browsers/servers get unhappy past ~8k; stay well under.
DEFAULT_MAX_URL = 6000


def environment_text(*, version: str = "", platform_str: str = "", project: str = "",
                     user: str = "", remote_root: str = "", local_root: str = "") -> str:
    """A markdown bullet list of identifying info to include in every report."""
    rows = [
        ("App version", version),
        ("Platform", platform_str),
        ("Project", project),
        ("User", user),
        ("Remote", remote_root),
        ("Local", local_root),
    ]
    return "\n".join(f"- **{label}:** {value or '—'}" for label, value in rows)


def issue_body(description: str, env_text: str, log_tail: str = "",
               attached_names: list[str] | None = None) -> str:
    parts = [(description or "").strip() or "_(no description provided)_", ""]
    parts += ["## Environment", env_text, ""]
    if log_tail and log_tail.strip():
        parts += ["## Log (tail)", "```", log_tail.strip(), "```", ""]
    if attached_names:
        parts += ["## Attachments",
                  "Drag these files from the folder that just opened into this issue:"]
        parts += [f"- `{name}`" for name in attached_names]
        parts += [""]
    return "\n".join(parts)


def new_issue_url(title: str, body: str, repo: str = GITHUB_REPO,
                  labels: str = "bug") -> str:
    q = {"title": title or "Bug report", "body": body}
    if labels:
        q["labels"] = labels
    return f"https://github.com/{repo}/issues/new?" + urllib.parse.urlencode(q)


def build_issue(title: str, description: str, env_text: str, log_tail: str = "",
                attached_names: list[str] | None = None, repo: str = GITHUB_REPO,
                max_url: int = DEFAULT_MAX_URL) -> tuple[str, str]:
    """Return (url, body), trimming the OLDEST lines of the inline log tail until the
    URL fits `max_url`. The full log is dragged in separately, so dropping the inline
    tail entirely (last resort) loses nothing important."""
    tail = log_tail or ""
    while True:
        body = issue_body(description, env_text, tail, attached_names)
        url = new_issue_url(title, body, repo=repo)
        if len(url) <= max_url or not tail:
            return url, body
        lines = tail.splitlines()
        if len(lines) <= 4:
            tail = ""  # give up on the inline tail; full log.txt still attached
        else:
            tail = "\n".join(lines[max(1, len(lines) // 5):])  # drop oldest ~20%
