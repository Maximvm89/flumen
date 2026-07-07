"""Local storage triage: what's on disk and what is safe to delete.

Almost everything under the local root is a mirror of the FTP, so deletability
is knowable: a file whose byte size matches its server counterpart costs
nothing to delete (the app re-downloads on demand), temp/backup files are
regenerable, and superseded work versions are probably-safe. Only files that
exist nowhere else are truly unsafe. Pure functions — the GUI feeds in the
local walk + a remote size index and renders the verdicts.
"""

from __future__ import annotations

import os
import re

# Categories, in display order. The first two are safe to delete (pre-checked
# in the UI), old_work is opt-in, the last two are never offered.
MIRRORED = "mirrored"        # safe — same file (size-verified) on the server
TEMP = "temp"                # safe — regenerable temp/backup files
OLD_WORK = "old_work"        # probably safe — superseded work versions
ACTIVE_WORK = "active_work"  # keep — the newest work versions of each file
LOCAL_ONLY = "local_only"    # keep — exists only on this machine (or modified)

CATEGORIES = (MIRRORED, TEMP, OLD_WORK, ACTIVE_WORK, LOCAL_ONLY)
SAFE = frozenset({MIRRORED, TEMP})
DELETABLE = frozenset({MIRRORED, TEMP, OLD_WORK})

LABELS = {
    MIRRORED: "Safe — on the server (re-downloads on demand)",
    TEMP: "Safe — temp / backup files",
    OLD_WORK: "Probably safe — superseded work versions",
    ACTIVE_WORK: "Keep — latest work versions",
    LOCAL_ONLY: "Keep — only on this machine (not backed up)",
}

TEMP_DIR_MARKERS = ("_tt_frames_", "__pycache__")
TEMP_SUFFIXES = (".blend1", ".blend2", ".blend3", ".tmp")
TEMP_NAMES = (".DS_Store", "Thumbs.db", "desktop.ini")

_VERSIONED = re.compile(r"_v(\d+)(\.[A-Za-z0-9]+)$")


def scan_local(local_root: str) -> list[dict]:
    """Every file under the local root: [{rel, size, mtime}], rel with forward
    slashes. Unreadable entries are skipped."""
    out = []
    for dirpath, _dirnames, filenames in os.walk(local_root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            rel = os.path.relpath(path, local_root).replace(os.sep, "/")
            out.append({"rel": rel, "size": st.st_size, "mtime": st.st_mtime})
    return out


def is_temp(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return (name in TEMP_NAMES
            or name.endswith(TEMP_SUFFIXES)
            or any(m in rel for m in TEMP_DIR_MARKERS))


def _work_group(rel: str) -> tuple[str, str] | None:
    """(dir, versionless-name) for a versioned file inside a work/ folder, else
    None. Groups 'disco_model_v003.blend' with its other versions."""
    head, _, name = rel.rpartition("/")
    if "work" not in head.split("/"):
        return None
    m = _VERSIONED.search(name)
    if not m:
        return None
    return head, _VERSIONED.sub(m.group(2), name)


def _version_of(rel: str) -> int:
    m = _VERSIONED.search(rel.rsplit("/", 1)[-1])
    return int(m.group(1)) if m else 0


def split_work_versions(files: list[dict],
                        keep_latest: int = 2) -> tuple[set, set]:
    """(old, active) rels of versioned work files: per (folder, name) group the
    newest `keep_latest` versions are active, the rest are superseded."""
    groups: dict[tuple, list[str]] = {}
    for f in files:
        key = _work_group(f["rel"])
        if key is not None and not is_temp(f["rel"]):
            groups.setdefault(key, []).append(f["rel"])
    old, active = set(), set()
    for rels in groups.values():
        rels.sort(key=_version_of, reverse=True)
        active.update(rels[:keep_latest])
        old.update(rels[keep_latest:])
    return old, active


def classify(files: list[dict], remote_sizes: dict[str, int],
             keep_latest: int = 2) -> list[dict]:
    """Each local file with a `category` verdict. `remote_sizes` maps rel ->
    size for every server file in the folders that exist locally; a size match
    is the proof a local copy is expendable. Active (newest) work versions are
    never marked deletable, even when mirrored — an artist's working set
    shouldn't be one pre-checked box away from disappearing."""
    old, active = split_work_versions(files, keep_latest)
    out = []
    for f in files:
        rel = f["rel"]
        mirrored = remote_sizes.get(rel) == f["size"]
        if is_temp(rel):
            cat = TEMP
        elif rel in active:
            cat = ACTIVE_WORK
        elif rel in old:
            cat = MIRRORED if mirrored else OLD_WORK
        elif mirrored:
            cat = MIRRORED
        else:
            cat = LOCAL_ONLY
        out.append({**f, "category": cat})
    return out


def summarize(records: list[dict]) -> dict:
    """{category: {count, size}} over classified records (all categories
    present), plus 'reclaimable' = the safe categories' total size."""
    out = {c: {"count": 0, "size": 0} for c in CATEGORIES}
    for r in records:
        s = out[r["category"]]
        s["count"] += 1
        s["size"] += r["size"]
    out["reclaimable"] = sum(out[c]["size"] for c in SAFE)
    return out


def group_key(rel: str) -> str:
    """Grouping bucket for the storage tree: the entity folder when the path is
    deep enough (03_assets/environments/disco, 04_shots/sq010/sh010,
    07_dailies/…), else the top-level folder."""
    parts = rel.split("/")
    return "/".join(parts[:3]) if len(parts) > 3 else (parts[0] if parts else "")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"
