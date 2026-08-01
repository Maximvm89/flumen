"""Tests for blend_deps — the missing-library preflight.

Blender loads a missing linked library as EMPTY, silently; these tests pin the
scanner (raw byte scan, no Blender), the machine-independent re-rooting of
another artist's paths, and the recursive fetch.
"""

import gzip
import os

from flumen import blend_deps as B


def _blob(*paths):
    """A synthetic .blend: header + NUL-separated binary junk with library
    path strings embedded exactly as Blender stores them (NUL-terminated)."""
    parts = [b"BLENDER-v501RENDH\x08\x00\x00\x00"]
    for p in paths:
        parts.append(b"\x93\x07binaryjunk" + p.encode() + b"\x00")
    return b"".join(parts)


def test_scanner_finds_relative_windows_and_posix_paths(tmp_path):
    f = tmp_path / "shot.blend"
    f.write_bytes(_blob(
        r"//..\..\03_assets\props\benda\rig\publish\benda_rig_v003.blend",
        "//../../03_assets/characters/orso/rig/publish/orso_rig_v004.blend",
        "//SEQ010_SH0010_animation_TORNADO2.blend",
        "not_a_path.txt",
    ))
    got = B.blend_library_paths(str(f))
    assert len(got) == 3
    assert got[0].endswith("benda_rig_v003.blend")
    assert got[2] == "//SEQ010_SH0010_animation_TORNADO2.blend"


def test_scanner_reads_gzip_and_gives_up_cleanly_on_garbage(tmp_path):
    f = tmp_path / "gz.blend"
    f.write_bytes(gzip.compress(_blob("//../03_assets/a/b/c.blend")))
    assert B.blend_library_paths(str(f)) == ["//../03_assets/a/b/c.blend"]
    g = tmp_path / "junk.blend"
    g.write_bytes(b"\x00\x01\x02 definitely not a blend")
    assert B.blend_library_paths(str(g)) == []          # no-op, never a raise


def test_project_rel_reroots_another_machines_absolute_path():
    # Elena's Windows mount is meaningless here — the schema folder inside the
    # path is what identifies the file project-wide.
    assert B._project_rel(
        r"C:\Users\elena\LEGAMI\03_assets\props\benda\rig\publish\x.blend"
    ) == "03_assets/props/benda/rig/publish/x.blend"
    assert B._project_rel("/mnt/proj/04_sequences/SEQ010/SH0010/a.blend") \
        == "04_sequences/SEQ010/SH0010/a.blend"
    assert B._project_rel("/home/elena/Desktop/loose.blend") is None


def test_missing_libraries_only_reports_fetchable_gaps(tmp_path):
    """Present files and out-of-project files are skipped; a '//' path missing
    from the mirror maps to its server rel. This is the benda_rig_v003 case."""
    root = tmp_path / "LEGAMI"
    workdir = root / "04_sequences/SEQ010/SH0010/animation/work"
    workdir.mkdir(parents=True)
    present = root / "03_assets/characters/orso/rig/publish"
    present.mkdir(parents=True)
    (present / "orso_rig_v004.blend").write_bytes(b"BLENDERx")
    f = workdir / "shot.blend"
    f.write_bytes(_blob(
        r"//..\..\..\..\..\03_assets\props\benda\rig\publish\benda_rig_v003.blend",
        r"//..\..\..\..\..\03_assets\characters\orso\rig\publish\orso_rig_v004.blend",
        r"//..\..\..\..\..\..\Program Files\Blender\assets\essentials.blend",
    ))
    missing = B.missing_libraries(str(f), str(root))
    assert [rel for rel, _ in missing] == \
        ["03_assets/props/benda/rig/publish/benda_rig_v003.blend"]
    _, local_abs = missing[0]
    assert local_abs == str(root / "03_assets/props/benda/rig/publish/benda_rig_v003.blend")


class _FakeSFTP:
    """Serves rel->bytes like the server; records what was downloaded."""
    def __init__(self, files):
        self.files, self.got = files, []

    def download(self, remote, local):
        rel = remote.split("/LEG/", 1)[1]
        if rel not in self.files:
            raise FileNotFoundError(rel)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(self.files[rel])
        self.got.append(rel)


def test_fetch_recurses_into_what_it_fetched(tmp_path):
    """The real TORNADO2 case: the work file links a sibling work file, which
    itself links a publish neither machine has. One preflight gets both."""
    root = tmp_path / "LEGAMI"
    workdir = root / "04_sequences/SEQ010/SH0010/animation/work"
    workdir.mkdir(parents=True)
    f = workdir / "shot.blend"
    f.write_bytes(_blob("//TORNADO2.blend"))
    srv = _FakeSFTP({
        "04_sequences/SEQ010/SH0010/animation/work/TORNADO2.blend":
            _blob(r"//..\..\..\..\..\03_assets\props\benda\rig\publish\benda_rig_v001.blend"),
        "03_assets/props/benda/rig/publish/benda_rig_v001.blend":
            b"BLENDERx",
    })
    fetched, failed = B.fetch_missing_libraries(srv, "/LEG", str(root), str(f))
    assert failed == []
    assert fetched == [
        "04_sequences/SEQ010/SH0010/animation/work/TORNADO2.blend",
        "03_assets/props/benda/rig/publish/benda_rig_v001.blend"]
    assert (root / "03_assets/props/benda/rig/publish/benda_rig_v001.blend").is_file()


def test_fetch_reports_what_the_server_lacks_without_raising(tmp_path):
    root = tmp_path / "LEGAMI"
    workdir = root / "04_sequences/S/S/animation/work"
    workdir.mkdir(parents=True)
    f = workdir / "shot.blend"
    f.write_bytes(_blob("//gone.blend"))
    fetched, failed = B.fetch_missing_libraries(
        _FakeSFTP({}), "/LEG", str(root), str(f))
    assert fetched == []
    assert failed == ["04_sequences/S/S/animation/work/gone.blend"]
