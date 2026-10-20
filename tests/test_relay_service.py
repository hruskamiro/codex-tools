from __future__ import annotations

import errno
import json
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_tools import relay, relay_service


class RelayServiceTests(unittest.TestCase):
    def ensure(self, root):
        return relay_service.ensure(8780, root / "relay.json", root / "cache.sqlite3", timeout=1)

    def test_healthy_relay_is_reused_without_spawning_or_changing_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (patch.object(relay_service.paths, "user_state_dir", return_value=root),
                  patch.object(relay_service.relay_transport, "load_config"),
                  patch.object(relay_service, "healthy", return_value=True),
                  patch.object(relay_service.subprocess, "Popen") as spawn):
                self.assertEqual(self.ensure(root)["status"], "running")
                spawn.assert_not_called()
                self.assertFalse((root / "relay-serve/8780/process.json").exists())

    def test_missing_relay_is_detached_and_bound_only_to_loopback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process = Mock(pid=321)
            process.poll.return_value = None
            with (patch.object(relay_service.paths, "user_state_dir", return_value=root),
                  patch.object(relay_service.relay_transport, "load_config"),
                  patch.object(relay_service, "healthy", side_effect=[False, True]),
                  patch.object(relay_service.subprocess, "Popen", return_value=process) as spawn):
                result = self.ensure(root)
            self.assertEqual(result["status"], "started")
            command = spawn.call_args.args[0]
            self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
            self.assertTrue(spawn.call_args.kwargs["start_new_session"])
            self.assertTrue(spawn.call_args.kwargs["close_fds"])
            self.assertEqual(spawn.call_args.kwargs["stdin"], subprocess.DEVNULL)
            process.terminate.assert_not_called()
            process.kill.assert_not_called()
            directory = root / "relay-serve/8780"
            self.assertEqual(json.loads((directory / "process.json").read_text()), {"pid": 321})
            self.assertEqual((directory / "serve.log").stat().st_mode & 0o777, 0o600)

    def test_unhealthy_or_occupied_port_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (patch.object(relay_service.paths, "user_state_dir", return_value=root),
                  patch.object(relay_service.relay_transport, "load_config"),
                  patch.object(relay_service, "healthy", side_effect=RuntimeError("occupied")),
                  patch.object(relay_service.subprocess, "Popen") as spawn):
                with self.assertRaisesRegex(RuntimeError, "occupied"):
                    self.ensure(root)
                spawn.assert_not_called()

    def test_existing_startup_pid_prevents_duplicates_after_interrupted_ensure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "relay-serve/8780"
            directory.mkdir(parents=True)
            (directory / "process.json").write_text('{"pid": 321}')
            with (patch.object(relay_service.paths, "user_state_dir", return_value=root),
                  patch.object(relay_service.relay_transport, "load_config"),
                  patch.object(relay_service, "healthy", return_value=False),
                  patch.object(relay_service, "process_alive", return_value=True),
                  patch.object(relay_service.subprocess, "Popen") as spawn):
                with self.assertRaisesRegex(RuntimeError, "exists but is not healthy"):
                    self.ensure(root)
                spawn.assert_not_called()

    def test_only_connection_refused_means_missing(self):
        with patch.object(relay_service.socket, "create_connection", side_effect=ConnectionRefusedError(errno.ECONNREFUSED, "refused")):
            self.assertFalse(relay_service.healthy(8780))
        with patch.object(relay_service.socket, "create_connection", side_effect=TimeoutError("slow")):
            with self.assertRaisesRegex(RuntimeError, "no relay was replaced"):
                relay_service.healthy(8780)
        with (patch.object(relay_service.socket, "create_connection"),
              patch.object(relay_service, "build_opener") as opener):
            opener.return_value.open.side_effect = OSError("invalid HTTP")
            with self.assertRaisesRegex(RuntimeError, "leaving it untouched"):
                relay_service.healthy(8780)

    def test_dead_startup_reports_logs_and_never_kills_a_remote_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process = Mock(pid=321, returncode=2)
            process.poll.return_value = 2
            with (patch.object(relay_service.paths, "user_state_dir", return_value=root),
                  patch.object(relay_service.relay_transport, "load_config"),
                  patch.object(relay_service, "healthy", return_value=False),
                  patch.object(relay_service.subprocess, "Popen", return_value=process)):
                with self.assertRaisesRegex(RuntimeError, "startup exited.*serve.log"):
                    self.ensure(root)
            process.terminate.assert_not_called()

    def test_cli_exposes_only_private_ensure_arguments(self):
        args = relay.parse_args(["ensure", "--port", "8781", "--json"])
        self.assertEqual(args.port, 8781)
        self.assertTrue(args.json)

    def test_uninitialized_identity_is_not_created_automatically(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(relay_service.subprocess, "Popen") as spawn:
                with self.assertRaises(FileNotFoundError):
                    self.ensure(Path(temporary))
                spawn.assert_not_called()
