"""Shared low-level plumbing for the Flumen operators.

Extracted from operators.py so the feature modules (cache, lights, looks,
build_shot, publish, …) can share the toolkit-shell + logging helpers without
importing operators.py (which would be circular). operators.py re-imports every
name defined here, so existing references keep working unchanged.
"""

import json
import os
import subprocess

import bpy


def _prefs():
    """Addon preferences if the addon was installed the normal way, else None
    (when auto-loaded for a session, settings come from env vars instead)."""
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


def _pref_local_root():
    p = _prefs()
    return getattr(p, "local_root", None) if p else None


def _toolkit_cmd(args):
    """Build the argv to invoke the flumen toolkit, or None if unavailable.

    From source the launcher sets MODULE=flumen and PY=python, so we run
    `python -m flumen …`. When frozen, PY is flumen.exe and MODULE is empty,
    so we call the executable directly."""
    py = os.environ.get("FLUMEN_TOOLKIT_PY")
    td = os.environ.get("FLUMEN_TOOLKIT_DIR")
    if not py or not td:
        return None, None
    mod = os.environ.get("FLUMEN_TOOLKIT_MODULE", "flumen")
    prefix = [py] + (["-m", mod] if mod else [])
    return prefix + list(args), td


# Every publish/upload writes a trace here, no matter how Blender was started.
# The console is invisible on Windows GUI Blender, and ~/.flumen/blender.log
# only exists when launched from the Workspace app — this file is the one
# place a failed publish is guaranteed to leave evidence.
PUBLISH_LOG = os.path.join(os.path.expanduser("~"), ".flumen", "publish.log")


def _publog(msg, echo=True):
    """Append a timestamped line to ~/.flumen/publish.log (and echo it to the
    console). Best-effort: logging must never break a publish."""
    import datetime
    if echo:
        print("[Flumen]", msg)
    try:
        os.makedirs(os.path.dirname(PUBLISH_LOG), exist_ok=True)
        if (os.path.exists(PUBLISH_LOG)
                and os.path.getsize(PUBLISH_LOG) > 2_000_000):
            os.replace(PUBLISH_LOG, PUBLISH_LOG + ".1")
        with open(PUBLISH_LOG, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


def _no_window():
    """Extra Popen kwargs so toolkit subprocesses don't flash a console window
    on Windows (GUI Blender has no console for them to attach to)."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _preflight_server(timeout=45.0):
    """End-to-end server test via the toolkit (`flumen test-connection`):
    reaches the host, logs in, and checks the project's remote_root exists.
    Returns (ok, message); ok is None when the toolkit itself is missing."""
    cmd, td = _toolkit_cmd(["test-connection"])
    if cmd is None:
        return None, ("Toolkit not available — launch Blender from the "
                      "Workspace app ('Open in Blender').")
    _publog("preflight: " + " ".join(str(c) for c in cmd), echo=False)
    try:
        p = subprocess.run(cmd, cwd=td, capture_output=True, text=True,
                           timeout=timeout, **_no_window())
    except subprocess.TimeoutExpired:
        msg = (f"Server test timed out after {int(timeout)}s — "
               f"check your network/VPN.")
        _publog("preflight: " + msg, echo=False)
        return False, msg
    except Exception as exc:  # noqa: BLE001 — toolkit present but unrunnable
        _publog(f"preflight: could not run the toolkit: {exc}", echo=False)
        return False, f"Could not run the toolkit: {exc}"
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    for line in out.splitlines():
        _publog("  " + line, echo=False)
    if p.returncode != 0:
        tail = out.splitlines()[-1] if out else f"exit code {p.returncode}"
        return False, tail
    return True, (out.splitlines()[-1] if out else "Connection OK.")


def _shell_toolkit(args, report):
    """Run an flumen CLI command via the toolkit the launcher exposed."""
    cmd, td = _toolkit_cmd(args)
    if cmd is None:
        report({"ERROR"}, "Toolkit not available — launch from the Workspace app.")
        return False
    try:
        subprocess.check_call(cmd, cwd=td, **_no_window())
        return True
    except Exception as exc:  # noqa: BLE001
        _publog(f"toolkit command failed: {' '.join(map(str, cmd))}: {exc}")
        report({"ERROR"}, f"Command failed: {exc}")
        return False


def _shell_json(args):
    """Run a toolkit command and parse its last line as JSON, or None."""
    cmd, td = _toolkit_cmd(args)
    if cmd is None:
        return None
    try:
        p = subprocess.run(cmd, cwd=td, text=True, capture_output=True,
                           **_no_window())
        if p.returncode != 0:
            _publog(f"{args[0]} failed (rc {p.returncode}): "
                    f"{(p.stderr or p.stdout or '').strip()}")
            return None
        out = (p.stdout or "").strip()
        return json.loads(out.splitlines()[-1]) if out else None
    except Exception as exc:  # noqa: BLE001
        _publog(f"{args[0]} failed: {exc}")
        return None


def _apply_one(report, label, fn):
    """Run a single setting application, collecting warnings instead of crashing."""
    try:
        fn()
        return True
    except Exception as exc:  # noqa: BLE001 — we want to keep going
        report.append(f"  - skipped {label}: {exc}")
        return False


def active_task():
    """The task this Blender session was opened for (set by the Workspace app via
    env vars), or None if Blender was launched without a task context."""
    tid = os.environ.get("FLUMEN_TASK_ID")
    if not tid:
        return None
    return {
        "id": tid,
        "type": os.environ.get("FLUMEN_TASK_TYPE", ""),
        "entity": os.environ.get("FLUMEN_TASK_ENTITY", ""),
        "step": os.environ.get("FLUMEN_TASK_STEP", ""),
        "title": os.environ.get("FLUMEN_TASK_TITLE", ""),
        "work_dir": os.environ.get("FLUMEN_TASK_WORK_DIR", ""),
    }
