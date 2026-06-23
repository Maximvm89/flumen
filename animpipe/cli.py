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
        client.put(args.local, remote)
    print("done." if not args.dry_run else "(dry-run: nothing uploaded)")
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
