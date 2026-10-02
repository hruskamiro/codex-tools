"""Install small command aliases for Codex Tools."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


ALIAS_NAME = "ct"
TARGET_COMMAND = "codex-tools"
COMPLETION_NAMES = (ALIAS_NAME, TARGET_COMMAND)
DEFAULT_BIN_DIR = Path("~/.local/bin")
DEFAULT_COMPLETION_DIR = Path("~/.local/share/bash-completion/completions")


COMPLETION_SCRIPT = """# bash completion for ct and codex-tools
_codex_tools_completion() {
  local cur prev words cword
  _init_completion -n : || return

  local commands="search summary viewer manager alias structured config diagnose"
  local summary_commands="today yesterday day week model site clean"
  local viewer_commands="serve start restart stop status open pick doctor"
  local manager_commands="new rename export import list run install uninstall remove rm delete path doctor repair"
  local alias_commands="install remove list"
  local structured_commands="run batch check"
  local config_commands="show path edit validate set unset"

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
    structured)
      [[ ${cword} -eq 2 ]] && COMPREPLY=( $(compgen -W "${structured_commands}" -- "${cur}") )
      ;;
    config)
      [[ ${cword} -eq 2 ]] && COMPREPLY=( $(compgen -W "${config_commands}" -- "${cur}") )
      ;;
  esac
}
complete -F _codex_tools_completion ct codex-tools
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-tools alias",
        description="Manage the short ct alias and Bash completion.",
    )
    parser.add_argument("--bin-dir", type=Path, default=DEFAULT_BIN_DIR)
    parser.add_argument(
        "--completion-dir",
        type=Path,
        default=DEFAULT_COMPLETION_DIR,
        help="Bash completion directory.",
    )
    sub = parser.add_subparsers(dest="command")

    install = sub.add_parser(
        "install", help="Install ct and completion for ct and codex-tools."
    )
    install.add_argument(
        "--force",
        action="store_true",
        help="Replace the existing ct command and completion files.",
    )
    install.set_defaults(func=command_install)

    remove = sub.add_parser("remove", help="Remove ct and the completion files.")
    remove.set_defaults(func=command_remove)

    list_cmd = sub.add_parser("list", help="Show alias status.")
    list_cmd.set_defaults(func=command_list)
    return parser


def alias_path(args: argparse.Namespace) -> Path:
    return args.bin_dir.expanduser().resolve() / ALIAS_NAME


def completion_paths(args: argparse.Namespace) -> tuple[Path, ...]:
    root = args.completion_dir.expanduser().resolve()
    return tuple(root / name for name in COMPLETION_NAMES)


def path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def target_path() -> Path | None:
    found = shutil.which(TARGET_COMMAND)
    return Path(found) if found else None


def command_install(args: argparse.Namespace) -> int:
    alias = alias_path(args)
    completions = completion_paths(args)
    target = target_path()
    if target is None:
        print(f"error: could not find {TARGET_COMMAND} on PATH", file=sys.stderr)
        return 127
    alias_exists = path_exists(alias)
    existing_completions = tuple(path for path in completions if path_exists(path))
    if alias_exists and alias.is_dir() and not alias.is_symlink():
        print(f"error: refusing to replace existing directory: {alias}", file=sys.stderr)
        return 2
    for completion in existing_completions:
        if completion.is_dir() and not completion.is_symlink():
            print(
                f"error: refusing to replace existing directory: {completion}",
                file=sys.stderr,
            )
            return 2
    if alias_exists and not args.force:
        print(f"error: refusing to replace existing alias: {alias}", file=sys.stderr)
        return 2
    if existing_completions and not args.force:
        print(
            f"error: refusing to replace existing completion: {existing_completions[0]}",
            file=sys.stderr,
        )
        return 2

    alias.parent.mkdir(parents=True, exist_ok=True)
    completions[0].parent.mkdir(parents=True, exist_ok=True)
    if alias_exists:
        alias.unlink()
    for completion in existing_completions:
        completion.unlink()
    alias.symlink_to(target)
    alias_completion, command_completion = completions
    command_completion.write_text(COMPLETION_SCRIPT, encoding="utf-8")
    alias_completion.symlink_to(command_completion.name)
    print(f"Installed {ALIAS_NAME} -> {target}")
    print(f"Installed bash completion: {command_completion}")
    print(
        f"Installed bash completion link: {alias_completion} -> "
        f"{command_completion.name}"
    )
    print(f"Reload completion now with: source {command_completion}")
    return 0


def command_remove(args: argparse.Namespace) -> int:
    alias = alias_path(args)
    completions = completion_paths(args)
    removed = False
    if path_exists(alias):
        alias.unlink()
        print(f"Removed {alias}")
        removed = True
    for completion in completions:
        if path_exists(completion):
            completion.unlink()
            print(f"Removed {completion}")
            removed = True
    if not removed:
        print("No ct alias or Codex Tools completion installed.")
    return 0


def command_list(args: argparse.Namespace) -> int:
    alias = alias_path(args)
    print(f"{ALIAS_NAME}: {alias if path_exists(alias) else 'not installed'}")
    for name, completion in zip(COMPLETION_NAMES, completion_paths(args), strict=True):
        status = completion if path_exists(completion) else "not installed"
        print(f"completion ({name}): {status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return int(args.func(args))
