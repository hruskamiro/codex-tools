"""Inspect and message local Codex agent sessions."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from codex_tools import agents_tui, codex_daemon, paths, relay_cache, search


@dataclass(frozen=True)
class AgentExchange:
    user: dict[str, Any]
    assistant: list[dict[str, Any]]


def add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=search.DEFAULT_SESSIONS_ROOT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--session-index",
        type=Path,
        default=search.DEFAULT_SESSION_INDEX,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--control-socket",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-tools agents",
        description="Inspect and message currently loaded Codex agent sessions.",
    )
    commands = parser.add_subparsers(dest="agents_command")

    list_cmd = commands.add_parser("list", help="List currently loaded local agents.")
    list_cmd.add_argument("--limit", type=int, default=20)
    list_cmd.add_argument("--json", action="store_true")
    location = list_cmd.add_mutually_exclusive_group()
    location.add_argument(
        "--here",
        action="store_true",
        help="Only include agents based in the current directory.",
    )
    location.add_argument(
        "--work-dir",
        type=Path,
        help="Only include agents based in this directory.",
    )
    add_source_args(list_cmd)

    show = commands.add_parser("show", help="Show an agent's latest exchanges.")
    show.add_argument("agent", help="Exact name or unique session-ID prefix.")
    show.add_argument("--turns", type=int, default=1)
    show.add_argument("--json", action="store_true")
    add_source_args(show)

    send = commands.add_parser("send", help="Queue a message for an agent.")
    send.add_argument("agent", help="Exact name or unique session-ID prefix.")
    send.add_argument("message", nargs=argparse.REMAINDER)
    send.add_argument("--codex-bin", default="codex", help=argparse.SUPPRESS)
    add_source_args(send)

    ui = commands.add_parser(
        "ui",
        help="Open the terminal Agent Console.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""controls:
  type immediately         edit the selected agent's draft
  Enter / Ctrl-J           send / insert a newline
  Shift-Left / Right       select agents / enter the composer
  Ctrl-P / Ctrl-N          move up / down within the draft
  Ctrl-Left / Ctrl-Right   move by word
  Page Up / Page Down      scroll the conversation
  Ctrl-R / Ctrl-S          refresh selected / refresh everything
  Ctrl-F / Ctrl-V          find an agent / open in the viewer
  Ctrl-X                   maximize or restore the conversation
  Ctrl-C twice             quit""",
    )
    ui.add_argument(
        "--no-refresh",
        action="store_true",
        help="Do not refresh local sessions before opening.",
    )
    ui.add_argument(
        "--no-local",
        action="store_true",
        help="Show only synchronized agents from remote relay hosts.",
    )
    ui.add_argument(
        "--cache-file", type=Path, default=relay_cache.DEFAULT_CACHE_PATH,
        help=argparse.SUPPRESS,
    )
    ui.add_argument(
        "--relay-config",
        type=Path,
        default=paths.RELAY_CONFIG_FILE,
        help=argparse.SUPPRESS,
    )
    ui.add_argument("--codex-bin", default="codex", help=argparse.SUPPRESS)
    add_source_args(ui)

    args = parser.parse_args(argv)
    if args.agents_command is None:
        args = parser.parse_args(["list", *(argv or [])])
    return args


def agent_summaries(
    sessions_root: Path,
    session_index: Path,
    control_socket: Path | None = None,
) -> list[dict[str, Any]]:
    del sessions_root, session_index
    socket_path = control_socket or codex_daemon.default_control_socket()
    if not socket_path.expanduser().exists():
        return []
    return [
        thread_summary(thread)
        for thread in codex_daemon.live_threads(socket_path)
        if visible_thread(thread)
    ]


def visible_thread(thread: dict[str, Any]) -> bool:
    """Exclude empty startup threads until they have a name or conversation."""
    return bool(str(thread.get("name") or "").strip()) or bool(
        str(thread.get("preview") or "").strip()
    )


def thread_summary(thread: dict[str, Any]) -> dict[str, Any]:
    thread_id = str(thread.get("id") or "")
    status = thread.get("status") if isinstance(thread.get("status"), dict) else {}
    status_type = str(status.get("type") or "unknown")
    flags = [str(value) for value in status.get("activeFlags", [])]
    if status_type == "active" and flags:
        status_label = "needs-you"
    elif status_type == "active":
        status_label = "working"
    elif status_type == "idle":
        status_label = "ready"
    else:
        status_label = status_type
    updated = int(thread.get("updatedAt") or 0)
    updated_at = datetime.fromtimestamp(updated).astimezone() if updated else None
    preview = str(thread.get("preview") or "").strip().splitlines()
    title = str(thread.get("name") or (preview[0] if preview else thread_id))
    return {
        "id": thread_id,
        "threadId": thread_id,
        "sessionId": str(thread.get("sessionId") or thread_id),
        "rolloutId": thread_id,
        "title": title,
        "titleSource": "daemon",
        "path": str(thread.get("path") or ""),
        "cwd": str(thread.get("cwd") or ""),
        "source": str(thread.get("source") or ""),
        "createdAt": str(thread.get("createdAt") or ""),
        "lastAt": updated_at.isoformat() if updated_at else "",
        "lastLabel": updated_at.strftime("%Y-%m-%d %H:%M") if updated_at else "",
        "status": status_type,
        "statusFlags": flags,
        "statusLabel": status_label,
        "model": thread.get("model"),
        "reasoningEffort": thread.get("reasoningEffort"),
    }


def agent_identifier(agent: dict[str, Any]) -> str:
    return str(agent.get("threadId") or agent.get("sessionId") or agent.get("id") or "")


