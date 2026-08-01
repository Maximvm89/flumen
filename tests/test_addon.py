"""Tests for the Blender addon that don't need a running Blender.

A minimal fake `bpy` is injected so the addon modules import, then apply_settings
is exercised against a fake scene to confirm the JSON->scene mapping is correct.
"""

import json
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "blender_addon"))


# --- inject a fake bpy so addon modules import outside Blender ---------------
def _install_fake_bpy():
    bpy = types.ModuleType("bpy")
    bpy.types = types.SimpleNamespace(Operator=object, Panel=object,
                                      AddonPreferences=object, Menu=object,
                                      PropertyGroup=object)
    def _prop(*a, **k):
        return None
    bpy.props = types.SimpleNamespace(
        BoolProperty=_prop, StringProperty=_prop, IntProperty=_prop,
        FloatProperty=_prop, EnumProperty=_prop, CollectionProperty=_prop,
        PointerProperty=_prop)
    bpy.utils = types.SimpleNamespace(
        user_resource=lambda *a, **k: "/tmp/flumen_modules")
    bpy.context = types.SimpleNamespace()
    bpy.data = types.SimpleNamespace(scenes=[])
    sys.modules["bpy"] = bpy


_install_fake_bpy()

from flumen_pipeline import settings_io, operators  # noqa: E402


def _ns(**kw):
    return types.SimpleNamespace(**kw)


def _fake_scene():
    return _ns(
        display_settings=_ns(display_device=None),
        view_settings=_ns(view_transform=None, look=None, exposure=None, gamma=None),
        sequencer_colorspace_settings=_ns(name=None),
        render=_ns(engine=None, film_transparent=None, resolution_x=None,
                   resolution_y=None, resolution_percentage=None, fps=None,
                   fps_base=None, filepath=None,
                   image_settings=_ns(file_format=None, color_depth=None, exr_codec=None)),
        cycles=_ns(device=None, samples=None, use_denoising=None),
        eevee=_ns(taa_render_samples=None, use_raytracing=None),
        unit_settings=_ns(system=None, scale_length=None, length_unit=None),
        frame_start=None, frame_end=None,
    )


SAMPLE = json.loads((ROOT / "pipeline_config" / "project_settings.json").read_text())


def test_settings_loader(tmp_path):
    # Build a fake project root with the settings file in place.
    d = tmp_path / "02_pipeline"
    d.mkdir(parents=True)
    (d / "project_settings.json").write_text(json.dumps(SAMPLE))
    data = settings_io.load_settings(str(tmp_path))
    assert settings_io.get(data, "render.fps") == 24
    assert settings_io.get(data, "color_management.working_space") == "ACEScg"
    assert settings_io.get(data, "missing.key", "x") == "x"


def test_apply_settings_maps_all_fields():
    scene = _fake_scene()
    warnings = []
    operators.apply_settings(scene, SAMPLE, "/proj/LEGAMI", warnings)

    assert warnings == [], warnings  # nothing should fail against a fake scene
    assert scene.render.engine == "BLENDER_EEVEE"   # project finals: EEVEE
    assert scene.render.fps == 24
    assert scene.render.resolution_x == 1920
    assert scene.view_settings.view_transform == "ACES 1.0 - SDR Video"
    assert scene.display_settings.display_device == "sRGB - Display"
    assert scene.unit_settings.system == "METRIC"
    assert scene.frame_end == 250
    assert scene.eevee.taa_render_samples == 64      # EEVEE finals settings
    assert scene.eevee.use_raytracing is True
    assert scene.render.image_settings.file_format == "OPEN_EXR_MULTILAYER"
    # output path joined under the project root + the rel path
    assert scene.render.filepath.startswith(os.path.join("/proj/LEGAMI", "06_renders"))


def test_apply_skips_cycles_when_engine_not_cycles():
    scene = _fake_scene()
    data = json.loads(json.dumps(SAMPLE))
    data["render"]["engine"] = "BLENDER_EEVEE_NEXT"
    warnings = []
    operators.apply_settings(scene, data, "/proj/LEGAMI", warnings)
    assert scene.cycles.samples is None  # cycles block skipped
    assert scene.render.engine == "BLENDER_EEVEE_NEXT"


