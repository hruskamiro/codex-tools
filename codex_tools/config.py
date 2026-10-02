"""Typed per-user configuration and CLI commands for Codex Tools."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

from codex_tools import codex_exec, paths


@dataclass(frozen=True)
class Setting:
    key: str
    kind: str
    default: Any
    description: str
    choices: tuple[str, ...] = ()


SETTINGS = {
    setting.key: setting
    for setting in (
        Setting("summary.daily.words", "int", 200, "Approximate daily summary length."),
        Setting(
            "summary.daily.format",
            "str",
            "freeform",
            "Daily summary organization.",
            ("freeform", "worklog"),
        ),
        Setting("summary.daily.model", "optional_str", None, "Daily summary model."),
        Setting(
            "summary.daily.reasoning_effort",
            "str",
            "low",
            "Daily summary model reasoning effort.",
            tuple(codex_exec.REASONING_EFFORTS),
        ),
        Setting("summary.daily.timeout_seconds", "int", 300, "Daily model timeout."),
        Setting(
            "summary.daily.max_record_chars",
            "int",
            900,
            "Maximum characters retained from each transcript message.",
        ),
        Setting(
            "summary.daily.max_context_chars",
            "int",
            120_000,
            "Maximum daily context sent to the model.",
        ),
        Setting(
            "summary.daily.prompt_template",
            "optional_path",
            None,
            "Custom daily prompt template path.",
        ),
        Setting("summary.weekly.words", "int", 200, "Approximate weekly summary length."),
        Setting(
            "summary.weekly.format",
            "str",
            "freeform",
            "Weekly summary organization.",
            ("freeform", "worklog"),
        ),
        Setting("summary.weekly.model", "optional_str", None, "Weekly summary model."),
        Setting(
            "summary.weekly.reasoning_effort",
            "str",
            "low",
            "Weekly summary model reasoning effort.",
            tuple(codex_exec.REASONING_EFFORTS),
        ),
        Setting("summary.weekly.timeout_seconds", "int", 300, "Weekly model timeout."),
        Setting(
            "summary.weekly.max_record_chars",
            "int",
            900,
            "Maximum transcript-message length used while refreshing daily notes.",
        ),
        Setting(
            "summary.weekly.max_context_chars",
            "int",
            120_000,
            "Maximum weekly context sent to the model.",
        ),
        Setting(
            "summary.weekly.refresh_dailies",
            "str",
            "auto",
            "Daily-summary refresh policy used by weekly summaries.",
            ("auto", "missing", "all", "none"),
        ),
        Setting(
            "summary.weekly.prompt_template",
            "optional_path",
            None,
            "Custom weekly prompt template path.",
        ),
        Setting(
            "viewer.default_view",
            "str",
            "markdown",
            "Initial conversation view.",
            ("markdown", "latex"),
        ),
        Setting(
            "viewer.typeset_code_mode",
            "str",
            "auto",
            "LaTeX code-block renderer.",
            ("auto", "pygments", "verbatim"),
        ),
    )
}


def _nested_value(data: dict[str, Any], key: str) -> tuple[bool, Any]:
    current: Any = data
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _set_nested(data: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    current = data
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"configuration path is not a table: {part}")
        current = child
    current[parts[-1]] = value


def _unset_nested(data: dict[str, Any], key: str) -> None:
    parts = key.split(".")
    stack: list[tuple[dict[str, Any], str]] = []
    current = data
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            return
        stack.append((current, part))
        current = child
    current.pop(parts[-1], None)
    for parent, part in reversed(stack):
        child = parent.get(part)
        if isinstance(child, dict) and not child:
            parent.pop(part)


def parse_value(setting: Setting, value: Any) -> Any:
    if setting.kind == "int":
        if isinstance(value, bool):
            raise ValueError(f"{setting.key} must be a positive integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{setting.key} must be a positive integer") from exc
        if parsed <= 0:
            raise ValueError(f"{setting.key} must be a positive integer")
        return parsed
    if setting.kind in {"str", "optional_str", "optional_path"}:
        if value is None and setting.kind.startswith("optional"):
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{setting.key} must be a nonempty string")
        parsed = value.strip()
        if setting.choices and parsed not in setting.choices:
            choices = ", ".join(setting.choices)
            raise ValueError(f"{setting.key} must be one of: {choices}")
        return parsed
    raise ValueError(f"unsupported configuration type for {setting.key}")


def _leaf_keys(data: Any, prefix: str = "") -> list[str]:
    if not isinstance(data, dict):
        return [prefix.rstrip(".")]
    keys: list[str] = []
    for key, value in data.items():
        child = f"{prefix}{key}"
        if isinstance(value, dict):
            keys.extend(_leaf_keys(value, child + "."))
        else:
            keys.append(child)
    return keys


def validate_config(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("configuration root must be a TOML table")
    allowed_top = {"version", "summary", "viewer"}
    unknown_top = set(data) - allowed_top
    if unknown_top:
        raise ValueError(f"unknown configuration sections: {sorted(unknown_top)}")
    version = data.get("version", 1)
    if version != 1:
        raise ValueError(f"unsupported configuration version: {version!r}")
    for key in _leaf_keys({k: v for k, v in data.items() if k != "version"}):
        setting = SETTINGS.get(key)
        if setting is None:
            raise ValueError(f"unknown configuration key: {key}")
        found, value = _nested_value(data, key)
        if found:
            parse_value(setting, value)
    return data


def read_config(config_path: Path | None = None) -> dict[str, Any]:
    path = config_path or paths.CONFIG_FILE
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {"version": 1}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"could not read configuration {path}: {exc}") from exc
    return validate_config(data)


def _legacy_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def legacy_value(key: str) -> tuple[bool, Any]:
    if key in {"summary.daily.model", "summary.weekly.model"}:
        value = _legacy_json(paths.SUMMARY_CONFIG_FILE).get("default_model")
        if isinstance(value, str) and value.strip():
            return True, value.strip()
    if key == "viewer.default_view":
        value = _legacy_json(paths.VIEWER_CONFIG_FILE).get("default_view")
        if value in {"markdown", "latex"}:
            return True, value
    return False, None


def _config_with_legacy_values() -> dict[str, Any]:
    data: dict[str, Any] = {"version": 1}
    for key in SETTINGS:
        found, selected = legacy_value(key)
        if found:
            _set_nested(data, key, selected)
    return data


def resolve(key: str, config_path: Path | None = None) -> tuple[Any, str]:
    setting = SETTINGS[key]
    data = read_config(config_path)
    found, value = _nested_value(data, key)
    if found:
        return parse_value(setting, value), "user config"
    if config_path is None and not paths.CONFIG_FILE.exists():
        found, value = legacy_value(key)
        if found:
            return parse_value(setting, value), "legacy config"
    return setting.default, "built-in"


def value(key: str, config_path: Path | None = None) -> Any:
    return resolve(key, config_path)[0]


def section(name: str, config_path: Path | None = None) -> dict[str, Any]:
    prefix = name + "."
    return {
        key.removeprefix(prefix): value(key, config_path)
        for key in SETTINGS
        if key.startswith(prefix)
    }


def display_value(setting: Setting, selected: Any) -> str:
    if selected is None and setting.key.endswith(".model"):
        return "Codex profile default"
    if selected is None:
        return "not set"
    return str(selected)


def default_help(key: str) -> str:
    selected, source = resolve(key)
    return f"{display_value(SETTINGS[key], selected)} ({source})"


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise ValueError(f"unsupported TOML value: {value!r}")


def render_toml(data: dict[str, Any]) -> str:
    lines = [f"version = {int(data.get('version', 1))}"]
    for section_name in ("summary.daily", "summary.weekly", "viewer"):
        found, table = _nested_value(data, section_name)
        if not found or not isinstance(table, dict) or not table:
            continue
        lines.extend(["", f"[{section_name}]"])
        for key in sorted(table):
            lines.append(f"{key} = {_toml_scalar(table[key])}")
    return "\n".join(lines) + "\n"


def write_config(data: dict[str, Any], config_path: Path | None = None) -> None:
    path = config_path or paths.CONFIG_FILE
    validate_config(data)
    paths.ensure_private_dir(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(render_toml(data))
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def set_value(key: str, raw_value: Any, config_path: Path | None = None) -> Any:
    setting = SETTINGS.get(key)
    if setting is None:
        raise ValueError(f"unknown configuration key: {key}")
    selected = parse_value(setting, raw_value)
    data = (
        _config_with_legacy_values()
        if config_path is None and not paths.CONFIG_FILE.exists()
        else read_config(config_path)
    )
    _set_nested(data, key, selected)
    write_config(data, config_path)
    return selected


def unset_value(key: str, config_path: Path | None = None) -> None:
    if key not in SETTINGS:
        raise ValueError(f"unknown configuration key: {key}")
    data = (
        _config_with_legacy_values()
        if config_path is None and not paths.CONFIG_FILE.exists()
        else read_config(config_path)
    )
    _unset_nested(data, key)
    write_config(data, config_path)


def show_config() -> None:
    print(f"Configuration: {paths.CONFIG_FILE}")
    print()
    width = max(len(key) for key in SETTINGS)
    for key, setting in SETTINGS.items():
        selected, source = resolve(key)
        print(f"{key:<{width}}  {display_value(setting, selected):<24}  {source}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-tools config",
        description="Inspect and change per-user Codex Tools defaults.",
    )
    commands = parser.add_subparsers(dest="config_command")
    commands.add_parser("show", help="Show effective values and their sources.")
    commands.add_parser("path", help="Print the user configuration path.")
    commands.add_parser("validate", help="Validate the user configuration file.")
    set_cmd = commands.add_parser("set", help="Set a typed configuration value.")
    set_cmd.add_argument("key", choices=tuple(SETTINGS))
    set_cmd.add_argument("value")
    unset_cmd = commands.add_parser("unset", help="Restore a built-in or legacy default.")
    unset_cmd.add_argument("key", choices=tuple(SETTINGS))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.config_command or "show"
    try:
        if command == "show":
            show_config()
        elif command == "path":
            print(paths.CONFIG_FILE)
        elif command == "validate":
            read_config()
            print(f"Configuration is valid: {paths.CONFIG_FILE}")
        elif command == "set":
            selected = set_value(args.key, args.value)
            print(f"{args.key} = {display_value(SETTINGS[args.key], selected)}")
        elif command == "unset":
            unset_value(args.key)
            selected, source = resolve(args.key)
            print(
                f"{args.key} = {display_value(SETTINGS[args.key], selected)} "
                f"({source})"
            )
    except ValueError as exc:
        parser.error(str(exc))
    return 0
