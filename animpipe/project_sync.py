"""Download the show's project config from the server into the local cache.

The standalone bundle ships no show config — the artist signs in with host +
project root + login, and the app pulls the project's config.yaml + folder schema
from <remote_root>/02_pipeline/ (the same area the launcher already syncs
project_settings.json + OCIO from). Cached under ~/.legami/cache so both the GUI
and the toolkit the Blender add-on shells out to read the same project.
"""
from __future__ import annotations

import os

from .config import CACHE_DIR, CACHED_CONFIG, SFTPCredentials

PIPELINE_SUBDIR = "02_pipeline"
# Files that make up the project config on the server (config.yaml references the
# schema by relative name, so both land in the same cache dir).
CONFIG_FILES = ["config.yaml", "folder_schema.yaml"]


def remote_config_dir(remote_root: str) -> str:
    return remote_root.rstrip("/") + "/" + PIPELINE_SUBDIR


def fetch_project_config(creds: SFTPCredentials, remote_root: str) -> str:
    """Download the project config from the server into the cache. Also doubles as
    the sign-in connectivity check (raises on bad host/root/login). Returns the
    cached config.yaml path."""
    from .sftp import SFTPClient

    os.makedirs(CACHE_DIR, exist_ok=True)
    base = remote_config_dir(remote_root)
    with SFTPClient(creds) as client:
        # config.yaml is required; the schema is optional (older projects may omit it).
        client.download(base + "/config.yaml", CACHED_CONFIG)
        for name in CONFIG_FILES[1:]:
            try:
                client.download(base + "/" + name, os.path.join(CACHE_DIR, name))
            except (IOError, OSError):
                pass
    return CACHED_CONFIG
