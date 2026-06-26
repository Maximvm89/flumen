"""Command-line interface for the SFTP folder-structure tool.

Commands:
  test-connection   Verify SFTP credentials.
  init-project      Create the top-level structure (+ assets/shots from config).
  add-asset         Create one asset's folder tree.
  add-shot          Create one shot's folder tree.

Global flags:
  -c/--config       Path to project config YAML (default: config.yaml)
  --env             Path to .env file (default: .env)
  --dry-run         Print what would be created; touch nothing on the server.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import schema as schema_mod
from .config import ProjectConfig, SFTPCredentials
from .sftp import SFTPClient


def _report(created: list[str], skipped: list[str], dry_run: bool) -> None:
    tag = "WOULD CREATE" if dry_run else "CREATED"
    for p in created:
        print(f"  [{tag}] {p}")
    if skipped:
        print(f"  ({len(skipped)} already existed, skipped)")
    print(f"\n{len(created)} folder(s) {'to create' if dry_run else 'created'}, "
          f"{len(skipped)} skipped.")


def _client(args) -> SFTPClient:
    if args.dry_run:
        # Dry-run touches no server, so real credentials aren't required.
        try:
            creds = SFTPCredentials.from_env(args.env)
        except ValueError:
            creds = SFTPCredentials(host="(dry-run)", port=22, user="(dry-run)")
    else:
        creds = SFTPCredentials.from_env(args.env)
    return SFTPClient(creds, dry_run=args.dry_run)


def cmd_test_connection(args) -> int:
    creds = SFTPCredentials.from_env(args.env)
    print(f"Connecting to {creds.user}@{creds.host}:{creds.port} ...")
    with SFTPClient(creds, dry_run=False) as client:
        ok = client.exists(".")
    print("Connection OK." if ok else "Connected, but home dir not readable.")
    return 0


def cmd_init_project(args) -> int:
    cfg = ProjectConfig.load(args.config)
    paths = schema_mod.project_paths(cfg.schema, cfg.remote_root)

    # Bulk assets / shots from config.
    for asset_type, names in cfg.assets.items():
        for name in names:
            paths += schema_mod.asset_paths(cfg.schema, cfg.remote_root, asset_type, name)
    for seq, shots in cfg.shots.items():
        for shot in shots:
            paths += schema_mod.shot_paths(cfg.schema, cfg.remote_root, seq, shot)

    paths = sorted(set(paths))
    print(f"Project '{cfg.name}' [{cfg.code}] -> {cfg.remote_root}\n")
    with _client(args) as client:
        created, skipped = client.create_all(paths)
    _report(created, skipped, args.dry_run)
    return 0


def cmd_add_asset(args) -> int:
    cfg = ProjectConfig.load(args.config)
    valid = list((cfg.schema.get("root", {}).get("03_assets") or {}).keys())
    if valid and args.type not in valid:
        print(f"warning: asset type '{args.type}' not in schema types {valid}",
              file=sys.stderr)
    paths = schema_mod.asset_paths(cfg.schema, cfg.remote_root, args.type, args.name)
    print(f"Asset: {args.type}/{args.name}\n")
    with _client(args) as client:
        created, skipped = client.create_all(paths)
    _report(created, skipped, args.dry_run)
    return 0


def cmd_add_shot(args) -> int:
    cfg = ProjectConfig.load(args.config)
    paths = schema_mod.shot_paths(cfg.schema, cfg.remote_root, args.seq, args.shot)
    print(f"Shot: {args.seq}/{args.shot}\n")
    with _client(args) as client:
        created, skipped = client.create_all(paths)
    _report(created, skipped, args.dry_run)
    return 0


def cmd_put(args) -> int:
    import os
    import posixpath as _pp

    cfg = ProjectConfig.load(args.config)
    if not os.path.isfile(args.local):
        print(f"error: local file not found: {args.local}", file=sys.stderr)
        return 1

    remote = args.remote
    if not remote.startswith("/"):
        remote = _pp.join(cfg.remote_root, remote)  # relative -> under remote_root
    if remote.endswith("/"):  # directory given -> keep the local filename
        remote = _pp.join(remote, os.path.basename(args.local))

    print(f"Upload: {args.local}\n    -> {remote}\n")
    with _client(args) as client:
        client.upload(args.local, remote)  # preserves mtime (clean diffs)
        # Record attribution in the ledger (rel path under remote_root).
        if not args.dry_run and remote.startswith(cfg.remote_root):
            from . import ledger
            rel = _pp.relpath(remote, cfg.remote_root)
            ledger.record_uploads(client, cfg.remote_root, client.creds.user, [rel])
    print("done." if not args.dry_run else "(dry-run: nothing uploaded)")
    return 0


def sanitize_published_config(raw: dict) -> dict:
    """Strip per-machine fields so a published config doesn't leak one person's
    paths to every artist (local_root and tools.blender_path)."""
    (raw.get("project") or {}).pop("local_root", None)
    raw.pop("tools", None)
    return raw


def cmd_publish_config(args) -> int:
    """Upload the project config to the server so artists' apps download it on
    sign-in (config.yaml + its folder schema, into 02_pipeline/). Machine-specific
    fields (local_root, tools.blender_path) are stripped so they don't leak to
    every artist."""
    import os
    import posixpath as _pp
    import tempfile

    import yaml

    cfg = ProjectConfig.load(args.config)
    base = _pp.join(cfg.remote_root, "02_pipeline") + "/"

    with open(args.config, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    raw = sanitize_published_config(raw)

    tmp_dir = tempfile.mkdtemp()
    clean_cfg = os.path.join(tmp_dir, "config.yaml")
    with open(clean_cfg, "w", encoding="utf-8") as fh:
        yaml.safe_dump(raw, fh, sort_keys=False, default_flow_style=False)

    uploads = [(clean_cfg, base + "config.yaml")]
    schema_local = os.path.join(os.path.dirname(os.path.abspath(args.config)),
                                "folder_schema.yaml")
    if os.path.isfile(schema_local):
        uploads.append((schema_local, base + "folder_schema.yaml"))

    for local, remote in uploads:
        print(f"Upload: {os.path.basename(remote)}\n    -> {remote}")
    if args.dry_run:
        print("(dry-run: nothing uploaded; local_root/tools stripped)")
        return 0
    with _client(args) as client:
        for local, remote in uploads:
            client.upload(local, remote)
    print("done — artists will pick this up on their next sign-in.")
    return 0


def cmd_sync(args) -> int:
    import posixpath as _pp
    cfg = ProjectConfig.load(args.config)
    remote = args.remote
    if not remote.startswith("/"):
        remote = _pp.join(cfg.remote_root, remote)
    local = os.path.expanduser(args.local) if args.local else os.path.join(
        cfg.resolved_local_root(), _pp.relpath(remote, cfg.remote_root))
    print(f"Sync: {remote}\n   -> {local}\n")
    creds = SFTPCredentials.from_env(args.env)
    with SFTPClient(creds, dry_run=args.dry_run) as client:
        n = client.download_dir(remote, local)
    print(f"{n} file(s) synced." if not args.dry_run else "(dry-run)")
    return 0


def cmd_launch(args) -> int:
    from .launcher import launch
    cfg = ProjectConfig.load(args.config)
    if args.dry_run:
        creds = SFTPCredentials(host="(dry-run)", port=22, user="(dry-run)")
    else:
        creds = SFTPCredentials.from_env(args.env)
    return launch(cfg, creds, extra_args=args.blender_args or None,
                  dry_run=args.dry_run, no_sync=args.no_sync)


def cmd_new_task(args) -> int:
    cfg = ProjectConfig.load(args.config)
    from . import tasks as T
    if args.dry_run:
        print(f"(dry-run) would create task {T.make_id(args.type, args.entity, args.step)}")
        return 0
    creds = SFTPCredentials.from_env(args.env)
    task = T.new_task(args.type, args.entity, args.step, title=args.title or None)
    with SFTPClient(creds) as client:
        T.save_task(client, cfg.remote_root, task, actor=creds.user)
    print(f"created task: {task['id']}")
    return 0


def cmd_publish(args) -> int:
    cfg = ProjectConfig.load(args.config)
    missing = [f for f in args.local if not os.path.isfile(f)]
    if missing:
        print(f"error: local file(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"(dry-run) would publish {len(args.local)} file(s) for task "
              f"{args.task} and set status '{args.status}'")
        return 0
    from . import tasks as T
    creds = SFTPCredentials.from_env(args.env)
    with SFTPClient(creds) as client:
        rels = T.publish_task(client, cfg.remote_root, creds.user,
                              args.local, args.task, args.status,
                              description=args.description)
    if not rels:
        print(f"error: task not found: {args.task}", file=sys.stderr)
        return 1
    for rel in rels:
        print(f"published -> {cfg.remote_root}/{rel}")
    print(f"task {args.task} -> {args.status}")
    return 0


def cmd_turntable(args) -> int:
    cfg = ProjectConfig.load(args.config)
    if not args.dry_run and not os.path.isfile(args.model):
        print(f"error: model file not found: {args.model}", file=sys.stderr)
        return 1
    from . import turntable
    creds = (SFTPCredentials(host="(dry-run)", port=22, user="(dry-run)")
             if args.dry_run else SFTPCredentials.from_env(args.env))
    return turntable.run_turntable(cfg, creds, args.model, args.task,
                                   dry_run=args.dry_run, preview=args.preview,
                                   syncsketch=not args.no_syncsketch)


def cmd_syncsketch_setup(args) -> int:
    """Admin one-time: push the shared SyncSketch service-account secret to the
    server (02_pipeline/syncsketch.json) and cache it locally. Artists pick it up
    on their next sign-in. The secret is never committed to git."""
    import json
    import posixpath as _pp
    import tempfile

    from . import syncsketch as ss
    from .config import CACHED_SYNCSKETCH, CACHE_DIR

    cfg = ProjectConfig.load(args.config)
    payload = json.dumps({"login": args.login, "api_key": args.api_key}, indent=2)
    remote = _pp.join(cfg.remote_root, ss.SECRET_REL)
    print(f"Upload: SyncSketch secret\n    -> {remote}")
    if args.dry_run:
        print("(dry-run: nothing uploaded)")
        return 0

    tmp = os.path.join(tempfile.mkdtemp(), "syncsketch.json")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload + "\n")
    with _client(args) as client:
        client.upload(tmp, remote)
    # Cache locally too so this machine can upload immediately.
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHED_SYNCSKETCH, "w", encoding="utf-8") as fh:
        fh.write(payload + "\n")
    try:
        os.chmod(CACHED_SYNCSKETCH, 0o600)
    except OSError:
        pass
    print("done — artists will pick up the SyncSketch login on next sign-in.")
    return 0


def cmd_syncsketch_sync(args) -> int:
    """Backfill: upload every dailies video that has no SyncSketch review yet."""
    from . import tasks as T
    from . import syncsketch as ss

    cfg = ProjectConfig.load(args.config)
    settings = ss.SyncSketchSettings.from_project_settings(
        _load_project_settings_for(cfg))
    if not settings.configured():
        print("SyncSketch is not enabled/configured in project_settings.json.")
        return 1

    creds = SFTPCredentials.from_env(args.env)
    local_root = cfg.resolved_local_root()
    # Read-only client even on --dry-run: we must read tasks to report what would
    # upload. Writes (upload + record) are guarded by args.dry_run below.
    with SFTPClient(creds) as client:
        task_list = T.load_tasks(client, cfg.remote_root)
        if args.task:
            task_list = [t for t in task_list if t.get("id") == args.task]
        pending = ss.pending_uploads(task_list)
        if not pending:
            print("Nothing to upload — all dailies are already on SyncSketch.")
            return 0
        print(f"{len(pending)} daily(ies) to upload.")
        done = 0
        for task, rec in pending:
            rel = rec["turntable"]
            version_label = os.path.splitext(os.path.basename(rel))[0]
            version_label = version_label.replace("_turntable", "")
            out_local = os.path.join(local_root, *rel.split("/"))
            if args.dry_run:
                print(f"(dry-run) would upload {rel} for task {task.get('id')}")
                continue
            if not os.path.isfile(out_local):  # fetch from server if not local
                try:
                    client.download(cfg.remote_root.rstrip("/") + "/" + rel, out_local)
                except Exception as exc:  # noqa: BLE001
                    print(f"warning: could not fetch {rel} ({exc}); skipping.")
                    continue
            url = ss.try_upload_daily(
                settings, project_name=cfg.name, video_local=out_local,
                task=task, version_label=version_label, username=creds.user)
            if url:
                ss.record_review_url(client, cfg.remote_root, task["id"], url, creds.user)
                print(f"  {task.get('id')}: {url}")
                done += 1
    print(f"uploaded {done} daily(ies) to SyncSketch."
          if not args.dry_run else "(dry-run)")
    return 0


def cmd_build_review(args) -> int:
    """Collect the turntables waiting for review into a dated folder on the server,
    write a clickable review sheet, and flag each as collected."""
    from . import tasks as T
    from . import review as R

    cfg = ProjectConfig.load(args.config)
    date_str = args.date or R.today_str()
    creds = SFTPCredentials.from_env(args.env)

    if args.dry_run:
        # Read-only: just report what would collect.
        with SFTPClient(creds) as client:
            waiting = R.collectable(T.load_tasks(client, cfg.remote_root),
                                    status=args.status)
        if not waiting:
            print(f"Nothing waiting for review (status '{args.status}').")
            return 0
        print(f"{len(waiting)} clip(s) for review {date_str}:")
        for task, rec in waiting:
            print(f"  {task.get('id')}  ->  {R.clip_name(rec)}")
        print(f"(dry-run) would add {len(waiting)} clip(s) to "
              f"{R.review_dir_rel(date_str)}/")
        return 0

    with SFTPClient(creds) as client:
        res = R.build_review_session(
            client, remote_root=cfg.remote_root, project_name=cfg.name,
            local_root=cfg.resolved_local_root(), username=creds.user,
            date_str=date_str, status=args.status, log=print)
    if not res["collected"]:
        print(f"Nothing waiting for review (status '{args.status}').")
        return 0
    print(f"\nReview built -> {cfg.remote_root}/{res['folder_rel']}\n"
          f"  local: {res['folder_local']}\n"
          f"  {res['count']} clip(s); open index.html to scrub the batch.")
    if getattr(args, "open", False):
        from . import clipboard
        clipboard.reveal(res["folder_local"])
    return 0


def cmd_reset_review(args) -> int:
    """Undo a review session: un-stamp the tasks collected that day and clear the
    review folder, so 'build-review' can rebuild it from scratch."""
    import shutil

    from . import tasks as T
    from . import review as R

    cfg = ProjectConfig.load(args.config)
    date_str = args.date or R.today_str()
    review_rel = R.review_dir_rel(date_str)
    base = cfg.remote_root.rstrip("/") + "/" + review_rel

    creds = SFTPCredentials.from_env(args.env)
    with SFTPClient(creds) as client:
        cleared = 0
        for task in T.load_tasks(client, cfg.remote_root):
            n = R.clear_reviewed(task, date_str)
            if n:
                cleared += n
                print(f"  un-stamp {task.get('id')} ({n})")
                if not args.dry_run:
                    T.save_task(client, cfg.remote_root, task, actor=creds.user)
        removed = 0
        if client.exists(base):
            for e in client.listdir(base):
                if not e["is_dir"]:
                    removed += 1
                    if not args.dry_run:
                        client.remove(base + "/" + e["name"])
        if args.dry_run:
            print(f"(dry-run) would un-stamp {cleared} record(s) and remove "
                  f"{removed} file(s) from {review_rel}/")
            return 0
        shutil.rmtree(os.path.join(cfg.resolved_local_root(), *review_rel.split("/")),
                      ignore_errors=True)

    print(f"Reset review {date_str}: un-stamped {cleared} record(s), cleared "
          f"{removed} file(s). Run 'build-review' to rebuild.")
    return 0


def _review_local_dir(cfg, date_str: str) -> str:
    from . import review as R
    return os.path.join(cfg.resolved_local_root(),
                        *R.review_dir_rel(date_str).split("/"))


def cmd_review_copy(args) -> int:
    """Copy a review video onto the clipboard (the file itself, like Finder ⌘C) so
    SyncSketch's MEDIA ▸ Upload from ▸ Clipboard grabs it. Local-only, no server."""
    import glob

    from . import review as R
    from . import clipboard

    cfg = ProjectConfig.load(args.config)
    date_str = args.date or R.today_str()
    review_local = _review_local_dir(cfg, date_str)
    if not os.path.isdir(review_local):
        print(f"No local review folder for {date_str}:\n  {review_local}\n"
              f"Run 'animpipe build-review' (or sync it down) first.")
        return 1
    clips = sorted(glob.glob(os.path.join(review_local, "*.mp4")))
    if not clips:
        print(f"No videos in {review_local}.")
        return 1
    names = [os.path.basename(c) for c in clips]

    if args.list:
        for i, n in enumerate(names, 1):
            print(f"  {i}. {n}")
        return 0

    # Single-clip clipboard copy (for SyncSketch ▸ MEDIA ▸ Upload from ▸ Clipboard),
    # which is the one case the OS file-clipboard handles reliably.
    chosen = None
    if args.clip:
        matches = [c for c, n in zip(clips, names) if args.clip in n]
        if not matches:
            print(f"No clip matching '{args.clip}'. Use --list to see options.")
            return 1
        chosen = matches[0]
    elif args.index:
        if not 1 <= args.index <= len(clips):
            print(f"Index out of range (1..{len(clips)}).")
            return 1
        chosen = clips[args.index - 1]

    if chosen is not None:
        if clipboard.copy_file(chosen):
            print(f"Copied to clipboard: {os.path.basename(chosen)}\n"
                  f"In SyncSketch: MEDIA ▸ Upload from ▸ Clipboard, then paste.")
            return 0
        print("Could not copy to the clipboard; opening the folder instead.")

    # Default (and the "all" case): open the folder so you can drag the clips
    # straight into SyncSketch (multi-file clipboard isn't reliable across OSes).
    print(f"{len(clips)} clip(s) in this review:")
    for n in names:
        print(f"  {n}")
    clipboard.reveal(review_local)
    print(f"\nOpened {review_local}\n"
          "Drag the clips into SyncSketch (MEDIA ▸ Your Computer, or drop them in).")
    return 0


