"""Animation publishing helpers — pure logic, no `bpy`.

A shot's animation is published as a set of **Actions** (keyframe data) plus a
manifest mapping each element to the object→action pairs it carries, so Build shot
can re-apply the animation onto freshly-linked rigs. This mirrors look.py (which
does the same for materials). The bpy-free pieces live here so they're testable;
the operator supplies the live holders/actions.

The anim artifact sits beside the shot publish: ``<shot>_<step>_vNNN_anim.blend``
(+ ``..._anim.manifest.json``), so it versions with the shot publish.
"""

from __future__ import annotations

ANIM_SUFFIX = "_anim"


def anim_blend_path(pub_path: str) -> str:
    """The anim .blend beside a shot publish .blend: '…_v003.blend' ->
    '…_v003_anim.blend'."""
    return pub_path[:-len(".blend")] + ANIM_SUFFIX + ".blend" \
        if pub_path.endswith(".blend") else pub_path + ANIM_SUFFIX + ".blend"


def anim_manifest_path(anim_blend: str) -> str:
    """'…_v003_anim.blend' -> '…_v003_anim.manifest.json'."""
    return anim_blend[:-len(".blend")] + ".manifest.json"


def is_anim_blend(name: str) -> bool:
    """True for an anim artifact name, so the main shot .blend is told apart."""
    return name.endswith(ANIM_SUFFIX + ".blend")


def build_anim_manifest(version: int, element_actions: dict,
                        hashes: dict | None = None) -> dict:
    """The <base>_vNNN_anim.manifest.json payload: which action sits on which object,
    grouped per element, plus a content hash per element for dedup. `element_actions`
    is {element_id: {object_name: action_name}} (empty maps dropped); `hashes` is
    {element_id: sha1} (kept only for the published elements)."""
    elements = {eid: dict(objs) for eid, objs in (element_actions or {}).items()
                if objs}
    keep = {eid: h for eid, h in (hashes or {}).items() if eid in elements}
    return {"version": int(version), "elements": elements, "hashes": keep}
