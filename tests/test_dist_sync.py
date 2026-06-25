"""Offline tests for scripts/dist_sync.py (the build-box bootstrap).

Covers the parts that don't need a live SFTP server: source archiving + ignore
rules, .env parsing, and remote_root resolution."""

import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import dist_sync as d


def test_load_env_parses_and_strips(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# comment\nSFTP_HOST="h.example.com"\nSFTP_PORT=2222\n\nBAD\n')
    parsed = d.load_env(str(env))
    assert parsed["SFTP_HOST"] == "h.example.com"
    assert parsed["SFTP_PORT"] == "2222"
    assert "BAD" not in parsed  # lines without '=' are ignored


def test_remote_root_yaml_and_regex(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text('project:\n  name: "x"\n  remote_root: "/projects/ZZZ/"\n')
    assert d.remote_root_from_config(str(cfg)) == "/projects/ZZZ"  # trailing / stripped


def test_source_zip_excludes_secrets_and_build(tmp_path):
    z = tmp_path / "src.zip"
    d.make_source_zip(str(z))
    names = set(zipfile.ZipFile(z).namelist())
    # required source is present
    for must in ("animpipe/turntable.py", "packaging/legami.spec", "build.py",
                 "scripts/dist_sync.py"):
        assert must in names, f"missing {must}"
    # secrets / build output / caches are excluded
    for name in names:
        assert not name.startswith((".git/", ".venv/", "dist/", "build/"))
        assert name not in (".env", "config.yaml")
        assert not name.endswith((".pyc", ".zip"))


def test_dist_base_joins_pipeline_path(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text('project:\n  remote_root: "/projects/MAF"\n')
    args = type("A", (), {"config": str(cfg)})()
    assert d._dist_base(args) == "/projects/MAF/02_pipeline/_dist"
