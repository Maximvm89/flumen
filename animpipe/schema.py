"""Turn the YAML folder schema into a flat, sorted list of POSIX paths.

Pure path logic — no network. Easy to unit-test.
"""

from __future__ import annotations

import posixpath
from typing import Any, Iterable


def _walk(node: Any, base: str, out: list[str]) -> None:
    """Recursively collect folder paths from a schema sub-tree."""
    if not node:  # leaf folder ({} or None) — nothing more to add.
        return
    if not isinstance(node, dict):
        raise TypeError(
            f"schema node at '{base}' must be a mapping or empty, got {type(node).__name__}"
        )
    for name, child in node.items():
        path = posixpath.join(base, str(name))
        out.append(path)
        _walk(child, path, out)


def expand_tree(node: dict[str, Any], base: str) -> list[str]:
    """Return every folder path under `node`, rooted at `base`."""
    out: list[str] = []
    _walk(node, base, out)
    return _dedupe_sorted(out)


def project_paths(schema: dict[str, Any], remote_root: str) -> list[str]:
    """All static top-level folders defined under `root`."""
    return expand_tree(schema.get("root") or {}, remote_root)


def asset_paths(
    schema: dict[str, Any], remote_root: str, asset_type: str, name: str
) -> list[str]:
    """Folders for a single asset, including its parent folder."""
    base = posixpath.join(remote_root, "03_assets", asset_type, name)
    paths = [base]
    paths += expand_tree(schema.get("asset_template") or {}, base)
    return _dedupe_sorted(paths)


def shot_paths(
    schema: dict[str, Any], remote_root: str, seq: str, shot: str
) -> list[str]:
    """Folders for a single shot, including its sequence + shot parents."""
    seq_base = posixpath.join(remote_root, "04_sequences", seq)
    base = posixpath.join(seq_base, shot)
    paths = [seq_base, base]
    paths += expand_tree(schema.get("shot_template") or {}, base)
    return _dedupe_sorted(paths)


def _dedupe_sorted(paths: Iterable[str]) -> list[str]:
    # Sorting guarantees parents precede children, so mkdir order is safe.
    return sorted(set(paths))
