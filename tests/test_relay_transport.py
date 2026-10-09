from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from email.message import Message
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

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


def session_event(role: str, text: str) -> str:
    return json.dumps(
        {
            "timestamp": "2026-10-09T12:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": role,
                "content": [{"type": "input_text", "text": text}],
            },
        }
    )


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class RelayTransportTests(unittest.TestCase):
    def test_jsonl_cursor_reads_only_complete_appended_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl"
            path.write_text(session_event("user", "Existing") + "\n", encoding="utf-8")
            cursor = relay_transport.event_cursor_for_path(
                path, "agent-1", "secret"
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(session_event("assistant", "Complete") + "\n")
                handle.write(session_event("assistant", "Partial"))

            first = relay_transport.read_jsonl_events(
                path, "agent-1", cursor, "secret"
            )

            self.assertEqual(
                [event["record"]["text"] for event in first["events"]],
                ["Complete"],
            )
            self.assertFalse(first["has_more"])

            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
            second = relay_transport.read_jsonl_events(
                path, "agent-1", first["next_cursor"], "secret"
            )
            self.assertEqual(
                [event["record"]["text"] for event in second["events"]],
                ["Partial"],
            )

    def test_jsonl_cursor_requires_reset_after_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl"
            path.write_text(
                session_event("user", "A reasonably long existing message") + "\n",
                encoding="utf-8",
            )
            cursor = relay_transport.event_cursor_for_path(
                path, "agent-1", "secret"
            )
            path.write_text(session_event("user", "short") + "\n", encoding="utf-8")

            result = relay_transport.read_jsonl_events(
                path, "agent-1", cursor, "secret"
            )

            self.assertTrue(result["reset_required"])

    def test_jsonl_cursor_requires_reset_after_same_inode_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl"
            original = session_event("user", "original") + "\n"
            replacement = session_event("user", "changed!") + "\n"
            self.assertEqual(len(original), len(replacement))
            path.write_text(original, encoding="utf-8")
            cursor = relay_transport.event_cursor_for_path(
                path, "agent-1", "secret"
            )
            path.write_text(replacement, encoding="utf-8")

            result = relay_transport.read_jsonl_events(
                path, "agent-1", cursor, "secret"
            )

            self.assertTrue(result["reset_required"])

    def test_jsonl_cursor_is_bound_to_agent_and_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl"
            path.write_text(session_event("user", "Existing") + "\n", encoding="utf-8")
            cursor = relay_transport.event_cursor_for_path(
                path, "agent-1", "secret"
            )

            with self.assertRaisesRegex(ValueError, "invalid relay event cursor"):
                relay_transport.read_jsonl_events(
                    path, "agent-2", cursor, "secret"
                )
            with self.assertRaisesRegex(ValueError, "invalid relay event cursor"):
                relay_transport.read_jsonl_events(
                    path, "agent-1", cursor, "different"
                )

    def test_incremental_events_append_to_cached_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "relay.sqlite3")
            cached = replace(agent(), event_cursor="old-cursor")
            cache.replace_host_snapshot("host-1", "Desktop", [cached])
            payload = {
                "protocol": 1,
                "host": {"id": "host-1", "name": "Desktop"},
                "agent_id": "agent-1",
                "events": [
                    {
                        "type": "record.appended",
                        "record": {
                            "timestamp": "2026-10-09T12:00:00Z",
                            "role": "assistant",
                            "text": "New response",
                            "line_no": 8,
                            "record_type": "message",
                        },
                    }
                ],
                "next_cursor": "new-cursor",
            }

            count = relay_transport.apply_agent_events(cache, payload)

            self.assertEqual(count, 1)
            self.assertEqual(
                [record["text"] for record in cache.records("host-1", "agent-1")],
                ["Done", "New response"],
            )
            self.assertEqual(
                cache.agents("host-1")[0]["event_cursor"], "new-cursor"
            )

    def test_agent_snapshot_establishes_incremental_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_path = root / "session.jsonl"
            session_path.write_text(
                session_event("assistant", "Existing") + "\n", encoding="utf-8"
            )
            cache = relay_cache.RelayCache(root / "relay.sqlite3")
            cache.replace_host_snapshot(
                "local",
                "This machine",
                [replace(agent(), origin_path=str(session_path))],
            )
            config = {
                "identity": {
                    "id": "host-1",
                    "name": "Desktop",
                    "token": "secret",
                }
            }

            payload = relay_transport.agent_snapshot_payload(
                cache, config, "agent-1"
            )

            self.assertIsNotNone(payload)
            remote_agent = (payload or {})["agent"]
            self.assertNotIn("origin_path", remote_agent)
            cursor = relay_transport.decode_event_cursor(
                remote_agent["events_cursor"], "agent-1", "secret"
            )
            self.assertEqual(cursor["o"], session_path.stat().st_size)

    def test_event_endpoint_returns_only_appended_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_path = root / "session.jsonl"
            session_path.write_text(
                session_event("user", "Existing") + "\n", encoding="utf-8"
            )
            cache = relay_cache.RelayCache(root / "relay.sqlite3")
            cache.replace_host_snapshot(
                "local",
                "This machine",
                [replace(agent(), origin_path=str(session_path))],
            )
            config = {
                "identity": {
                    "id": "host-1",
                    "name": "Desktop",
                    "token": "secret",
                }
            }
            cursor = relay_transport.event_cursor_for_path(
                session_path, "agent-1", "secret"
            )
            with session_path.open("a", encoding="utf-8") as handle:
                handle.write(session_event("assistant", "Appended") + "\n")
            handler = relay_transport.make_handler(cache, config)
            instance = handler.__new__(handler)
            instance.path = "/v1/agents/agent-1/events?" + urlencode(
                {"after": cursor, "wait": 0}
            )
            headers = Message()
            headers["Authorization"] = "Bearer secret"
            instance.headers = headers
            responses: list[tuple[dict[str, object], object]] = []
            instance._json = lambda payload, status=relay_transport.HTTPStatus.OK: responses.append((payload, status))  # type: ignore[method-assign]

            instance.do_GET()

            payload, status = responses[0]
            self.assertEqual(status, relay_transport.HTTPStatus.OK)
            self.assertEqual(payload["agent_id"], "agent-1")
            events = payload["events"]
            self.assertEqual(events[0]["record"]["text"], "Appended")  # type: ignore[index]

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

    def test_relay_server_delivery_queues_message_into_local_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = argparse.Namespace(
                config_file=root / "relay.json",
                cache_file=root / "relay.sqlite3",
                host="127.0.0.1",
                port=8780,
                tls_cert=None,
                tls_key=None,
                allow_insecure_http=False,
                sessions_root=root / "sessions",
                session_index=root / "index.jsonl",
                control_socket=None,
                codex_bin="codex-test",
            )
            config = {
                "identity": {"id": "host", "name": "Desktop", "token": "key"},
                "peers": {},
            }
            with (
                patch.object(relay.relay_transport, "load_config", return_value=config),
                patch.object(relay.relay_transport, "validate_server_transport"),
                patch.object(relay.relay_cache, "refresh_local", return_value=1),
                patch.object(relay.relay_cache, "refresh_local_agent") as refresh_agent,
                patch.object(relay.agents_ui, "send_message") as send_message,
                patch.object(relay.relay_transport, "serve") as serve,
                redirect_stdout(io.StringIO()),
            ):
                result = relay.command_serve(args)
                deliver = serve.call_args.kwargs["deliver_message"]
                deliver("message-1", "agent-1", {"text": "Continue"})

        self.assertEqual(result, 0)
        refresh_agent.assert_called_once()
        self.assertEqual(
            send_message.call_args.args[1],
            {"host": "local", "agent": "agent-1", "message": "Continue"},
        )
        self.assertEqual(send_message.call_args.args[2], "codex-test")

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

    def test_fetch_agent_events_sends_cursor_and_long_poll_timeout(self) -> None:
        payload = {
            "protocol": 1,
            "host": {"id": "h", "name": "Host"},
            "agent_id": "agent one",
            "events": [],
            "next_cursor": "next",
        }
        response = FakeResponse(json.dumps(payload).encode())

        with patch.object(relay_transport, "urlopen", return_value=response) as opened:
            result = relay_transport.fetch_agent_events(
                {"url": "http://localhost:8780", "token": "key"},
                "agent one",
                "cursor/value",
                wait=20,
            )

        self.assertEqual(result, payload)
        request = opened.call_args.args[0]
        self.assertIn("/v1/agents/agent%20one/events?", request.full_url)
        self.assertIn("after=cursor%2Fvalue", request.full_url)
        self.assertIn("wait=20", request.full_url)
        self.assertEqual(opened.call_args.kwargs["timeout"], 25.0)

    def test_post_message_uses_authenticated_json_request(self) -> None:
        response = FakeResponse(
            json.dumps({"delivery": "accepted", "message_id": "message-1"}).encode()
        )
        message = {
            "message_id": "message-1",
            "agent_id": "agent-1",
            "kind": "message",
            "payload": {"text": "Continue"},
        }

        with patch.object(relay_transport, "urlopen", return_value=response) as opened:
            result = relay_transport.post_message(
                {"url": "http://localhost:8780", "token": "pairing-key"},
                message,
            )

        request = opened.call_args.args[0]
        self.assertEqual(result["delivery"], "accepted")
        self.assertEqual(request.full_url, "http://localhost:8780/v1/messages")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer pairing-key")
        self.assertEqual(json.loads(request.data), message)

    def test_deliver_pending_marks_acknowledged_message_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "relay.sqlite3")
            cache.replace_host_snapshot("home", "Home", [agent()])
            message_id = cache.enqueue("home", "agent-1", {"text": "Continue"})

            with patch.object(
                relay_transport,
                "post_message",
                return_value={"delivery": "accepted", "message_id": message_id},
            ) as post:
                result = relay_transport.deliver_pending(
                    cache,
                    {"url": "https://home:8780", "token": "key"},
                    "home",
                )

            self.assertEqual(result["delivered"], 1)
            self.assertEqual(cache.outbox()[0]["state"], "delivered")
            self.assertEqual(post.call_args.args[1]["message_id"], message_id)

    def test_deliver_pending_keeps_network_failure_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "relay.sqlite3")
            cache.replace_host_snapshot("home", "Home", [agent()])
            first = cache.enqueue("home", "agent-1", {"text": "Continue"})
            second = cache.enqueue("home", "agent-1", {"text": "And then"})

            with patch.object(
                relay_transport,
                "post_message",
                side_effect=OSError("network unavailable"),
            ) as post:
                result = relay_transport.deliver_pending(
                    cache,
                    {"url": "https://home:8780", "token": "key"},
                    "home",
                )

            messages = {item["message_id"]: item for item in cache.outbox()}
            self.assertEqual(result["pending"], 2)
            self.assertEqual(messages[first]["state"], "pending")
            self.assertEqual(messages[first]["last_error"], "network unavailable")
            self.assertEqual(messages[second]["attempt_count"], 0)
            post.assert_called_once()

    def test_deliver_pending_marks_gone_agent_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "relay.sqlite3")
            cache.replace_host_snapshot("home", "Home", [agent()])
            cache.enqueue("home", "agent-1", {"text": "Continue"})
            gone = relay_transport.HTTPError(
                "https://home:8780/v1/messages",
                410,
                "Gone",
                {},
                None,
            )

            with patch.object(
                relay_transport, "post_message", side_effect=gone
            ):
                result = relay_transport.deliver_pending(
                    cache,
                    {"url": "https://home:8780", "token": "key"},
                    "home",
                )

            self.assertEqual(result["failed"], 1)
            self.assertEqual(cache.outbox()[0]["state"], "failed")

    def test_message_endpoint_authenticates_and_deduplicates_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = relay_cache.RelayCache(Path(temporary) / "relay.sqlite3")
            delivered: list[tuple[str, str, dict[str, str]]] = []
            handler = relay_transport.make_handler(
                cache,
                {"identity": {"token": "secret"}},
                deliver_message=lambda message_id, agent_id, payload: delivered.append(
                    (message_id, agent_id, payload)  # type: ignore[arg-type]
                ),
            )
            message = {
                "message_id": "message-1",
                "agent_id": "agent-1",
                "kind": "message",
                "payload": {"text": "Continue"},
            }

            def request() -> dict[str, object]:
                body = json.dumps(message).encode()
                headers = Message()
                headers["Authorization"] = "Bearer secret"
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(body))
                instance = handler.__new__(handler)
                instance.path = "/v1/messages"
                instance.headers = headers
                instance.rfile = io.BytesIO(body)
                responses: list[dict[str, object]] = []
                instance._json = lambda payload, status=relay_transport.HTTPStatus.OK: responses.append(payload)  # type: ignore[method-assign]
                instance.do_POST()
                return responses[0]

            first = request()
            second = request()

        self.assertEqual(first["delivery"], "accepted")
        self.assertTrue(second["duplicate"])
        self.assertEqual(
            delivered,
            [("message-1", "agent-1", {"text": "Continue"})],
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
