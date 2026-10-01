"""Shared support for model-backed summary commands."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import ValidationError, validators

from codex_tools import codex_exec, manager, paths


TRUNCATION_MARKER = "\n\n_Context truncated by --max-context-chars._\n\n"
DEFAULT_SUMMARY_WORDS = 200
DEFAULT_SUMMARY_FORMAT = "freeform"
SUMMARY_FORMATS = ("freeform", "worklog")
SUMMARY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary_markdown"],
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "summary_markdown": {"type": "string", "minLength": 1},
    },
}


def read_default_model(config_path: Path | None = None) -> str | None:
    path = config_path or paths.SUMMARY_CONFIG_FILE
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        value = config.get("default_model")
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def write_default_model(model: str | None, config_path: Path | None = None) -> None:
    path = config_path or paths.SUMMARY_CONFIG_FILE
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            config = {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        config = {}
    if model is None:
        config.pop("default_model", None)
    else:
        selected = model.strip()
        if not selected:
            raise ValueError("summary model cannot be empty")
        config["default_model"] = selected
    paths.ensure_private_dir(path.parent)
    paths.write_private_text(path, json.dumps(config, indent=2, sort_keys=True) + "\n")


def default_model_label() -> str:
    return read_default_model() or "Codex profile default"


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def format_instructions(kind: str, summary_format: str) -> str:
    if summary_format == "freeform":
        return (
            "Choose the organization that best fits the material. Use paragraphs, "
            "headings, or bullets only when they improve readability; do not force "
            "the content into fixed sections."
        )
    if summary_format == "worklog":
        sections = (
            "Main Work, Smaller Items, and Open Threads"
            if kind == "daily"
            else "Main Themes, Notable Details, and Open Threads"
        )
        return f"Organize the summary under these Markdown sections: {sections}."
    raise ValueError(f"unknown summary format: {summary_format}")


def structured_prompt(prompt: str) -> str:
    return (
        prompt.rstrip()
        + "\n\nReturn a JSON object matching the supplied schema. The title must not "
        "include Markdown heading markers. The summary_markdown value must contain "
        "the summary body only, without a top-level title. Do not use tools.\n"
    )


def _zoneinfo_name_from_path(path: Path) -> str | None:
    try:
        resolved = path.resolve()
    except OSError:
        return None
    parts = resolved.parts
    try:
        marker = parts.index("zoneinfo")
    except ValueError:
        return None
    name = "/".join(parts[marker + 1 :])
    return name or None


def _system_timezone_candidates() -> list[str]:
    candidates = []
    configured = os.environ.get("TZ", "").strip().removeprefix(":")
    if configured:
        if configured.startswith("/"):
            configured = _zoneinfo_name_from_path(Path(configured)) or ""
        if configured:
            candidates.append(configured)

    try:
        configured = Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except OSError:
        configured = ""
    if configured:
        candidates.append(configured)

    localtime_name = _zoneinfo_name_from_path(Path("/etc/localtime"))
    if localtime_name:
        candidates.append(localtime_name)

    local = datetime.now().astimezone().tzinfo
    local_key = getattr(local, "key", "")
    if local_key:
        candidates.append(str(local_key))
    return candidates


def detect_local_timezone() -> str:
    """Return the best available IANA timezone name, falling back to UTC."""
    for candidate in _system_timezone_candidates():
        try:
            ZoneInfo(candidate)
        except (ValueError, ZoneInfoNotFoundError):
            continue
        return candidate
    return "UTC"


def truncate_context(value: str, limit: int) -> str:
    """Limit context while retaining both initial framing and newest material."""
    if len(value) <= limit:
        return value
    available = max(0, limit - len(TRUNCATION_MARKER))
    head_size = available // 2
    tail_size = available - head_size
    head = value[:head_size].rstrip()
    tail = value[-tail_size:].lstrip() if tail_size else ""
    return head + TRUNCATION_MARKER + tail


def run_codex_exec(args: argparse.Namespace, prompt: str) -> str:
    """Run a constrained structured Codex turn and render its Markdown result."""
    validator_class = validators.validator_for(SUMMARY_SCHEMA)
    validator_class.check_schema(SUMMARY_SCHEMA)
    with tempfile.TemporaryDirectory(prefix="codex-tools-summary-") as temporary:
        root = Path(temporary)
        schema_path = root / "schema.json"
        response_path = root / "response.json"
        events_path = root / "events.jsonl"
        stderr_path = root / "stderr.log"
        schema_path.write_text(
            json.dumps(SUMMARY_SCHEMA, ensure_ascii=False), encoding="utf-8"
        )
        command = codex_exec.build_command(
            codex_bin=args.codex_bin,
            model=getattr(args, "model", None),
            reasoning_effort=getattr(args, "reasoning_effort", "low"),
            schema_path=schema_path,
            response_path=response_path,
        )
        try:
            execution = codex_exec.run_exec(
                prompt=prompt,
                command=command,
                cwd=Path.cwd(),
                events_path=events_path,
                stderr_path=stderr_path,
                timeout=getattr(args, "timeout", 300),
                env=manager.profile_environment(
                    args.profile, args.manager_root, args.default_home
                ),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"codex exec exceeded the {getattr(args, 'timeout', 300)}-second timeout"
            ) from exc
        if execution.returncode != 0:
            raise RuntimeError(
                "codex exec failed with exit code "
                f"{execution.returncode}\n\nSTDERR:\n{execution.stderr}"
            )
        if execution.event_parse_errors:
            raise RuntimeError("codex exec emitted non-JSON event output")
        if codex_exec.tool_events(execution.events):
            raise RuntimeError("summary task attempted to use tools")
        if not response_path.exists():
            raise RuntimeError("codex exec did not write a final response")
        try:
            result = json.loads(response_path.read_text(encoding="utf-8"))
            validator_class(SUMMARY_SCHEMA).validate(result)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise RuntimeError(f"invalid structured summary response: {exc}") from exc

    title = result["title"].strip().lstrip("#").strip()
    body = result["summary_markdown"].strip()
    if not title or not body:
        raise RuntimeError("structured summary response contained empty text")
    return f"# {title}\n\n{body}"
