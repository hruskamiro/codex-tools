"""Relay cache and, later, machine-to-machine synchronization commands."""

from __future__ import annotations

import argparse
import getpass
import json
import socket
import sqlite3
import sys
from pathlib import Path

from codex_tools import paths, relay_cache, relay_transport, search


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-tools relay",
        description="Manage resilient Codex agent synchronization.",
    )
    commands = parser.add_subparsers(dest="relay_command", required=True)

    initialize = commands.add_parser(
        "init", help="Create this machine's relay identity."
    )
    initialize.add_argument("--name", default=socket.gethostname())
    initialize.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    initialize.add_argument(
        "--config-file",
        type=Path,
        default=paths.RELAY_CONFIG_FILE,
        help=argparse.SUPPRESS,
    )

    token = commands.add_parser("token", help="Print this machine's pairing key.")
    token.add_argument(
        "--config-file",
        type=Path,
        default=paths.RELAY_CONFIG_FILE,
        help=argparse.SUPPRESS,
    )

    pair = commands.add_parser("pair", help="Register a relay peer.")
    pair.add_argument("name")
    pair.add_argument("url")
    pair.add_argument("--key-file", type=Path)
    pair.add_argument("--ca-file", type=Path)
    pair.add_argument("--allow-insecure-http", action="store_true")
    pair.add_argument(
        "--config-file",
        type=Path,
        default=paths.RELAY_CONFIG_FILE,
        help=argparse.SUPPRESS,
    )

    peers = commands.add_parser("peers", help="List configured relay peers.")
    peers.add_argument("--json", action="store_true")
    peers.add_argument(
        "--config-file",
        type=Path,
        default=paths.RELAY_CONFIG_FILE,
        help=argparse.SUPPRESS,
    )

    sync = commands.add_parser("sync", help="Pull a peer's current agent snapshot.")
    sync.add_argument("peer")
    sync.add_argument(
        "--config-file",
        type=Path,
        default=paths.RELAY_CONFIG_FILE,
        help=argparse.SUPPRESS,
    )
    sync.add_argument(
        "--cache-file",
        type=Path,
        default=relay_cache.DEFAULT_CACHE_PATH,
        help=argparse.SUPPRESS,
    )

    serve = commands.add_parser("serve", help="Serve this machine's relay snapshot.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8780)
    serve.add_argument("--tls-cert", type=Path)
    serve.add_argument("--tls-key", type=Path)
    serve.add_argument("--allow-insecure-http", action="store_true")
    serve.add_argument(
        "--config-file",
        type=Path,
        default=paths.RELAY_CONFIG_FILE,
        help=argparse.SUPPRESS,
    )
    serve.add_argument(
        "--cache-file",
        type=Path,
        default=relay_cache.DEFAULT_CACHE_PATH,
        help=argparse.SUPPRESS,
    )
    serve.add_argument(
        "--sessions-root",
        type=Path,
        default=search.DEFAULT_SESSIONS_ROOT,
        help=argparse.SUPPRESS,
    )
    serve.add_argument(
        "--session-index",
        type=Path,
        default=search.DEFAULT_SESSION_INDEX,
        help=argparse.SUPPRESS,
    )

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
                {
                    "cache": str(cache.path),
                    "schema": relay_cache.SCHEMA_VERSION,
                    "hosts": hosts,
                },
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


def command_init(args: argparse.Namespace) -> int:
    config = relay_transport.initialize_config(
        args.config_file, args.name, force=args.force
    )
    print(f"Relay identity: {config['identity']['name']} ({config['identity']['id']})")
    print(f"Configuration: {args.config_file.expanduser()}")
    print("Share the pairing key with `ct relay token` over a trusted channel.")
    return 0


def command_token(args: argparse.Namespace) -> int:
    print(relay_transport.load_config(args.config_file)["identity"]["token"])
    return 0


def command_pair(args: argparse.Namespace) -> int:
    if args.key_file:
        token = args.key_file.expanduser().read_text(encoding="utf-8").strip()
    else:
        token = getpass.getpass("Pairing key: ")
    relay_transport.add_peer(
        args.config_file,
        args.name,
        args.url,
        token,
        allow_insecure_http=args.allow_insecure_http,
        ca_file=args.ca_file,
    )
    print(f"Paired {args.name}: {args.url.rstrip('/')}")
    return 0


def command_peers(args: argparse.Namespace) -> int:
    peers = relay_transport.load_config(args.config_file)["peers"]
    public = [
        {
            "name": name,
            "url": peer.get("url", ""),
            "ca_file": peer.get("ca_file"),
            "allow_insecure_http": bool(peer.get("allow_insecure_http")),
        }
        for name, peer in sorted(peers.items())
        if isinstance(peer, dict)
    ]
    if args.json:
        print(json.dumps(public, ensure_ascii=False, indent=2))
        return 0
    if not public:
        print("No relay peers configured.")
        return 0
    print(f"{'PEER':20} {'TRANSPORT':10} URL")
    for peer in public:
        transport = "HTTP/VPN" if peer["allow_insecure_http"] else "HTTPS"
        peer_url = str(peer["url"])
        if relay_transport.is_loopback_url(peer_url):
            transport = "loopback"
        print(f"{peer['name'][:20]:20} {transport:10} {peer['url']}")
    return 0


def command_sync(args: argparse.Namespace) -> int:
    config = relay_transport.load_config(args.config_file)
    peer = config["peers"].get(args.peer)
    if not isinstance(peer, dict):
        raise FileNotFoundError(f"relay peer not found: {args.peer}")
    payload = relay_transport.fetch_snapshot(peer)
    count = relay_transport.apply_snapshot(
        relay_cache.RelayCache(args.cache_file), payload, base_url=str(peer["url"])
    )
    print(f"Synchronized {count} agent(s) from {payload['host']['name']}.")
    return 0


def command_serve(args: argparse.Namespace) -> int:
    config = relay_transport.load_config(args.config_file)
    cache = relay_cache.RelayCache(args.cache_file)
    relay_transport.validate_server_transport(
        args.host,
        args.port,
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
        allow_insecure_http=args.allow_insecure_http,
    )
    count = relay_cache.refresh_local(cache, args.sessions_root, args.session_index)
    scheme = "https" if args.tls_cert else "http"
    print(
        f"Serving {count} local agent(s) at {scheme}://{args.host}:{args.port}",
        flush=True,
    )
    relay_transport.serve(
        cache,
        config,
        args.host,
        args.port,
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
        allow_insecure_http=args.allow_insecure_http,
        refresh=lambda: relay_cache.refresh_local(
            cache, args.sessions_root, args.session_index
        ),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.relay_command == "init":
            return command_init(args)
        if args.relay_command == "token":
            return command_token(args)
        if args.relay_command == "pair":
            return command_pair(args)
        if args.relay_command == "peers":
            return command_peers(args)
        if args.relay_command == "sync":
            return command_sync(args)
        if args.relay_command == "serve":
            return command_serve(args)
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
