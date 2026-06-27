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
    missing = [f for f in [*args.local, *(args.texture or [])]
               if not os.path.isfile(f)]
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
                              description=args.description,
                              texture_files=args.texture)
    if not rels:
        print(f"error: task not found: {args.task}", file=sys.stderr)
        return 1
    for rel in rels:
        print(f"published -> {cfg.remote_root}/{rel}")
    print(f"task {args.task} -> {args.status}")
    return 0


def cmd_fetch_publish(args) -> int:
    """Download a task's newest published file (default .blend) to a local folder
    and print its path. Used by the Blender add-on to pull the latest model publish
    into a surface workfile to shade."""
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import tasks as T
    with SFTPClient(creds) as client:
        task = T.get_task(client, cfg.remote_root, args.task)
        if not task:
            print(f"error: task not found: {args.task}", file=sys.stderr)
            return 1
        # --step redirects to the sibling task at that step (same entity), e.g. a
        # surface task fetching its asset's published model.
        if args.step and args.step != task.get("step"):
            sib = T.make_id(task.get("type", "asset"), task.get("entity", ""),
                            args.step)
            task = T.get_task(client, cfg.remote_root, sib)
            if not task:
                print(f"error: no '{args.step}' task for {args.task}",
                      file=sys.stderr)
                return 1
        pubs = T.published_files(task, ext=args.ext)
        if not pubs:
            print(f"error: no published {args.ext} for task {args.task}",
                  file=sys.stderr)
            return 1
        rel = pubs[0]["rel"]                      # newest first
        # Default into the local mirror of the publish folder, so it's cached where
        # the rest of the project syncs; fall back to cwd if no local_root is set.
        if args.into:
            into = os.path.expanduser(args.into)
        else:
            into = os.path.join(cfg.resolved_local_root() or os.getcwd(),
                                *os.path.dirname(rel).split("/"))
        os.makedirs(into, exist_ok=True)
        local_path = os.path.join(into, os.path.basename(rel))
        client.download(cfg.remote_root.rstrip("/") + "/" + rel, local_path)
    print(local_path)
    return 0


def cmd_list_looks(args) -> int:
    """Print the named looks a surface task has published, as JSON. Feeds the
    Blender 'Apply look' dropdown."""
    import json
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import tasks as T
    with SFTPClient(creds) as client:
        task = T.get_task(client, cfg.remote_root, args.task)
        if not task:
            print(f"error: task not found: {args.task}", file=sys.stderr)
            return 1
        looks = T.published_looks(task)
    print(json.dumps(looks))
    return 0


def cmd_fetch_look(args) -> int:
    """Download a published look (its .blend + manifest + textures/) into the local
    mirror and print the local .blend path. Used by 'Apply look' so downstream can
    append the materials with their textures resolving relative to the .blend."""
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import tasks as T
    remote_root = cfg.remote_root.rstrip("/")
    local_root = cfg.resolved_local_root() or os.getcwd()
    with SFTPClient(creds) as client:
        task = T.get_task(client, cfg.remote_root, args.task)
        if not task:
            print(f"error: task not found: {args.task}", file=sys.stderr)
            return 1
        sel = next((l for l in T.published_looks(task) if l["look"] == args.look),
                   None)
        if not sel:
            print(f"error: no look '{args.look}' for {args.task}", file=sys.stderr)
            return 1
        blend_rel, manifest_rel = sel["blend_rel"], sel["manifest_rel"]
        local_blend = os.path.join(local_root, *blend_rel.split("/"))
        client.download(remote_root + "/" + blend_rel, local_blend)
        client.download(remote_root + "/" + manifest_rel,
                        os.path.join(local_root, *manifest_rel.split("/")))
        # The look's textures live in <publish>/textures and are referenced
        # relatively, so they must sit beside the fetched .blend.
        pub_dir = blend_rel.rsplit("/", 1)[0]
        try:
            client.download_dir(remote_root + "/" + pub_dir + "/textures",
                                os.path.join(local_root, *pub_dir.split("/"),
                                             "textures"))
        except Exception:  # noqa: BLE001 — a look may carry no textures
            pass
    print(local_blend)
    return 0


