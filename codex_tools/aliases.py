"""Install small command aliases for Codex Tools."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


ALIAS_NAME = "ct"
TARGET_COMMAND = "codex-tools"
DEFAULT_BIN_DIR = Path("~/.local/bin")
DEFAULT_COMPLETION_DIR = Path("~/.local/share/bash-completion/completions")


COMPLETION_SCRIPT = """# bash completion for ct
_ct_completion() {
  local cur prev words cword
  _init_completion -n : || return

  local commands="search summary viewer manager alias structured config diagnose"
  local summary_commands="today yesterday day week model site clean"
  local viewer_commands="serve start restart stop status open pick doctor"
  local manager_commands="new rename export import list run install uninstall remove rm delete path doctor repair"
  local alias_commands="install remove list"
  local config_commands="show path validate set unset"

  if [[ ${cword} -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "${commands}" -- "${cur}") )
    return
  fi

  case "${words[1]}" in
    summary)
      [[ ${cword} -eq 2 ]] && COMPREPLY=( $(compgen -W "${summary_commands}" -- "${cur}") )
      ;;
    viewer)
      [[ ${cword} -eq 2 ]] && COMPREPLY=( $(compgen -W "${viewer_commands}" -- "${cur}") )
      ;;
    manager)
      [[ ${cword} -eq 2 ]] && COMPREPLY=( $(compgen -W "${manager_commands}" -- "${cur}") )
      ;;
    alias)
      [[ ${cword} -eq 2 ]] && COMPREPLY=( $(compgen -W "${alias_commands}" -- "${cur}") )
      ;;
    config)
      [[ ${cword} -eq 2 ]] && COMPREPLY=( $(compgen -W "${config_commands}" -- "${cur}") )
      ;;
  esac
}
complete -F _ct_completion ct
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-tools alias",
        description="Install or remove the short ct command alias.",
    )
    parser.add_argument("--bin-dir", type=Path, default=DEFAULT_BIN_DIR)
    parser.add_argument(
        "--completion-dir",
        type=Path,
        default=DEFAULT_COMPLETION_DIR,
        help="Bash completion directory.",
    )
    sub = parser.add_subparsers(dest="command")

    install = sub.add_parser("install", help="Install ct and its bash completion.")
    install.set_defaults(func=command_install)

    remove = sub.add_parser("remove", help="Remove ct and its bash completion.")
    remove.set_defaults(func=command_remove)

    list_cmd = sub.add_parser("list", help="Show alias status.")
    list_cmd.set_defaults(func=command_list)
    return parser


def alias_path(args: argparse.Namespace) -> Path:
    return args.bin_dir.expanduser().resolve() / ALIAS_NAME


def completion_path(args: argparse.Namespace) -> Path:
    return args.completion_dir.expanduser().resolve() / ALIAS_NAME


def target_path() -> Path | None:
    found = shutil.which(TARGET_COMMAND)
    return Path(found) if found else None


def command_install(args: argparse.Namespace) -> int:
    alias = alias_path(args)
    completion = completion_path(args)
    target = target_path()
    if target is None:
        print(f"error: could not find {TARGET_COMMAND} on PATH", file=sys.stderr)
        return 127
    if alias.exists() or alias.is_symlink():
        print(f"error: refusing to replace existing alias: {alias}", file=sys.stderr)
        return 2
    if completion.exists():
        print(f"error: refusing to replace existing completion: {completion}", file=sys.stderr)
        return 2

    alias.parent.mkdir(parents=True, exist_ok=True)
    completion.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(target)
    completion.write_text(COMPLETION_SCRIPT, encoding="utf-8")
    print(f"Installed {ALIAS_NAME} -> {target}")
    print(f"Installed bash completion: {completion}")
    print(f"Reload completion now with: source {completion}")
    return 0


def command_remove(args: argparse.Namespace) -> int:
    alias = alias_path(args)
    completion = completion_path(args)
    removed = False
    if alias.exists() or alias.is_symlink():
        alias.unlink()
        print(f"Removed {alias}")
        removed = True
    if completion.exists():
        completion.unlink()
        print(f"Removed {completion}")
        removed = True
    if not removed:
        print("No ct alias or completion installed.")
    return 0


def command_list(args: argparse.Namespace) -> int:
    alias = alias_path(args)
    completion = completion_path(args)
    print(f"{ALIAS_NAME}: {alias if alias.exists() or alias.is_symlink() else 'not installed'}")
    print(f"completion: {completion if completion.exists() else 'not installed'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args))
