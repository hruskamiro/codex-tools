"""Run reproducible, schema-constrained Codex tasks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError

from jsonschema import SchemaError, ValidationError, validators

from codex_tools import app_server, codex_exec, manager
from codex_tools.batch_backoff import RateLimitBackoff, rate_limit_reason


BACKENDS = ("exec", "app-server")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-tools structured",
        description="Run reproducible, schema-constrained Codex tasks.",
    )
    commands = parser.add_subparsers(dest="structured_command", required=True)
    commands.add_parser(
        "connection",
        add_help=False,
        help="Manage ChatGPT-plan connections for the app-server backend.",
    )
    run = commands.add_parser("run", help="Run one prompt under a JSON Schema.")
    run.add_argument("--prompt", required=True, help="Prompt file, or '-' for stdin.")
    run.add_argument("--schema", required=True, type=Path, help="JSON Schema file.")
    run.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        help="New directory for all reproducibility artifacts.",
    )
    run.add_argument("--model", required=True, help="Codex model name.")
    run.add_argument(
        "--reasoning-effort",
        default="medium",
        choices=codex_exec.REASONING_EFFORTS,
    )
    run.add_argument("--timeout", type=int, default=1800, help="Timeout in seconds.")
    run.add_argument("--codex-bin", default="codex", help="Codex executable.")
    add_backend_arguments(run)
    batch = commands.add_parser(
        "batch", help="Run independent structured tasks, optionally in parallel."
    )
    batch.add_argument("manifest", type=Path, help="Versioned JSON batch manifest.")
    batch.add_argument("--batch-dir", required=True, type=Path)
    batch.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Maximum concurrent Codex tasks. Default: 1.",
    )
    batch.add_argument(
        "--idxs",
        type=int,
        nargs=2,
        metavar=("START", "STOP"),
        help="Run the zero-based half-open task range START:STOP.",
    )
    batch.add_argument("--model", help="Override the model for all selected tasks.")
    batch.add_argument(
        "--reasoning-effort",
        choices=codex_exec.REASONING_EFFORTS,
        help="Override the reasoning effort for all selected tasks.",
    )
    batch.add_argument(
        "--timeout",
        type=int,
        help="Override the timeout in seconds for all selected tasks.",
    )
    batch.add_argument("--codex-bin", default="codex", help="Codex executable.")
    batch.add_argument("--admission-deadline", help="UTC-offset ISO timestamp: stop new requests, let active calls finish.")
    batch.add_argument("--rate-limit-backoff", type=float, nargs="?", const=60.0, default=60.0,
                       help="Initial cooldown after provider limits/errors, in seconds. Default: 60; adaptive pacing is enabled by default.")
    batch.add_argument("--fixed", action="store_true",
                       help="Keep --jobs concurrency fixed; provider cooldowns still apply.")
    batch.add_argument("--backoff-decrease", metavar="RULE",
                       help="Concurrency decrease: factor (0.5x) or integer step (-1). Default: 0.5x.")
    batch.add_argument("--backoff-increase", metavar="RULE",
                       help="Recovery increase: integer step (+1) or factor (1.25x). Default: +1.")
    batch.add_argument("--backoff-recovery-period", type=float, metavar="SECONDS",
                       help="Healthy period before each increase, also requiring three successful tasks. Default: 30.")
    batch.add_argument("--backoff-state", type=Path, help="Share backoff state across sequential batches.")
    add_backend_arguments(batch)
    batch.add_argument(
        "--usage-codex-home", type=Path,
        default=app_server.DEFAULT_USAGE_CODEX_HOME,
        help=("Authenticated Codex home used for account-wide usage snapshots. "
              f"Default: {app_server.DEFAULT_USAGE_CODEX_HOME}"),
    )
    check = commands.add_parser(
        "check", help="Verify an individual run or complete batch directory."
    )
    check.add_argument("path", type=Path, help="Run or batch directory.")
    return parser.parse_args(argv)


def add_backend_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend", choices=BACKENDS, default="exec",
        help="Execution backend. Default: exec.",
    )
    parser.add_argument(
        "--profile",
        type=manager.valid_name,
        help="codex-manager profile for the exec backend. Default: default.",
    )
    parser.add_argument(
        "--connection",
        type=app_server.valid_name,
        help="ChatGPT connection for the app-server backend. Default: default.",
    )
    parser.add_argument(
        "--manager-root",
        type=Path,
        default=manager.DEFAULT_MANAGER_ROOT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--default-home",
        type=Path,
        default=manager.DEFAULT_CODEX_HOME,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--connection-root",
        type=Path,
        default=app_server.DEFAULT_ROOT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--app-server-home",
        type=Path,
        default=app_server.DEFAULT_CODEX_HOME,
        help=argparse.SUPPRESS,
    )


def resolve_backend_auth(
    backend: str, profile: str | None, connection: str | None
) -> tuple[str | None, str | None]:
    if backend == "exec":
        if connection is not None:
            raise ValueError("--connection is only valid with --backend app-server")
        return profile or "default", None
    if backend == "app-server":
        if profile is not None:
            raise ValueError("--profile is only valid with --backend exec")
        return None, connection or "default"
    raise ValueError(f"unsupported structured backend: {backend}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_write_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare_run_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.exists() and any(resolved.iterdir()):
        raise FileExistsError(f"run directory is not empty: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def read_prompt(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).expanduser().read_text(encoding="utf-8")


def run_task(
    *,
    prompt: str,
    schema: dict,
    run_dir: Path,
    model: str,
    reasoning_effort: str = "medium",
    timeout: int = 1800,
    codex_bin: str = "codex",
    backend: str = "exec",
    profile: str | None = None,
    connection: str | None = None,
    manager_root: Path = manager.DEFAULT_MANAGER_ROOT,
    default_home: Path = manager.DEFAULT_CODEX_HOME,
    connection_root: Path = app_server.DEFAULT_ROOT,
    app_server_home: Path = app_server.DEFAULT_CODEX_HOME,
    on_event: Callable[[dict], None] | None = None,
) -> dict:
    validator_class = validators.validator_for(schema)
    validator_class.check_schema(schema)
    run_dir = prepare_run_dir(run_dir)
    prompt_path = run_dir / "prompt.txt"
    schema_path = run_dir / "schema.json"
    response_path = run_dir / "response.txt"
    events_path = run_dir / "events.jsonl"
    stderr_path = run_dir / "stderr.log"
    result_path = run_dir / "result.json"
    run_path = run_dir / "run.json"
    profile, connection = resolve_backend_auth(backend, profile, connection)
    codex_home = (
        manager.resolve_profile_home(profile, manager_root, default_home)
        if backend == "exec"
        else app_server_home.expanduser().resolve()
    )

    atomic_write_text(prompt_path, prompt)
    atomic_write_json(schema_path, schema)
    command = (
        codex_exec.build_command(
            codex_bin=codex_bin, model=model,
            reasoning_effort=reasoning_effort, schema_path=schema_path,
            response_path=response_path,
        )
        if backend == "exec"
        else app_server.app_server_command(codex_bin)
    )
    metadata = {
        "status": "running",
        "backend": backend,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "timeout": timeout,
        **({"profile": profile} if profile is not None else {"connection": connection}),
        "codex_home": str(codex_home),
        "prompt_sha256": sha256_text(prompt),
        "schema_sha256": sha256_text(canonical_json(schema)),
        "started_at": utc_now(),
        "command": command,
    }
    atomic_write_json(run_path, metadata)

    try:
        if backend == "exec":
            execution = codex_exec.run_exec(
                prompt=prompt, command=command, cwd=run_dir,
                events_path=events_path, stderr_path=stderr_path, timeout=timeout,
                env=manager.profile_environment(profile, manager_root, default_home),
                **({"event_callback": on_event} if on_event is not None else {}),
            )
            usage = codex_exec.event_usage(execution.events)
            thread_id = codex_exec.event_thread_id(execution.events)
            event_parse_errors = execution.event_parse_errors
            tool_events = codex_exec.tool_events(execution.events)
        else:
            token = app_server.access_token(
                connection,
                connection_root,
                timeout + app_server.TOKEN_EXPIRY_MARGIN_SECONDS,
            )
            app_execution = app_server.run_structured_task(
                prompt=prompt, schema=schema, model=model,
                reasoning_effort=reasoning_effort, timeout=timeout,
                access_token_value=token, codex_bin=codex_bin,
                codex_home=app_server_home,
                **({"on_event": on_event} if on_event is not None else {}),
            )
            atomic_write_text(
                events_path,
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in app_execution.events),
            )
            atomic_write_text(stderr_path, app_execution.stderr)
            atomic_write_text(response_path, app_execution.response_text)
            execution = app_execution
            usage = app_execution.usage
            thread_id = app_execution.thread_id
            event_parse_errors = app_execution.parse_errors
            tool_events = app_server.app_server_tool_events(app_execution.events)
        metadata.update(
            {
                "returncode": execution.returncode,
                "duration_s": execution.duration_s,
                "usage": usage,
                "thread_id": thread_id,
                "event_parse_errors": event_parse_errors,
                "tool_events": tool_events,
            }
        )
        if execution.returncode != 0:
            if on_event is not None:
                reason = rate_limit_reason({"error": execution.stderr[-8192:]})
                if reason:
                    on_event({"error": reason})
            raise RuntimeError(f"codex exec exited with status {execution.returncode}")
        if event_parse_errors:
            raise RuntimeError("Codex emitted non-JSON event output")
        if metadata["tool_events"]:
            raise RuntimeError("structured task attempted to use tools")
        if not response_path.exists():
            raise RuntimeError("Codex did not write a final response")

        response_text = response_path.read_text(encoding="utf-8").strip()
        result = json.loads(response_text)
        validator_class(schema).validate(result)
        atomic_write_json(result_path, result)
        metadata["status"] = "success"
        return result
    except subprocess.TimeoutExpired:
        metadata["status"] = "timeout"
        metadata["error"] = f"Codex exceeded the {timeout}-second timeout"
        raise RuntimeError(metadata["error"])
    except (json.JSONDecodeError, ValidationError, OSError, RuntimeError) as exc:
        metadata["status"] = "failed"
        metadata["error"] = str(exc)
        raise
    finally:
        metadata["finished_at"] = utc_now()
        atomic_write_json(run_path, metadata)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_batch_manifest(
    manifest: Any,
    manifest_dir: Path,
    *,
    model_override: str | None = None,
    reasoning_effort_override: str | None = None,
    timeout_override: int | None = None,
) -> list[dict]:
    if not isinstance(manifest, dict):
        raise ValueError("batch manifest must be a JSON object")
    unknown = set(manifest) - {"version", "defaults", "tasks"}
    if unknown:
        raise ValueError(f"unknown batch manifest fields: {sorted(unknown)}")
    if manifest.get("version") != 1:
        raise ValueError("batch manifest version must be 1")

    defaults = manifest.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("batch defaults must be an object")
    unknown_defaults = set(defaults) - {"model", "reasoning_effort", "timeout"}
    if unknown_defaults:
        raise ValueError(f"unknown batch default fields: {sorted(unknown_defaults)}")

    raw_tasks = manifest.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("batch tasks must be a nonempty array")

    allowed_task_fields = {
        "id",
        "prompt",
        "schema",
        "model",
        "reasoning_effort",
        "timeout",
        "metadata",
    }
    task_ids = set()
    tasks = []
    for index, raw in enumerate(raw_tasks):
        if not isinstance(raw, dict):
            raise ValueError(f"batch task {index} must be an object")
        unknown_task = set(raw) - allowed_task_fields
        if unknown_task:
            raise ValueError(
                f"batch task {index} has unknown fields: {sorted(unknown_task)}"
            )
        task_id = raw.get("id")
        if not isinstance(task_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", task_id
        ):
            raise ValueError(
                f"batch task {index} id must use letters, numbers, '.', '_' or '-'"
            )
        if task_id in task_ids:
            raise ValueError(f"duplicate batch task id: {task_id}")
        task_ids.add(task_id)

        prompt_value = raw.get("prompt")
        schema_value = raw.get("schema")
        if not isinstance(prompt_value, str) or not prompt_value:
            raise ValueError(f"batch task {task_id} requires a prompt path")
        if not isinstance(schema_value, str) or not schema_value:
            raise ValueError(f"batch task {task_id} requires a schema path")
        prompt_path = (manifest_dir / prompt_value).expanduser().resolve()
        schema_path = (manifest_dir / schema_value).expanduser().resolve()
        prompt = prompt_path.read_text(encoding="utf-8")
        schema = load_json(schema_path)
        validator_class = validators.validator_for(schema)
        validator_class.check_schema(schema)

        model = model_override or raw.get("model", defaults.get("model"))
        if not isinstance(model, str) or not model:
            raise ValueError(f"batch task {task_id} requires a model")
        reasoning_effort = reasoning_effort_override or raw.get(
            "reasoning_effort", defaults.get("reasoning_effort", "medium")
        )
        if reasoning_effort not in codex_exec.REASONING_EFFORTS:
            raise ValueError(
                f"batch task {task_id} has invalid reasoning effort: {reasoning_effort}"
            )
        timeout = (
            timeout_override
            if timeout_override is not None
            else raw.get("timeout", defaults.get("timeout", 1800))
        )
        if not isinstance(timeout, int) or timeout <= 0:
            raise ValueError(f"batch task {task_id} timeout must be a positive integer")
        metadata = raw.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError(f"batch task {task_id} metadata must be an object")

        tasks.append(
            {
                "id": task_id,
                "prompt": prompt,
                "schema": schema,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "timeout": timeout,
                "metadata": metadata,
            }
        )
    return tasks


def reusable_success(run_dir: Path, task: dict) -> bool:
    try:
        metadata = load_json(run_dir / "run.json")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    metadata_matches = (
        metadata.get("status") == "success"
        and metadata.get("backend") == task["backend"]
        and metadata.get("model") == task["model"]
        and metadata.get("reasoning_effort") == task["reasoning_effort"]
        and metadata.get("profile") == task.get("profile")
        and metadata.get("connection") == task.get("connection")
        and metadata.get("prompt_sha256") == sha256_text(task["prompt"])
        and metadata.get("schema_sha256")
        == sha256_text(canonical_json(task["schema"]))
    )
    return metadata_matches and check_run(run_dir)["ok"]


def summed_usage(task_records: list[dict]) -> dict:
    total: dict[str, int] = {}
    for record in task_records:
        usage = record.get("usage")
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total


def run_batch(
    *,
    manifest: dict,
    manifest_path: Path,
    batch_dir: Path,
    jobs: int = 1,
    idxs: tuple[int, int] | None = None,
    model_override: str | None = None,
    reasoning_effort_override: str | None = None,
    timeout_override: int | None = None,
    codex_bin: str = "codex",
    backend: str = "exec",
    profile: str | None = None,
    connection: str | None = None,
    manager_root: Path = manager.DEFAULT_MANAGER_ROOT,
    default_home: Path = manager.DEFAULT_CODEX_HOME,
    connection_root: Path = app_server.DEFAULT_ROOT,
    app_server_home: Path = app_server.DEFAULT_CODEX_HOME,
    usage_codex_home: Path = app_server.DEFAULT_USAGE_CODEX_HOME,
    rate_limit_backoff: float = 60.0,
    backoff_state: Path | None = None,
    admission_deadline: str | None = None,
    fixed: bool = False,
    backoff_decrease: str | None = None,
    backoff_increase: str | None = None,
    backoff_recovery_period: float | None = None,
) -> dict:
    deadline = None
    if admission_deadline is not None:
        parsed = datetime.fromisoformat(admission_deadline.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("--admission-deadline must include a UTC offset")
        deadline = parsed.timestamp()
    if jobs <= 0:
        raise ValueError("--jobs must be a positive integer")
    if fixed and any(value is not None for value in (
        backoff_decrease, backoff_increase, backoff_recovery_period,
    )):
        raise ValueError("--fixed cannot be combined with adaptive concurrency tuning")
    state_path = (backoff_state or batch_dir / "backoff-state.json").expanduser().resolve()
    if (state_path in {(batch_dir / "manifest.json").expanduser().resolve(),
                       (batch_dir / "summary.json").expanduser().resolve()}
            or state_path.is_relative_to((batch_dir / "tasks").expanduser().resolve())):
        raise ValueError("--backoff-state must not replace batch artifacts")
    initial = load_json(state_path) if state_path.exists() else None
    # Validate policy and persisted state before creating batch artifacts.
    backoff = RateLimitBackoff(
        jobs, rate_limit_backoff, fixed=fixed,
        decrease=backoff_decrease if backoff_decrease is not None else "0.5x",
        increase=backoff_increase if backoff_increase is not None else "+1",
        recovery_period=backoff_recovery_period if backoff_recovery_period is not None else 30.0,
        initial=initial, save=lambda value: atomic_write_json(state_path, value),
    )
    manifest_path = manifest_path.expanduser().resolve()
    if model_override is not None and not model_override:
        raise ValueError("--model cannot be empty")
    if timeout_override is not None and timeout_override <= 0:
        raise ValueError("--timeout must be a positive integer")
    tasks = validate_batch_manifest(
        manifest,
        manifest_path.parent,
        model_override=model_override,
        reasoning_effort_override=reasoning_effort_override,
        timeout_override=timeout_override,
    )
    profile, connection = resolve_backend_auth(backend, profile, connection)
    if backend == "exec":
        manager.resolve_profile_home(profile, manager_root, default_home)
    else:
        app_server.access_token(connection, connection_root)
    for task in tasks:
        task["backend"] = backend
        task["profile"] = profile
        task["connection"] = connection
    if idxs is None:
        selection_start, selection_stop = 0, len(tasks)
    else:
        selection_start, selection_stop = idxs
        if (
            selection_start < 0
            or selection_stop <= selection_start
            or selection_stop > len(tasks)
        ):
            raise ValueError(
                f"--idxs must satisfy 0 <= START < STOP <= {len(tasks)}"
            )
    batch_dir = batch_dir.expanduser().resolve()
    batch_dir.mkdir(parents=True, exist_ok=True)
    tasks_dir = batch_dir / "tasks"
    stored_manifest_path = batch_dir / "manifest.json"
    summary_path = batch_dir / "summary.json"
    previous_subscription = None
    if summary_path.exists():
        try:
            previous_summary = load_json(summary_path)
            if isinstance(previous_summary, dict):
                previous_subscription = previous_summary.get("subscription_usage")
        except (json.JSONDecodeError, OSError):
            pass

    if stored_manifest_path.exists():
        stored_manifest = load_json(stored_manifest_path)
        if canonical_json(stored_manifest) != canonical_json(manifest):
            raise ValueError(
                f"batch directory contains a different manifest: {batch_dir}"
            )
    elif any(batch_dir.iterdir()):
        raise FileExistsError(
            f"batch directory is nonempty and has no manifest: {batch_dir}"
        )
    else:
        atomic_write_json(stored_manifest_path, manifest)
    tasks_dir.mkdir(exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(state_path, backoff.snapshot())

    started_at = utc_now()
    records: dict[str, dict] = {}
    pending = []
    for index, task in enumerate(tasks):
        task_dir = tasks_dir / task["id"]
        selected = selection_start <= index < selection_stop
        record = {
            "id": task["id"],
            "index": index,
            "status": "queued",
            "selected": selected,
            "run_dir": str(task_dir.relative_to(batch_dir)),
            "model": task["model"],
            "reasoning_effort": task["reasoning_effort"],
            "backend": backend,
            **({"profile": profile} if profile is not None else {"connection": connection}),
        }
        if task["metadata"] is not None:
            record["metadata"] = task["metadata"]
        if reusable_success(task_dir, task):
            run_metadata = load_json(task_dir / "run.json")
            record["status"] = "skipped" if selected else "existing"
            record["usage"] = run_metadata.get("usage")
        elif not selected:
            record["status"] = "not-selected"
        elif task_dir.exists() and any(task_dir.iterdir()):
            record["status"] = "incomplete"
            record["error"] = "existing task run is not reusable; it was not retried"
        else:
            pending.append(task)
        records[task["id"]] = record

    def execute(task: dict) -> tuple[str, dict]:
        task_id = task["id"]
        task_dir = tasks_dir / task_id
        try:
            admission = backoff.begin()
            run_task(
                prompt=task["prompt"],
                schema=task["schema"],
                run_dir=task_dir,
                model=task["model"],
                reasoning_effort=task["reasoning_effort"],
                timeout=task["timeout"],
                codex_bin=codex_bin,
                backend=backend,
                profile=profile,
                connection=connection,
                manager_root=manager_root,
                default_home=default_home,
                connection_root=connection_root,
                app_server_home=app_server_home,
                on_event=lambda event: backoff.event(task_id, event),
            )
            run_metadata = load_json(task_dir / "run.json")
            backoff.success(task_id, admission)
            return task_id, {
                "status": "success",
                "executed": True,
                "usage": run_metadata.get("usage"),
                "duration_s": run_metadata.get("duration_s"),
            }
        except Exception as exc:
            usage = None
            failure_metadata = {}
            run_metadata_path = task_dir / "run.json"
            if run_metadata_path.exists():
                try:
                    failure_metadata = load_json(run_metadata_path)
                    usage = failure_metadata.get("usage")
                except (json.JSONDecodeError, OSError):
                    pass
            # Validation errors may quote model output containing "HTTP 429";
            # only execution errors and actual HTTP failures are provider signals.
            reason = rate_limit_reason({"error": str(exc)}) if isinstance(exc, RuntimeError) else None
            if isinstance(exc, HTTPError) and exc.code in {429, 502, 503, 504}:
                reason = f"HTTP {exc.code}"
            if (reason or isinstance(exc, (TimeoutError, subprocess.TimeoutExpired))
                    or failure_metadata.get("status") == "timeout"):
                backoff.signal(task_id, reason or "request-timeout")
            return task_id, {
                "status": "failed",
                "executed": True,
                "error": str(exc),
                "usage": usage,
            }

    subscription_before = None
    subscription_after = None
    subscription_errors = []
    if backend == "app-server" and pending:
        try:
            subscription_before = app_server.rate_limits(
                min(timeout_override or 120, 120), codex_bin, usage_codex_home,
            )
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            subscription_errors.append(f"before: {exc}")

    def subscription_usage() -> dict | None:
        if backend != "app-server":
            return None
        if not pending and isinstance(previous_subscription, dict) and previous_subscription.get(
            "measured"
        ):
            return {**previous_subscription, "preserved_from_previous_run": True}
        return {
            "attempted": bool(pending),
            "measured": (
                isinstance(subscription_before, dict)
                and isinstance(subscription_after, dict)
            ),
            "before": subscription_before,
            "after": subscription_after,
            "delta": app_server.rate_limit_delta(
                subscription_before, subscription_after
            ),
            "errors": subscription_errors,
            "precision_note": "Usage percentages are integer-rounded account snapshots.",
        }

    def build_summary(status: str) -> dict:
        ordered_records = [records[task["id"]] for task in tasks]
        summary = {
            "status": status,
            "started_at": started_at,
            "finished_at": utc_now() if status != "running" else None,
            "jobs": jobs,
            "admission_deadline": admission_deadline,
            "task_count": len(tasks),
            "backend": backend,
            **({"profile": profile} if profile is not None else {"connection": connection}),
            "overrides": {
                "model": model_override,
                "reasoning_effort": reasoning_effort_override,
                "timeout": timeout_override,
            },
            "selection": {
                "start": selection_start,
                "stop": selection_stop,
                "count": selection_stop - selection_start,
            },
            "complete": all(
                record["status"] in {"success", "skipped", "existing"}
                for record in ordered_records
            ),
            "counts": {
                name: sum(record["status"] == name for record in ordered_records)
                for name in (
                    "queued",
                    "success",
                    "skipped",
                    "existing",
                    "not-selected",
                    "failed",
                    "incomplete",
                    "deferred-deadline",
                )
            },
            "usage": summed_usage(ordered_records),
            "invocation_usage": summed_usage(
                [record for record in ordered_records if record.get("executed")]
            ),
            "tasks": ordered_records,
        }
        if backend == "app-server":
            summary["subscription_usage"] = subscription_usage()
        summary["backoff"] = backoff.snapshot()
        return summary

    atomic_write_json(summary_path, build_summary("running"))
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        queue = iter(pending)
        next_task = next(queue, None)
        futures = {}
        while futures or next_task is not None:
            if deadline is not None and time.time() >= deadline:
                active_ids = {task["id"] for task in futures.values()}
                for task in pending:
                    record = records[task["id"]]
                    if task["id"] not in active_ids and record["status"] == "queued":
                        record.update(status="deferred-deadline", executed=False,
                                      error="Admission deadline reached; no model call started.")
                next_task = None
                queue = iter(())
            state = backoff.snapshot()
            while (next_task is not None and len(futures) < state["effective_jobs"]
                   and not state["pause_remaining_seconds"]
                   and (deadline is None or time.time() < deadline)):
                futures[executor.submit(execute, next_task)] = next_task
                next_task = next(queue, None)
                state = backoff.snapshot()
            if not futures:
                if next_task is None:
                    break
                time.sleep(min(1.0, state["pause_remaining_seconds"]))
            else:
                finished, _ = wait(futures, timeout=1.0, return_when=FIRST_COMPLETED)
                for future in finished:
                    task_id, update = future.result()
                    records[task_id].update(update)
                    del futures[future]
            atomic_write_json(summary_path, build_summary("running"))

    if backend == "app-server" and pending:
        try:
            subscription_after = app_server.rate_limits(
                min(timeout_override or 120, 120), codex_bin, usage_codex_home,
            )
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            subscription_errors.append(f"after: {exc}")

    selected_successful = all(
        records[task["id"]]["status"] in {"success", "skipped"}
        for task in tasks[selection_start:selection_stop]
    )
    summary = build_summary("success" if selected_successful else "failed")
    atomic_write_json(summary_path, summary)
    return summary


def read_events(path: Path) -> tuple[list[dict], list[str]]:
    events = []
    errors = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], [str(exc)]
    for line_number, line in enumerate(lines, start=1):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            errors.append(f"events.jsonl line {line_number}: {exc}")
    return events, errors


def check_run(run_dir: Path) -> dict:
    run_dir = run_dir.expanduser().resolve()
    errors = []
    required = (
        "prompt.txt",
        "schema.json",
        "events.jsonl",
        "stderr.log",
        "response.txt",
        "result.json",
        "run.json",
    )
    for name in required:
        if not (run_dir / name).is_file():
            errors.append(f"missing {name}")
    if errors:
        return {"kind": "run", "path": str(run_dir), "ok": False, "errors": errors}

    try:
        metadata = load_json(run_dir / "run.json")
        prompt = (run_dir / "prompt.txt").read_text(encoding="utf-8")
        schema = load_json(run_dir / "schema.json")
        result = load_json(run_dir / "result.json")
        response = load_json(run_dir / "response.txt")
        validator_class = validators.validator_for(schema)
        validator_class.check_schema(schema)
        validator_class(schema).validate(result)
        if response != result:
            errors.append("response.txt and result.json differ")
        if metadata.get("status") != "success":
            errors.append(f"run status is {metadata.get('status')!r}")
        if metadata.get("returncode") != 0:
            errors.append(f"Codex return code is {metadata.get('returncode')!r}")
        if metadata.get("prompt_sha256") != sha256_text(prompt):
            errors.append("prompt hash mismatch")
        if metadata.get("schema_sha256") != sha256_text(canonical_json(schema)):
            errors.append("schema hash mismatch")
        events, event_errors = read_events(run_dir / "events.jsonl")
        errors.extend(event_errors)
        tools = (
            app_server.app_server_tool_events(events)
            if metadata.get("backend") == "app-server"
            else codex_exec.tool_events(events)
        )
        if tools:
            errors.append(f"tool events found: {tools}")
        if metadata.get("tool_events"):
            errors.append("run metadata records tool events")
        if metadata.get("event_parse_errors"):
            errors.append("run metadata records event parse errors")
    except (json.JSONDecodeError, OSError, SchemaError, ValidationError) as exc:
        errors.append(str(exc))
        metadata = {}

    return {
        "kind": "run",
        "path": str(run_dir),
        "ok": not errors,
        "errors": errors,
        "usage": metadata.get("usage"),
    }


def check_path(path: Path) -> dict:
    path = path.expanduser().resolve()
    if (path / "tasks").is_dir() and (path / "manifest.json").is_file():
        try:
            manifest = load_json(path / "manifest.json")
            raw_tasks = manifest.get("tasks") if isinstance(manifest, dict) else None
            if manifest.get("version") != 1 or not isinstance(raw_tasks, list):
                raise ValueError("invalid stored batch manifest")
            task_ids = []
            for raw in raw_tasks:
                task_id = raw.get("id") if isinstance(raw, dict) else None
                if not isinstance(task_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._-]*", task_id
                ):
                    raise ValueError("stored batch task has an invalid id")
                task_ids.append(task_id)
            if len(task_ids) != len(set(task_ids)):
                raise ValueError("stored batch manifest contains duplicate task ids")
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            return {
                "kind": "batch",
                "path": str(path),
                "ok": False,
                "errors": [str(exc)],
                "tasks": [],
            }
        reports = [check_run(path / "tasks" / task_id) for task_id in task_ids]
        errors = []
        summary = None
        summary_path = path / "summary.json"
        if not summary_path.is_file():
            errors.append("missing summary.json")
        else:
            try:
                summary = load_json(summary_path)
                if summary.get("status") != "success":
                    errors.append(f"batch status is {summary.get('status')!r}")
                summary_tasks = summary.get("tasks")
                summary_ids = (
                    [record.get("id") for record in summary_tasks]
                    if isinstance(summary_tasks, list)
                    else None
                )
                if summary_ids != task_ids:
                    errors.append("summary task IDs or order do not match manifest")
            except (AttributeError, json.JSONDecodeError, OSError) as exc:
                errors.append(f"invalid summary.json: {exc}")
        return {
            "kind": "batch",
            "path": str(path),
            "ok": not errors and all(report["ok"] for report in reports),
            "errors": errors,
            "usage": summed_usage(reports),
            "subscription_usage": (
                summary.get("subscription_usage")
                if isinstance(summary, dict) else None
            ),
            "tasks": reports,
        }
    return check_run(path)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "connection":
        return app_server.main(
            raw_args[1:], prog="codex-tools structured connection"
        )
    args = parse_args(raw_args)
    exit_code = 0
    try:
        if args.structured_command == "run":
            prompt = read_prompt(args.prompt)
            schema = load_json(args.schema.expanduser())
            result = run_task(
                prompt=prompt,
                schema=schema,
                run_dir=args.run_dir,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                timeout=args.timeout,
                codex_bin=args.codex_bin,
                backend=args.backend,
                profile=args.profile,
                connection=args.connection,
                manager_root=args.manager_root,
                default_home=args.default_home,
                connection_root=args.connection_root,
                app_server_home=args.app_server_home,
            )
        elif args.structured_command == "batch":
            manifest = load_json(args.manifest.expanduser())
            result = run_batch(
                manifest=manifest,
                manifest_path=args.manifest,
                batch_dir=args.batch_dir,
                jobs=args.jobs,
                rate_limit_backoff=args.rate_limit_backoff,
                backoff_state=args.backoff_state,
                admission_deadline=args.admission_deadline,
                fixed=args.fixed,
                backoff_decrease=args.backoff_decrease,
                backoff_increase=args.backoff_increase,
                backoff_recovery_period=args.backoff_recovery_period,
                idxs=tuple(args.idxs) if args.idxs is not None else None,
                model_override=args.model,
                reasoning_effort_override=args.reasoning_effort,
                timeout_override=args.timeout,
                codex_bin=args.codex_bin,
                backend=args.backend,
                profile=args.profile,
                connection=args.connection,
                manager_root=args.manager_root,
                default_home=args.default_home,
                connection_root=args.connection_root,
                app_server_home=args.app_server_home,
                usage_codex_home=args.usage_codex_home,
            )
            if result["status"] != "success":
                exit_code = 1
        elif args.structured_command == "check":
            result = check_path(args.path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
        else:
            raise AssertionError(
                f"unhandled structured command: {args.structured_command}"
            )
    except (
        FileExistsError,
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        SchemaError,
        ValidationError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
