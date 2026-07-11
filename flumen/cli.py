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


def cmd_notify_test(args) -> int:
    """Validate the dailies-notification config end-to-end: load
    notifications.json from the server and send a test to every configured
    channel (email and/or Discord)."""
    from . import notify
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    with SFTPClient(creds) as client:
        nc = notify.load_notify_config(client, cfg.remote_root) or {}
    mail = nc.get("dailies_email") or {}
    disc = nc.get("dailies_discord") or {}
    if not (mail or disc):
        print(f"error: no dailies_email/dailies_discord block in "
              f"{cfg.remote_root}/{notify.NOTIFY_FILE_REL}", file=sys.stderr)
        return 1

    results = []
    if mail:
        if not mail.get("recipients"):
            print("email: SKIP (recipients empty)")
        else:
            if not mail.get("enabled"):
                print("note: dailies_email.enabled is false — testing anyway.")
            body = (f"This is a test from `flumen notify-test`.\n\n"
                    f"Project root: {cfg.remote_root}\n"
                    f"Recipients: {', '.join(mail['recipients'])}\n\n— Flumen")
            ok = notify.send_email(mail.get("smtp") or {},
                                   list(mail["recipients"]),
                                   "[Flumen] Test notification", body)
            print("email: OK" if ok else "email: FAILED — check smtp "
                  "host/port/user/password in notifications.json.")
            results.append(ok)
    if disc:
        if not disc.get("webhook"):
            print("discord: SKIP (webhook empty)")
        else:
            if not disc.get("enabled"):
                print("note: dailies_discord.enabled is false — testing anyway.")
            ok = notify.send_discord(disc["webhook"], {
                "username": "Flumen Dailies",
                "embeds": [{"title": "Test notification",
                            "description": "This is a test from "
                                           "`flumen notify-test`.\n\n— Flumen",
                            "color": 9109504}]})
            print("discord: OK" if ok else "discord: FAILED — check the webhook "
                  "URL in notifications.json.")
            results.append(ok)
    if not results:
        print("error: nothing to test (no recipients / webhook).", file=sys.stderr)
        return 1
    return 0 if all(results) else 1


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
    cfg_dir = os.path.dirname(os.path.abspath(args.config))
    schema_local = os.path.join(cfg_dir, "folder_schema.yaml")
    if os.path.isfile(schema_local):
        uploads.append((schema_local, base + "folder_schema.yaml"))
    # Studio policy files maintained beside the toolkit: project settings and
    # the Blender menu config. Published together so one command syncs policy.
    for name in ("project_settings.json", "menu.json"):
        local = os.path.join(cfg_dir, "pipeline_config", name)
        if os.path.isfile(local):
            uploads.append((local, base + name))

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
    from . import progress as P
    import time as _time
    creds = SFTPCredentials.from_env(args.env)

    # Emit throttled progress lines the Blender add-on parses to drive a bar.
    start = _time.monotonic()
    last = [0.0, -1]   # (last emit time, last percent) — throttle the stream
    def _emit(done, total, name):
        now = _time.monotonic()
        pct = P.percent(done, total)
        if pct == last[1] and now - last[0] < 0.25:
            return
        last[0], last[1] = now, pct
        print(P.format_line(done, total, now - start, f"uploading {name}"), flush=True)

    with SFTPClient(creds) as client:
        rels = T.publish_task(client, cfg.remote_root, creds.user,
                              args.local, args.task, args.status,
                              description=args.description,
                              texture_files=args.texture, progress=_emit)
    print(P.format_line(1, 1, _time.monotonic() - start, "done"), flush=True)
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


def cmd_list_dressings(args) -> int:
    """Print the named set-dressings an environment's dressing task has published,
    as JSON. Feeds the shot-breakdown / Blender dropdowns."""
    import json
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import tasks as T
    with SFTPClient(creds) as client:
        task = T.get_task(client, cfg.remote_root, args.task)
        if not task:
            print(f"error: task not found: {args.task}", file=sys.stderr)
            return 1
        dressings = T.published_dressings(task)
    print(json.dumps(dressings))
    return 0