def _load_project_settings_for(cfg) -> dict:
    """project_settings.json for the show, from the local cache/synced copy."""
    import json
    path = os.path.join(cfg.resolved_local_root(), "02_pipeline",
                        "project_settings.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except ValueError:
            return {}
    return {}


def build_parser() -> argparse.ArgumentParser:
    # Common flags live on a parent parser so they work either before OR after
    # the subcommand (e.g. both `animpipe --dry-run init-project` and
    # `animpipe init-project --dry-run`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default="config.yaml",
                        help="project config YAML")
    common.add_argument("--env", default=".env", help="path to .env file")
    common.add_argument("--dry-run", action="store_true",
                        help="preview without touching the server")

    p = argparse.ArgumentParser(
        prog="animpipe",
        parents=[common],
        description="SFTP folder-structure publisher for a 3D animation pipeline.",
    )
    from .version import get_version
    p.add_argument("--version", action="version", version=f"animpipe {get_version()}")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("test-connection", parents=[common],
                   help="verify SFTP credentials") \
        .set_defaults(func=cmd_test_connection)

    sub.add_parser("init-project", parents=[common],
                   help="create top-level structure") \
        .set_defaults(func=cmd_init_project)

    a = sub.add_parser("add-asset", parents=[common],
                       help="create one asset's folders")
    a.add_argument("--type", required=True, help="asset type (e.g. characters)")
    a.add_argument("--name", required=True, help="asset name (e.g. hero)")
    a.set_defaults(func=cmd_add_asset)

    s = sub.add_parser("add-shot", parents=[common],
                       help="create one shot's folders")
    s.add_argument("--seq", required=True, help="sequence code (e.g. SEQ010)")
    s.add_argument("--shot", required=True, help="shot code (e.g. SH0010)")
    s.set_defaults(func=cmd_add_shot)

    u = sub.add_parser("put", parents=[common],
                       help="upload a local file to the server")
    u.add_argument("--local", required=True, help="local file to upload")
    u.add_argument("--remote", required=True,
                   help="remote path (absolute, or relative to remote_root; "
                        "end with / to keep the local filename)")
    u.set_defaults(func=cmd_put)

    pc = sub.add_parser("publish-config", parents=[common],
                        help="upload config.yaml + folder schema to the server so "
                             "artists' apps download the project on sign-in")
    pc.set_defaults(func=cmd_publish_config)

    sy = sub.add_parser("sync", parents=[common],
                        help="download a remote folder to local")
    sy.add_argument("--remote", required=True,
                    help="remote folder (absolute or relative to remote_root)")
    sy.add_argument("--local", help="local destination (default: under local_root)")
    sy.set_defaults(func=cmd_sync)

    lc = sub.add_parser("launch", parents=[common],
                        help="sync pipeline config, set OCIO, start Blender")
    lc.add_argument("--no-sync", action="store_true",
                    help="skip the FTP sync, just launch with existing local config")
    lc.add_argument("blender_args", nargs="*",
                    help="extra args passed through to Blender")
    lc.set_defaults(func=cmd_launch)

    nt = sub.add_parser("new-task", parents=[common], help="create a task")
    nt.add_argument("--type", required=True, choices=["shot", "asset"])
    nt.add_argument("--entity", required=True,
                    help="e.g. characters/frankenstein or SEQ010/SH0010")
    nt.add_argument("--step", required=True, help="e.g. model, animation")
    nt.add_argument("--title", default="", help="optional display title")
    nt.set_defaults(func=cmd_new_task)

    pb = sub.add_parser("publish", parents=[common],
                        help="publish a file into a task's publish/ folder")
    pb.add_argument("--local", required=True, nargs="+",
                    help="local file(s) to publish")
    pb.add_argument("--task", required=True, help="task id")
    pb.add_argument("--status", default="review",
                    help="task status to set after publish (default: review)")
    pb.add_argument("--description", default="",
                    help="publish notes recorded in the task history")
    pb.set_defaults(func=cmd_publish)

    tt = sub.add_parser("turntable", parents=[common],
                        help="render a model turntable and publish it to 07_dailies")
    tt.add_argument("--model", required=True, help="published model .blend to render")
    tt.add_argument("--task", required=True, help="task id")
    tt.add_argument("--preview", action="store_true",
                    help="open Blender interactively to preview framing (no render)")
    tt.add_argument("--no-syncsketch", action="store_true",
                    help="skip the automatic SyncSketch upload for this render")
    tt.set_defaults(func=cmd_turntable)

    ssu = sub.add_parser("syncsketch-sync", parents=[common],
                         help="upload any dailies not yet on SyncSketch (backfill)")
    ssu.add_argument("--task", default="", help="limit to one task id")
    ssu.set_defaults(func=cmd_syncsketch_sync)

    sss = sub.add_parser("syncsketch-setup", parents=[common],
                         help="admin: push the shared SyncSketch login to the server")
    sss.add_argument("--login", required=True, help="SyncSketch service-account email")
    sss.add_argument("--api-key", required=True, help="SyncSketch API key")
    sss.set_defaults(func=cmd_syncsketch_setup)

    br = sub.add_parser("build-review", parents=[common],
                        help="collect dailies waiting for review into a dated folder")
    br.add_argument("--date", default="",
                    help="review session date (default: today, YYYY-MM-DD)")
    br.add_argument("--status", default="review",
                    help="task status to collect (default: review)")
    br.add_argument("--open", action="store_true",
                    help="reveal the review folder when done")
    br.set_defaults(func=cmd_build_review)

    rr = sub.add_parser("reset-review", parents=[common],
                        help="undo a review session so build-review can redo it")
    rr.add_argument("--date", default="",
                    help="review session date to reset (default: today)")
    rr.set_defaults(func=cmd_reset_review)

    rc = sub.add_parser("review-copy", parents=[common],
                        help="open the review folder (or copy one clip to clipboard)")
    rc.add_argument("--date", default="", help="review date (default: today)")
    rc.add_argument("--index", type=int, default=0,
                    help="copy this clip number to the clipboard (see --list)")
    rc.add_argument("--clip", default="",
                    help="copy the clip matching this filename substring")
    rc.add_argument("--list", action="store_true", help="just list the clips")
    rc.set_defaults(func=cmd_review_copy)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # friendly error, no traceback for CLI users
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
