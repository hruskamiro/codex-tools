#!/usr/bin/env python3
"""Manage isolated Codex profiles with optionally shared conversations."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CODEX_HOME = Path("~/.codex").expanduser()
DEFAULT_MANAGER_ROOT = Path("~/.codex-manager").expanduser()
PROFILE_FILE = "profile.json"
SHARED_PATHS = {
    "sessions": "dir",
    "archived_sessions": "dir",
    "attachments": "dir",
    "shell_snapshots": "dir",
    "thread-writer-locks": "dir",
    "session_index.jsonl": "file",
}
SEEDED_STATE_FILES = ("state_5.sqlite",)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_name(value: str) -> str:
    if not value:
        raise argparse.ArgumentTypeError("profile name cannot be empty")
    if value == "default":
        return value
    if not all(char.isalnum() or char in {"-", "_"} for char in value):
        raise argparse.ArgumentTypeError(
            "profile names may contain only letters, numbers, '-' and '_'"
        )
    return value


def valid_command_name(value: str) -> str:
    if not value or value in {".", ".."} or Path(value).name != value:
        raise argparse.ArgumentTypeError("command name must be a single filename")
    if not all(char.isalnum() or char in {"-", "_"} for char in value):
        raise argparse.ArgumentTypeError(
            "command names may contain only letters, numbers, '-' and '_'"
        )
    return value


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def manager_root(args: argparse.Namespace) -> Path:
    return args.manager_root.expanduser().resolve()


def default_home(args: argparse.Namespace) -> Path:
    return args.default_home.expanduser().resolve()


def profiles_dir(root: Path) -> Path:
    return root / "profiles"


def stores_dir(root: Path) -> Path:
    return root / "stores"


def profile_dir(root: Path, name: str) -> Path:
    return profiles_dir(root) / name


def profile_home(root: Path, name: str) -> Path:
    if name == "default":
        raise ValueError("default profile home is provided by --default-home")
    return profile_dir(root, name) / "home"


def profile_file(root: Path, name: str) -> Path:
    return profile_dir(root, name) / PROFILE_FILE


def store_dir(root: Path, name: str) -> Path:
    return stores_dir(root) / f"{name}-conversations"


def trash_dir(root: Path) -> Path:
    return root / "trash"


def display_path(path: Path) -> str:
    try:
        return str(path.expanduser().resolve()).replace(str(Path.home()), "~", 1)
    except OSError:
        return str(path).replace(str(Path.home()), "~", 1)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_private_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    path.chmod(0o600)


def load_profile(root: Path, name: str, default: Path) -> dict[str, Any]:
    if name == "default":
        return {
            "name": "default",
            "home": str(default),
            "conversation_store": str(default),
            "share": "self",
            "created_at": "",
        }
    path = profile_file(root, name)
    if not path.exists():
        raise FileNotFoundError(f"profile does not exist: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


def iter_managed_profiles(root: Path) -> list[dict[str, Any]]:
    rows = []
    base = profiles_dir(root)
    if base.exists():
        for path in sorted(base.iterdir()):
            if (path / PROFILE_FILE).exists():
                rows.append(load_profile(root, path.name, DEFAULT_CODEX_HOME))
    return rows


def save_profile(root: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(profile_file(root, str(payload["name"])), payload)


def home_from_profile(profile: dict[str, Any]) -> Path:
    return Path(str(profile["home"])).expanduser().resolve()


def resolve_profile_home(
    name: str,
    root: Path = DEFAULT_MANAGER_ROOT,
    default: Path = DEFAULT_CODEX_HOME,
) -> Path:
    """Resolve a codex-manager profile name to the CODEX_HOME it owns."""
    profile = load_profile(root.expanduser().resolve(), name, default.expanduser().resolve())
    return home_from_profile(profile)


def profile_environment(
    name: str,
    root: Path = DEFAULT_MANAGER_ROOT,
    default: Path = DEFAULT_CODEX_HOME,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["CODEX_HOME"] = str(resolve_profile_home(name, root, default))
    return env


def store_from_profile(profile: dict[str, Any]) -> Path:
    return Path(str(profile["conversation_store"])).expanduser().resolve()


def ensure_store(path: Path) -> None:
    ensure_private_dir(path)
    for rel, kind in SHARED_PATHS.items():
        target = path / rel
        if kind == "dir":
            ensure_private_dir(target)
        else:
            ensure_private_dir(target.parent)
            target.touch(exist_ok=True, mode=0o600)
            target.chmod(0o600)


def ensure_default_shared_paths(path: Path) -> None:
    for rel, kind in SHARED_PATHS.items():
        target = path / rel
        if target.exists() or target.is_symlink():
            continue
        if kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()


def link_shared_paths(home: Path, store: Path) -> None:
    ensure_private_dir(home)
    for rel in SHARED_PATHS:
        link = home / rel
        target = store / rel
        if link.is_symlink():
            if link.resolve() == target.resolve():
                continue
            raise FileExistsError(f"{link} already links somewhere else")
        if link.exists():
            raise FileExistsError(f"{link} already exists; refusing to replace it")
        link.symlink_to(target, target_is_directory=target.is_dir())


def copy_default_config(home: Path, default: Path) -> None:
    source = default / "config.toml"
    target = home / "config.toml"
    if source.exists() and not target.exists():
        shutil.copy2(source, target)


def backup_existing_sqlite(target: Path) -> Path | None:
    if not target.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = target.parent / ".codex-manager-backups" / f"{target.name}-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    copied = False
    for path in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
        if path.exists():
            shutil.copy2(path, backup_dir / path.name)
            copied = True
    return backup_dir if copied else None


def sqlite_backup(source: Path, target: Path, preserve_existing: bool = False) -> Path | None:
    if not source.exists():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    backup_dir = backup_existing_sqlite(target) if preserve_existing else None
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        target_connection = sqlite3.connect(tmp)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
    except sqlite3.Error:
        if tmp.exists():
            tmp.unlink()
        return None
    tmp.replace(target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    return backup_dir or target


def seed_state_files(
    home: Path, source_home: Path, force: bool = False
) -> tuple[list[str], list[Path]]:
    seeded = []
    backups = []
    for rel in SEEDED_STATE_FILES:
        source = source_home / rel
        target = home / rel
        if target.exists() and not force:
            continue
        result = sqlite_backup(source, target, preserve_existing=force)
        if result:
            seeded.append(rel)
            if result != target:
                backups.append(result)
    return seeded, backups


def shared_source_home(root: Path, share: str | None, default: Path) -> Path | None:
    if not share or share == "isolated":
        return None
    try:
        profile = load_profile(root, share, default)
    except FileNotFoundError:
        return None
    return home_from_profile(profile)


def store_is_used_by_other_profiles(root: Path, store: Path, name: str) -> bool:
    try:
        target = store.resolve()
    except OSError:
        target = store
    for profile in iter_managed_profiles(root):
        if profile.get("name") == name:
            continue
        try:
            other = store_from_profile(profile).resolve()
        except OSError:
            other = store_from_profile(profile)
        if other == target:
            return True
    return False


def move_to_trash(root: Path, path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = trash_dir(root) / f"{path.name}-{stamp}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target))
    return target


def command_new(args: argparse.Namespace) -> int:
    root = manager_root(args)
    default = default_home(args)
    name = args.name
    if name == "default":
        print("error: `default` is reserved for the existing Codex home", file=sys.stderr)
        return 2

    target_profile_dir = profile_dir(root, name)
    if target_profile_dir.exists() and not args.force:
        print(f"error: profile already exists: {name}", file=sys.stderr)
        return 2

    share = args.share
    if args.isolated:
        share = None
    elif share is None:
        share = "default"

    home = profile_home(root, name)
    if home.exists() and any(home.iterdir()) and not args.force:
        print(f"error: profile home is not empty: {home}", file=sys.stderr)
        return 2
    ensure_private_dir(home)
    copy_default_config(home, default)

    if share:
        shared_profile = load_profile(root, share, default)
        store = store_from_profile(shared_profile)
        source_home = home_from_profile(shared_profile)
        if share == "default":
            ensure_default_shared_paths(store)
        else:
            ensure_store(store)
    else:
        store = store_dir(root, name)
        source_home = None
        ensure_store(store)

    try:
        link_shared_paths(home, store)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = {
        "name": name,
        "home": str(home),
        "conversation_store": str(store),
        "share": share or "isolated",
        "created_at": utc_now(),
    }
    seeded, _backups = seed_state_files(home, source_home) if source_home else ([], [])
    save_profile(root, payload)
    print(f"Created profile {name}")
    print("Auth/config: isolated")
    if share:
        print(f"Conversations: shared with {share}")
    else:
        print("Conversations: isolated")
    if seeded:
        print(f"Seeded state: {', '.join(seeded)}")
    print(f"Run: codex-manager run {name}")
    return 0


def command_list(args: argparse.Namespace) -> int:
    root = manager_root(args)
    default = default_home(args)
    rows = [load_profile(root, "default", default)]
    base = profiles_dir(root)
    if base.exists():
        for path in sorted(base.iterdir()):
            if (path / PROFILE_FILE).exists():
                rows.append(load_profile(root, path.name, default))

    for profile in rows:
        name = str(profile["name"])
        share = str(profile.get("share") or "")
        home = display_path(home_from_profile(profile))
        store = display_path(store_from_profile(profile))
        print(f"{name}\tshare={share}\thome={home}\tstore={store}")
    return 0


def command_path(args: argparse.Namespace) -> int:
    profile = load_profile(manager_root(args), args.name, default_home(args))
    path = store_from_profile(profile) if args.store else home_from_profile(profile)
    print(path)
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    profile = load_profile(manager_root(args), args.name, default_home(args))
    home = home_from_profile(profile)
    store = store_from_profile(profile)
    print(f"Profile: {profile['name']}")
    print(f"CODEX_HOME: {display_path(home)}")
    print("Auth: isolated" if profile["name"] != "default" else "Auth: default")
    print("Config: isolated" if profile["name"] != "default" else "Config: default")
    print(f"Conversations: {profile.get('share', 'unknown')}")
    print(f"Conversation store: {display_path(store)}")
    for rel in SHARED_PATHS:
        path = home / rel
        status = "missing"
        if path.is_symlink():
            status = f"linked -> {display_path(path.resolve())}"
        elif path.exists():
            status = "local"
        print(f"{rel}: {status}")
    for rel in ("auth.json", "config.toml", "thread_history_1.sqlite", "state_5.sqlite"):
        path = home / rel
        print(f"{rel}: {'present' if path.exists() else 'missing'}")
    return 0


def command_repair(args: argparse.Namespace) -> int:
    root = manager_root(args)
    default = default_home(args)
    profile = load_profile(root, args.name, default)
    home = home_from_profile(profile)
    source_home = shared_source_home(root, str(profile.get("share") or ""), default)
    if source_home is None:
        print(f"Profile {args.name} does not share with another profile.")
        return 0
    seeded, backups = seed_state_files(home, source_home, force=args.force)
    if seeded:
        print(f"Seeded state for {args.name}: {', '.join(seeded)}")
        for backup in backups:
            print(f"Backup: {backup}")
    else:
        print(f"No missing seedable state files for {args.name}.")
    return 0


def command_run(args: argparse.Namespace) -> int:
    profile = load_profile(manager_root(args), args.name, default_home(args))
    home = home_from_profile(profile)
    command = [args.codex_binary, *args.codex_args]
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    executable = shutil.which(args.codex_binary)
    if executable is None:
        print(f"error: could not find Codex executable: {args.codex_binary}", file=sys.stderr)
        return 127
    os.execvpe(executable, command, env)
    return 127


def wrapper_script(name: str, codex_binary: str, root: Path, default: Path) -> str:
    python = shlex.quote(sys.executable)
    source_root = shlex.quote(str(Path(__file__).resolve().parent.parent))
    module_args = " ".join(
        shlex.quote(part)
        for part in (
            "-m",
            "codex_tools.manager",
            "--manager-root",
            str(root),
            "--default-home",
            str(default),
            "run",
            "--codex-binary",
            codex_binary,
            name,
            "--",
        )
    )
    return f"""#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH={source_root}${{PYTHONPATH:+:${{PYTHONPATH}}}}
