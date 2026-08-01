"""Find animation ROLLBACKS in a shot's publish history (read-only).

Animation publishes per element, and Build shot resolves each element to the
newest version containing it. But a publish captures whatever is in the
PUBLISHER's scene — with no idea whether that is newer or older than what is
already on the server. So publishing from a scene that is behind silently
re-publishes an OLD animation over a colleague's newer one, on elements the
publisher never touched. Nothing errors; the publish dialog just says
"changed", because it is different — just different in the wrong direction.

This walks each element's published history in true chronological order and
flags any publish whose captured animation is byte-identical to an OLDER
version while a DIFFERENT one existed in between.

    .venv/bin/python scripts/anim_audit.py SEQ010/SH0010
    .venv/bin/python scripts/anim_audit.py SEQ010/SH0010 --step animation --show orso_1

Comparison uses the per-element hashes each publish already records, so it
costs one task read and never opens a .blend.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _when(ts):
    try:
        return _dt.datetime.fromtimestamp(ts or 0).strftime("%d %b %H:%M")
    except Exception:  # noqa: BLE001
        return "?"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("shot", help="shot entity, e.g. SEQ010/SH0010")
    ap.add_argument("--step", default="animation", help="shot step (default: animation)")
    ap.add_argument("--show", default="", help="also print this element's full lineage")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args()

    from flumen.config import ProjectConfig
    from flumen.sftp import SFTPClient, SFTPCredentials
    from flumen import elements as E

    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    with SFTPClient(creds) as c:
        anims = E.published_animations(c, cfg.remote_root, args.shot, args.step)
        res = E.resolved_animation(c, cfg.remote_root, args.shot, args.step)
    current = {eid: d["version"]
               for eid, d in (res or {}).get("elements", {}).items()}

    # ONE sequence only: 'layout' publishes are a separate, upstream chain whose
    # version numbers interleave with the animation ones — mixing them invents
    # rollbacks that never happened.
    anims = [a for a in anims if a.get("step") == args.step]
    anims.sort(key=lambda a: a.get("time") or 0)          # true chronology
    if not anims:
        print(f"no '{args.step}' animation publishes for {args.shot}.")
        return 0

    hist: dict[str, list] = {}
    for a in anims:
        for eid, hv in (a.get("hashes") or {}).items():
            hist.setdefault(eid, []).append(
                (a.get("version"), hv, a.get("by", "?"), a.get("time") or 0))

    print(f"=== {args.shot} · {args.step} · {len(anims)} publish(es), "
          f"{len(hist)} element(s) ===")
    rollbacks = 0
    for eid, rows in sorted(hist.items()):
        seen: dict[str, int] = {}
        for i, (ver, hv, by, t) in enumerate(rows):
            if hv in seen:
                j = seen[hv]
                lost = [r for r in rows[j + 1:i] if r[1] != hv]
                if lost:
                    rollbacks += 1
                    print(f"\n  ROLLBACK  {eid}")
                    print(f"     {ver} by {by} on {_when(t)}")
                    print(f"     re-published the animation from {rows[j][0]}")
                    for r in lost:
                        print(f"     -> overwrote {r[0]} by {r[2]} ({_when(r[3])})")
            seen[hv] = i
    print(f"\n{rollbacks} rollback(s) found."
          + ("" if rollbacks else "  Every publish moved each element forward."))

    if args.show:
        rows = hist.get(args.show)
        if not rows:
            print(f"\nno published animation for element {args.show!r}.")
        else:
            print(f"\n=== lineage: {args.show} "
                  f"(currently resolves to {current.get(args.show)}) ===")
            tags: dict[str, str] = {}
            for ver, hv, by, t in rows:
                tags.setdefault(hv, chr(ord("A") + len(tags)))
                mark = "  <== CURRENT" if ver == current.get(args.show) else ""
                print(f"   {str(ver):<8} {_when(t)}  {by:<20} "
                      f"content={tags[hv]}{mark}")
            print("   (same letter = byte-identical animation)")
    return 1 if rollbacks else 0


if __name__ == "__main__":
    raise SystemExit(main())
