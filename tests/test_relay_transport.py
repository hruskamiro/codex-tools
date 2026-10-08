from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_tools import relay, relay_cache, relay_transport


def agent() -> relay_cache.CachedAgent:
    return relay_cache.CachedAgent(
        agent_id="agent-1",
        session_id="session-1",
        thread_id="thread-1",
        rollout_id="rollout-1",
        title="Relay work",
        title_source="index",
        cwd="/work/codex-tools",
        source="cli",
        created_at="2026-10-07T10:00:00Z",
        updated_at="2026-10-07T11:00:00Z",
        origin_path="/private/session.jsonl",
        status="ready",
        status_label="Ready",
        records=(
            relay_cache.CachedRecord(
                "2026-10-07T11:00:00Z", "assistant", "Done", 4, "message"
            ),
        ),
    )


def cached_sibling_agent() -> relay_cache.CachedAgent:
    return replace(
        agent(),
        agent_id="agent-2",
        session_id="session-2",
        thread_id="thread-2",
        rollout_id="rollout-2",
        title="Other work",
    )


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class RelayTransportTests(unittest.TestCase):
    def test_serve_parser_accepts_control_socket_override(self) -> None:
        args = relay.parse_args(
            ["serve", "--control-socket", "/tmp/test-control.sock"]
        )

        self.assertEqual(args.control_socket, Path("/tmp/test-control.sock"))

    def test_server_stops_cleanly_on_keyboard_interrupt(self) -> None:
        class Server:
            socket = object()

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                self.closed = True

        server = Server()
        config = {
            "identity": {"token": "key"},
        }

        with patch.object(relay_transport, "ThreadingHTTPServer", return_value=server):
            relay_transport.serve(
                object(),  # type: ignore[arg-type]
                config,
                "127.0.0.1",
                8780,
            )

        self.assertTrue(server.closed)

    def test_initialize_creates_private_identity_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "relay.json"

            config = relay_transport.initialize_config(path, "Laptop")

            self.assertEqual(config["identity"]["name"], "Laptop")
            self.assertGreaterEqual(len(config["identity"]["token"]), 32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(FileExistsError):
                relay_transport.initialize_config(path, "Again")

    def test_transport_policy_allows_loopback_and_https(self) -> None:
        self.assertEqual(
            relay_transport.validate_url("http://127.0.0.2:8780/"),
            "http://127.0.0.2:8780",
        )
        self.assertEqual(
            relay_transport.validate_url("https://relay.example:8780"),
            "https://relay.example:8780",
        )
        with self.assertRaisesRegex(ValueError, "unencrypted"):
            relay_transport.validate_url("http://10.0.0.2:8780")
        self.assertEqual(
            relay_transport.validate_url(
                "http://10.0.0.2:8780", allow_insecure_http=True
            ),
            "http://10.0.0.2:8780",
        )

    def test_server_policy_rejects_remote_plain_http_and_bad_ports(self) -> None:
        with self.assertRaisesRegex(ValueError, "unencrypted"):
            relay_transport.validate_server_transport("0.0.0.0", 8780)
        with self.assertRaisesRegex(ValueError, "between"):
            relay_transport.validate_server_transport("127.0.0.1", 70000)
        with self.assertRaisesRegex(ValueError, "together"):
            relay_transport.validate_server_transport(
                "127.0.0.1", 8780, tls_cert=Path("cert.pem")
            )

    def test_add_peer_persists_connection_without_changing_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "relay.json"
            original = relay_transport.initialize_config(path, "Laptop")

            peer = relay_transport.add_peer(
                path,
                "desktop",
                "http://10.0.0.2:8780/",
                "secret-key",
                allow_insecure_http=True,
            )
            loaded = relay_transport.load_config(path)

            self.assertEqual(loaded["identity"], original["identity"])
            self.assertEqual(loaded["peers"]["desktop"], peer)
            self.assertEqual(peer["url"], "http://10.0.0.2:8780")

    def test_snapshot_round_trip_hides_origin_and_tracks_remote_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = relay_cache.RelayCache(root / "source.sqlite3")
            target = relay_cache.RelayCache(root / "target.sqlite3")
            source.replace_host_snapshot("local", "This machine", [agent()])
            config = {
                "version": 1,
                "identity": {"id": "host-1", "name": "Desktop", "token": "key"},
                "peers": {},
            }

            payload = relay_transport.snapshot_payload(source, config)
            count = relay_transport.apply_snapshot(
                target, payload, base_url="https://desktop:8780"
            )

            self.assertEqual(count, 1)
            self.assertNotIn("origin_path", payload["agents"][0])
            self.assertEqual(target.hosts()[0]["base_url"], "https://desktop:8780")
            self.assertEqual(target.agents("host-1")[0]["title"], "Relay work")
            self.assertEqual(target.records("host-1", "agent-1")[0]["text"], "Done")

    def test_fetch_uses_bearer_key(self) -> None:
        payload = {"protocol": 1, "host": {"id": "h", "name": "Host"}, "agents": []}
        response = FakeResponse(json.dumps(payload).encode())

        with patch.object(relay_transport, "urlopen", return_value=response) as opened:
            result = relay_transport.fetch_snapshot(
                {"url": "http://localhost:8780", "token": "pairing-key"}
            )

        request = opened.call_args.args[0]
        self.assertEqual(result, payload)
        self.assertEqual(request.get_header("Authorization"), "Bearer pairing-key")

    def test_fetch_agent_targets_only_encoded_agent(self) -> None:
        payload = {
            "protocol": 1,
            "host": {"id": "h", "name": "Host"},
            "agent": {"agent_id": "agent one"},
        }
        response = FakeResponse(json.dumps(payload).encode())

        with patch.object(relay_transport, "urlopen", return_value=response) as opened:
            result = relay_transport.fetch_agent(
                {"url": "http://localhost:8780", "token": "key"},
                "agent one",
            )

        self.assertEqual(result, payload)
        self.assertEqual(
            opened.call_args.args[0].full_url,
            "http://localhost:8780/v1/agents/agent%20one",
        )

    def test_agent_snapshot_updates_one_conversation_without_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = relay_cache.RelayCache(root / "source.sqlite3")
            target = relay_cache.RelayCache(root / "target.sqlite3")
            source.replace_host_snapshot("local", "This machine", [agent()])
            target.replace_host_snapshot(
                "host-1",
                "Desktop",
                [agent(), cached_sibling_agent()],
            )
            config = {
                "identity": {"id": "host-1", "name": "Desktop", "token": "key"}
            }

            payload = relay_transport.agent_snapshot_payload(
                source, config, "agent-1"
            )
            self.assertIsNotNone(payload)
            relay_transport.apply_agent_snapshot(
                target,
                payload or {},
                base_url="https://desktop:8780",
            )

            self.assertEqual(
                {item["agent_id"] for item in target.agents("host-1")},
                {"agent-1", "agent-2"},
            )

    def test_peer_listing_never_prints_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "relay.json"
            relay_transport.initialize_config(path, "Laptop")
            relay_transport.add_peer(
                path, "desktop", "https://desktop:8780", "do-not-print"
            )
            output = io.StringIO()

            with redirect_stdout(output):
                result = relay.command_peers(
                    argparse.Namespace(config_file=path, json=True)
                )

        self.assertEqual(result, 0)
        self.assertNotIn("do-not-print", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())[0]["name"], "desktop")


if __name__ == "__main__":
    unittest.main()
