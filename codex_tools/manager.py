#!/usr/bin/env python3
"""Manage isolated Codex profiles with optionally shared conversations."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import secrets
import shlex
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from codex_tools import profile_bundle


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
EXPORTED_CONVERSATION_PATHS = (
    "sessions",
    "archived_sessions",
    "attachments",
    "session_index.jsonl",
)
BUNDLE_MANIFEST = "manifest.json"
BUNDLE_VERSION = 1


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


def default_wrapper_name(profile_name: str) -> str:
    return f"codex-{profile_name}"


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


def atomic_write_private_text(path: Path, content: str) -> None:
    ensure_private_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
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


def wrapper_from_profile(profile: dict[str, Any]) -> dict[str, Any] | None:
    wrapper = profile.get("wrapper")
    return wrapper if isinstance(wrapper, dict) else None


def validate_wrapper_record(wrapper: dict[str, Any]) -> None:
    command = wrapper.get("command")
    path_value = wrapper.get("path")
    digest = wrapper.get("sha256")
    if not isinstance(command, str):
        raise ValueError("invalid registered wrapper command")
    valid_command_name(command)
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise ValueError("invalid registered wrapper path")
    if Path(path_value).name != command:
        raise ValueError("registered wrapper path does not match its command")
    if wrapper.get("naming") not in {"default", "custom"}:
        raise ValueError("invalid registered wrapper naming mode")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("invalid registered wrapper digest")


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


def prompt_export_passphrase() -> str:
    passphrase = getpass.getpass("Export passphrase: ")
    if len(passphrase) < 8:
        raise ValueError("export passphrase must contain at least 8 characters")
    confirmation = getpass.getpass("Confirm passphrase: ")
    if not secrets.compare_digest(passphrase, confirmation):
        raise ValueError("passphrases do not match")
    return passphrase


def prompt_import_passphrase() -> str:
    passphrase = getpass.getpass("Passphrase: ")
    if not passphrase:
        raise ValueError("passphrase cannot be empty")
    return passphrase


def force_file_auth_store(config: Path) -> None:
    """Ensure imported credentials are read from the portable auth.json file."""
    content = config.read_text(encoding="utf-8") if config.exists() else ""
    lines = content.splitlines(keepends=True)
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("["):
            break
        is_auth_store = (
            "=" in stripped
            and stripped.split("=", 1)[0].strip() == "cli_auth_credentials_store"
        )
        if is_auth_store:
            ending = "\n" if line.endswith("\n") else ""
            lines[index] = f'cli_auth_credentials_store = "file"{ending}'
            replaced = True
            break
    if not replaced:
        lines.insert(0, 'cli_auth_credentials_store = "file"\n')
    atomic_write_private_text(config, "".join(lines))


def zip_add_file(archive: zipfile.ZipFile, source: Path, arcname: str) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"refusing to export non-regular file: {source}")
    archive.write(source, arcname)


def zip_add_tree(archive: zipfile.ZipFile, source: Path, arcname: str) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"refusing to export non-directory: {source}")
    archive.writestr(f"{arcname.rstrip('/')}/", b"")
    for current, directories, filenames in os.walk(source, followlinks=False):
        current_path = Path(current)
        for directory in list(directories):
            path = current_path / directory
            if path.is_symlink():
                raise ValueError(f"refusing to export symlink: {path}")
            relative = path.relative_to(source).as_posix()
            archive.writestr(f"{arcname.rstrip('/')}/{relative}/", b"")
        for filename in filenames:
            path = current_path / filename
            relative = path.relative_to(source).as_posix()
            zip_add_file(archive, path, f"{arcname.rstrip('/')}/{relative}")


def create_profile_archive(
    archive_path: Path,
    profile: dict[str, Any],
    include_conversations: bool,
) -> None:
    home = home_from_profile(profile)
    auth = home / "auth.json"
    if not auth.exists():
        raise FileNotFoundError(
            f"file-backed credentials are missing for {profile['name']}: {auth}"
        )
    if auth.is_symlink() or not auth.is_file():
        raise ValueError(f"refusing to export non-regular credential file: {auth}")

    config = home / "config.toml"
    manifest = {
        "format": "codex-manager-profile",
        "version": BUNDLE_VERSION,
        "name": str(profile["name"]),
        "exported_at": utc_now(),
        "includes": {
            "auth": True,
            "config": config.is_file() and not config.is_symlink(),
            "conversations": include_conversations,
        },
    }
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        archive.writestr(BUNDLE_MANIFEST, json.dumps(manifest, sort_keys=True) + "\n")
        zip_add_file(archive, auth, "profile/auth.json")
        if manifest["includes"]["config"]:
            zip_add_file(archive, config, "profile/config.toml")
        if include_conversations:
            store = store_from_profile(profile)
            for rel in EXPORTED_CONVERSATION_PATHS:
                source = store / rel
                arcname = f"conversations/{rel}"
                if not source.exists():
                    continue
                if source.is_dir():
                    zip_add_tree(archive, source, arcname)
                else:
                    zip_add_file(archive, source, arcname)


def read_bundle_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError("bundle contains duplicate paths")
    if BUNDLE_MANIFEST not in names:
        raise ValueError("bundle manifest is missing")
    if archive.getinfo(BUNDLE_MANIFEST).file_size > 64 * 1024:
        raise ValueError("bundle manifest is too large")
    try:
        manifest = json.loads(archive.read(BUNDLE_MANIFEST))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("bundle manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != "codex-manager-profile":
        raise ValueError("not a codex-manager profile archive")
    if manifest.get("version") != BUNDLE_VERSION:
        raise ValueError(f"unsupported profile archive version: {manifest.get('version')}")
    name = manifest.get("name")
    if not isinstance(name, str):
        raise ValueError("bundle profile name is invalid")
    valid_name(name)
    includes = manifest.get("includes")
    if not isinstance(includes, dict) or includes.get("auth") is not True:
        raise ValueError("bundle does not contain file-backed credentials")
    if "profile/auth.json" not in names:
        raise ValueError("bundle credential file is missing")
    if archive.getinfo("profile/auth.json").is_dir():
        raise ValueError("bundle credential path is not a file")
    if archive.getinfo("profile/auth.json").file_size > 10 * 1024 * 1024:
        raise ValueError("bundle credential file is too large")
    has_config = "profile/config.toml" in names
    if bool(includes.get("config")) != has_config:
        raise ValueError("bundle config metadata does not match its contents")
    if has_config and (
        archive.getinfo("profile/config.toml").is_dir()
        or archive.getinfo("profile/config.toml").file_size > 10 * 1024 * 1024
    ):
        raise ValueError("bundle config file is invalid")
    validate_bundle_paths(archive, bool(includes.get("conversations")))
    return manifest


def validate_bundle_paths(archive: zipfile.ZipFile, conversations: bool) -> None:
    allowed_files = {BUNDLE_MANIFEST, "profile/auth.json", "profile/config.toml"}
    allowed_conversation_roots = set(EXPORTED_CONVERSATION_PATHS)
    for info in archive.infolist():
        name = info.filename
        if "\\" in name:
            raise ValueError(f"invalid path in bundle: {name}")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe path in bundle: {name}")
        if name in allowed_files:
            continue
        if conversations and len(path.parts) >= 2 and path.parts[0] == "conversations":
            if path.parts[1] in allowed_conversation_roots:
                continue
        raise ValueError(f"unexpected path in bundle: {name}")


def write_zip_member(
    archive: zipfile.ZipFile, member: str, target: Path, mode: int = 0o600
) -> None:
    ensure_private_dir(target.parent)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with archive.open(member) as source, os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            shutil.copyfileobj(source, destination)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        target.unlink(missing_ok=True)
        raise


def restore_conversations(archive: zipfile.ZipFile, store: Path) -> None:
    prefix = PurePosixPath("conversations")
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        if not path.parts or path.parts[0] != prefix.name:
            continue
        relative = Path(*path.parts[1:])
        target = store / relative
        if info.is_dir():
            ensure_private_dir(target)
        else:
            write_zip_member(archive, info.filename, target)


def command_export(args: argparse.Namespace) -> int:
    profile = load_profile(manager_root(args), args.name, default_home(args))
    output = (args.output or Path(f"{args.name}.codex-profile")).expanduser().resolve()
    if output.exists():
        print(f"error: refusing to replace existing export: {output}", file=sys.stderr)
        return 2
    if output.parent.exists() and not output.parent.is_dir():
        raise ValueError(f"export parent is not a directory: {output.parent}")
    if not output.parent.exists():
        ensure_private_dir(output.parent)
    passphrase = prompt_export_passphrase()
    with tempfile.TemporaryDirectory(prefix="codex-profile-export-") as temporary:
        archive_path = Path(temporary) / "profile.zip"
        encrypted_path = output.with_name(f".{output.name}.tmp-{secrets.token_hex(4)}")
        try:
            create_profile_archive(
                archive_path, profile, include_conversations=args.include_conversations
            )
            profile_bundle.encrypt_file(archive_path, encrypted_path, passphrase)
            try:
                os.link(encrypted_path, output)
            except FileExistsError:
                print(
                    f"error: refusing to replace existing export: {output}",
                    file=sys.stderr,
                )
                return 2
            encrypted_path.unlink()
            output.chmod(0o600)
        finally:
            encrypted_path.unlink(missing_ok=True)
    print(f"Exported profile {args.name}: {output}")
    print(
        "Conversations: included"
        if args.include_conversations
        else "Conversations: not included"
    )
    return 0


def command_import(args: argparse.Namespace) -> int:
    source = args.bundle.expanduser().resolve()
    if not source.is_file():
        print(f"error: profile bundle does not exist: {source}", file=sys.stderr)
        return 2
    passphrase = prompt_import_passphrase()
    root = manager_root(args)
    default = default_home(args)

    with tempfile.TemporaryDirectory(prefix="codex-profile-import-") as temporary:
        archive_path = Path(temporary) / "profile.zip"
        profile_bundle.decrypt_file(source, archive_path, passphrase)
        try:
            archive = zipfile.ZipFile(archive_path, "r")
        except zipfile.BadZipFile as exc:
            raise ValueError("decrypted profile archive is invalid") from exc
        with archive:
            manifest = read_bundle_manifest(archive)
            name = args.name or str(manifest["name"])
            valid_name(name)
            if name == "default":
                raise ValueError(
                    "the default profile cannot be imported in place; "
                    "use --name to create a managed profile"
                )
            target_profile = profile_dir(root, name)
            if target_profile.exists():
                raise FileExistsError(f"profile already exists: {name}")
            if args.install:
                wrapper_name = args.wrapper_name or default_wrapper_name(name)
                wrapper = args.bin_dir.expanduser().resolve() / wrapper_name
                if wrapper.exists() or wrapper.is_symlink():
                    raise FileExistsError(f"command already exists: {wrapper}")

            includes = manifest["includes"]
            has_conversations = bool(includes.get("conversations"))
            private_store = has_conversations or args.isolated
            store = store_dir(root, name) if private_store else default
            if private_store and store.exists():
                raise FileExistsError(f"conversation store already exists: {store}")

            home = profile_home(root, name)
            created_store = private_store
            try:
                ensure_private_dir(home)
                write_zip_member(archive, "profile/auth.json", home / "auth.json")
                if "profile/config.toml" in archive.namelist():
                    write_zip_member(archive, "profile/config.toml", home / "config.toml")
                else:
                    copy_default_config(home, default)
                force_file_auth_store(home / "config.toml")

                if private_store:
                    ensure_private_dir(store)
                    if has_conversations:
                        restore_conversations(archive, store)
                    ensure_store(store)
                else:
                    ensure_default_shared_paths(store)
                link_shared_paths(home, store)

                payload = {
                    "name": name,
                    "home": str(home),
                    "conversation_store": str(store),
                    "share": "isolated" if private_store else "default",
                    "created_at": utc_now(),
                }
                if not private_store:
                    seed_state_files(home, default)
                save_profile(root, payload)
            except Exception:
                if target_profile.exists():
                    shutil.rmtree(target_profile)
                if created_store and store.exists():
                    shutil.rmtree(store)
                raise

    print(f"Imported profile {name}")
    if has_conversations:
        print("Conversations: restored to a private store")
    elif args.isolated:
        print("Conversations: isolated")
    else:
        print("Conversations: shared with default")

    if args.install:
        install_args = argparse.Namespace(
            manager_root=args.manager_root,
            default_home=args.default_home,
            name=name,
            bin_dir=args.bin_dir,
            wrapper_name=args.wrapper_name,
            codex_binary=args.codex_binary,
            force=False,
        )
        return command_install(install_args)
    return 0


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
        wrapper = wrapper_from_profile(profile)
        wrapper_name = str(wrapper.get("command")) if wrapper else "-"
        print(
            f"{name}\tshare={share}\twrapper={wrapper_name}"
            f"\thome={home}\tstore={store}"
        )
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
    wrapper = wrapper_from_profile(profile)
    if wrapper:
        print(f"Wrapper: {wrapper.get('command')} -> {wrapper.get('path')}")
    else:
        print("Wrapper: not installed")
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


def wrapper_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write_wrapper(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
        temporary.chmod(0o755)
        temporary.replace(path)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def wrapper_matches_record(path: Path, wrapper: dict[str, Any]) -> bool:
    expected = wrapper.get("sha256")
    if not isinstance(expected, str) or not path.is_file() or path.is_symlink():
        return False
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return secrets.compare_digest(actual, expected)


def discover_untracked_wrapper(
    name: str, root: Path, default: Path, bin_dir: Path
) -> dict[str, Any] | None:
    """Recognize an older manager wrapper that predates profile metadata."""
    content = wrapper_script(name, "codex", root, default)
    for command in (default_wrapper_name(name), name):
        path = bin_dir / command
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if path.read_text(encoding="utf-8") != content:
                continue
        except (OSError, UnicodeError):
            continue
        return {
            "command": command,
            "path": str(path),
            "codex_binary": "codex",
            "naming": "default",
            "sha256": wrapper_digest(content),
        }
    return None


def command_install(args: argparse.Namespace) -> int:
    root = manager_root(args)
    default = default_home(args)
    profile = load_profile(root, args.name, default)
    existing_wrapper = wrapper_from_profile(profile)
    if existing_wrapper:
        validate_wrapper_record(existing_wrapper)
    bin_dir = args.bin_dir.expanduser().resolve()
    wrapper_name = args.wrapper_name or default_wrapper_name(args.name)
    target = bin_dir / wrapper_name
    if existing_wrapper:
        existing_path = Path(str(existing_wrapper.get("path", ""))).expanduser().resolve()
        if existing_path != target or not args.force:
            print(
                f"error: profile already has a registered wrapper: {existing_path}",
                file=sys.stderr,
            )
            return 2
    if (target.exists() or target.is_symlink()) and not args.force:
        print(f"error: command already exists: {target}", file=sys.stderr)
        return 2
    content = wrapper_script(args.name, args.codex_binary, root, default)
    write_wrapper(target, content)
    if args.name != "default":
        profile["wrapper"] = {
            "command": wrapper_name,
            "path": str(target),
            "codex_binary": args.codex_binary,
            "naming": "default" if args.wrapper_name is None else "custom",
            "sha256": wrapper_digest(content),
        }
        save_profile(root, profile)
    print(f"Installed {wrapper_name} -> {target}")
    return 0


def command_uninstall(args: argparse.Namespace) -> int:
    root = manager_root(args)
    default = default_home(args)
    profile = load_profile(root, args.name, default)
    wrapper = wrapper_from_profile(profile)
    if wrapper:
        validate_wrapper_record(wrapper)
        registered_name = str(wrapper.get("command", ""))
        if args.wrapper_name is not None and args.wrapper_name != registered_name:
            print(
                f"error: registered wrapper is {registered_name}, not {args.wrapper_name}",
                file=sys.stderr,
            )
            return 2
        target = Path(str(wrapper.get("path", ""))).expanduser().resolve()
        if target.exists() and not args.force and not wrapper_matches_record(target, wrapper):
            print(
                f"error: refusing to remove modified wrapper: {target}",
                file=sys.stderr,
            )
            return 2
    else:
        wrapper_name = args.wrapper_name or default_wrapper_name(args.name)
        target = args.bin_dir.expanduser().resolve() / wrapper_name
        legacy_target = args.bin_dir.expanduser().resolve() / args.name
        if args.wrapper_name is None and not target.exists() and legacy_target.exists():
            target = legacy_target

    if not target.exists() and not target.is_symlink():
        print(f"Nothing to uninstall: {target}")
        if wrapper and args.name != "default":
            profile.pop("wrapper", None)
            save_profile(root, profile)
        return 0
    target.unlink()
    if wrapper and args.name != "default":
        profile.pop("wrapper", None)
        save_profile(root, profile)
    print(f"Removed {target}")
    return 0


def command_rename(args: argparse.Namespace) -> int:
    root = manager_root(args)
    default = default_home(args)
    old_name = args.name
    new_name = args.new_name
    if old_name == "default" or new_name == "default":
        print("error: the default profile cannot be renamed", file=sys.stderr)
        return 2
    if old_name == new_name:
        print(f"Profile is already named {new_name}.")
        return 0

    profile = load_profile(root, old_name, default)
    old_path = profile_dir(root, old_name)
    new_path = profile_dir(root, new_name)
    if new_path.exists():
        print(f"error: profile already exists: {new_name}", file=sys.stderr)
        return 2

    wrapper = wrapper_from_profile(profile)
    if wrapper is None:
        wrapper = discover_untracked_wrapper(
            old_name,
            root,
            default,
            args.bin_dir.expanduser().resolve(),
        )
    old_wrapper_path: Path | None = None
    new_wrapper_path: Path | None = None
    wrapper_content: str | None = None
    if wrapper:
        validate_wrapper_record(wrapper)
        old_wrapper_path = Path(str(wrapper.get("path", ""))).expanduser().resolve()
        if old_wrapper_path.exists() or old_wrapper_path.is_symlink():
            if not wrapper_matches_record(old_wrapper_path, wrapper):
                print(
                    f"error: refusing to update modified wrapper: {old_wrapper_path}",
                    file=sys.stderr,
                )
                return 2
        wrapper_name = str(wrapper.get("command", ""))
        if wrapper.get("naming") == "default":
            wrapper_name = default_wrapper_name(new_name)
            new_wrapper_path = old_wrapper_path.with_name(wrapper_name)
        else:
            new_wrapper_path = old_wrapper_path
        if (
            new_wrapper_path != old_wrapper_path
            and (new_wrapper_path.exists() or new_wrapper_path.is_symlink())
        ):
            print(
                f"error: command already exists: {new_wrapper_path}",
                file=sys.stderr,
            )
            return 2
        codex_binary = str(wrapper.get("codex_binary") or "codex")
        wrapper_content = wrapper_script(new_name, codex_binary, root, default)

    dependents = [
        candidate
        for candidate in iter_managed_profiles(root)
        if candidate.get("name") != old_name and candidate.get("share") == old_name
    ]

    old_path.rename(new_path)
    profile["name"] = new_name
    profile["home"] = str(profile_home(root, new_name))
    if wrapper and new_wrapper_path and wrapper_content is not None:
        profile["wrapper"] = {
            **wrapper,
            "command": new_wrapper_path.name,
            "path": str(new_wrapper_path),
            "sha256": wrapper_digest(wrapper_content),
        }
    save_profile(root, profile)

    for dependent in dependents:
        dependent["share"] = new_name
        save_profile(root, dependent)

    if wrapper and old_wrapper_path and new_wrapper_path and wrapper_content is not None:
        write_wrapper(new_wrapper_path, wrapper_content)
        if new_wrapper_path != old_wrapper_path:
            old_wrapper_path.unlink(missing_ok=True)

    print(f"Renamed profile {old_name} -> {new_name}")
    if wrapper and new_wrapper_path:
        print(f"Wrapper: {new_wrapper_path.name} -> {new_wrapper_path}")
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
    private_store = profile.get("share") == "isolated"
    store_used_elsewhere = store_is_used_by_other_profiles(root, store, name)

    if args.uninstall:
        uninstall_args = argparse.Namespace(
            manager_root=args.manager_root,
            default_home=args.default_home,
            name=name,
            bin_dir=args.bin_dir,
            wrapper_name=args.wrapper_name,
            force=False,
        )
        result = command_uninstall(uninstall_args)
        if result:
            return result

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

    rename = sub.add_parser("rename", help="Rename a managed profile.")
    rename.add_argument("name", type=valid_name, help="Current profile name.")
    rename.add_argument("new_name", type=valid_name, help="New profile name.")
    rename.add_argument("--bin-dir", type=Path, default=Path("~/.local/bin"))
    rename.set_defaults(func=command_rename)

    export = sub.add_parser(
        "export", help="Export file-backed login and profile setup."
    )
    export.add_argument("name", type=valid_name)
    export.add_argument("--output", "-o", type=Path)
    export.add_argument(
        "--include-conversations",
        action="store_true",
        help="Also include portable conversation content.",
    )
    export.set_defaults(func=command_export)

    import_cmd = sub.add_parser("import", help="Import an encrypted profile bundle.")
    import_cmd.add_argument("bundle", type=Path)
    import_cmd.add_argument(
        "--name", type=valid_name, help="Rename the imported profile."
    )
    import_cmd.add_argument(
        "--isolated",
        action="store_true",
        help="Use an empty private conversation store instead of sharing with default.",
    )
    import_cmd.add_argument(
        "--install",
        action="store_true",
        help="Install the profile's wrapper command.",
    )
    import_cmd.add_argument(
        "--wrapper",
        "--command-name",
        dest="wrapper_name",
        type=valid_command_name,
        help="Wrapper name. Default: codex-PROFILE.",
    )
    import_cmd.add_argument("--bin-dir", type=Path, default=Path("~/.local/bin"))
    import_cmd.add_argument("--codex-binary", default="codex")
    import_cmd.set_defaults(func=command_import)

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
    install.add_argument(
        "--wrapper",
        "--command-name",
        dest="wrapper_name",
        type=valid_command_name,
        help="Wrapper name. Default: codex-PROFILE.",
    )
    install.add_argument("--codex-binary", default="codex")
    install.add_argument("--force", action="store_true")
    install.set_defaults(func=command_install)

    uninstall = sub.add_parser("uninstall", help="Remove an installed profile wrapper.")
    uninstall.add_argument("name", type=valid_name)
    uninstall.add_argument("--bin-dir", type=Path, default=Path("~/.local/bin"))
    uninstall.add_argument(
        "--wrapper",
        "--command-name",
        dest="wrapper_name",
        type=valid_command_name,
        help="Legacy untracked wrapper name.",
    )
    uninstall.add_argument(
        "--force", action="store_true", help="Remove a modified wrapper."
    )
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
    remove.add_argument(
        "--wrapper",
        "--command-name",
        dest="wrapper_name",
        type=valid_command_name,
        help="Legacy untracked wrapper name.",
    )
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
    except (ValueError, argparse.ArgumentTypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
