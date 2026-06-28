"""Keep a rolling log of the Workspace app's output.

GUI apps (especially the frozen .exe with no console) otherwise drop everything
they print. We tee stdout/stderr to ~/.legami/workspace.log so the bug reporter
has a real log to attach. Best-effort throughout — logging must never crash the app.
"""

from __future__ import annotations

import datetime
import os
import sys

LOG_PATH = os.path.join(os.path.expanduser("~"), ".legami", "workspace.log")
# Blender (and everything it spawns: the addon, the toolkit it shells out to for
# turntables/publishes, the nested headless render, ffmpeg) writes its console
# output here. Kept SEPARATE from workspace.log on purpose: Blender holds its own
# OS handle, and two independent handles appending to one file race and corrupt
# lines on Windows. Within the Blender subtree every process shares one inherited
# handle, so its own writes stay ordered. The bug reporter attaches both files.
BLENDER_LOG_PATH = os.path.join(os.path.expanduser("~"), ".legami", "blender.log")


class _Tee:
    """Write/flush to every (non-None) stream; swallow any stream error."""

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
            except Exception:  # noqa: BLE001 — logging must never raise
                pass
        return len(s)

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self):
        return False


def _rotate(path: str, max_bytes: int) -> None:
    """Keep a single backup if `path` grew past max_bytes. Never raises."""
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass


def setup_logging(path: str = LOG_PATH, max_bytes: int = 1_000_000) -> str:
    """Tee stdout/stderr to `path` (one rotated backup) and log uncaught exceptions.
    Returns the log path. Safe to call once at app startup; never raises."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _rotate(path, max_bytes)
        fh = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        fh.write(f"\n==== Workspace session start {stamp} ====\n")
        sys.stdout = _Tee(sys.__stdout__, fh)
        sys.stderr = _Tee(sys.__stderr__, fh)

        def _hook(exc_type, exc, tb):
            import traceback
            traceback.print_exception(exc_type, exc, tb)  # -> tee'd stderr

        sys.excepthook = _hook
    except Exception:  # noqa: BLE001 — never block startup on logging
        pass
    return path


def prepare_blender_log(max_bytes: int = 4_000_000) -> str:
    """Rotate the Blender console log if it grew large and return its path, for
    launch() to redirect Blender's stdout/stderr into. Never raises."""
    try:
        os.makedirs(os.path.dirname(BLENDER_LOG_PATH), exist_ok=True)
        _rotate(BLENDER_LOG_PATH, max_bytes)
    except Exception:  # noqa: BLE001
        pass
    return BLENDER_LOG_PATH


def read_tail(path: str = LOG_PATH, n_lines: int = 200) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-n_lines:])
    except OSError:
        return ""


def copy_full(path: str, dest: str) -> bool:
    import shutil
    try:
        shutil.copy2(path, dest)
        return True
    except OSError:
        return False
