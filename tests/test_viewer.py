from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_tools import search, typeset, viewer


class ViewerCommandTests(unittest.TestCase):
    def test_remote_bind_requires_explicit_acknowledgement(self) -> None:
        self.assertTrue(
            viewer.validate_bind_host(
                SimpleNamespace(host="127.0.0.1", allow_remote=False)
            )
        )
        self.assertFalse(
            viewer.validate_bind_host(
                SimpleNamespace(host="0.0.0.0", allow_remote=False)
            )
        )
        self.assertTrue(
            viewer.validate_bind_host(
                SimpleNamespace(host="0.0.0.0", allow_remote=True)
            )
        )

    def test_remote_acknowledgement_reaches_daemon(self) -> None:
        args = viewer.parse_args(["start", "--host", "0.0.0.0", "--allow-remote"])

        self.assertIn("--allow-remote", viewer.daemon_command(args))

    def test_session_summary_uses_shared_title_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl"
            path.touch()
            session = search.Session(
                path=path,
                session_id="child",
                parent_thread_id="parent",
                records=[search.TextRecord("", "user", "Fallback", 1, "message")],
            )
            titles = {
                "parent": search.SessionTitle(
                    "Indexed title", search.TITLE_SOURCE_SESSION_INDEX
                )
            }

            with patch.object(viewer, "sampled_session", return_value=session):
                summary = viewer.session_summary(path, titles)

        self.assertEqual(summary["title"], "Indexed title")
        self.assertEqual(summary["titleSource"], search.TITLE_SOURCE_SESSION_INDEX)

    def test_semantic_prose_highlighting_skips_code(self) -> None:
        self.assertIn('"code",', viewer.VIEW_JS)
        self.assertIn('token.startsWith("“")', viewer.VIEW_JS)
        self.assertIn('token.startsWith("‘")', viewer.VIEW_JS)

    def test_message_navigation_keeps_a_cursor_near_the_page_end(self) -> None:
        for script in (viewer.VIEW_JS, viewer.TYPESET_JS):
            self.assertIn("let messageNavigationIndex = -1", script)
            self.assertIn("cursorIsVisible", script)
            self.assertIn("messageNavigationIndex = nextIndex", script)

    def test_typeset_view_builds_selectable_pdf_text_layer(self) -> None:
        self.assertIn("new pdf.TextLayer", viewer.TYPESET_JS)

    def test_rendered_pdf_page_does_not_keep_loader_minimum_height(self) -> None:
        base_rule = viewer.APP_CSS.split(".typeset-pdf-page {", 1)[1].split("}", 1)[0]
        loading_rule = viewer.APP_CSS.split(
            ".typeset-pdf-page.is-loading,\n.typeset-pdf-page.is-error {", 1
        )[1].split("}", 1)[0]

        self.assertNotIn("min-height", base_rule)
        self.assertIn("min-height: 120px", loading_rule)
        self.assertIn("page.getTextContent", viewer.TYPESET_JS)
        self.assertIn('textLayerElement.className = "textLayer"', viewer.TYPESET_JS)
        self.assertIn(".typeset-pdf-page .textLayer", viewer.APP_CSS)

    def test_external_typeset_header_joins_the_pdf_surface(self) -> None:
        self.assertIn(".typeset-external-header + .typeset-pdf-page", viewer.APP_CSS)
        self.assertIn("border-radius: 0 0 8px 8px", viewer.APP_CSS)
        self.assertIn("typeset-external-header-actions", viewer.TYPESET_JS)
        self.assertIn('>Open isolated ↗</a>', viewer.TYPESET_JS)
        self.assertIn(
            'typesetHeaderMode === "embedded" ? typesetDebugBar(record) : ""',
            viewer.TYPESET_JS,
        )
        self.assertIn("assistant-continued", viewer.TYPESET_JS)
        self.assertIn("assistant-continuing", viewer.TYPESET_JS)
        self.assertIn("externalTypesetHeader(record, !previousIsAssistant)", viewer.TYPESET_JS)
        self.assertIn(
            ".typeset-message.assistant-continued .typeset-external-header",
            viewer.APP_CSS,
        )
        continued_header_rule = viewer.APP_CSS.split(
            ".typeset-message.assistant-continued .typeset-external-header", 1
        )[1].split("}", 1)[0]
        self.assertIn("border-top: 0", continued_header_rule)
        self.assertIn(
            "border-left: 1px solid var(--panel-line)", continued_header_rule
        )
        continuing_rule = viewer.APP_CSS.split(
            ".typeset-message.assistant-continuing .typeset-pdf-page,", 1
        )[1].split("}", 1)[0]
        self.assertIn("border-bottom: 0", continuing_rule)

    def test_typeset_header_can_copy_original_markdown(self) -> None:
        self.assertIn('title="Copy Markdown"', viewer.TYPESET_JS)
        self.assertIn("${copyMarkdownButton(record)}", viewer.TYPESET_JS)
        self.assertIn("navigator.clipboard?.writeText", viewer.TYPESET_JS)
        self.assertIn('lastConversationData?.records?.find(', viewer.TYPESET_JS)
        self.assertIn('copyRecordMarkdown(button)', viewer.TYPESET_JS)
        self.assertIn(".typeset-copy-markdown", viewer.APP_CSS)

    def test_restart_command_stops_then_starts_viewer(self) -> None:
        args = viewer.parse_args(["restart"])

        with (
            patch.object(viewer, "command_stop", return_value=0) as stop,
            patch.object(viewer, "command_start", return_value=0) as start,
        ):
            result = args.func(args)

        self.assertEqual(result, 0)
        stop.assert_called_once_with(args)
        start.assert_called_once_with(args)

    def test_statusbar_is_collapsed_behind_slash_toggle(self) -> None:
        for html in (viewer.VIEW_HTML, viewer.TYPESET_HTML):
            self.assertIn('id="viewStatusbar" class="view-statusbar"', html)
            self.assertIn('id="statusbarToggle"', html)
            self.assertIn('aria-expanded="false"', html)
            self.assertIn('id="statusbarActions" class="statusbar-actions"', html)
            self.assertIn('>Choose <kbd>C</kbd></a>', html)
            self.assertNotIn('class="status-link"', html)
        self.assertIn(
            ".view-statusbar.is-expanded .statusbar-actions", viewer.APP_CSS
        )
        self.assertIn(".conversation.statusbar-expanded", viewer.APP_CSS)
        self.assertIn(".view-statusbar.is-expanded .statusbar-actions", viewer.APP_CSS)
        self.assertIn("max-width: calc(100vw - 76px)", viewer.APP_CSS)
        self.assertIn(
            "padding: 30px clamp(20px, 4vw, 56px) 24px", viewer.APP_CSS
        )
        for script in (viewer.VIEW_JS, viewer.TYPESET_JS):
            self.assertIn('event.key === "/"', script)
            self.assertIn("toggleStatusbar", script)
            self.assertIn(
                'classList.toggle("statusbar-expanded", expanded)', script
            )
            self.assertIn('setAttribute("aria-expanded"', script)

    def test_statusbar_switches_between_markdown_and_latex(self) -> None:
        view_actions = viewer.VIEW_HTML.split(
            'id="statusbarActions" class="statusbar-actions"', 1
        )[1].split("</div>", 1)[0]
        typeset_actions = viewer.TYPESET_HTML.split(
            'id="statusbarActions" class="statusbar-actions"', 1
        )[1].split("</div>", 1)[0]

        self.assertIn('id="typesetViewLink"', view_actions)
        self.assertIn(">LaTeX <kbd>T</kbd></a>", view_actions)
        self.assertIn('id="normalViewLink"', typeset_actions)
        self.assertIn(">Markdown <kbd>T</kbd></a>", typeset_actions)
        self.assertIn("els.typesetViewLink.href", viewer.VIEW_JS)
        self.assertIn("els.normalViewLink.href", viewer.TYPESET_JS)

    def test_statusbar_actions_show_and_handle_keyboard_shortcuts(self) -> None:
        for html in (viewer.VIEW_HTML, viewer.TYPESET_HTML):
            for key in ("U", "L", "E", "A", "R", "T", "C"):
                self.assertIn(f"<kbd>{key}</kbd>", html)
            self.assertIn("Left Arrow shortcut", html)
            self.assertIn("Right Arrow shortcut", html)

        for script in (viewer.VIEW_JS, viewer.TYPESET_JS):
            self.assertIn('["u", "l", "e", "a", "t", "c"]', script)
            self.assertIn('key === "u"', script)
            self.assertIn('key === "l"', script)
            self.assertIn('key === "e"', script)
            self.assertIn('key === "a"', script)
            self.assertIn('key === "t"', script)
            self.assertIn('key === "c"', script)
            self.assertIn('location.assign("/")', script)

        self.assertIn("location.assign(els.typesetViewLink.href)", viewer.VIEW_JS)
        self.assertIn("location.assign(els.normalViewLink.href)", viewer.TYPESET_JS)

    def test_bundled_web_assets_are_default_with_cdn_override(self) -> None:
        args = viewer.parse_args(["restart"])
        self.assertEqual(args.web_assets, "bundled")
        self.assertIn("--web-assets", viewer.daemon_command(args))
        self.assertIn("bundled", viewer.daemon_command(args))

        bundled = viewer.viewer_document(viewer.VIEW_HTML, "bundled")
        cdn = viewer.viewer_document(viewer.VIEW_HTML, "cdn")
        self.assertIn('src="/vendor/marked.min.js"', bundled)
        self.assertNotIn("cdn.jsdelivr.net", bundled)
        self.assertIn('data-web-assets="cdn"', cdn)
        self.assertIn("cdn.jsdelivr.net/npm/marked@15.0.12", cdn)

    def test_bundled_pdf_and_katex_assets_are_present(self) -> None:
        expected = [
            "pdf.min.mjs",
            "pdf.worker.min.mjs",
            "katex/katex.min.css",
            "katex/fonts/KaTeX_Main-Regular.woff2",
            "licenses/LICENSE-PDF.js",
        ]
        for relative in expected:
            self.assertTrue((viewer.VENDOR_DIR / relative).is_file(), relative)
        self.assertIn(': "/vendor/pdf.min.mjs"', viewer.TYPESET_JS)
        self.assertIn(': "/vendor/pdf.worker.min.mjs"', viewer.TYPESET_JS)

    def test_restart_can_enable_typeset_debug(self) -> None:
        args = viewer.parse_args(["restart", "--typeset-debug"])

        self.assertTrue(args.typeset_debug)
        self.assertIn("--typeset-debug", viewer.daemon_command(args))

    def test_isolated_typeset_request_forces_one_assistant_bubble(self) -> None:
        path = Path("/tmp/session.jsonl")
        session = search.Session(
            path=path,
            records=[
                search.TextRecord("", "user", "question", 10, "message"),
                search.TextRecord("", "assistant", "first", 11, "message"),
                search.TextRecord("", "assistant", "target", 12, "message"),
            ],
        )
        state = SimpleNamespace(typeset_debug=True, titles={})
        result = typeset.TypesetResult(True, "a" * 64, Path("bubble.pdf"))

        with (
            patch.object(viewer, "resolve_session_path", return_value=path),
            patch.object(viewer, "read_session", return_value=session),
            patch.object(viewer, "session_summary", return_value={"title": "Test"}),
            patch.object(viewer.typeset, "render_pdf", return_value=result) as render,
        ):
            payload = viewer.handle_typeset(state, {"line": ["12"]}, isolated=True)

        self.assertEqual([record["line_no"] for record in payload["records"]], [12])
        self.assertEqual(payload["debugLine"], 12)
        self.assertIn("?fresh=", payload["records"][0]["typeset"]["pdfUrl"])
        render.assert_called_once_with(
            "target",
            title="Assistant answer",
            force=True,
            header_mode="external",
            code_mode="auto",
        )
        self.assertEqual(payload["typesetHeaderMode"], "external")
        self.assertEqual(payload["typesetCodeMode"], "pygments")

    def test_isolated_typeset_request_requires_debug_mode(self) -> None:
        path = Path("/tmp/session.jsonl")
        session = search.Session(path=path)
        state = SimpleNamespace(typeset_debug=False, titles={})

        with (
            patch.object(viewer, "resolve_session_path", return_value=path),
            patch.object(viewer, "read_session", return_value=session),
        ):
            with self.assertRaises(PermissionError):
                viewer.handle_typeset(state, {"line": ["12"]}, isolated=True)

    def test_normal_typeset_api_rejects_line_targeting(self) -> None:
        path = Path("/tmp/session.jsonl")
        state = SimpleNamespace(typeset_debug=True, titles={})

        with patch.object(viewer, "resolve_session_path", return_value=path):
            with patch.object(viewer, "read_session", return_value=search.Session(path=path)):
                with self.assertRaises(FileNotFoundError):
                    viewer.handle_typeset(state, {"line": ["12"]})


if __name__ == "__main__":
    unittest.main()