def test_parse_progress_matches_toolkit_format():
    # The add-on parser must agree with flumen.progress (separate Pythons).
    from flumen import progress as P
    line = P.format_line(50, 100, 5, "uploading panda_model_v001.blend")
    assert operators._parse_progress(line) == (50, 5.0, "uploading panda_model_v001.blend")
    assert operators._parse_progress("not a progress line") is None
    # blank eta early on
    pct, eta, _ = operators._parse_progress(P.format_line(0, 100, 0, "x"))
    assert pct == 0 and eta is None


def test_human_eta_formatting():
    assert operators._human_eta(None) == ""
    assert operators._human_eta(8) == "~8s left"
    assert operators._human_eta(125) == "~2m left"


def test_dressing_collect_prop_instances_and_environment():
    from types import SimpleNamespace as NS
    from flumen_pipeline import dressing as D

    class FakeObj:
        def __init__(self, name, props=None, matrix=None):
            self.name = name
            self._props = props or {}
            self.matrix_world = matrix or [[1, 0, 0, 0], [0, 1, 0, 0],
                                           [0, 0, 1, 0], [0, 0, 0, 1]]
        def get(self, key, default=None):
            return self._props.get(key, default)

    objs = [
        FakeObj("prop_root__lantern", {
            "flumen_prop_id": "lantern", "flumen_prop_asset": "props/lantern",
            "flumen_prop_step": "model",
            "flumen_prop_blend_rel": "03_assets/props/lantern/model/publish/l_v002.blend",
            "flumen_prop_collection": "lantern"},
            [[1, 0, 0, 2.5], [0, 1, 0, -1], [0, 0, 1, 0], [0, 0, 0, 1]]),
        FakeObj("some_mesh"),                       # ignored: not a prop root
        FakeObj("prop_root__crate", {}),            # minimal: id from the name
    ]
    props = D.collect_prop_instances(objs)
    assert [p["id"] for p in props] == ["crate", "lantern"]        # sorted
    lantern = props[1]
    assert lantern["asset"] == "props/lantern"
    assert lantern["object"] == "prop_root__lantern"
    assert lantern["matrix_world"][0][3] == 2.5
    assert props[0]["source_step"] == "model"                       # default

    colls = [FakeObj("element__x"), FakeObj("environment__market_square", {
        "flumen_env_asset": "environments/market_square",
        "flumen_env_blend_rel": "03_assets/environments/market_square/model/publish/m_v004.blend"})]
    env = D.collect_environment(colls)
    assert env["asset"] == "environments/market_square"
    assert env["source_step"] == "model"
    assert D.collect_environment([FakeObj("element__x")]) is None


def test_dressing_unmanaged_holders_and_ids_and_rel():
    from flumen_pipeline import dressing as D

    class N:
        def __init__(self, name):
            self.name = name
        def get(self, k, d=None):
            return d

    colls = [N("prop__lantern"), N("prop__crate"), N("environment__m")]
    objs = [N("prop_root__lantern")]
    assert D.unmanaged_prop_holders(colls, objs) == ["prop__crate"]

    assert D.prop_id_for("Lantern", set()) == "lantern"
    assert D.prop_id_for("lantern", {"lantern"}) == "lantern_2"
    assert D.prop_id_for("lantern", {"lantern", "lantern_2"}) == "lantern_3"

    assert D.rel_from_local("E:\\Legami_4\\03_assets\\props\\l.blend",
                            "E:/Legami_4") == "03_assets/props/l.blend"
    assert D.rel_from_local("/mnt/other/x.blend", "/home/me/proj") == ""


def test_dressing_naming_parity_with_toolkit():
    # The addon slug must agree with flumen.dressing (separate Pythons at runtime).
    from flumen_pipeline import dressing as AD
    from flumen import dressing as TD
    for raw in ("Night Market!", "", "  a--b  "):
        assert AD.normalize_dressing_name(raw) == TD.normalize_dressing_name(raw)


