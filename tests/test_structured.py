from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from codex_tools import codex_exec, manager, structured


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}


class StructuredTaskTests(unittest.TestCase):
    def make_fake_codex(self, root: Path) -> Path:
        executable = root / "fake-codex"
        executable.write_text(
            """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
response = pathlib.Path(args[args.index("--output-last-message") + 1])
sys.stdin.read()
response.write_text(json.dumps({"answer": "ok"}), encoding="utf-8")
pathlib.Path("codex-home.txt").write_text(os.environ.get("CODEX_HOME", ""), encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": "test-thread"}))
print(json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 12, "output_tokens": 3, "reasoning_output_tokens": 1
}}))
""",
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return executable

    def test_run_task_writes_reproducibility_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            result = structured.run_task(
                prompt="Return an answer.",
                schema=SCHEMA,
                run_dir=run_dir,
                model="test-model",
                codex_bin=str(self.make_fake_codex(root)),
            )

            self.assertEqual(result, {"answer": "ok"})
            metadata = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "success")
            self.assertEqual(metadata["profile"], "default")
            self.assertEqual(metadata["thread_id"], "test-thread")
            self.assertEqual(metadata["usage"]["input_tokens"], 12)
            for name in (
                "prompt.txt",
                "schema.json",
                "events.jsonl",
                "stderr.log",
                "response.txt",
                "result.json",
                "run.json",
            ):
                self.assertTrue((run_dir / name).exists(), name)
            self.assertTrue(structured.check_run(run_dir)["ok"])

    def test_managed_profile_sets_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager_root = root / "manager"
            default_home = root / "default-home"
            profile_home = root / "work-home"
            profile_home.mkdir()
            manager.save_profile(
                manager_root,
                {
                    "name": "work",
                    "home": str(profile_home),
                    "conversation_store": str(root / "store"),
                    "share": "isolated",
                    "created_at": "",
                },
            )
            run_dir = root / "run"
            structured.run_task(
                prompt="Return an answer.",
                schema=SCHEMA,
                run_dir=run_dir,
                model="test-model",
                codex_bin=str(self.make_fake_codex(root)),
                profile="work",
                manager_root=manager_root,
                default_home=default_home,
            )

            self.assertEqual(
                (run_dir / "codex-home.txt").read_text(encoding="utf-8"),
                str(profile_home.resolve()),
            )
            metadata = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["profile"], "work")
            self.assertEqual(metadata["codex_home"], str(profile_home.resolve()))

    def test_parallel_batch_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_codex = self.make_fake_codex(root)
            (root / "one.txt").write_text("First task.", encoding="utf-8")
            (root / "two.txt").write_text("Second task.", encoding="utf-8")
            (root / "schema.json").write_text(
                json.dumps(SCHEMA), encoding="utf-8"
            )
            manifest = {
                "version": 1,
                "defaults": {
                    "model": "test-model",
                    "reasoning_effort": "medium",
                },
                "tasks": [
                    {"id": "one", "prompt": "one.txt", "schema": "schema.json"},
                    {
                        "id": "two",
                        "prompt": "two.txt",
                        "schema": "schema.json",
                        "model": "task-model",
                        "reasoning_effort": "low",
                    },
                ],
            }
            manifest_path = root / "batch.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            batch_dir = root / "batch"

            first = structured.run_batch(
                manifest=manifest,
                manifest_path=manifest_path,
                batch_dir=batch_dir,
                jobs=2,
                model_override="override-model",
                reasoning_effort_override="high",
                timeout_override=77,
                codex_bin=str(fake_codex),
            )
            self.assertEqual(first["status"], "success")
            self.assertEqual(first["counts"]["success"], 2)
            self.assertEqual(first["usage"]["input_tokens"], 24)
            self.assertEqual(first["overrides"]["model"], "override-model")
            for record in first["tasks"]:
                self.assertEqual(record["model"], "override-model")
                self.assertEqual(record["reasoning_effort"], "high")
                metadata = json.loads(
                    (batch_dir / record["run_dir"] / "run.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(metadata["model"], "override-model")
                self.assertEqual(metadata["reasoning_effort"], "high")
                self.assertEqual(metadata["timeout"], 77)
            self.assertTrue(structured.check_path(batch_dir)["ok"])

            second = structured.run_batch(
                manifest=manifest,
                manifest_path=manifest_path,
                batch_dir=batch_dir,
                jobs=2,
                model_override="override-model",
                reasoning_effort_override="high",
                timeout_override=77,
                codex_bin=str(fake_codex),
            )
            self.assertEqual(second["counts"]["skipped"], 2)

            (batch_dir / "tasks" / "one" / "result.json").write_text(
                '{"answer": 7}\n', encoding="utf-8"
            )
            self.assertFalse(structured.check_path(batch_dir)["ok"])

    def test_batch_index_ranges_accumulate_completed_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_codex = self.make_fake_codex(root)
            (root / "one.txt").write_text("First task.", encoding="utf-8")
            (root / "two.txt").write_text("Second task.", encoding="utf-8")
            (root / "schema.json").write_text(
                json.dumps(SCHEMA), encoding="utf-8"
            )
            manifest = {
                "version": 1,
                "defaults": {"model": "test-model"},
                "tasks": [
                    {"id": "one", "prompt": "one.txt", "schema": "schema.json"},
                    {"id": "two", "prompt": "two.txt", "schema": "schema.json"},
                ],
            }
            manifest_path = root / "batch.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            batch_dir = root / "batch"

            first = structured.run_batch(
                manifest=manifest,
                manifest_path=manifest_path,
                batch_dir=batch_dir,
                jobs=2,
                idxs=(0, 1),
                codex_bin=str(fake_codex),
            )
            self.assertEqual(first["counts"]["success"], 1)
            self.assertEqual(first["counts"]["not-selected"], 1)
            self.assertFalse(first["complete"])
            self.assertEqual(first["usage"]["input_tokens"], 12)
            self.assertEqual(first["invocation_usage"]["input_tokens"], 12)

            second = structured.run_batch(
                manifest=manifest,
                manifest_path=manifest_path,
                batch_dir=batch_dir,
                jobs=2,
                idxs=(1, 2),
                codex_bin=str(fake_codex),
            )
            self.assertEqual(second["counts"]["existing"], 1)
            self.assertEqual(second["counts"]["success"], 1)
            self.assertTrue(second["complete"])
            self.assertEqual(second["usage"]["input_tokens"], 24)
            self.assertEqual(second["invocation_usage"]["input_tokens"], 12)
            self.assertTrue(structured.check_path(batch_dir)["ok"])

    def test_nonempty_run_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "existing").write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                structured.run_task(
                    prompt="test",
                    schema=SCHEMA,
                    run_dir=run_dir,
                    model="test-model",
                    codex_bin="unused",
                )

    def test_tool_events_are_detected(self) -> None:
        events = [
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "pwd"},
            }
        ]
        self.assertEqual(
            codex_exec.tool_events(events),
            [{"event": "item.completed", "item_type": "command_execution"}],
        )


if __name__ == "__main__":
    unittest.main()