def cmd_list_asset_publishes(args) -> int:
    """Print every asset that has a published .blend at a step (default: model),
    as JSON [{entity, step, blend_rel}]. Feeds the Blender 'Add prop' dropdown."""
    import json
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import tasks as T
    out = []
    with SFTPClient(creds) as client:
        for task in T.load_tasks(client, cfg.remote_root):
            if task.get("type") != "asset" or task.get("step") != args.step:
                continue
            pubs = [p for p in T.published_files(task)
                    if p["name"].endswith(".blend")]
            if pubs:
                out.append({"entity": task.get("entity", ""), "step": args.step,
                            "blend_rel": pubs[0]["rel"]})
    out.sort(key=lambda r: r["entity"])
    print(json.dumps(out))
    return 0


def cmd_review_still(args) -> int:
    """Upload a rendered review still into 07_dailies/<entity>/<step>/ and fire
    the dailies notification. Used by the Blender 'Render review' tool."""
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import tasks as T, turntable as TT
    if not os.path.isfile(args.file):
        print(f"error: file not found: {args.file}", file=sys.stderr)
        return 1
    rr = cfg.remote_root.rstrip("/")
    with SFTPClient(creds) as client:
        task = T.get_task(client, rr, args.task)
        if not task:
            print(f"error: task not found: {args.task}", file=sys.stderr)
            return 1
        rel = (f"07_dailies/{task['entity']}/{task['step']}/"
               f"{os.path.basename(args.file)}")
        client.upload(args.file, rr + "/" + rel)
        # Registers the still on the task so the Dailies tab lists it, plus
        # ledger attribution + the email/Discord notification.
        TT.record_still(client, rr, args.task, rel, creds.user)
    print(f"review still -> {rr}/{rel}")
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


def cmd_playblast(args) -> int:
    """Render a shot playblast (its frame range through the shot camera) and publish
    it to 07_dailies, attached to the publish record. Used after a shot publish."""
    cfg = ProjectConfig.load(args.config)
    if not args.dry_run and not os.path.isfile(args.shot_file):
        print(f"error: shot file not found: {args.shot_file}", file=sys.stderr)
        return 1
    from . import playblast
    creds = (SFTPCredentials(host="(dry-run)", port=22, user="(dry-run)")
             if args.dry_run else SFTPCredentials.from_env(args.env))
    return playblast.run_playblast(cfg, creds, args.shot_file, args.task,
                                   dry_run=args.dry_run)


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
              f"Run 'flumen build-review' (or sync it down) first.")
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


def cmd_assembly_list(args) -> int:
    """Print a shot's element breakdown as JSON."""
    import json
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import elements as E
    with SFTPClient(creds) as client:
        asm = E.load_assembly(client, cfg.remote_root, args.shot)
    print(json.dumps(asm["elements"], indent=2))
    return 0


def cmd_assembly_add(args) -> int:
    """Add an element (an asset, or the shot's own camera) to a shot's breakdown."""
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import elements as E
    kind = "camera" if args.camera else "asset"
    if kind == "asset" and not args.asset:
        print("error: --asset required (or use --camera)", file=sys.stderr)
        return 1
    el = E.new_element(args.asset or "", kind, args.label or "", args.look or "",
                       dressing=getattr(args, "dressing", "") or "")
    with SFTPClient(creds) as client:
        asm = E.load_assembly(client, cfg.remote_root, args.shot)
        E.add_element(asm, el)
        E.save_assembly(client, cfg.remote_root, args.shot, asm, actor=creds.user)
    print(f"added {kind} to {args.shot}: {asm['elements'][-1]['id']}")
    return 0


def cmd_assembly_remove(args) -> int:
    """Remove an element from a shot's breakdown by its id."""
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import elements as E
    with SFTPClient(creds) as client:
        asm = E.load_assembly(client, cfg.remote_root, args.shot)
        before = len(asm["elements"])
        E.remove_element(asm, args.id)
        if len(asm["elements"]) == before:
            print(f"error: no element '{args.id}' in {args.shot}", file=sys.stderr)
            return 1
        E.save_assembly(client, cfg.remote_root, args.shot, asm, actor=creds.user)
    print(f"removed {args.id} from {args.shot}")
    return 0


