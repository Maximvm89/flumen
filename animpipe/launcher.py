"""Cross-platform Blender launcher.

Flow:
  1. Sync the project's pipeline config (02_pipeline/) from SFTP to the local
     project root (so the OCIO config + project_settings.json are present).
  2. Set BLENDER_OCIO to the local OCIO config (Blender reads it at startup —
     this is what guarantees correct color every session).
  3. Find the Blender executable and launch it.

Blender deps (paramiko etc.) come from animpipe's own environment, so artists
never have to install anything into Blender's Python just to launch.
"""

from __future__ import annotations

import glob
import os
import platform
import subprocess
import sys

from .config import ProjectConfig, SFTPCredentials
from .sftp import SFTPClient


def find_blender(explicit: str | None = None) -> str | None:
    """Locate the Blender executable. Priority: explicit path > LEGAMI_BLENDER
    env > OS-standard locations."""
    candidates: list[str] = []
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    if os.environ.get("LEGAMI_BLENDER"):
        candidates.append(os.environ["LEGAMI_BLENDER"])

    system = platform.system()
    if system == "Darwin":
        candidates += sorted(glob.glob("/Applications/Blender*.app/Contents/MacOS/Blender"),
                             reverse=True)
        candidates.append("/Applications/Blender.app/Contents/MacOS/Blender")
    elif system == "Windows":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            candidates += sorted(
                glob.glob(os.path.join(base, "Blender Foundation", "Blender*", "blender.exe")),
                reverse=True)
    else:  # Linux
        from shutil import which
        for name in ("blender",):
            found = which(name)
            if found:
                candidates.append(found)
        candidates += ["/usr/bin/blender", "/usr/local/bin/blender",
                       "/var/lib/flatpak/exports/bin/org.blender.Blender"]

    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _resolve_ocio(local_root: str) -> str | None:
    """Find the OCIO config to use. Prefer the stable 'config.ocio' name; if it's
    missing (e.g. a symlink didn't survive transfer to Windows), fall back to the
    newest *.ocio file in the folder."""
    ocio_dir = os.path.join(local_root, "02_pipeline", "ocio")
    preferred = os.path.join(ocio_dir, "config.ocio")
    if os.path.isfile(preferred):
        return preferred
    candidates = sorted(glob.glob(os.path.join(ocio_dir, "*.ocio")), reverse=True)
    return candidates[0] if candidates else None


def sync_pipeline_config(cfg: ProjectConfig, creds: SFTPCredentials,
                         dry_run: bool = False) -> str:
    """Sync remote 02_pipeline/ into the local project root. Returns local path."""
    local_root = cfg.resolved_local_root()
    remote_pipeline = cfg.remote_root.rstrip("/") + "/02_pipeline"
    local_pipeline = os.path.join(local_root, "02_pipeline")
    print(f"Syncing pipeline config:\n  {remote_pipeline}\n  -> {local_pipeline}")
    with SFTPClient(creds, dry_run=dry_run) as client:
        n = client.download_dir(remote_pipeline, local_pipeline)
    print(f"  {n} file(s) synced.")
    return local_root


def launch(cfg: ProjectConfig, creds: SFTPCredentials, extra_args: list[str] | None = None,
           dry_run: bool = False, no_sync: bool = False,
           extra_env: dict | None = None, open_file: str | None = None) -> int:
    local_root = cfg.resolved_local_root()
    if not no_sync:
        local_root = sync_pipeline_config(cfg, creds, dry_run=dry_run)

    env = os.environ.copy()
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})
    ocio = _resolve_ocio(local_root)
    if ocio:
        env["BLENDER_OCIO"] = ocio
        print(f"BLENDER_OCIO = {ocio}")
    else:
        print("warning: no OCIO config found under 02_pipeline/ocio/ — Blender "
              "will use its bundled config. Run a sync first.", file=sys.stderr)

    # Pass the project root to the addon so it can find project_settings.json.
    env["LEGAMI_PROJECT_ROOT"] = local_root
    # Let the addon shell back to this toolkit (for publish uploads).
    env["LEGAMI_TOOLKIT_PY"] = sys.executable
    env["LEGAMI_TOOLKIT_DIR"] = os.getcwd()
    # Folder containing the legami_pipeline package, for auto-load on launch.
    addon_dir = os.path.join(os.getcwd(), "blender_addon")
    if os.path.isdir(addon_dir):
        env["LEGAMI_ADDON_DIR"] = addon_dir

    blender = find_blender(cfg.blender_path)
    if not blender:
        msg = ("could not find Blender. Set tools.blender_path in config.yaml "
               "or the LEGAMI_BLENDER environment variable.")
        if dry_run:
            print(f"(dry-run) warning: {msg}")
            return 0
        print(f"error: {msg}", file=sys.stderr)
        return 1
    print(f"Launching: {blender}")

    if dry_run:
        print("(dry-run: not actually launching)")
        return 0

    cmd = [blender]
    # Open a specific .blend (e.g. the latest published version) if given.
    if open_file and os.path.isfile(open_file):
        cmd.append(open_file)
        print(f"Opening: {open_file}")
    elif open_file:
        print(f"warning: file to open not found: {open_file}", file=sys.stderr)
    # Auto-load the add-on for this session (no manual install needed).
    bootstrap = os.path.join(os.path.dirname(__file__), "blender_bootstrap.py")
    if "LEGAMI_ADDON_DIR" in env and os.path.isfile(bootstrap):
        cmd += ["--python", bootstrap]
    cmd += (extra_args or [])
    # Detach so closing the terminal doesn't kill Blender.
    subprocess.Popen(cmd, env=env)
    return 0