def test_dressing_collect_local_extras():
    from types import SimpleNamespace as NS
    from flumen_pipeline import dressing as D

    def obj(name, otype="MESH", lib=None, override=None, data_lib=None):
        return NS(name=name, type=otype, library=lib, override_library=override,
                  data=NS(library=data_lib))

    local_mesh = obj("crate_quick")                       # modeled in-scene
    local_curve = obj("cable", otype="CURVE")
    linked_env = obj("wall", lib=object())                # linked environment
    override_prop = obj("lantern", override=object())     # overridden prop
    linked_data = obj("glass", data_lib=object())         # override w/ linked mesh
    prop_root = obj("prop_root__lantern", otype="EMPTY")
    light = obj("key_light", otype="LIGHT")
    cam = obj("REVIEW_Camera", otype="CAMERA")

    out = D.collect_local_extras([local_mesh, local_curve, linked_env,
                                  override_prop, linked_data, prop_root,
                                  light, cam])
    assert [o.name for o in out] == ["cable", "crate_quick"]


def test_look_mesh_matching_handles_instance_suffixes():
    # look re-apply must map a look manifest's clean mesh names onto a cache
    # instance's collision-suffixed meshes — both Blender '.001' and Alembic's
    # underscore form '_001' (the .abc can't store dots).
    from flumen_pipeline import looks

    class O:
        def __init__(self, n):
            self.name = n

    m = looks._match_meshes_by_name
    # first instance: clean names, exact match
    r = m(["Skeleton_Base", "Skeleton_Head"],
          [O("Skeleton_Base"), O("Skeleton_Head")])
    assert r["Skeleton_Base"].name == "Skeleton_Base"
    # 2nd instance from a cache: underscore-suffixed names still match
    r = m(["Skeleton_Base", "Skeleton_Head"],
          [O("Skeleton_Base_001"), O("Skeleton_Head_001")])
    assert r["Skeleton_Base"].name == "Skeleton_Base_001"
    assert r["Skeleton_Head"].name == "Skeleton_Head_001"
    # ambiguous base (two BODY meshes) paired by sorted order
    r = m(["BODY", "BODY.001"], [O("BODY.002"), O("BODY.003")])
    assert r["BODY"].name == "BODY.002" and r["BODY.001"].name == "BODY.003"
    assert looks._base_name("X_001") == "X" and looks._base_name("X.001") == "X"
    assert looks._base_name("arm_01") == "arm_01"    # 2-digit: not a suffix


def test_classify_anim_status_detects_a_rollback():
    """Publishing from a scene that is BEHIND silently overwrites newer work.

    Modelled on the real SEQ010/SH0010 case: v027 carried orso_1's v024
    animation while v025 (a colleague's) was newer — the dialog called it
    'changed', because it WAS different, just older."""
    c = operators.classify_anim_status
    # history is NEWEST first: (version, hash, author)
    hist = [("v025", "K", "francesco.catena"),
            ("v024", "J", "elena.zaretti"),
            ("v023", "I", "francesco.catena")]

    # the scene still holds v024's animation -> publishing would bury v025
    assert c("J", hist) == ("behind", "v025", "francesco.catena")
    # matching the newest is simply unchanged
    assert c("K", hist) == ("unchanged", "v025", "francesco.catena")
    # genuinely new content is a normal change
    assert c("Z", hist)[0] == "changed"
    # never published before
    assert c("Z", []) == ("new", "", "")
    # an element with a single publish can never be 'behind'
    assert c("A", [("v001", "A", "x")])[0] == "unchanged"
    assert c("B", [("v001", "A", "x")])[0] == "changed"