def cmd_resolve_assembly(args) -> int:
    """Resolve a shot's elements for a step, download each element's rig/model
    publish into the local mirror, and print JSON for the Blender add-on to link.
    The shot is given by --task (a shot task id) or --shot + --step."""
    import json
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import tasks as T, elements as E, turntable
    rr = cfg.remote_root.rstrip("/")
    local_root = cfg.resolved_local_root() or os.getcwd()
    settings = turntable._load_project_settings(local_root)

    with SFTPClient(creds) as client:
        if args.task:
            t = T.get_task(client, rr, args.task)
            if not t or t.get("type") != "shot":
                print(f"error: not a shot task: {args.task}", file=sys.stderr)
                return 1
            shot_entity, step = t["entity"], (args.step or t["step"])
        elif args.shot and args.step:
            shot_entity, step = args.shot, args.step
        else:
            print("error: give --task, or both --shot and --step", file=sys.stderr)
            return 1
        picks = {}
        for p in args.pick or []:                    # "id=step" overrides
            if "=" in p:
                k, v = p.split("=", 1)
                picks[k] = v
        fs, fe = E.frame_range(E.load_assembly(client, rr, shot_entity))
        resolved = E.resolved_elements(client, rr, shot_entity, step, settings,
                                       picks=picks)
        only = set(args.only or [])
        out = []
        for r in resolved:
            if only and r["id"] not in only:         # --only: just these elements
                continue
            rel = r.get("blend_rel") or ""
            local = ""
            # --list previews the breakdown without downloading anything; otherwise
            # fetch each chosen element's publish into the local mirror.
            if rel and not args.list:
                local = os.path.join(local_root, *rel.split("/"))
                client.download(rr + "/" + rel, local)
            collection = "" if r["kind"] == "camera" else r["asset"].split("/")[-1]
            entry = {"id": r["id"], "label": r["label"], "kind": r["kind"],
                     "asset": r["asset"], "blend_local": local,
                     "source_step": r["source_step"], "collection": collection,
                     "available_steps": r.get("available_steps", []),
                     "look": r.get("look", ""), "load": r.get("load", "link"),
                     "apply_look": r.get("apply_look", False),
                     "camera_name": r.get("camera_name", "")}
            # Environment element with a named set-dressing: resolve the newest
            # published version of that name, inline its manifest and fetch each
            # prop's publish so Build shot only has to link + place.
            if r.get("dressing"):
                d = E.newest_dressing(client, rr, r["asset"], r["dressing"])
                if not d:
                    entry["dressing_error"] = (f"no published dressing "
                                               f"'{r['dressing']}' on {r['asset']}")
                elif args.list:
                    entry["dressing"] = {"name": r["dressing"],
                                         "version": d["version"]}
                else:
                    txt = client.read_text(rr + "/" + d["manifest_rel"])
                    try:
                        manifest = json.loads(txt) if txt else {}
                    except ValueError:
                        manifest = {}
                    props, fetched = [], {}
                    for p in manifest.get("props") or []:
                        brel = p.get("blend_rel") or ""
                        if not brel:
                            continue
                        if brel not in fetched:
                            lp = os.path.join(local_root, *brel.split("/"))
                            client.download(rr + "/" + brel, lp)
                            fetched[brel] = lp
                        props.append({"id": p.get("id", ""),
                                      "asset": p.get("asset", ""),
                                      "blend_local": fetched[brel],
                                      "collection": p.get("collection", ""),
                                      "object": p.get("object", ""),
                                      "matrix_world": p.get("matrix_world")})
                    entry["dressing"] = {"name": r["dressing"],
                                         "version": d["version"], "props": props}
            out.append(entry)

        # The shot's published animation (Actions) — Build shot re-applies it onto the
        # freshly-linked rigs. Resolved PER ELEMENT to its newest version, so each
        # element's blend may differ. Skipped for --list (the dialog doesn't need it).
        anim = {}
        if not args.list:
            ra = E.resolved_animation(client, rr, shot_entity, step)
            if ra:
                elems, blends = {}, {}
                for eid, info in ra["elements"].items():
                    brel = info["blend_rel"]
                    if brel not in blends:
                        local = os.path.join(local_root, *brel.split("/"))
                        client.download(rr + "/" + brel, local)
                        blends[brel] = local
                    elems[eid] = {"blend_local": blends[brel],
                                  "objects": info["objects"],
                                  "version": info.get("version", "")}
                if elems:
                    anim = {"elements": elems}
    result = {"frame_start": fs, "frame_end": fe, "elements": out}
    if anim:
        result["anim"] = anim
    print(json.dumps(result))
    return 0


