"""Unified command-line entry point for Codex tools."""

from __future__ import annotations

import sys

from codex_tools import aliases, config, manager, search, structured, summary, viewer


def help_text() -> str:
    return """\
usage: codex-tools <command> [options]

Commands:
  search      Search local Codex conversations and thread history.
  summary     Build daily/weekly summaries or a static summary site.
  viewer      Run the local conversation viewer.
  manager     Manage isolated Codex profiles.
  alias       Install or remove the short ct alias.
  structured  Run reproducible schema-constrained Codex tasks.
  config      Inspect or change per-user defaults.
  diagnose    Inspect local Codex source health.

Examples:
  codex-tools search "sqlite history"
  codex-tools summary today --show-context
  codex-tools summary day 2026-09-11
  codex-tools summary week --last-week
  codex-tools summary site
  codex-tools viewer
  codex-tools manager list
  codex-tools alias install
  codex-tools structured run --prompt prompt.txt --schema schema.json --run-dir run --model gpt-5.6-sol
  codex-tools structured batch batch.json --batch-dir runs/batch-001 --jobs 4
  codex-tools structured batch batch.json --batch-dir runs/batch-001 --jobs 4 --idxs 10 20
  codex-tools structured batch batch.json --batch-dir runs/model-test --model gpt-6-astra --reasoning-effort high
  codex-tools structured check runs/batch-001
  codex-tools config show
  codex-tools diagnose
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(help_text())
        return 0

    command = args[0]
    rest = args[1:]
    if command == "search":
        return search.main(rest)
    if command == "summary":
        return summary.main(rest)
    if command == "viewer":
        return viewer.main(rest, prog="codex-tools viewer")
    if command == "manager":
        return manager.main(rest, prog="codex-tools manager")
    if command == "alias":
        return aliases.main(rest)
    if command == "structured":
        return structured.main(rest)
    if command == "config":
        return config.main(rest)
    if command == "diagnose":
        return search.diagnose_main(rest)

    print(f"error: unknown command: {command}", file=sys.stderr)
    print("Run `codex-tools --help` for usage.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
