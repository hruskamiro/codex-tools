from __future__ import annotations

import io
import json
import plistlib
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_tools import paths, relay_transport


class MacosConnectionTests(unittest.TestCase):
    def test_auto_start_preferences_are_saved_and_forwarded_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launch = Mock(side_effect=lambda action, *args: subprocess.CompletedProcess([], int(action == "print")))
            self.assertEqual(self.run_controller(root, ["start", "--auto-start", "--remote-bin", "/opt/ct"], launch), 0)
            preferences = json.loads((root / "config/ssh-launcher.json").read_text())
            self.assertTrue(preferences["connections"]["desktop"]["auto_start"])
            definition = plistlib.loads((root / "config/ssh-services/desktop.plist").read_bytes())
            self.assertIn("--auto-start", definition["ProgramArguments"])
            self.assertIn("/opt/ct", definition["ProgramArguments"])
            self.assertEqual(self.run_controller(root, ["start", "--no-auto-start"], launch), 0)
            definition = plistlib.loads((root / "config/ssh-services/desktop.plist").read_bytes())
            self.assertNotIn("--auto-start", definition["ProgramArguments"])

    def run_controller(self, root, arguments, launch, *, workstation_port=18781, console_hook=None):
        namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "ct-connect-macos"))
        main = namespace["main"]
        def child(command):
            if console_hook:
                console_hook(command)
            process = Mock()
            process.wait.return_value = 0
            process.poll.return_value = 0
            return process
        with (
            patch.dict(main.__globals__, {"launchctl": launch}),
            patch.object(sys, "platform", "darwin"),
            patch.object(sys, "argv", ["ct-connect", *arguments]),
            patch.object(paths, "user_config_dir", return_value=root / "config"),
            patch.object(paths, "user_state_dir", return_value=root / "state"),
            patch.object(relay_transport, "load_config", return_value={
                "peers": {
                    "desktop": {"url": "http://127.0.0.1:18780", "token": "secret"},
                    "workstation": {"url": f"http://127.0.0.1:{workstation_port}", "token": "other-secret"},
                },
            }),
            patch("sys.stdout", new_callable=io.StringIO),
            patch.object(subprocess, "Popen", side_effect=child),
        ):
            return main()

    def connection_service(self):
        state = {"loaded": False}
        def launch(action, *args):
            if action == "print":
                return subprocess.CompletedProcess([], 0 if state["loaded"] else 1)
            if action == "bootstrap":
                state["loaded"] = True
            if action == "bootout":
                state["loaded"] = False
            return subprocess.CompletedProcess([], 0)
        return state, Mock(side_effect=launch)

    def test_console_owns_connection_and_releases_configuration_lock(self):
        import fcntl
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, launch = self.connection_service()
            commands = []
            def console(command):
                commands.append(command)
                self.assertTrue(state["loaded"])
                with (root / "config/ssh-launcher.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_controller(root, ["console"], launch, console_hook=console), 0)
            self.assertFalse(state["loaded"])
            self.assertEqual(commands[0][-2:], ["--peer", "desktop"])
            saved = json.loads((root / "config/ssh-launcher.json").read_text())
            self.assertFalse(saved["connections"]["desktop"]["persistent"])

    def test_explicit_background_start_survives_console_exit_without_restarting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, launch = self.connection_service()
            self.run_controller(root, ["start"], launch)
            self.run_controller(root, ["console"], launch)
            self.assertTrue(state["loaded"])
            self.assertEqual(sum(call.args[0] == "bootstrap" for call in launch.call_args_list), 1)
            self.assertFalse(any(call.args[0] == "bootout" for call in launch.call_args_list))

    def test_multiple_console_leases_stop_only_after_last_client_and_ignore_stale_files(self):
        namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/ct-connect-macos"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config/ssh-launcher.json").write_text(json.dumps({
                "version": 2, "default": "desktop", "connections": {"desktop": {"persistent": False}},
            }))
            with patch.object(paths, "user_config_dir", return_value=root / "config"), \
                 patch.object(paths, "user_state_dir", return_value=root / "state"), \
                 patch.dict(namespace["release_console"].__globals__, {
                     "launchctl": Mock(return_value=subprocess.CompletedProcess([], 0)),
                     "stop_service": Mock(),
                 }):
                first = namespace["console_lease"]("desktop")
                second = namespace["console_lease"]("desktop")
                (first[0].parent / "client-stale").touch()
                stop = namespace["release_console"].__globals__["stop_service"]
                namespace["release_console"]("desktop", first)
                stop.assert_not_called()
                namespace["release_console"]("desktop", second)
                stop.assert_called_once()
                self.assertFalse(list(first[0].parent.iterdir()))

    def test_background_service_is_private_and_does_not_enable_login_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launch = Mock(side_effect=lambda action, *args: subprocess.CompletedProcess([], int(action == "print")))
            self.assertEqual(self.run_controller(root, ["start", "--ssh", "user@desktop"], launch), 0)
            definition_path = root / "config" / "ssh-services" / "desktop.plist"
            definition = plistlib.loads(definition_path.read_bytes())
            self.assertTrue(definition["KeepAlive"])
            self.assertEqual(definition["Umask"], 0o077)
            command = definition["ProgramArguments"]
            self.assertEqual(command[command.index("--ui") + 1], "none")
            self.assertEqual(definition["Label"], "local.codex-tools.relay.desktop")
            self.assertIn("user@desktop", definition["ProgramArguments"])
            self.assertEqual(definition_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("secret", definition_path.read_text())
            self.assertEqual(launch.call_args.args[0], "bootstrap")
            self.assertNotIn("Library/LaunchAgents", launch.call_args.args[-1])

    def test_stop_still_works_with_damaged_preferences(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "ssh-launcher.json").write_text("invalid json")
            launch = Mock(return_value=subprocess.CompletedProcess([], 0))
            self.assertEqual(self.run_controller(root, ["stop"], launch), 0)
            self.assertEqual(launch.call_args.args[0], "bootout")

    def test_existing_background_service_is_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "ssh-launcher.json").write_text(json.dumps({
                "version": 2, "default": "desktop", "connections": {
                    "desktop": {"ssh": "user@desktop", "peer": "desktop", "remote_port": 8780},
                },
            }))
            launch = Mock(return_value=subprocess.CompletedProcess([], 0))
            self.assertEqual(self.run_controller(root, ["start"], launch), 0)
            launch.assert_called_once()

    def test_second_connection_preserves_first_and_uses_separate_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            preferences = root / "config" / "ssh-launcher.json"
            preferences.write_text(json.dumps({
                "version": 2, "default": "desktop", "connections": {
                    "desktop": {"ssh": "user@desktop", "peer": "desktop", "remote_port": 8780},
                },
            }))
            launch = Mock(side_effect=lambda action, *args: subprocess.CompletedProcess([], int(action == "print")))
            self.assertEqual(self.run_controller(root, ["start", "workstation", "--ssh", "user@work"], launch), 0)
            self.assertEqual(set(json.loads(preferences.read_text())["connections"]), {"desktop", "workstation"})
            definition = plistlib.loads((root / "config" / "ssh-services" / "workstation.plist").read_bytes())
            self.assertEqual(definition["Label"], "local.codex-tools.relay.workstation")
            self.assertFalse(any(call.args[0] == "bootout" for call in launch.call_args_list))
            launch.reset_mock()
            launch.side_effect = None
            launch.return_value = subprocess.CompletedProcess([], 0)
            self.assertEqual(self.run_controller(root, ["stop", "workstation"], launch), 0)
            self.assertTrue(launch.call_args.args[1].endswith(".workstation"))
            self.assertFalse(any(call.args[-1].endswith(".desktop") for call in launch.call_args_list))

    def test_legacy_configuration_and_running_service_migrate_with_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            old = {"ssh": "user@desktop", "peer": "desktop", "remote_port": 8780}
            preferences = root / "config" / "ssh-launcher.json"
            preferences.write_text(json.dumps(old))

            stopped = False
            def call(action, *args):
                nonlocal stopped
                if action == "bootout":
                    stopped = True
                legacy = args and args[0].endswith("/local.codex-tools.relay")
                return subprocess.CompletedProcess([], int(action == "print" and (not legacy or stopped)))

            launch = Mock(side_effect=call)
            self.assertEqual(self.run_controller(root, ["start", "desktop"], launch), 0)
            saved = json.loads(preferences.read_text())
            self.assertEqual(saved["version"], 2)
            self.assertEqual(saved["connections"]["desktop"], {**old, "persistent": True})
            self.assertEqual(json.loads((root / "config" / "ssh-launcher.v1.json").read_text()), old)
            self.assertTrue(any(call.args[0] == "bootout" and call.args[1].endswith("/local.codex-tools.relay") for call in launch.call_args_list))

    def test_reconfiguration_waits_for_asynchronous_bootout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config/ssh-launcher.json").write_text(json.dumps({
                "version": 2, "default": "desktop", "connections": {
                    "desktop": {"peer": "desktop", "ssh": "desktop", "remote_port": 8780},
                },
            }))
            prints = 0
            def call(action, *args):
                nonlocal prints
                if action == "print":
                    prints += 1
                    return subprocess.CompletedProcess([], int(prints >= 3))
                return subprocess.CompletedProcess([], 0)
            launch = Mock(side_effect=call)
            self.run_controller(root, ["start", "--auto-start"], launch)
            self.assertEqual(launch.call_args.args[0], "bootstrap")
            self.assertGreaterEqual(prints, 3)

    def test_colliding_ports_are_rejected_before_touching_a_running_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "ssh-launcher.json").write_text(json.dumps({"ssh": "user@desktop"}))
            launch = Mock()
            with self.assertRaisesRegex(ValueError, "different local port"):
                self.run_controller(root, ["start", "workstation"], launch, workstation_port=18780)
            launch.assert_not_called()

    def test_status_without_a_name_lists_both_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "ssh-launcher.json").write_text(json.dumps({
                "version": 2, "default": "desktop", "connections": {
                    "desktop": {"ssh": "one"}, "workstation": {"ssh": "two"},
                },
            }))
            launch = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="pid = 123"))
            self.assertEqual(self.run_controller(root, ["status"], launch), 0)
            self.assertEqual({call.args[1].rsplit(".", 1)[-1] for call in launch.call_args_list}, {"desktop", "workstation"})
