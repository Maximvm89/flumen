"""Workspace logic — no GUI, no Qt. Unit-testable.

Responsibilities:
  * mirror_structure: shallow copy (folders only) of the remote project locally
  * scan_local:       list local files under work/ and publish/ with sizes
  * diff:             compare local vs remote by size + modified time
  * set_local_root_in_config: point the rest of the pipeline at the local root
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

AREA_NAMES = ("work", "publish")
MTIME_TOLERANCE = 2.0  # seconds; filesystems/servers round mtimes differently

# Diff statuses
IN_SYNC = "in_sync"
LOCAL_ONLY = "local_only"      # exists locally, not on FTP  -> upload candidate
REMOTE_ONLY = "remote_only"    # exists on FTP, not locally  -> download candidate
LOCAL_NEWER = "local_newer"    # -> upload candidate
REMOTE_NEWER = "remote_newer"  # -> download candidate
SIZE_DIFFERS = "size_differs"  # same path, different size   -> needs attention


@dataclass
class DiffRow:
    rel: str
    status: str
    local_size: int | None
    remote_size: int | None
    local_mtime: float | None
    remote_mtime: float | None


def in_tracked_area(rel: str, area_names=AREA_NAMES) -> bool:
    parts = rel.replace("\\", "/").split("/")
    return any(p in area_names for p in parts)


# --- shallow copy of the structure ------------------------------------------
def mirror_structure(sftp, remote_root: str, local_root: str) -> list[str]:
    """Create the remote folder tree locally (directories only). Returns the list
    of directories created (skips ones that already exist)."""
    created: list[str] = []
    os.makedirs(local_root, exist_ok=True)
    for entry in sftp.walk_remote(remote_root):
        if not entry["is_dir"]:
            continue
        local_dir = os.path.join(local_root, *entry["rel"].split("/"))
        if not os.path.isdir(local_dir):
            os.makedirs(local_dir, exist_ok=True)
            created.append(local_dir)
    return created


# --- local scan -------------------------------------------------------------
def scan_local(local_root: str, area_names=AREA_NAMES) -> dict[str, tuple[int, float]]:
    """Map rel-path -> (size, mtime) for files under tracked areas."""
    out: dict[str, tuple[int, float]] = {}
    for dirpath, _dirs, files in os.walk(local_root):
        for f in files:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, local_root).replace("\\", "/")
            if not in_tracked_area(rel, area_names):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            out[rel] = (st.st_size, st.st_mtime)
    return out


def local_total_size(local_files: dict[str, tuple[int, float]]) -> int:
    return sum(sz for sz, _ in local_files.values())


# --- diff -------------------------------------------------------------------
def _status(lsz, lmt, rsz, rmt) -> str:
    if lsz != rsz:
        return SIZE_DIFFERS
    if lmt is not None and rmt is not None:
        if lmt > rmt + MTIME_TOLERANCE:
            return LOCAL_NEWER
        if rmt > lmt + MTIME_TOLERANCE:
            return REMOTE_NEWER
    return IN_SYNC


def diff(sftp, remote_root: str, local_root: str,
         area_names=AREA_NAMES) -> list[DiffRow]:
    """Compare tracked-area files between local and remote."""
    remote_files = {
        e["rel"]: e for e in sftp.walk_remote(remote_root)
        if not e["is_dir"] and in_tracked_area(e["rel"], area_names)
    }
    local_files = scan_local(local_root, area_names)

    rows: list[DiffRow] = []
    for rel in sorted(set(remote_files) | set(local_files)):
        loc = local_files.get(rel)
        rem = remote_files.get(rel)
        if loc and rem:
            status = _status(loc[0], loc[1], rem["size"], rem["mtime"])
            rows.append(DiffRow(rel, status, loc[0], rem["size"], loc[1], rem["mtime"]))
        elif loc and not rem:
            rows.append(DiffRow(rel, LOCAL_ONLY, loc[0], None, loc[1], None))
        else:
            rows.append(DiffRow(rel, REMOTE_ONLY, None, rem["size"], None, rem["mtime"]))
    return rows


def summarize(rows: list[DiffRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


# --- helpers for transfers --------------------------------------------------
def remote_path_for(remote_root: str, rel: str) -> str:
    return remote_root.rstrip("/") + "/" + rel


def local_path_for(local_root: str, rel: str) -> str:
    return os.path.join(local_root, *rel.split("/"))


# --- wire the rest of the pipeline to the chosen local root -----------------
def set_local_root_in_config(config_path: str, value: str) -> None:
    """Set project.local_root in config.yaml, preserving comments. This is how
    'Configure Blender' makes the launcher + addon save into this structure."""
    with open(config_path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    value_line_re = re.compile(r"^(\s*)local_root:\s*.*$")
    remote_re = re.compile(r"^(\s*)remote_root:\s*.*$")

    # 1. replace an existing active local_root line
    for i, line in enumerate(lines):
        if value_line_re.match(line) and not line.lstrip().startswith("#"):
            indent = value_line_re.match(line).group(1)
            lines[i] = f'{indent}local_root: "{value}"'
            break
    else:
        # 2. otherwise insert right after remote_root
        for i, line in enumerate(lines):
            if remote_re.match(line):
                indent = remote_re.match(line).group(1)
                lines.insert(i + 1, f'{indent}local_root: "{value}"')
                break
        else:
            raise ValueError("could not find project.remote_root in config to anchor local_root")

    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def human_size(n: int | None) -> str:
    if n is None:
        return "—"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"