def resolve_agent(agents: list[dict[str, Any]], selector: str) -> dict[str, Any]:
    exact = [
        agent
        for agent in agents
        if selector
        in {
            str(agent.get("title") or ""),
            str(agent.get("id") or ""),
            str(agent.get("threadId") or ""),
            str(agent.get("sessionId") or ""),
            str(agent.get("rolloutId") or ""),
        }
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError(f"agent name is ambiguous: {selector}")
    prefix = [
        agent
        for agent in agents
        if any(
            str(agent.get(key) or "").startswith(selector)
            for key in ("id", "threadId", "sessionId", "rolloutId")
        )
    ]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise ValueError(f"agent ID prefix is ambiguous: {selector}")
    raise FileNotFoundError(f"agent not found: {selector}")


def message_payload(record: search.TextRecord) -> dict[str, Any]:
    return {
        "timestamp": record.timestamp,
        "role": record.role,
        "line": record.line_no,
        "text": record.text,
    }


def recent_exchanges(
    records: list[search.TextRecord], limit: int
) -> list[AgentExchange]:
    exchanges: list[AgentExchange] = []
    current_user: search.TextRecord | None = None
    assistant: list[dict[str, Any]] = []
    for record in records:
        if record.role == "user" and search.is_internal_prompt_text(record.text):
            continue
        if record.role == "user":
            if current_user is not None:
                exchanges.append(
                    AgentExchange(message_payload(current_user), assistant)
                )
            current_user = record
            assistant = []
        elif record.role == "assistant" and current_user is not None:
            assistant.append(message_payload(record))
    if current_user is not None:
        exchanges.append(AgentExchange(message_payload(current_user), assistant))
    return exchanges[-max(0, limit) :] if limit > 0 else []


def shortened(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def display_name(agent: dict[str, Any]) -> str:
    return str(agent.get("title") or agent_identifier(agent) or "unnamed")


def command_list(args: argparse.Namespace) -> int:
    if args.limit < 1:
        print("error: --limit must be positive", file=sys.stderr)
        return 2
    agents = agent_summaries(
        args.sessions_root, args.session_index, getattr(args, "control_socket", None)
    )
    work_dir = search.selected_work_dir(args)
    if work_dir is not None:
        agents = [
            agent
            for agent in agents
            if agent.get("cwd")
            and search.canonical_work_dir(Path(str(agent["cwd"]))) == work_dir
        ]
    agents = agents[: args.limit]
    if args.json:
        print(json.dumps(agents, ensure_ascii=False, indent=2))
        return 0
    if not agents:
        print("No local agents found.")
        return 0
    print(f"{'AGENT':34} {'STATUS':11} {'SESSION':10} {'UPDATED':16} BASEDIR")
    for agent in agents:
        identifier = agent_identifier(agent)
        print(
            f"{shortened(display_name(agent), 34):34} "
            f"{str(agent.get('statusLabel') or 'unknown')[:11]:11} "
            f"{identifier[:8]:10} "
            f"{str(agent.get('lastLabel') or '')[:16]:16} "
            f"{agent.get('cwd') or '-'}"
        )
    return 0


def load_agent_session(
    agent: dict[str, Any], session_index: Path
) -> search.Session:
    session = search.read_session(
        Path(str(agent["path"])), include_tools=False, include_system=False
    )
    search.resolve_session_title(
        session, search.load_session_index_titles(session_index.expanduser())
    )
    return session


def command_show(args: argparse.Namespace) -> int:
    if args.turns < 1:
        print("error: --turns must be positive", file=sys.stderr)
        return 2
    agent = resolve_agent(
        agent_summaries(
            args.sessions_root,
            args.session_index,
            getattr(args, "control_socket", None),
        ), args.agent
    )
    session = load_agent_session(agent, args.session_index)
    exchanges = recent_exchanges(session.records, args.turns)
    payload = {
        "agent": display_name(agent),
        "session_id": agent_identifier(agent),
        "cwd": agent.get("cwd") or "",
        "source": agent.get("source") or "",
        "updated_at": agent.get("lastAt") or "",
        "exchanges": [asdict(exchange) for exchange in exchanges],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"Agent:   {payload['agent']}")
    print(f"Session: {payload['session_id']}")
    print(f"Basedir: {payload['cwd'] or '-'}")
    print(f"Updated: {payload['updated_at'] or '-'}")
    for exchange in exchanges:
        print()
        print("User:")
        print(exchange.user["text"])
        for answer in exchange.assistant:
            print()
            print("Assistant:")
            print(answer["text"])
    return 0


def command_send(args: argparse.Namespace) -> int:
    message_parts = list(args.message)
    if message_parts[:1] == ["--"]:
        message_parts.pop(0)
    message = " ".join(message_parts).strip()
    if not message:
        print("error: a non-empty message is required", file=sys.stderr)
        return 2
    agent = resolve_agent(
        agent_summaries(
            args.sessions_root,
            args.session_index,
            getattr(args, "control_socket", None),
        ), args.agent
    )
    identifier = agent_identifier(agent)
    if not identifier:
        print("error: selected agent has no session ID", file=sys.stderr)
        return 1
    try:
        result = subprocess.run(
            [
                args.codex_bin,
                "queue",
                "--thread",
                identifier,
                "--message",
                message,
            ],
            check=False,
        )
    except OSError as exc:
        print(f"error: could not run {args.codex_bin}: {exc}", file=sys.stderr)
        return 1
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.agents_command == "list":
            return command_list(args)
        if args.agents_command == "show":
            return command_show(args)
        if args.agents_command == "send":
            return command_send(args)
        if args.agents_command == "ui":
            return agents_tui.command_ui(args)
        raise AssertionError(f"unhandled agents command: {args.agents_command}")
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
