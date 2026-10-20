from __future__ import annotations

import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from codex_tools import attachments, relay_cache, relay_transport, typeset, viewer


class Response(io.BytesIO):
    def __init__(self, body: bytes, content_type: str = "application/pdf") -> None:
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(body))


class RemoteTypesetTests(unittest.TestCase):
    def cache_with_agent(self, root: Path) -> relay_cache.RelayCache:
        path = root / "session.jsonl"
        path.write_text(json.dumps({
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Older answer"}]},
        }) + "\n", encoding="utf-8")
        cache = relay_cache.RelayCache(root / "relay.sqlite3")
        cache.replace_host_snapshot("local", "Desktop", [relay_cache.CachedAgent(
            agent_id="agent-1", session_id="session-1", thread_id="thread-1",
            rollout_id="rollout-1", title="Work", title_source="index", cwd=str(root),
            source="cli", created_at="", updated_at="", origin_path=str(path),
            records=(relay_cache.CachedRecord("", "assistant", "Cached answer", 2, "message"),),
        )])
        return cache

    def test_renderer_resolves_server_records_and_rejects_unknown_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = self.cache_with_agent(Path(temporary))
            with patch.object(typeset, "render_pdf") as render:
                for header, code in (("invalid", "auto"), ("external", "invalid")):
                    with self.assertRaises(ValueError):
                        relay_transport.render_agent_record(
                            cache, "agent-1", "a" * 64,
                            header_mode=header, code_mode=code,
                        )
                for text in ("Cached answer", "Older answer"):
                    relay_transport.render_agent_record(
                        cache, "agent-1", attachments.attachment_record_key(text),
                        header_mode="external", code_mode="auto",
                    )
                    self.assertEqual(render.call_args.args, (text,))
                with self.assertRaises(FileNotFoundError):
                    relay_transport.render_agent_record(
                        cache, "agent-1", "a" * 64,
                        header_mode="external", code_mode="auto",
                    )
                with self.assertRaises(FileNotFoundError):
                    relay_transport.render_agent_record(
                        cache, "other-agent", attachments.attachment_record_key("Cached answer"),
                        header_mode="external", code_mode="auto",
                    )
                self.assertEqual(render.call_count, 2)

    def test_endpoint_authenticates_and_allows_only_render_options(self) -> None:
        handler = relay_transport.make_handler(object(), {"identity": {"token": "secret"}})
        instance = handler.__new__(handler)
        instance.path = "/v1/agents/agent-1/typeset/" + "a" * 64
        instance.headers = Message()
        responses = []
        served = []
        instance._json = lambda payload, status=200: responses.append((payload, status))
        instance._attachment = lambda *args: served.append(args)
        with patch.object(relay_transport, "render_agent_record") as render:
            instance.do_GET()
            self.assertEqual(responses[-1][1], 401)
            render.assert_not_called()
            instance.headers["Authorization"] = "Bearer secret"
            for suffix in ("?path=/etc/passwd", "?markdown=arbitrary", "?code=auto&code=verbatim"):
                instance.path = "/v1/agents/agent-1/typeset/" + "a" * 64 + suffix
                instance.do_GET()
                self.assertEqual(responses[-1][1], 400)
            render.assert_not_called()
            with tempfile.TemporaryDirectory() as temporary:
                pdf = Path(temporary) / "bubble.pdf"
                pdf.write_bytes(b"%PDF-1.4\n")
                render.return_value = typeset.TypesetResult(True, "b" * 64, pdf)
                instance.path = "/v1/agents/agent-1/typeset/" + "a" * 64 + "?code=verbatim"
                instance.do_GET()
                self.assertEqual(served[-1], (pdf, "application/pdf", "Codex answer.pdf"))
                render.return_value = typeset.TypesetResult(False, "b" * 64, None, error="xelatex was not found")
                instance.do_GET()
                self.assertEqual(responses[-1][1], 503)
                self.assertIn("xelatex", responses[-1][0]["error"])

    def test_download_authentication_and_pdf_validation(self) -> None:
        peer = {"url": "http://localhost:8780", "token": "secret"}
        with patch.object(relay_transport, "urlopen", return_value=Response(b"%PDF-1.4\n")) as opened:
            self.assertEqual(relay_transport.fetch_typeset(peer, "agent one", "a" * 64), b"%PDF-1.4\n")
            request = opened.call_args.args[0]
            self.assertIn("/v1/agents/agent%20one/typeset/", request.full_url)
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        for body, mime in ((b"not a pdf", "application/pdf"), (b"%PDF-1.4\n", "text/html")):
            with patch.object(relay_transport, "urlopen", return_value=Response(body, mime)):
                with self.assertRaises(ValueError):
                    relay_transport.fetch_typeset(peer, "agent-1", "a" * 64)
        with (
            patch.object(relay_transport, "MAX_TYPESET_BYTES", 4),
            patch.object(relay_transport, "urlopen", return_value=Response(b"%PDF-1.4\n")),
        ):
            with self.assertRaisesRegex(ValueError, "safety limit"):
                relay_transport.fetch_typeset(peer, "agent-1", "a" * 64)

    def test_download_explains_remote_rendering_failure(self) -> None:
        error = HTTPError("http://peer", 503, "Unavailable", Message(), io.BytesIO(b'{"error":"xelatex was not found"}'))
        with patch.object(relay_transport, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "xelatex was not found"):
                relay_transport.fetch_typeset({"url": "http://peer", "token": "secret"}, "agent-1", "a" * 64)

    def test_remote_pdf_cache_works_offline_and_tracks_content_and_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = SimpleNamespace()
            session = {"hostId": "desktop", "id": "agent-1"}
            record = {"text": "An answer", "timestamp": ""}
            with (
                patch.object(typeset, "typeset_cache_dir", return_value=Path(temporary)),
                patch.object(viewer, "relay_peer_for_host", return_value=("desktop", {})) as lookup,
                patch.object(relay_transport, "fetch_typeset", return_value=b"%PDF-1.4\n") as fetch,
            ):
                result = viewer.render_relay_pdf(state, session, record, header_mode="external", code_mode="auto")
                self.assertTrue(result.ok)
                self.assertFalse(result.cached)
                self.assertEqual(result.pdf_path.read_bytes(), b"%PDF-1.4\n")
                fetch.side_effect = OSError("offline")
                cached = viewer.render_relay_pdf(state, session, record, header_mode="external", code_mode="auto")
                self.assertTrue(cached.ok)
                self.assertTrue(cached.cached)
                fetch.assert_called_once()
                lookup.assert_called_once()
                for changed, code in (({"text": "Changed", "timestamp": ""}, "auto"), (record, "verbatim")):
                    missing = viewer.render_relay_pdf(state, session, changed, header_mode="external", code_mode=code)
                    self.assertFalse(missing.ok)
                    self.assertNotEqual(missing.key, result.key)
                    self.assertIn("offline", missing.error)
                    self.assertIn("Markdown", missing.error)

    def test_auto_selection_uses_peer_only_when_local_xelatex_is_missing(self) -> None:
        session = {"id": "agent-1", "hostId": "desktop", "cwd": ""}
        records = [{"role": "assistant", "text": "Answer", "timestamp": "", "line_no": 1}]
        state = SimpleNamespace(typeset_debug=False, typeset_backend="auto")
        result = typeset.TypesetResult(True, "a" * 64, Path("bubble.pdf"))
        for backend, executable, expected in (
            ("auto", None, "remote"), ("auto", "/bin/xelatex", "local"),
            ("remote", "/bin/xelatex", "remote"), ("local", None, "local"),
        ):
            with (
                self.subTest(backend=backend, executable=executable),
                patch.object(viewer, "relay_session_snapshot", return_value=(session, records, None)),
                patch.object(viewer.shutil, "which", return_value=executable),
                patch.object(viewer, "render_relay_pdf", return_value=result) as remote,
                patch.object(typeset, "render_pdf", return_value=result) as local,
            ):
                payload = viewer.handle_typeset(state, {"source": ["relay"], "backend": [backend]})
                self.assertEqual(payload["typesetBackend"], expected)
                self.assertEqual(remote.call_count, int(expected == "remote"))
                self.assertEqual(local.call_count, int(expected == "local"))

    def test_backend_option_survives_background_viewer_start(self) -> None:
        args = viewer.parse_args(["start", "--typeset-backend", "remote"])
        state = viewer.server_state_from_args(args)
        self.assertEqual(state.typeset_backend, "remote")
        command = viewer.daemon_command(args)
        self.assertEqual(command[command.index("--typeset-backend") + 1], "remote")

    def test_doctor_does_not_require_local_tex_for_configured_remote_backend(self) -> None:
        with (
            patch.object(viewer, "viewer_startup_checks", return_value=[("browser", True, "open")]),
            patch.object(viewer, "latex_requirement_checks", return_value=[("xelatex", False, "missing")]),
            patch.object(viewer, "read_default_view", return_value="latex"),
            patch.object(viewer.config, "value", return_value="remote"),
            patch.object(relay_transport, "load_config", return_value={"peers": {"desktop": {}}}),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(viewer.command_doctor(viewer.parse_args(["doctor"])), 0)
            self.assertIn("no local TeX is required", output.getvalue())
