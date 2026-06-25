#!/usr/bin/env python3
"""Standalone source/bundle sync over SFTP — the build-box bootstrap.

Depends ONLY on paramiko (no animpipe import), so it can seed a bare Windows
build box before any source exists there. Reads SFTP credentials from .env and
the project remote_root from config.yaml (same files the rest of the toolkit
uses). Everything lives under <remote_root>/02_pipeline/_dist/:

    _dist/src/legami-src.zip      the source tree (one atomic archive)
    _dist/<os>/Legami-<os>.zip    built bundles for artists

Typical loop:
    # on the Mac (dev) — publish the latest source:
    python scripts/dist_sync.py push
    # on the Windows build box — pull, build, publish the bundle for artists:
    python scripts/dist_sync.py pull
    python build.py --zip
    python scripts/dist_sync.py publish-bundle

Seed a fresh build box once with just this file + .env + config.yaml, then
`pull` brings down everything else (it overwrites this script too — self-updating).
"""
from __future__ import annotations

import argparse
import os
import platform
import posixpath
import re
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_SUBDIR = "02_pipeline/_dist"
SRC_ARCHIVE = "src/legami-src.zip"

# Excluded from the source archive (secrets, build output, caches, local data).
IGNORE_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache",
               "build", "dist", "LEGAMI", "Legami", "node_modules"}
IGNORE_NAMES = {".env", "config.yaml", ".DS_Store", "Thumbs.db"}
IGNORE_SUFFIXES = (".pyc", ".pyo", ".zip")


# --------------------------------------------------------------------------- #
# Config / credentials (no external deps beyond paramiko)
# --------------------------------------------------------------------------- #
def load_env(path: str) -> dict:
    """Minimal .env parser (KEY=VALUE, # comments). Avoids a python-dotenv dep."""
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def remote_root_from_config(path: str) -> str:
    """Read project.remote_root from config.yaml. Uses PyYAML if present, else a
    tolerant line scan (keeps this script dependency-light for bootstrapping)."""
    if not os.path.isfile(path):
        raise SystemExit(f"error: config not found: {path} (need remote_root)")
    try:
        import yaml  # noqa: WPS433
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        rr = (data.get("project") or {}).get("remote_root")
        if rr:
            return rr.rstrip("/")
    except Exception:  # noqa: BLE001 — fall back to the line scan
        pass
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"\s*remote_root:\s*[\"']?([^\"'#\n]+)", line)
            if m:
                return m.group(1).strip().rstrip("/")
    raise SystemExit("error: could not find project.remote_root in config.yaml")


def connect(env: dict):
    """Open a paramiko SFTP session from .env credentials."""
    try:
        import paramiko
    except ImportError:
        raise SystemExit("error: paramiko is required (pip install paramiko)")
    host = env.get("SFTP_HOST")
    if not host:
        raise SystemExit("error: SFTP_HOST missing from .env")
    port = int(env.get("SFTP_PORT") or 22)
    user = env.get("SFTP_USER")
    transport = paramiko.Transport((host, port))
    key_file = env.get("SFTP_KEY_FILE")
    if key_file:
        pkey = paramiko.RSAKey.from_private_key_file(
            os.path.expanduser(key_file), password=env.get("SFTP_KEY_PASSPHRASE") or None)
        transport.connect(username=user, pkey=pkey)
    else:
        transport.connect(username=user, password=env.get("SFTP_PASSWORD") or None)
    return transport, transport.open_sftp_client()


def sftp_mkdirs(sftp, remote_dir: str) -> None:
    parts, cur = remote_dir.strip("/").split("/"), ""
    for p in parts:
        cur += "/" + p
        try:
            sftp.stat(cur)
        except IOError:
            sftp.mkdir(cur)


# --------------------------------------------------------------------------- #
# Source archive
# --------------------------------------------------------------------------- #
def _should_skip(rel_parts) -> bool:
    if any(part in IGNORE_DIRS for part in rel_parts[:-1]):
        return True
    name = rel_parts[-1]
    return name in IGNORE_NAMES or name.endswith(IGNORE_SUFFIXES)


def make_source_zip(dest_zip: str) -> int:
    count = 0
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT)
                if _should_skip(rel.split(os.sep)):
                    continue
                zf.write(full, rel)
                count += 1
    return count


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _dist_base(args) -> str:
    rr = remote_root_from_config(args.config)
    return posixpath.join(rr, DIST_SUBDIR)


def cmd_push(args) -> int:
    env = load_env(args.env)
    base = _dist_base(args)
    remote = posixpath.join(base, SRC_ARCHIVE)
    with tempfile.TemporaryDirectory() as td:
        local_zip = os.path.join(td, "legami-src.zip")
        n = make_source_zip(local_zip)
        size = os.path.getsize(local_zip)
        print(f"packed {n} files ({size/1e6:.1f} MB) -> {remote}")
        transport, sftp = connect(env)
        try:
            sftp_mkdirs(sftp, posixpath.dirname(remote))
            sftp.put(local_zip, remote)
        finally:
            sftp.close(); transport.close()
    print("source pushed.")
    return 0


def cmd_pull(args) -> int:
    env = load_env(args.env)
    remote = posixpath.join(_dist_base(args), SRC_ARCHIVE)
    target = os.path.abspath(args.into)
    os.makedirs(target, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        local_zip = os.path.join(td, "legami-src.zip")
        transport, sftp = connect(env)
        try:
            print(f"downloading {remote}")
            sftp.get(remote, local_zip)
        finally:
            sftp.close(); transport.close()
        with zipfile.ZipFile(local_zip) as zf:
            zf.extractall(target)
            n = len(zf.namelist())
    print(f"extracted {n} files into {target}")
    return 0


def cmd_publish_bundle(args) -> int:
    env = load_env(args.env)
    tag = "windows" if os.name == "nt" else platform.system().lower()
    local_zip = args.bundle or os.path.join(ROOT, "dist", f"Legami-{tag}.zip")
    if not os.path.isfile(local_zip):
        raise SystemExit(f"error: bundle not found: {local_zip} "
                         f"(run `python build.py --zip` first)")
    remote = posixpath.join(_dist_base(args), tag, os.path.basename(local_zip))
    transport, sftp = connect(env)
    try:
        sftp_mkdirs(sftp, posixpath.dirname(remote))
        size = os.path.getsize(local_zip)
        print(f"uploading {os.path.basename(local_zip)} ({size/1e6:.1f} MB) -> {remote}")
        sftp.put(local_zip, remote)
    finally:
        sftp.close(); transport.close()
    print("bundle published.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", default=os.path.join(ROOT, ".env"),
                   help="path to .env (default: ./.env)")
    p.add_argument("--config", default=os.path.join(ROOT, "config.yaml"),
                   help="path to config.yaml (for remote_root)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("push", help="zip the source tree and upload to SFTP _dist/src/"
                   ).set_defaults(func=cmd_push)

    pl = sub.add_parser("pull", help="download + extract the source tree (build box)")
    pl.add_argument("--into", default=ROOT, help="target dir (default: repo root)")
    pl.set_defaults(func=cmd_pull)

    pb = sub.add_parser("publish-bundle",
                        help="upload the built Legami-<os>.zip for artists")
    pb.add_argument("--bundle", default="", help="explicit zip path (default: dist/Legami-<os>.zip)")
    pb.set_defaults(func=cmd_publish_bundle)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
