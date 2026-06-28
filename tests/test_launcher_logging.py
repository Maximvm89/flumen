"""launch() captures Blender's console output into the app log.

Regression: bug reports were missing all Blender/turntable subprocess output
because launch() spawned Blender with inherited fds (and nothing at all on a
frozen windowed .exe). With a log_path, Blender's stdout/stderr go to that file
so the whole downstream tree (addon -> toolkit -> headless render -> ffmpeg) is
collected.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from animpipe import launcher
from animpipe.config import ProjectConfig, SFTPCredentials


def _cfg():
    return ProjectConfig(name="Legami", code="LEGAMI", remote_root="/shared/Legami",
                         schema={}, assets={}, shots={}, local_root="/tmp/legami")


def _patch_common(monkeypatch, recorder):
    monkeypatch.setattr(launcher, "find_blender", lambda explicit=None: "/fake/blender")
    monkeypatch.setattr(launcher, "_resolve_ocio", lambda root: None)

    class _FakePopen:
        def __init__(self, cmd, **kw):
            recorder["cmd"] = cmd
            recorder["stdout"] = kw.get("stdout")
            recorder["stderr"] = kw.get("stderr")
    monkeypatch.setattr(launcher.subprocess, "Popen", _FakePopen)


def test_log_path_redirects_blender_output(tmp_path, monkeypatch):
    rec = {}
    _patch_common(monkeypatch, rec)
    log = tmp_path / "workspace.log"
    rc = launcher.launch(_cfg(), SFTPCredentials(host="h", port=22, user="u"),
                         no_sync=True, log_path=str(log))
    assert rc == 0
    # Blender's stdout was pointed at a writable handle, stderr merged into it.
    assert rec["stdout"] is not None
    assert rec["stderr"] == launcher.subprocess.STDOUT
    # The session header was written to the log file.
    assert "Blender session" in log.read_text()


def test_no_log_path_inherits_fds(tmp_path, monkeypatch):
    rec = {}
    _patch_common(monkeypatch, rec)
    rc = launcher.launch(_cfg(), SFTPCredentials(host="h", port=22, user="u"),
                         no_sync=True)
    assert rc == 0
    # CLI / detached default: inherit the parent's console (stdout=None).
    assert rec["stdout"] is None
    assert rec["stderr"] is None
