"""Copy text to the OS clipboard and reveal paths in the file manager.

Cross-platform, dependency-free (shells out to the native tool). Best-effort:
returns False instead of raising if no clipboard tool is available (e.g. a bare
Linux box), so callers can fall back to just printing the path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def copy(text: str) -> bool:
    """Put `text` on the clipboard. Returns True on success."""
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif sys.platform.startswith("win"):
        candidates = [["clip"]]
    else:
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"],
                      ["xsel", "--clipboard", "--input"]]
    for cmd in candidates:
        # `clip` lives in System32 and may not resolve via which; try it anyway.
        if not shutil.which(cmd[0]) and not sys.platform.startswith("win"):
            continue
        try:
            subprocess.run(cmd, input=text.encode("utf-8"), check=True)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def copy_file(path: str) -> bool:
    """Put a single file on the clipboard (as ⌘C on a file in Finder does), so an
    app's 'upload from clipboard' grabs the file itself. macOS + Windows; returns
    False elsewhere. (Multi-file clipboard isn't reliable across OSes — open the
    folder and drag instead.)"""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return False
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", f'set the clipboard to POSIX file "{path}"'],
                check=True)
            return True
        if sys.platform.startswith("win"):
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Set-Clipboard -LiteralPath '{path}'"], check=True)
            return True
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def open_path(path: str) -> bool:
    """Open a file in its default application (e.g. play an mp4). Distinct from
    reveal(), which selects it in the file manager."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        elif sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 — user-facing open
        else:
            subprocess.run(["xdg-open", path], check=False)
        return True
    except (OSError, subprocess.SubprocessError, AttributeError):
        return False


def reveal(path: str) -> bool:
    """Open `path` in the file manager (a folder opens it; a file is selected)."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path] if os.path.isdir(path)
                           else ["open", "-R", path], check=False)
        elif sys.platform.startswith("win"):
            if os.path.isdir(path):
                os.startfile(path)  # noqa: S606 — user-facing reveal
            else:
                subprocess.run(["explorer", "/select,", path], check=False)
        else:
            subprocess.run(["xdg-open", path if os.path.isdir(path)
                            else os.path.dirname(path)], check=False)
        return True
    except (OSError, subprocess.SubprocessError, AttributeError):
        return False