def cmd_look_review(args) -> int:
    """Render a shaded look turntable + texture/UV sheet and publish them to
    07_dailies, attached to the look's publish record. Fetches the look, model and
    HDRI it needs, so it works standalone (re-generate) or right after a publish."""
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import tasks as T, lookdev, turntable
    rr = cfg.remote_root.rstrip("/")
    local_root = cfg.resolved_local_root() or os.getcwd()

    def _dl(client, rel, dest):
        client.download(rr + "/" + rel, dest)

    with SFTPClient(creds) as client:
        task = T.get_task(client, rr, args.task)
        if not task:
            print(f"error: task not found: {args.task}", file=sys.stderr)
            return 1
        entity = task["entity"]
        asset = entity.split("/")[-1]
        sel = next((l for l in T.published_looks(task) if l["look"] == args.look),
                   None)
        if not sel:
            print(f"error: no look '{args.look}' for {args.task}", file=sys.stderr)
            return 1
        version = args.version or sel["version"]
        base = f"{asset}_surface_{args.look}"
        version_label = f"{base}_v{version:03d}"
        pub_dir = f"03_assets/{entity}/surface/publish"
        blend_rel = f"{pub_dir}/{version_label}.blend"
        manifest_rel = f"{pub_dir}/{version_label}.manifest.json"

        look_blend = os.path.join(local_root, *blend_rel.split("/"))
        manifest_local = os.path.join(local_root, *manifest_rel.split("/"))
        _dl(client, blend_rel, look_blend)
        _dl(client, manifest_rel, manifest_local)
        try:
            client.download_dir(rr + "/" + pub_dir + "/textures/" + version_label,
                                os.path.join(local_root, *pub_dir.split("/"),
                                             "textures", version_label))
        except Exception:  # noqa: BLE001
            pass
        # The model to shade: the asset's latest published model .blend.
        model_task = T.get_task(client, rr, T.model_task_id(entity))
        mpubs = T.published_files(model_task, ".blend") if model_task else []
        if not mpubs:
            print(f"error: no published model for {entity}", file=sys.stderr)
            return 1
        model_rel = mpubs[0]["rel"]
        model_local = os.path.join(local_root, *model_rel.split("/"))
        _dl(client, model_rel, model_local)

    project_settings = turntable._load_project_settings(local_root)
    hdri = lookdev.resolve_hdri(project_settings, args.hdri, local_root)
    return turntable.run_look_review(
        cfg, creds, task_id=args.task, entity=entity, base=base, version=version,
        model_path=model_local, look_blend=look_blend,
        manifest_path=manifest_local, blend_rel=blend_rel, hdri=hdri,
        sheet_only=args.sheet_only, dry_run=args.dry_run)


def cmd_next_version(args) -> int:
    """Print the next publish version for a task (from its server history). Used by
    the Blender add-on so versions stay monotonic across machines."""
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import tasks as T
    with SFTPClient(creds) as client:
        task = T.get_task(client, cfg.remote_root, args.task)
    if not task:
        print(f"error: task not found: {args.task}", file=sys.stderr)
        return 1
    # --base lets the caller version a sub-variant (e.g. a named surface look,
    # '<asset>_surface_<look>') independently of the task's default base.
    base = args.base or f"{task['entity'].split('/')[-1]}_{task['step']}"
    print(T.next_version(task, base))
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
                                   dry_run=args.dry_run, preview=args.preview)


def cmd_build_review(args) -> int:
    """Export review items (optionally filtered by review status) to a dated folder
    with a clickable index.html, for sharing/offline scrubbing."""
    from . import tasks as T
    from . import review as R

    cfg = ProjectConfig.load(args.config)
    date_str = args.date or R.today_str()
    statuses = None if args.status == "all" else [args.status]
    creds = SFTPCredentials.from_env(args.env)

    with SFTPClient(creds) as client:
        items = R.review_items(T.load_tasks(client, cfg.remote_root), statuses)
        if not items:
            print(f"No review items (status '{args.status}').")
            return 0
        if args.dry_run:
            for it in items:
                print(f"  {it['date']}  {it['entity']} · {it['version']}  "
                      f"[{it['status']}]")
            print(f"(dry-run) would export {len(items)} clip(s) to "
                  f"{R.review_dir_rel(date_str)}/")
            return 0
        res = R.write_review_folder(
            client, remote_root=cfg.remote_root, local_root=cfg.resolved_local_root(),
            items=items, date_str=date_str, username=creds.user, log=print)
    print(f"\nExported -> {cfg.remote_root}/{res['folder_rel']}\n"
          f"  local: {res['folder_local']}\n"
          f"  {res['count']} clip(s); open index.html to scrub the batch.")
    if getattr(args, "open", False):
        from . import clipboard
        clipboard.reveal(res["folder_local"])
    return 0