def test_classify_anim_status_flags_a_stale_base():
    """New work built on an OLD publish still buries whatever landed since.

    A content hash cannot see this: the animation matches no published version,
    so it looks like ordinary progress. The holder's flumen_anim stamp — the
    version the scene was BUILT from — is what gives it away. Real case:
    Francesco's v021 work file carries orso built from v025 while v029 exists."""
    c = operators.classify_anim_status
    hist = [("v029", "M", "marco.parisi2"), ("v025", "K", "francesco.catena")]

    # brand-new content, but the scene was assembled from v025 -> buries v029
    assert c("NEW", hist, "v025", "v029") == ("stale", "v029", "marco.parisi2")
    # same content, built from the newest -> ordinary progress
    assert c("NEW", hist, "v029", "v029")[0] == "changed"
    # no stamp to judge by -> fall back to 'changed', never a false alarm
    assert c("NEW", hist, "", "v029")[0] == "changed"
    # an exact older match is a REVERT, which outranks 'stale'
    assert c("K", hist, "v025", "v029")[0] == "behind"
    # matching the newest is unchanged even from a stale-looking stamp
    assert c("M", hist, "v025", "v029")[0] == "unchanged"
    assert operators._ver_num("animation v025") == 25
    assert operators._ver_num("") == 0


# --- animator-added constraints ---------------------------------------------
def _fake_constraint(name, **props):
    """A constraint stand-in: name lookup is all _apply_one's guard needs."""
    c = _ns(name=name, **props)
    c.bl_rna = _ns(properties={k: _ns(identifier=k, type="FLOAT",
                                      is_readonly=False, is_array=False)
                               for k in props})
    return c


class _FakeStack(list):
    """A constraint collection: iterable, plus .new(type=…)."""
    def new(self, type=""):  # noqa: A002 — mirrors Blender's signature
        c = _fake_constraint("", influence=1.0)
        c.type = type
        self.append(c)
        return c


def test_constraint_digest_is_order_independent_and_sees_a_retarget():
    """The publish dialog decides 'changed' from a content hash. A constraint
    retarget or an influence tweak must move it, while re-reading the SAME
    stack must not — otherwise every publish reads as changed."""
    from flumen_pipeline import constraints as C
    a = {"bones": {"wing": [{"name": "hand off", "type": "CHILD_OF",
                             "props": {"influence": 0.75, "subtarget": "root"}}]}}
    b = {"bones": {"wing": [{"type": "CHILD_OF", "name": "hand off",
                             "props": {"subtarget": "root", "influence": 0.75}}]}}
    assert C.digest(a) == C.digest(b)          # key order is not content
    c = json.loads(json.dumps(a))
    c["bones"]["wing"][0]["props"]["influence"] = 0.5
    assert C.digest(c) != C.digest(a)


def test_constraint_apply_never_duplicates_an_existing_one():
    """Restore runs against a rig that already carries the RIGGER's stack, and
    a shot can be rebuilt twice. Both must be no-ops: matching by name is what
    stops the bat growing a second 'hand off' on every build."""
    from flumen_pipeline import constraints as C
    stack = _FakeStack([_fake_constraint("RIGGER copy rot")])
    spec = {"name": "hand off", "type": "CHILD_OF", "props": {"influence": 0.5}}
    trace = []
    assert C._apply_one(stack, spec, trace) is True
    assert [x.name for x in stack] == ["RIGGER copy rot", "hand off"]
    assert stack[1].influence == 0.5
    assert C._apply_one(stack, spec, trace) is False        # second build
    assert len(stack) == 2
    # a name the rig already uses is left alone, never overwritten
    assert C._apply_one(stack, {"name": "RIGGER copy rot", "type": "CHILD_OF",
                                "props": {}}, trace) is False
    assert len(stack) == 2


def test_constraints_ride_the_anim_manifest_bindings():
    """An element whose only publishable state is a constraint (no action of
    its own) must still reach the manifest — the build reads constraints out of
    'bindings', and build_anim_manifest drops empty maps."""
    from flumen_pipeline import anim
    bindings = {"pipistrello": {"rig": {"constraints": {
        "bones": {"wing": [{"name": "hand off", "type": "CHILD_OF",
                            "props": {}}]}}}}}
    m = anim.build_anim_manifest(7, {}, {"pipistrello": "h"}, {}, bindings)
    assert m["bindings"] == bindings
    assert m["hashes"] == {"pipistrello": "h"}   # kept: bindings make it known
    assert m["elements"] == {}
