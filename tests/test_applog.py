"""Workspace logging helpers: separate Blender log + rotation."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workspace_app import applog


def test_blender_log_is_a_separate_file():
    # Must not share a handle/file with the app log (concurrent appends corrupt
    # lines on Windows).
    assert applog.BLENDER_LOG_PATH != applog.LOG_PATH


def test_prepare_blender_log_creates_dir_and_returns_path(tmp_path, monkeypatch):
    p = tmp_path / "sub" / "blender.log"
    monkeypatch.setattr(applog, "BLENDER_LOG_PATH", str(p))
    out = applog.prepare_blender_log()
    assert out == str(p)
    assert p.parent.is_dir()


def test_prepare_blender_log_rotates_when_large(tmp_path, monkeypatch):
    p = tmp_path / "blender.log"
    p.write_text("x" * 100)
    monkeypatch.setattr(applog, "BLENDER_LOG_PATH", str(p))
    applog.prepare_blender_log(max_bytes=10)        # over the limit -> rotate
    assert (tmp_path / "blender.log.1").exists()
    assert (tmp_path / "blender.log.1").read_text() == "x" * 100


def test_prepare_blender_log_keeps_small_file(tmp_path, monkeypatch):
    p = tmp_path / "blender.log"
    p.write_text("small")
    monkeypatch.setattr(applog, "BLENDER_LOG_PATH", str(p))
    applog.prepare_blender_log(max_bytes=10_000)    # under the limit -> keep
    assert not (tmp_path / "blender.log.1").exists()
    assert p.read_text() == "small"
