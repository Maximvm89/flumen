"""Configuration + schema + credential loading.

Credentials are read from environment variables (optionally seeded by a .env
file). Project settings and the folder schema are read from YAML.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional; env vars still work without it.
    load_dotenv = None


# Everything per-user lives under ~/.legami so artists sign in once (via the
# Workspace app) and never edit a file: the saved login (credentials.env) and the
# show config downloaded from the server (cache/). Both the GUI and the toolkit
# the Blender add-on shells out to read from here, so a publish/turntable from
# Blender uses the same login and the same cached project config.
LEGAMI_HOME = os.path.join(os.path.expanduser("~"), ".legami")
USER_CRED_FILE = os.path.join(LEGAMI_HOME, "credentials.env")
CACHE_DIR = os.path.join(LEGAMI_HOME, "cache")
CACHED_CONFIG = os.path.join(CACHE_DIR, "config.yaml")


@dataclass
class SFTPCredentials:
    host: str
    port: int
    user: str
    password: str | None = None
    key_file: str | None = None
    key_passphrase: str | None = None
    remote_root: str | None = None     # project root entered at sign-in (where config lives)

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = ".env") -> "SFTPCredentials":
        # Precedence: already-exported env vars > project .env (devs) > the
        # per-user credentials the app saved (artists).
        if load_dotenv is not None:
            if env_file and Path(env_file).exists():
                load_dotenv(env_file)
            if os.path.exists(USER_CRED_FILE):
                load_dotenv(USER_CRED_FILE)

        host = os.environ.get("SFTP_HOST")
        user = os.environ.get("SFTP_USER")
        if not host or not user:
            raise ValueError(
                "Not signed in. Open the Workspace app and sign in, or set "
                "SFTP_HOST and SFTP_USER (in the environment or .env)."
            )

        return cls(
            host=host,
            port=int(os.environ.get("SFTP_PORT", "22")),
            user=user,
            password=os.environ.get("SFTP_PASSWORD") or None,
            key_file=os.environ.get("SFTP_KEY_FILE") or None,
            key_passphrase=os.environ.get("SFTP_KEY_PASSPHRASE") or None,
            remote_root=os.environ.get("LEGAMI_REMOTE_ROOT") or None,
        )

    def save_user(self) -> str:
        """Persist this login + project root to the per-user file (read by both the
        app and the toolkit). Returns the path written."""
        os.makedirs(os.path.dirname(USER_CRED_FILE), exist_ok=True)
        lines = [
            f"SFTP_HOST={self.host}",
            f"SFTP_PORT={self.port}",
            f"SFTP_USER={self.user}",
            f"SFTP_PASSWORD={self.password or ''}",
            f"LEGAMI_REMOTE_ROOT={self.remote_root or ''}",
        ]
        with open(USER_CRED_FILE, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        try:
            os.chmod(USER_CRED_FILE, 0o600)  # best-effort: keep it user-only
        except OSError:
            pass
        # Reflect immediately in this process so a subsequent from_env sees it.
        os.environ.update({"SFTP_HOST": self.host, "SFTP_PORT": str(self.port),
                           "SFTP_USER": self.user, "SFTP_PASSWORD": self.password or "",
                           "LEGAMI_REMOTE_ROOT": self.remote_root or ""})
        return USER_CRED_FILE

    @classmethod
    def signed_in(cls) -> bool:
        """True if a saved login is available (file or already-set env)."""
        if os.environ.get("SFTP_HOST") and os.environ.get("SFTP_USER"):
            return True
        return os.path.exists(USER_CRED_FILE)


@dataclass
class ProjectConfig:
    name: str
    code: str
    remote_root: str
    schema: dict[str, Any]
    assets: dict[str, list[str]]
    shots: dict[str, list[str]]
    local_root: str | None = None      # where the project is synced on this machine
    blender_path: str | None = None    # explicit Blender executable (optional)
    naming: dict = None                # naming-convention regex overrides
    sftp_host: str | None = None       # show SFTP server (so the bundle preconfigures it)
    sftp_port: int = 22

    def resolved_local_root(self) -> str:
        """Local project folder, defaulting to ~/Legami/<CODE> if not set."""
        if self.local_root:
            return os.path.expanduser(self.local_root)
        return os.path.join(os.path.expanduser("~"), "Legami", self.code)

    @classmethod
    def load(cls, config_path: str | os.PathLike) -> "ProjectConfig":
        config_path = Path(config_path)
        # Generic bundle / artist machine: no local config.yaml — fall back to the
        # show config the Workspace app downloaded from the server into the cache.
        if not config_path.exists() and Path(CACHED_CONFIG).exists():
            config_path = Path(CACHED_CONFIG)
        with open(config_path) as fh:
            raw = yaml.safe_load(fh) or {}

        project = raw.get("project") or {}
        for key in ("name", "code", "remote_root"):
            if not project.get(key):
                raise ValueError(f"config 'project.{key}' is required in {config_path}")

        # Resolve schema path relative to the config file.
        schema_ref = raw.get("schema", "folder_schema.yaml")
        schema_path = Path(schema_ref)
        if not schema_path.is_absolute():
            schema_path = config_path.parent / schema_path
        if not schema_path.exists():
            raise FileNotFoundError(f"schema file not found: {schema_path}")
        with open(schema_path) as fh:
            schema = yaml.safe_load(fh) or {}

        tools = raw.get("tools") or {}
        sftp = raw.get("sftp") or {}
        return cls(
            name=project["name"],
            code=project["code"],
            remote_root=project["remote_root"].rstrip("/") or "/",
            schema=schema,
            assets=raw.get("assets") or {},
            shots=raw.get("shots") or {},
            local_root=project.get("local_root"),
            blender_path=tools.get("blender_path"),
            naming=raw.get("naming") or {},
            sftp_host=sftp.get("host"),
            sftp_port=int(sftp.get("port", 22)),
        )
