from __future__ import annotations

import subprocess
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_tools import relay, relay_ssh


class RelaySshTests(unittest.TestCase):
    def test_remote_bootstrap_quotes_executable_and_supports_ssh_aliases(self):
        command = relay_ssh.bootstrap_command("ufront-through-a370", 8781, "/a path/ct;not-a-shell-command")
        self.assertNotIn("-N", command)
        self.assertNotIn("-L", command)
        self.assertEqual(command[-2], "ufront-through-a370")
        self.assertEqual(shlex.split(command[-1]), ["/a path/ct;not-a-shell-command", "relay", "ensure", "--port", "8781", "--json"])
        for path in ("", "-options", "ct\nother", "ct\x00other"):
            with self.assertRaises(ValueError):
                relay_ssh.bootstrap_command("desktop", 8780, path)

    def test_bootstrap_confirms_health_and_does_not_receive_pairing_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            worker = self.supervisor(Path(temporary))
            worker.bootstrap = relay_ssh.bootstrap_command("desktop", 8780, "/usr/local/bin/ct")
            process = Mock(returncode=0)
            process.communicate.return_value = (b'{"service":"codex-tools-relay","status":"running"}', b"")
            with patch.object(relay_ssh.subprocess, "Popen", return_value=process) as spawn:
                worker.ensure_remote()
            self.assertNotIn("private-key", str(spawn.call_args))
            process.communicate.assert_called_once_with(timeout=45)
            self.assertIn("Remote relay running", worker._message)

    def test_failed_remote_check_does_not_open_a_tunnel(self):
        with tempfile.TemporaryDirectory() as temporary:
            worker = self.supervisor(Path(temporary))
            worker.bootstrap = ["ssh", "desktop", "ct relay ensure"]
            process = Mock(returncode=1)
            process.communicate.return_value = (b"", b"occupied or unhealthy")
            with patch.object(relay_ssh.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "occupied or unhealthy"):
                    worker.ensure_remote()

    def test_bootstrap_timeout_cleans_only_its_ssh_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            worker = self.supervisor(Path(temporary))
            worker.bootstrap = ["ssh", "desktop", "ct relay ensure"]
            process = Mock()
            process.poll.return_value = None
            process.communicate.side_effect = subprocess.TimeoutExpired("ssh", 45)
            with patch.object(relay_ssh.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    worker.ensure_remote()
            process.terminate.assert_called_once()

    def supervisor(self, root: Path) -> relay_ssh.TunnelSupervisor:
        cache = Mock()
        cache.pending.return_value = []
        return relay_ssh.TunnelSupervisor(
            relay_ssh.ssh_command("desktop", 18780, 8780),
            {"url": "http://127.0.0.1:18780", "token": "private-key"},
            cache, root,
        )

    def test_ssh_uses_private_forwarding_and_noninteractive_keepalives(self) -> None:
        command = relay_ssh.ssh_command("user@desktop", 18780, 8780)
        self.assertIn("127.0.0.1:18780:127.0.0.1:8780", command)
        for option in ("BatchMode=yes", "ExitOnForwardFailure=yes", "ServerAliveInterval=15",
                       "ServerAliveCountMax=3", "ControlMaster=no", "ControlPath=none"):
            self.assertIn(option, command)
        self.assertEqual(command[-1], "user@desktop")
        for host in ("-oProxyCommand=anything", "desktop extra", "desktop\ncommand"):
            with self.assertRaises(ValueError):
                relay_ssh.ssh_command(host, 18780, 8780)
        with self.assertRaises(ValueError):
            relay_ssh.ssh_command("desktop", 70000, 8780)

    def test_retry_delay_is_bounded_even_after_long_outages(self) -> None:
        with patch.object(relay_ssh.random, "uniform", return_value=1.0):
            self.assertEqual([relay_ssh.retry_delay(n) for n in range(7)], [1, 2, 4, 8, 16, 30, 30])
            self.assertEqual(relay_ssh.retry_delay(1000000), 30)

    def test_supervisor_restarts_failed_ssh_and_cleans_owned_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            worker = self.supervisor(Path(temporary))
            processes = [Mock(), Mock()]
            for process in processes:
                process.poll.return_value = None
            calls = []

            def monitor(process):
                calls.append(process)
                if len(calls) == 1:
                    raise OSError("network disconnected")
                worker.stop.set()

            with (
                patch.object(relay_ssh.subprocess, "Popen", side_effect=processes) as spawn,
                patch.object(worker, "monitor", side_effect=monitor),
                patch.object(worker.stop, "wait", return_value=False),
            ):
                worker.run()
            self.assertEqual(spawn.call_count, 2)
            self.assertEqual(calls, processes)
            for process in processes:
                process.terminate.assert_called_once()
                process.wait.assert_called_once()
            self.assertNotIn("private-key", (Path(temporary) / "status.json").read_text())
            self.assertEqual((Path(temporary) / "ssh.log").stat().st_mode & 0o777, 0o600)

    def test_sync_retries_without_restarting_a_healthy_ssh_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            worker = self.supervisor(Path(temporary))
            process = Mock()
            process.poll.return_value = None
            waits = []

            def wait(_delay):
                waits.append(True)
                if len(waits) == 2:
                    worker.stop.set()

            with (
                patch.object(relay_ssh.relay_transport, "fetch_json", return_value={"service": "codex-tools-relay"}),
                patch.object(worker, "synchronize", side_effect=[OSError("temporary timeout"), "host-1"]) as sync,
                patch.object(relay_ssh, "retry_delay", return_value=0),
                patch.object(worker.stop, "wait", side_effect=wait),
            ):
                worker.monitor(process)
            self.assertEqual(sync.call_count, 2)
            process.terminate.assert_not_called()

    def test_repeated_health_failures_trigger_reconnection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            worker = self.supervisor(Path(temporary))
            process = Mock()
            process.poll.return_value = None
            with (
                patch.object(relay_ssh.relay_transport, "fetch_json", side_effect=[
                    {"service": "codex-tools-relay"}, OSError("lost"), OSError("lost"), OSError("lost"),
                ]) as health,
                patch.object(worker, "synchronize", return_value="host-1") as sync,
                patch.object(worker.stop, "wait", return_value=False),
            ):
                worker.monitor(process)
            self.assertEqual(health.call_count, 4)
            sync.assert_called_once()
            self.assertFalse(worker.connected.is_set())

    def test_pending_messages_retry_without_full_snapshot_polling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            worker = self.supervisor(Path(temporary))
            worker.cache.pending.return_value = [{"message_id": "queued-1"}]
            process = Mock()
            process.poll.return_value = None
            with (
                patch.object(relay_ssh.relay_transport, "fetch_json", return_value={"service": "codex-tools-relay"}),
                patch.object(worker, "synchronize", return_value="host-1") as sync,
                patch.object(relay_ssh.relay_transport, "deliver_pending") as deliver,
                patch.object(worker.stop, "wait", side_effect=lambda _delay: worker.stop.set()),
            ):
                worker.monitor(process)
            sync.assert_called_once()
            deliver.assert_called_once_with(worker.cache, worker.peer, "host-1")

    def test_close_kills_only_the_child_it_started_if_termination_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            worker = self.supervisor(Path(temporary))
            process = Mock()
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired("ssh", 2), 0]
            worker.process = process
            worker.close()
            process.terminate.assert_called_once()
            process.kill.assert_called_once()
            self.assertIsNone(worker.process)

    def test_occupied_port_produces_an_error_instead_of_replacing_another_tunnel(self) -> None:
        with patch.object(relay_ssh.socket, "socket") as socket:
            socket.return_value.__enter__.return_value.bind.side_effect = OSError("in use")
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                relay_ssh.require_free_port(18780)

    def test_console_opens_without_waiting_for_connectivity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = relay.parse_args(["connect", "desktop"])
            with (
                patch.object(relay_ssh.paths, "user_state_dir", return_value=Path(temporary)),
                patch.object(relay_ssh.relay_transport, "load_config", return_value={
                    "peers": {"desktop": {"url": "http://127.0.0.1:18780", "token": "key"}},
                }),
                patch.object(relay_ssh, "require_free_port"),
                patch.object(relay_ssh, "TunnelSupervisor") as supervisor,
                patch.object(relay_ssh.subprocess, "Popen") as ui,
            ):
                ui.return_value.wait.return_value = 0
                self.assertEqual(relay_ssh.command_connect(args), 0)
            command = ui.call_args.args[0]
            self.assertIn("--no-local", command)
            self.assertEqual(command[command.index("--peer") + 1], "desktop")
            supervisor.return_value.thread.start.assert_called_once()
            supervisor.return_value.connected.wait.assert_not_called()
            supervisor.return_value.close.assert_called_once()

    def test_network_exposed_peer_urls_are_not_accepted_for_ssh_forwarding(self) -> None:
        args = relay.parse_args(["connect", "desktop"])
        for url in ("http://0.0.0.0:8780", "https://127.0.0.1:18780", "http://user@localhost:18780"):
            with patch.object(relay_ssh.relay_transport, "load_config", return_value={"peers": {"desktop": {"url": url}}}):
                with self.assertRaisesRegex(ValueError, "peer URL"):
                    relay_ssh.command_connect(args)