exec {python} {module_args} "$@"
"""


def command_install(args: argparse.Namespace) -> int:
    root = manager_root(args)
    default = default_home(args)
    load_profile(root, args.name, default)
    bin_dir = args.bin_dir.expanduser().resolve()
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / args.command_name
    if target.exists() and not args.force:
        print(f"error: command already exists: {target}", file=sys.stderr)
        return 2
    target.write_text(
        wrapper_script(args.name, args.codex_binary, root, default),
        encoding="utf-8",
    )
    target.chmod(0o755)
    print(f"Installed {args.command_name} -> {target}")
    return 0


def command_uninstall(args: argparse.Namespace) -> int:
    target = args.bin_dir.expanduser().resolve() / args.command_name
    if not target.exists():
        print(f"Nothing to uninstall: {target}")
        return 0
    target.unlink()
    print(f"Removed {target}")
    return 0


def command_remove(args: argparse.Namespace) -> int:
    root = manager_root(args)
    default = default_home(args)
    name = args.name
    if name == "default":
        print("error: refusing to remove the default Codex home", file=sys.stderr)
        return 2

    profile = load_profile(root, name, default)
    profile_path = profile_dir(root, name)
    store = store_from_profile(profile)
    private_store = store == store_dir(root, name)
    store_used_elsewhere = store_is_used_by_other_profiles(root, store, name)

    if args.uninstall:
        uninstall_args = argparse.Namespace(
            bin_dir=args.bin_dir,
            command_name=args.command_name or name,
        )
        command_uninstall(uninstall_args)

    if args.remove_store:
        if not private_store:
            print(
                f"error: refusing to remove non-private store: {store}",
                file=sys.stderr,
            )
            return 2
        if store_used_elsewhere:
            print(
                f"error: refusing to remove store still used by another profile: {store}",
                file=sys.stderr,
            )
            return 2

    if args.purge:
        shutil.rmtree(profile_path)
        print(f"Deleted profile {name}: {profile_path}")
        if args.remove_store and store.exists():
            shutil.rmtree(store)
            print(f"Deleted conversation store: {store}")
        return 0

    trashed_profile = move_to_trash(root, profile_path)
    print(f"Moved profile {name} to trash: {trashed_profile}")
    if args.remove_store and store.exists():
        trashed_store = move_to_trash(root, store)
        print(f"Moved conversation store to trash: {trashed_store}")
    return 0


def build_parser(prog: str = "codex-manager") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Manage isolated Codex profiles with shared conversation stores.",
    )
    parser.add_argument(
        "--manager-root",
        type=Path,
        default=DEFAULT_MANAGER_ROOT,
        help=f"Profile manager root. Default: {DEFAULT_MANAGER_ROOT}",
    )
    parser.add_argument(
        "--default-home",
        type=Path,
        default=DEFAULT_CODEX_HOME,
        help=f"Existing default Codex home. Default: {DEFAULT_CODEX_HOME}",
    )
    sub = parser.add_subparsers(dest="command")

    new = sub.add_parser("new", help="Create a new Codex profile.")
    new.add_argument("name", type=valid_name)
    new.add_argument(
        "--share",
        metavar="PROFILE",
        type=valid_name,
        help="Share conversations with this profile. Default: default.",
    )
    new.add_argument(
        "--isolated",
        action="store_true",
        help="Create a separate conversation store instead of sharing with default.",
    )
    new.add_argument("--force", action="store_true", help="Allow reuse of existing dirs.")
    new.set_defaults(func=command_new)

    list_cmd = sub.add_parser("list", help="List profiles.")
    list_cmd.set_defaults(func=command_list)

    run = sub.add_parser("run", help="Run Codex with a profile.")
    run.add_argument("name", type=valid_name)
    run.add_argument("--codex-binary", default="codex")
    run.add_argument("codex_args", nargs=argparse.REMAINDER)
    run.set_defaults(func=command_run)

    install = sub.add_parser("install", help="Install a profile wrapper command.")
    install.add_argument("name", type=valid_name)
    install.add_argument("--bin-dir", type=Path, default=Path("~/.local/bin"))
    install.add_argument("--command-name", type=valid_command_name, help="Installed command name.")
    install.add_argument("--codex-binary", default="codex")
    install.add_argument("--force", action="store_true")
    install.set_defaults(func=command_install)

    uninstall = sub.add_parser("uninstall", help="Remove an installed profile wrapper.")
    uninstall.add_argument("name", type=valid_name)
    uninstall.add_argument("--bin-dir", type=Path, default=Path("~/.local/bin"))
    uninstall.add_argument("--command-name", type=valid_command_name, help="Installed command name.")
    uninstall.set_defaults(func=command_uninstall)

    remove = sub.add_parser("remove", aliases=["rm", "delete"], help="Remove a managed profile.")
    remove.add_argument("name", type=valid_name)
    remove.add_argument("--purge", action="store_true", help="Delete instead of moving to trash.")
    remove.add_argument(
        "--remove-store",
        action="store_true",
        help="Also remove the profile's private conversation store.",
    )
    remove.add_argument(
        "--uninstall",
        action="store_true",
        help="Also remove the installed wrapper command.",
    )
    remove.add_argument("--bin-dir", type=Path, default=Path("~/.local/bin"))
    remove.add_argument("--command-name", type=valid_command_name, help="Installed command name.")
    remove.set_defaults(func=command_remove)

    path = sub.add_parser("path", help="Print a profile home path.")
    path.add_argument("name", type=valid_name)
    path.add_argument("--store", action="store_true", help="Print conversation store path.")
    path.set_defaults(func=command_path)

    doctor = sub.add_parser("doctor", help="Show what a profile isolates and shares.")
    doctor.add_argument("name", type=valid_name)
    doctor.set_defaults(func=command_doctor)

    repair = sub.add_parser("repair", help="Seed missing derived state for a profile.")
    repair.add_argument("name", type=valid_name)
    repair.add_argument("--force", action="store_true", help="Replace existing seedable state.")
    repair.set_defaults(func=command_repair)
    return parser


def normalize_remainder(args: argparse.Namespace) -> None:
    if hasattr(args, "codex_args") and args.codex_args[:1] == ["--"]:
        args.codex_args = args.codex_args[1:]
    if getattr(args, "command_name", None) is None and hasattr(args, "name"):
        args.command_name = args.name


def main(argv: list[str] | None = None, *, prog: str = "codex-manager") -> int:
    parser = build_parser(prog)
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    normalize_remainder(args)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: invalid profile metadata: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
