from __future__ import annotations

import json
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_tools import __version__, app_server, structured


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}


class AppServerTests(unittest.TestCase):
    def make_fake_codex(self, root: Path) -> Path:
        executable = root / "fake-codex"
        executable.write_text(
            """#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        assert message["params"]["capabilities"]["experimentalApi"] is True
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif method == "account/rateLimits/read":
        print(json.dumps({"id": message["id"], "result": {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "limitId": "codex", "planType": "prolite",
                "primary": {"usedPercent": 18, "windowDurationMins": 10080,
                            "resetsAt": 1791621434}
            }
        }}), flush=True)
    elif method == "thread/start":
        print(json.dumps({"id": message["id"], "result": {
            "thread": {"id": "app-thread"}
        }}), flush=True)
    elif method == "turn/start":
        print(json.dumps({"id": message["id"], "result": {
            "turn": {"id": "app-turn"}
        }}), flush=True)
        print(json.dumps({"method": "item/agentMessage/delta", "params": {
            "delta": json.dumps({"answer": "ok"})
        }}), flush=True)
        print(json.dumps({"method": "thread/tokenUsage/updated", "params": {
            "tokenUsage": {"last": {
                "totalTokens": 11, "inputTokens": 8,
                "cachedInputTokens": 4, "outputTokens": 3,
                "reasoningOutputTokens": 0
            }}
        }}), flush=True)
        print(json.dumps({"method": "turn/completed", "params": {
            "turn": {"id": "app-turn", "status": "completed", "items": []}
        }}), flush=True)
""",
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return executable

    def test_connection_store_is_private_and_redacts_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "connections"
            payload = {
                "version": 1,
                "connections": {
                    "default": {
                        "name": "default",
                        "status": "connected",
                        "client_id": "oaiapp_test",
                        "email": "reader@example.test",
                        "access_token": "secret",
                    }
                },
            }
            app_server.save_connections(payload, root)

            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (root / "connections.json").stat().st_mode & 0o777, 0o600
            )
            public = app_server.public_connection(payload["connections"]["default"])
            self.assertNotIn("access_token", public)
            self.assertEqual(public["email"], "reader@example.test")

    def test_access_token_refreshes_and_saves_rotated_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "connections"
            original = {
                "name": "default",
                "status": "connected",
                "client_id": "oaiapp_test",
                "access_token": "old",
                "refresh_token": "refresh-old",
                "expires_at": time.time() - 1,
            }
            app_server.save_connections(
                {"version": 1, "connections": {"default": original}}, root
            )
            refreshed = {
                **original,
                "access_token": "new",
                "refresh_token": "refresh-new",
                "expires_at": time.time() + 3600,
            }
            with patch.object(app_server, "_refresh", return_value=refreshed):
                token = app_server.access_token("default", root)

            self.assertEqual(token, "new")
            stored = app_server.load_connections(root)["connections"]["default"]
            self.assertEqual(stored["refresh_token"], "refresh-new")

    def test_access_token_refreshes_for_requested_run_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "connections"
            original = {
                "name": "default",
                "status": "connected",
                "client_id": "oaiapp_test",
                "access_token": "old",
                "refresh_token": "refresh-old",
                "expires_at": time.time() + 120,
            }
            app_server.save_connections(
                {"version": 1, "connections": {"default": original}}, root
            )
            refreshed = {
                **original,
                "access_token": "new",
                "expires_at": time.time() + 3600,
            }
            with patch.object(app_server, "_refresh", return_value=refreshed) as refresh:
                token = app_server.access_token("default", root, 300)

            self.assertEqual(token, "new")
            refresh.assert_called_once()

    def test_access_token_rejects_timeout_longer_than_new_token_lifetime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "connections"
            original = {
                "name": "default",
                "status": "connected",
                "client_id": "oaiapp_test",
                "access_token": "old",
                "refresh_token": "refresh-old",
                "expires_at": time.time() + 120,
            }
            app_server.save_connections(
                {"version": 1, "connections": {"default": original}}, root
            )
            refreshed = {**original, "access_token": "new", "expires_at": time.time() + 600}
            with patch.object(app_server, "_refresh", return_value=refreshed):
                with self.assertRaisesRegex(RuntimeError, "token lifetime"):
                    app_server.access_token("default", root, 1800)

    def test_structured_app_server_backend_writes_compatible_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_home = root / "app-home"
            run_dir = root / "run"
            with patch.object(
                app_server, "access_token", return_value="plan-token"
            ) as token:
                result = structured.run_task(
                    prompt="Return an answer.", schema=SCHEMA, run_dir=run_dir,
                    model="test-model", reasoning_effort="low",
                    codex_bin=str(self.make_fake_codex(root)),
                    backend="app-server", connection="work",
                    connection_root=root / "connections",
                    app_server_home=app_home,
                )

            token.assert_called_once_with("work", root / "connections", 1860)

            self.assertEqual(result, {"answer": "ok"})
            metadata = json.loads((run_dir / "run.json").read_text())
            self.assertEqual(metadata["backend"], "app-server")
            self.assertEqual(metadata["connection"], "work")
            self.assertNotIn("profile", metadata)
            self.assertEqual(metadata["usage"]["input_tokens"], 8)
            self.assertEqual(metadata["codex_home"], str(app_home.resolve()))
            self.assertEqual(list(app_home.glob("runtime-*")), [])
            self.assertTrue(structured.check_run(run_dir)["ok"])

    def test_app_server_uses_the_package_version(self) -> None:
        self.assertEqual(__version__, "0.5.0")
        self.assertIn("attr: codex_tools.__version__", Path("setup.cfg").read_text())

    def test_app_server_disables_shell_tools(self) -> None:
        command = app_server.app_server_command("codex-test")
        self.assertEqual(command[:2], ["codex-test", "app-server"])
        adjacent = [command[index:index + 2] for index in range(len(command) - 1)]
        self.assertIn(["--disable", "shell_tool"], adjacent)

    def test_parallel_app_server_batch_uses_compatible_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one.txt").write_text("First task.", encoding="utf-8")
            (root / "two.txt").write_text("Second task.", encoding="utf-8")
            (root / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
            manifest = {
                "version": 1,
                "defaults": {"model": "test-model", "reasoning_effort": "low"},
                "tasks": [
                    {"id": "one", "prompt": "one.txt", "schema": "schema.json"},
                    {"id": "two", "prompt": "two.txt", "schema": "schema.json"},
                ],
            }
            manifest_path = root / "batch.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (root / "usage-home").mkdir()
            with patch.object(app_server, "access_token", return_value="plan-token"):
                summary = structured.run_batch(
                    manifest=manifest, manifest_path=manifest_path,
                    batch_dir=root / "batch", jobs=2,
                    codex_bin=str(self.make_fake_codex(root)),
                    backend="app-server", connection="work",
                    connection_root=root / "connections",
                    app_server_home=root / "app-home",
                    usage_codex_home=root / "usage-home",
                )

            self.assertEqual(summary["status"], "success")
            self.assertEqual(summary["counts"]["success"], 2)
            self.assertEqual(summary["usage"]["input_tokens"], 16)
            self.assertEqual(summary["connection"], "work")
            subscription = summary["subscription_usage"]
            self.assertTrue(subscription["measured"])
            self.assertEqual(
                subscription["before"]["limits"]["codex"]["primary"]["used_percent"],
                18,
            )
            self.assertEqual(
                subscription["delta"]["codex"]["used_percentage_points"], 0
            )
            self.assertEqual(list((root / "app-home").glob("runtime-*")), [])
            for record in summary["tasks"]:
                metadata = json.loads(
                    (root / "batch" / record["run_dir"] / "run.json").read_text()
                )
                self.assertEqual(metadata["backend"], "app-server")
                self.assertEqual(metadata["connection"], "work")

            with patch.object(app_server, "access_token", return_value="plan-token"):
                repeated = structured.run_batch(
                    manifest=manifest, manifest_path=manifest_path,
                    batch_dir=root / "batch", jobs=2,
                    codex_bin=str(self.make_fake_codex(root)),
                    backend="app-server", connection="work",
                    connection_root=root / "connections",
                    app_server_home=root / "app-home",
                    usage_codex_home=root / "usage-home",
                )
            self.assertTrue(repeated["subscription_usage"]["measured"])
            self.assertTrue(
                repeated["subscription_usage"]["preserved_from_previous_run"]
            )

    def test_rate_limits_returns_public_normalized_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "usage-home").mkdir()
            snapshot = app_server.rate_limits(
                30, str(self.make_fake_codex(root)), root / "usage-home",
            )

            primary = snapshot["limits"]["codex"]["primary"]
            self.assertEqual(primary["used_percent"], 18)
            self.assertEqual(primary["remaining_percent"], 82)
            self.assertEqual(primary["window_duration_minutes"], 10080)
            self.assertNotIn("account_id", snapshot)
            self.assertEqual(snapshot["source"]["kind"], "codex-home")

    def test_backend_specific_selectors_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "--connection"):
            structured.resolve_backend_auth("exec", None, "work")
        with self.assertRaisesRegex(ValueError, "--profile"):
            structured.resolve_backend_auth("app-server", "work", None)


if __name__ == "__main__":
    unittest.main()
