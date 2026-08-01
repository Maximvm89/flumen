"""Find the .blend libraries a work file links, and fetch the missing ones.

Blender opens a file whose linked libraries are absent SILENTLY: every override
collapses to an empty datablock and the shot just looks broken — no error, no
dialog. That is exactly what happens when artist A links an older publish
(benda_rig_v003) that artist B's mirror never downloaded (builds only fetch the
newest). The cure is a preflight: before launching Blender on a work file, scan
it for library paths, map each one into the local mirror, and download whatever
the server has that the disk hasn't.

The scan reads the .blend directly — no Blender launch. Library paths are
stored as NUL-terminated strings ('//../rig/publish/x.blend' relative to the
file, or absolute from the authoring machine), so a byte scan finds them
without parsing DNA. Absolute paths from ANOTHER machine are remapped by
locating the project-schema folder ('03_assets/…', '04_sequences/…') inside
them. Compressed saves are handled when the stdlib can (gzip always, zstd on
Python 3.14+); otherwise the scan returns nothing and the preflight is a no-op
— never an error in the open flow.
"""

from __future__ import annotations

import gzip
import os
import re

# A library path either starts with '//' (blend-relative) or is absolute
# (posix or windows). Trailing NUL-terminated string inside a datablock, so
# anchor at the end of the chunk.
_PATH_RE = re.compile(
    r"(?:(?://|/|[A-Za-z]:[\\/])[\w\-. ()@+~\\/]{0,400}?)\.blend$")

# Where a path can be re-rooted into the local mirror: the first schema folder
# found in it. Matches the server layout (see folder_schema.yaml).
_SCHEMA_ROOTS = ("02_pipeline/", "03_assets/", "04_sequences/",
                 "05_shots/", "06_renders/", "07_dailies/")


def _decompress(data: bytes) -> bytes | None:
    """Raw bytes of the uncompressed .blend, or None when we can't tell.
    Blender saves plain ('BLENDER…'), gzip (pre-3.0 compress) or zstd (3.0+
    compress)."""
    if data[:7] == b"BLENDER":
        return data
    if data[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(data)
        except Exception:  # noqa: BLE001
            return None
    if data[:4] == b"\x28\xb5\x2f\xfd":
        try:  # stdlib on 3.14+, the 'zstandard' wheel elsewhere; else give up
            try:
                from compression import zstd  # type: ignore
                return zstd.decompress(data)
            except ImportError:
                import zstandard  # type: ignore
                return zstandard.ZstdDecompressor().decompress(
                    data, max_output_size=1 << 31)
        except Exception:  # noqa: BLE001
            return None
    return None


def blend_library_paths(blend_path: str) -> list[str]:
    """Every library path stored in a .blend, exactly as written (relative
    '//…' or absolute), deduplicated in file order. Empty when the file can't
    be read or decompressed — callers treat that as 'nothing to fetch'."""
    try:
        with open(blend_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    data = _decompress(data)
    if data is None:
        return []
    out, seen = [], set()
    for chunk in data.split(b"\x00"):
        if not chunk.endswith(b".blend"):
            continue
        # the string sits at the END of the chunk; whatever precedes it is the
        # datablock's other binary fields
        m = _PATH_RE.search(chunk[-420:].decode("latin-1"))
        if not m:
            continue
        p = m.group(0)
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _project_rel(path: str) -> str | None:
    """The path's project-relative form ('03_assets/…/x.blend'), found by the
    first schema folder inside it — machine-independent. None when the path
    doesn't live under the project layout at all."""
    norm = path.replace("\\", "/")
    for root in _SCHEMA_ROOTS:
        i = norm.find("/" + root)
        if i >= 0:
            return norm[i + 1:]
        if norm.startswith(root):
            return norm
    return None


def missing_libraries(blend_path: str, local_root: str) -> list[tuple[str, str]]:
    """The linked libraries this file will FAIL to load: [(rel, local_abs)…].
    rel is the project-relative path (what to download from the server),
    local_abs where it belongs in the mirror. A library outside the project
    layout (an artist's stray desktop file) can't be fetched and is skipped —
    Blender will still warn about it, but that one really is theirs to fix."""
    blend_dir = os.path.dirname(os.path.abspath(blend_path))
    self_name = os.path.basename(blend_path)
    out, seen = [], set()
    for p in blend_library_paths(blend_path):
        if p.startswith("//"):
            absolute = os.path.normpath(
                os.path.join(blend_dir, p[2:].replace("\\", "/")))
        else:
            absolute = os.path.normpath(p.replace("\\", "/"))
        if os.path.basename(absolute) == self_name:
            continue                      # a self-reference, not a library
        if os.path.isfile(absolute):
            continue                      # resolves as stored — nothing to do
        rel = _project_rel(p if not p.startswith("//") else absolute)
        if not rel or rel in seen:
            continue
        seen.add(rel)
        local_abs = os.path.join(local_root, *rel.split("/"))
        if not os.path.isfile(local_abs):
            out.append((rel, local_abs))
    return out


def fetch_missing_libraries(sftp, remote_root: str, local_root: str,
                            blend_path: str, log=None) -> tuple[list, list]:
    """Download every library `blend_path` links that the local mirror lacks.
    Recurses into what it fetched (a publish can link further publishes).
    Returns (fetched_rels, failed_rels); a rel the server doesn't have lands in
    failed — the caller decides whether that's worth a warning. Never raises
    for a single bad file; the preflight must not block opening."""
    rr = remote_root.rstrip("/")
    fetched, failed, todo, seen = [], [], [blend_path], set()
    while todo:
        current = todo.pop(0)
        for rel, local_abs in missing_libraries(current, local_root):
            if rel in seen:
                continue
            seen.add(rel)
            try:
                os.makedirs(os.path.dirname(local_abs), exist_ok=True)
                sftp.download(f"{rr}/{rel}", local_abs)
                fetched.append(rel)
                todo.append(local_abs)    # its own links may be missing too
                if log:
                    log(f"fetched missing library: {rel}")
            except Exception as exc:  # noqa: BLE001
                failed.append(rel)
                if log:
                    log(f"could not fetch {rel}: {exc}")
    return fetched, failed