def cmd_review_status(args) -> int:
    """Set the review status of a task's daily (approved completes the task)."""
    from . import tasks as T
    from . import review as R

    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    with SFTPClient(creds) as client:
        task = T.get_task(client, cfg.remote_root, args.task)
        if not task:
            print(f"error: task not found: {args.task}", file=sys.stderr)
            return 1
        # Match the clip by filename substring against the task's turntables.
        match = None
        for rec in task.get("publishes") or []:
            tt = rec.get("turntable")
            if tt and (not args.clip or args.clip in os.path.basename(tt)):
                match = tt
        if not match:
            print(f"error: no turntable matching '{args.clip}' on {args.task}",
                  file=sys.stderr)
            return 1
        if args.dry_run:
            print(f"(dry-run) would set {os.path.basename(match)} -> {args.status}")
            return 0
        R.set_review_status(client, cfg.remote_root, args.task, match,
                            args.status, creds.user)
    print(f"{os.path.basename(match)} -> {args.status}"
          + ("  (task -> done)" if args.status == "approved" else ""))
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
    pb.add_argument("--texture", action="append", default=[],
                    help="texture file to publish under publish/textures/ "
                         "(repeatable); for surface looks")
    pb.set_defaults(func=cmd_publish)

    fp = sub.add_parser("fetch-publish", parents=[common],
                        help="download a task's newest published file (e.g. the "
                             "model to shade)")
    fp.add_argument("--task", required=True, help="task id to fetch from")
    fp.add_argument("--step", help="fetch from the sibling task at this step "
                                   "instead (same entity), e.g. 'model'")
    fp.add_argument("--ext", default=".blend", help="file type (default: .blend)")
    fp.add_argument("--into", help="local folder to download into "
                                   "(default: local mirror of the publish folder)")
    fp.set_defaults(func=cmd_fetch_publish)

    ll = sub.add_parser("list-looks", parents=[common],
                        help="list a surface task's published looks (JSON)")
    ll.add_argument("--task", required=True, help="surface task id")
    ll.set_defaults(func=cmd_list_looks)

    fl = sub.add_parser("fetch-look", parents=[common],
                        help="download a published look (.blend + manifest + textures)")
    fl.add_argument("--task", required=True, help="surface task id")
    fl.add_argument("--look", required=True, help="look name to fetch")
    fl.set_defaults(func=cmd_fetch_look)

    lr = sub.add_parser("look-review", parents=[common],
                        help="render a shaded look turntable + texture/UV sheet")
    lr.add_argument("--task", required=True, help="surface task id")
    lr.add_argument("--look", required=True, help="look name")
    lr.add_argument("--version", type=int, help="look version (default: latest)")
    lr.add_argument("--hdri", help="HDRI name under 05_library/hdri (default: "
                                   "project default, else neutral)")
    lr.add_argument("--sheet-only", action="store_true",
                    help="regenerate just the texture/UV sheet (skip the turntable)")
    lr.set_defaults(func=cmd_look_review)

    nv = sub.add_parser("next-version", parents=[common],
                        help="print the next publish version for a task")
    nv.add_argument("--task", required=True, help="task id")
    nv.add_argument("--base", help="version this base name instead of the task "
                                   "default (e.g. a named surface look)")
    nv.set_defaults(func=cmd_next_version)

    tt = sub.add_parser("turntable", parents=[common],
                        help="render a model turntable and publish it to 07_dailies")
    tt.add_argument("--model", required=True, help="published model .blend to render")
    tt.add_argument("--task", required=True, help="task id")
    tt.add_argument("--preview", action="store_true",
                    help="open Blender interactively to preview framing (no render)")
    tt.set_defaults(func=cmd_turntable)

    br = sub.add_parser("build-review", parents=[common],
                        help="export review items to a dated folder + index.html")
    br.add_argument("--date", default="",
                    help="export folder date (default: today, YYYY-MM-DD)")
    br.add_argument("--status", default="all",
                    choices=["all", "to_review", "reviewed", "approved"],
                    help="which review items to export (default: all)")
    br.add_argument("--open", action="store_true",
                    help="reveal the export folder when done")
    br.set_defaults(func=cmd_build_review)

    rs = sub.add_parser("review-status", parents=[common],
                        help="set a daily's review status (approved completes the task)")
    rs.add_argument("--task", required=True, help="task id")
    rs.add_argument("--clip", default="",
                    help="match the turntable by filename substring (default: latest)")
    rs.add_argument("--status", required=True,
                    choices=["to_review", "reviewed", "approved"])
    rs.set_defaults(func=cmd_review_status)

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
