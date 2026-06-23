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


@dataclass
class SFTPCredentials:
    host: str
    port: int
    user: str
    password: str | None = None
    key_file: str | None = None
    key_passphrase: str | None = None

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = ".env") -> "SFTPCredentials":
        if load_dotenv is not None and env_file and Path(env_file).exists():
            load_dotenv(env_file)

        host = os.environ.get("SFTP_HOST")
        user = os.environ.get("SFTP_USER")
        if not host or not user:
            raise ValueError(
                "SFTP_HOST and SFTP_USER must be set (in the environment or .env). "
                "See .env.example."
            )

        return cls(
            host=host,
            port=int(os.environ.get("SFTP_PORT", "22")),
            user=user,
            password=os.environ.get("SFTP_PASSWORD") or None,
            key_file=os.environ.get("SFTP_KEY_FILE") or None,
            key_passphrase=os.environ.get("SFTP_KEY_PASSPHRASE") or None,
        )


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

    def resolved_local_root(self) -> str:
        """Local project folder, defaulting to ~/Legami/<CODE> if not set."""
        if self.local_root:
            return os.path.expanduser(self.local_root)
        return os.path.join(os.path.expanduser("~"), "Legami", self.code)

    @classmethod
    def load(cls, config_path: str | os.PathLike) -> "ProjectConfig":
        config_path = Path(config_path)
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
        return cls(
            name=project["name"],
            code=project["code"],
            remote_root=project["remote_root"].rstrip("/") or "/",
            schema=schema,
            assets=raw.get("assets") or {},
            shots=raw.get("shots") or {},
            local_root=project.get("local_root"),
            blender_path=tools.get("blender_path"),
        )
