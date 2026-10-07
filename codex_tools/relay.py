"""Relay cache and, later, machine-to-machine synchronization commands."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from codex_tools import relay_cache, search


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-tools relay",
        description="Manage resilient Codex agent synchronization.",
    )
    commands = parser.add_subparsers(dest="relay_command", required=True)
    cache = commands.add_parser("cache", help="Inspect or refresh the local relay cache.")
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)

    refresh = cache_commands.add_parser(
        "refresh", help="Refresh the local-machine cache from Codex sessions."
    )
    refresh.add_argument(
        "--sessions-root", type=Path, default=search.DEFAULT_SESSIONS_ROOT,
        help=argparse.SUPPRESS,
    )
    refresh.add_argument(
        "--session-index", type=Path, default=search.DEFAULT_SESSION_INDEX,
        help=argparse.SUPPRESS,
    )
    refresh.add_argument(
        "--cache-file", type=Path, default=relay_cache.DEFAULT_CACHE_PATH,
        help=argparse.SUPPRESS,
    )

    status = cache_commands.add_parser("status", help="Show cached hosts and counts.")
    status.add_argument("--json", action="store_true")
    status.add_argument(
        "--cache-file", type=Path, default=relay_cache.DEFAULT_CACHE_PATH,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def command_refresh(args: argparse.Namespace) -> int:
    cache = relay_cache.RelayCache(args.cache_file)
    count = relay_cache.refresh_local(
        cache, args.sessions_root, args.session_index
    )
    print(f"Cached {count} local agent session(s) in {cache.path}")
    return 0


def command_status(args: argparse.Namespace) -> int:
    cache = relay_cache.RelayCache(args.cache_file)
    hosts = cache.hosts()
    if args.json:
        print(
            json.dumps(
                {"cache": str(cache.path), "schema": relay_cache.SCHEMA_VERSION,
                 "hosts": hosts},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"Relay cache: {cache.path}")
    print(f"Schema: {relay_cache.SCHEMA_VERSION}")
    if not hosts:
        print("No cached hosts.")
        return 0
    print()
    print(f"{'HOST':20} {'AGENTS':>8} {'PENDING':>8} LAST SYNC")
    for host in hosts:
        print(
            f"{str(host['name'])[:20]:20} {int(host['agent_count']):>8} "
            f"{int(host['pending_count']):>8} {host['last_sync_at'] or '-'}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.relay_command == "cache" and args.cache_command == "refresh":
            return command_refresh(args)
        if args.relay_command == "cache" and args.cache_command == "status":
            return command_status(args)
        raise AssertionError("unhandled relay command")
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