def cmd_list_animations(args) -> int:
    """List the shot step's published animations (newest first) and download each
    _anim.blend locally, printing JSON for the 'Load animation' picker so the artist
    can choose a published version per element."""
    import json
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import tasks as T, elements as E
    rr = cfg.remote_root.rstrip("/")
    local_root = cfg.resolved_local_root() or os.getcwd()
    with SFTPClient(creds) as client:
        if args.task:
            t = T.get_task(client, rr, args.task)
            if not t or t.get("type") != "shot":
                print(f"error: not a shot task: {args.task}", file=sys.stderr)
                return 1
            shot_entity, step = t["entity"], (args.step or t["step"])
        elif args.shot and args.step:
            shot_entity, step = args.shot, args.step
        else:
            print("error: give --task, or both --shot and --step", file=sys.stderr)
            return 1
        anims = E.published_animations(client, rr, shot_entity, step)
        if not args.no_fetch:                    # --no-fetch: metadata + hashes only
            for a in anims:
                local = os.path.join(local_root, *a["blend_rel"].split("/"))
                client.download(rr + "/" + a["blend_rel"], local)
                a["blend_local"] = local
    print(json.dumps(anims))
    return 0


def cmd_assembly_set_range(args) -> int:
    """Set a shot's duration (and optionally its start frame). Default start 1001."""
    cfg = ProjectConfig.load(args.config)
    creds = SFTPCredentials.from_env(args.env)
    from . import elements as E
    with SFTPClient(creds) as client:
        asm = E.load_assembly(client, cfg.remote_root, args.shot)
        if args.start:
            asm["frame_start"] = args.start
        asm["duration"] = args.duration
        asm = E.save_assembly(client, cfg.remote_root, args.shot, asm,
                              actor=creds.user)
    fs, fe = E.frame_range(asm)
    print(f"{args.shot}: frames {fs}-{fe} ({asm['duration']} frame(s))")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # Common flags live on a parent parser so they work either before OR after
    # the subcommand (e.g. both `flumen --dry-run init-project` and
    # `flumen init-project --dry-run`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default="config.yaml",
                        help="project config YAML")
    common.add_argument("--env", default=".env", help="path to .env file")
    common.add_argument("--dry-run", action="store_true",
                        help="preview without touching the server")

    p = argparse.ArgumentParser(
        prog="flumen",
        parents=[common],
        description="SFTP folder-structure publisher for a 3D animation pipeline.",
    )
    from .version import get_version
    p.add_argument("--version", action="version", version=f"flumen {get_version()}")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("test-connection", parents=[common],
                   help="verify SFTP credentials") \
        .set_defaults(func=cmd_test_connection)

    nt = sub.add_parser("notify-test", parents=[common],
                        help="send a test dailies-notification email")
    nt.set_defaults(func=cmd_notify_test)

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

    rs = sub.add_parser("review-still", parents=[common],
                        help="upload a review still to 07_dailies + notify")
    rs.add_argument("--task", required=True, help="task id")
    rs.add_argument("--file", required=True, help="rendered image (local path)")
    rs.set_defaults(func=cmd_review_still)

    ld = sub.add_parser("list-dressings", parents=[common],
                        help="list an environment's published set-dressings (JSON)")
    ld.add_argument("--task", required=True, help="dressing task id")
    ld.set_defaults(func=cmd_list_dressings)

    lap = sub.add_parser("list-asset-publishes", parents=[common],
                         help="list assets with a published .blend at a step (JSON)")
    lap.add_argument("--step", default="model", help="step to look at (default: model)")
    lap.set_defaults(func=cmd_list_asset_publishes)

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

    pbl = sub.add_parser("playblast", parents=[common],
                         help="render a shot playblast and publish it to 07_dailies")
    pbl.add_argument("--shot-file", required=True, dest="shot_file",
                     help="published shot .blend to playblast")
    pbl.add_argument("--task", required=True, help="shot task id")
    pbl.set_defaults(func=cmd_playblast)

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

    asm = sub.add_parser("assembly", parents=[common],
                         help="manage a shot's element breakdown")
    asm_sub = asm.add_subparsers(dest="assembly_cmd", required=True)

    al = asm_sub.add_parser("list", parents=[common],
                            help="print a shot's elements (JSON)")
    al.add_argument("--shot", required=True, help="shot entity, e.g. SEQ010/SH0010")
    al.set_defaults(func=cmd_assembly_list)

    aa = asm_sub.add_parser("add", parents=[common],
                            help="add an element to a shot")
    aa.add_argument("--shot", required=True, help="shot entity, e.g. SEQ010/SH0010")
    aa.add_argument("--asset", help="asset entity, e.g. characters/frankenstein")
    aa.add_argument("--camera", action="store_true",
                    help="add the shot's own camera as an element")
    aa.add_argument("--label", default="", help="display label (default: asset name)")
    aa.add_argument("--look", default="",
                    help="surface look name to apply downstream (lighting)")
    aa.add_argument("--dressing", default="",
                    help="published set-dressing to load with an environment")
    aa.set_defaults(func=cmd_assembly_add)

    ar = asm_sub.add_parser("remove", parents=[common],
                            help="remove an element from a shot by id")
    ar.add_argument("--shot", required=True, help="shot entity")
    ar.add_argument("--id", required=True, help="element id (see 'assembly list')")
    ar.set_defaults(func=cmd_assembly_remove)

    asr = asm_sub.add_parser("set-range", parents=[common],
                             help="set a shot's frame duration (start defaults 1001)")
    asr.add_argument("--shot", required=True, help="shot entity")
    asr.add_argument("--duration", type=int, required=True,
                     help="shot length in frames (e.g. 100)")
    asr.add_argument("--start", type=int, default=0,
                     help="start frame (default: keep current / 1001)")
    asr.set_defaults(func=cmd_assembly_set_range)

    ra = sub.add_parser("resolve-assembly", parents=[common],
                        help="resolve a shot's elements + download their publishes "
                             "(JSON for the Blender add-on)")
    ra.add_argument("--task", help="shot task id (entity + step)")
    ra.add_argument("--shot", help="shot entity (if not using --task)")
    ra.add_argument("--step", help="shot step (layout/animation/...); "
                                   "default: the task's step")
    ra.add_argument("--list", action="store_true",
                    help="resolve + preview the breakdown without downloading")
    ra.add_argument("--only", action="append", default=[],
                    help="resolve/fetch only this element id (repeatable)")
    ra.add_argument("--pick", action="append", default=[],
                    help="override an element's step as id=step (repeatable)")
    ra.set_defaults(func=cmd_resolve_assembly)

    lan = sub.add_parser("list-animations", parents=[common],
                         help="list a shot's published animations (+ fetch them) for "
                              "the Load-animation picker")
    lan.add_argument("--task", help="shot task id (entity + step)")
    lan.add_argument("--shot", help="shot entity (if not using --task)")
    lan.add_argument("--step", help="shot step; default: the task's step")
    lan.add_argument("--no-fetch", action="store_true",
                     help="metadata + hashes only, don't download the anim blends")
    lan.set_defaults(func=cmd_list_animations)

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
