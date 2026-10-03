from __future__ import annotations

import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_tools import config, search, typeset, viewer


class ViewerCommandTests(unittest.TestCase):
    def test_unified_viewer_help_uses_the_unified_command_name(self) -> None:
        with patch("sys.stdout", new_callable=StringIO) as stdout:
            with self.assertRaises(SystemExit) as raised:
                viewer.parse_args(["--help"], prog="codex-tools viewer")

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("usage: codex-tools viewer", stdout.getvalue())

    def test_default_viewer_command_opens_the_picker(self) -> None:
        with patch.object(viewer, "command_pick", return_value=0) as pick:
            result = viewer.command_default(SimpleNamespace())

        self.assertEqual(result, 0)
        pick.assert_called_once()

    def test_viewer_doctor_reports_ready_and_missing_requirements(self) -> None:
        args = viewer.parse_args(["doctor"])
        ready = [("xelatex", True, "/usr/bin/xelatex")]
        missing = [("xelatex", False, "not found on PATH")]

        with patch.object(viewer, "latex_requirement_checks", return_value=ready):
            self.assertEqual(args.func(args), 0)
        with patch.object(viewer, "latex_requirement_checks", return_value=missing):
            self.assertEqual(args.func(args), 1)

    def test_default_view_preference_is_private_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config" / "viewer.json"

            self.assertEqual(viewer.read_default_view(config), "markdown")
            viewer.write_default_view("latex", config)

            self.assertEqual(viewer.read_default_view(config), "latex")
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.parent.stat().st_mode & 0o777, 0o700)

    def test_default_view_uses_unified_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            with patch.object(config.paths, "CONFIG_FILE", path):
                viewer.write_default_view("latex")

                self.assertEqual(viewer.read_default_view(), "latex")
                self.assertEqual(config.value("viewer.default_view"), "latex")

    def test_default_view_flags_select_the_preference_command(self) -> None:
        latex = viewer.parse_args(["--set-default-latex"])
        markdown = viewer.parse_args(["--set-default-markdown"])

        self.assertEqual(latex.default_view, "latex")
        self.assertEqual(markdown.default_view, "markdown")
        self.assertIs(latex.func, viewer.command_set_default_view)
        self.assertIs(markdown.func, viewer.command_set_default_view)

        with self.assertRaises(SystemExit):
            viewer.parse_args(["--set-default-latex", "start"])

    def test_chooser_and_terminal_urls_honor_default_view(self) -> None:
        chooser = viewer.chooser_document("latex")

        self.assertIn('data-default-view="latex"', chooser)
        self.assertIn('state.defaultView === "latex" ? "t" : "v"', viewer.APP_JS)
        self.assertEqual(
            viewer.view_url("http://localhost:8765", "/tmp/a.jsonl", 8, False, "abc", "latex"),
            "http://localhost:8765/t/abc?tail=8",
        )
        self.assertEqual(
            viewer.view_url("http://localhost:8765", "/tmp/a.jsonl", 8, False, mode="latex"),
            "http://localhost:8765/typeset?tail=8&path=%2Ftmp%2Fa.jsonl",
        )

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

    def test_session_deduplication_prefers_the_codex_cli_thread_rollout(self) -> None:
        items = [
            {
                "path": "/sessions/root.jsonl",
                "sessionId": "thread-1",
                "threadId": "thread-1",
                "rolloutId": "thread-1",
                "recordCount": 12,
                "lastAt": "2026-09-28T21:09:38Z",
                "mtime": 1.0,
            },
            {
                "path": "/sessions/guardian.jsonl",
                "sessionId": "thread-1",
                "threadId": "thread-1",
                "rolloutId": "guardian-1",
                "recordCount": 0,
                "lastAt": "2026-10-01T00:05:15Z",
                "mtime": 2.0,
            },
        ]

        selected = viewer.dedupe_session_summaries(items)

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["rolloutId"], "thread-1")
        self.assertEqual(selected[0]["rolloutCount"], 2)

    def test_session_deduplication_falls_back_to_a_renderable_rollout(self) -> None:
        items = [
            {
                "path": "/sessions/renderable.jsonl",
                "sessionId": "thread-1",
                "threadId": "thread-1",
                "rolloutId": "child-1",
                "recordCount": 2,
                "lastAt": "2026-09-30T00:05:15Z",
                "mtime": 1.0,
            },
            {
                "path": "/sessions/empty.jsonl",
                "sessionId": "thread-1",
                "threadId": "thread-1",
                "rolloutId": "child-2",
                "recordCount": 0,
                "lastAt": "2026-10-01T00:05:15Z",
                "mtime": 2.0,
            },
        ]

        selected = viewer.dedupe_session_summaries(items)

        self.assertEqual(selected[0]["rolloutId"], "child-1")

    def test_semantic_prose_highlighting_skips_code(self) -> None:
        self.assertIn('"code",', viewer.VIEW_JS)
        self.assertIn('token.startsWith("“")', viewer.VIEW_JS)
        self.assertIn('token.startsWith("‘")', viewer.VIEW_JS)

    def test_message_navigation_keeps_a_cursor_near_the_page_end(self) -> None:
        for script in (viewer.VIEW_JS, viewer.TYPESET_JS):
            self.assertIn("let messageNavigationIndex = -1", script)
            self.assertIn("cursorIsVisible", script)
            self.assertIn("messageNavigationIndex = nextIndex", script)

    def test_refresh_reports_progress_and_preserves_the_visible_message(self) -> None:
        for html in (viewer.VIEW_HTML, viewer.TYPESET_HTML):
            self.assertIn('id="refreshStatus"', html)
            self.assertIn('class="refresh-icon"', html)

        for script in (viewer.VIEW_JS, viewer.TYPESET_JS):
            self.assertIn('els.refreshStatus.textContent = "Refreshing..."', script)
            self.assertIn('"Up to date"', script)
            self.assertIn("function setStatusbarRefreshState(refreshing)", script)
            self.assertIn("els.statusbarToggle.disabled = refreshing", script)
            self.assertIn(
                "els.statusbarToggle.classList.toggle(\"is-refreshing\", refreshing)",
                script,
            )
            self.assertIn(
                "els.statusbarToggle.innerHTML = '<span class=\"refresh-icon\"",
                script,
            )
            self.assertIn('els.statusbarToggle.textContent = "/"', script)
            self.assertIn("setStatusbarRefreshState(refreshing)", script)
            self.assertNotIn("showRefreshBubble", script)
            self.assertNotIn("data-refresh-bubble", script)
            self.assertIn("function sameConversationContent(previous, next)", script)
            self.assertIn("renderOptions.avoidUnchangedRender", script)
            self.assertIn("preserveScroll: !jumpToLatest", script)
            self.assertIn("if (jumpToLatest) scrollToLatestAssistant()", script)
            self.assertIn(
                "refreshConversation({ jumpToLatest: event.shiftKey })", script
            )
            self.assertIn("restoreScrollAnchor(options.scrollAnchor)", script)
            self.assertIn('toggleAttribute("aria-busy", refreshing)', script)
            self.assertIn('apiParams.set("anchor", renderOptions.scrollAnchor.lineNo)', script)
            self.assertIn("function extendsConversationContent(previous, next)", script)
            self.assertIn('insertAdjacentHTML(', script)
            self.assertIn('apiParams.set("anchor", firstRenderedLine)', script)

        self.assertIn("const scrollAnchor = options.preserveScroll", viewer.VIEW_JS)
        self.assertNotIn("els.conversation.scrollTop = previousScrollTop", viewer.VIEW_JS)
        self.assertIn("@keyframes refresh-spin", viewer.APP_CSS)
        conversation_rule = viewer.APP_CSS.split(".conversation {", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-anchor: none", conversation_rule)
        self.assertIn(".statusbar-toggle.is-refreshing", viewer.APP_CSS)
        self.assertNotIn(".refresh-bubble", viewer.APP_CSS)
        self.assertNotIn(".refresh-bubble-spinner", viewer.APP_CSS)
        self.assertIn(
            "if (route.active && !lastConversationData)", viewer.TYPESET_JS
        )

    def test_refresh_anchor_keeps_the_visible_record_ahead_of_the_tail(self) -> None:
        records = [SimpleNamespace(line_no=line) for line in range(1, 31)]

        selected = viewer.records_for_view(records, tail=4, all_records=False, anchor_line=24)

        self.assertEqual([record.line_no for record in selected], list(range(24, 31)))

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
        self.assertIn('>Open answer ↗</a>', viewer.TYPESET_JS)
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

    def test_open_answer_is_available_without_typeset_debug(self) -> None:
        self.assertIn('class="typeset-answer-link"', viewer.VIEW_JS)
        self.assertIn(">Open answer ↗</a>", viewer.VIEW_JS)
        self.assertIn(">Open answer ↗</a>", viewer.TYPESET_JS)
        self.assertIn(
            "return `/t/${encodeURIComponent(identity.get(\"id\"))}/${encodeURIComponent(record.line_no)}`",
            viewer.VIEW_JS,
        )
        self.assertIn('"/api/typeset/answer"', viewer.TYPESET_JS)
        self.assertIn(">Fresh render ↗</a>", viewer.TYPESET_JS)

    def test_typeset_header_can_copy_original_markdown(self) -> None:
        self.assertIn('title="Copy Markdown"', viewer.TYPESET_JS)
        self.assertIn("${copyMarkdownButton(record)}", viewer.TYPESET_JS)
        self.assertIn("navigator.clipboard?.writeText", viewer.TYPESET_JS)
        self.assertIn('lastConversationData?.records?.find(', viewer.TYPESET_JS)
        self.assertIn('copyRecordMarkdown(button)', viewer.TYPESET_JS)
        self.assertIn(".typeset-copy-markdown", viewer.APP_CSS)

    def test_typeset_pdf_annotations_copy_exact_fenced_code(self) -> None:
        self.assertIn(
            'const CODE_COPY_ORIGIN = "https://codex-tools.invalid"',
            viewer.TYPESET_JS,
        )
        self.assertIn("page.getAnnotations", viewer.TYPESET_JS)
        self.assertIn('button.className = "typeset-code-copy"', viewer.TYPESET_JS)
        self.assertIn("record?.typeset?.codeBlocks?.[copyIndex]", viewer.TYPESET_JS)
        self.assertIn("copyRecordCode(codeButton)", viewer.TYPESET_JS)
        self.assertIn(".typeset-code-copy", viewer.APP_CSS)

    def test_typeset_attachments_open_in_a_new_tab(self) -> None:
        self.assertIn('target="_blank" rel="noopener"', viewer.TYPESET_JS)
        self.assertIn("resourceLinks(record)", viewer.TYPESET_JS)
        self.assertIn(
            'attachments.length === 1 ? "Attachment" : "Attachments"',
            viewer.TYPESET_JS,
        )
        self.assertIn(".typeset-attachments", viewer.APP_CSS)

    def test_external_links_are_collected_for_the_typeset_footer(self) -> None:
        markdown = """\
[OpenAI docs](https://platform.openai.com/docs), plus
https://example.com/a_(b). The docs repeat at https://platform.openai.com/docs.
`https://inline.example/ignored`

```text
https://fenced.example/ignored
```
[Local file](/tmp/report.pdf) and [mail](mailto:person@example.com).
"""

        payload = viewer.external_link_payload(markdown)

        self.assertEqual(
            payload,
            [
                {
                    "label": "OpenAI docs",
                    "url": "https://platform.openai.com/docs",
                    "host": "platform.openai.com",
                },
                {
                    "label": "https://example.com/a_(b)",
                    "url": "https://example.com/a_(b)",
                    "host": "example.com",
                },
            ],
        )
        self.assertIn('rel="noopener noreferrer"', viewer.TYPESET_JS)
        self.assertIn('linkHeading = externalLinks.length === 1 ? "Link" : "Links"', viewer.TYPESET_JS)

    def test_typeset_payload_includes_external_links_for_assistant_records(self) -> None:
        record = search.TextRecord(
            "",
            "assistant",
            "See [the reference](https://example.com/reference).",
            42,
            "message",
        )
        session = search.Session(path=Path("session.jsonl"), records=[record])
        state = SimpleNamespace(
            titles={},
            typeset_debug=False,
            typeset_code_mode="verbatim",
        )
        rendered = SimpleNamespace(
            ok=False,
            key="test",
            cached=False,
            error="not rendered",
        )
        with (
            patch.object(viewer, "resolve_session_path", return_value=session.path),
            patch.object(viewer, "read_session", return_value=session),
            patch.object(viewer, "session_summary", return_value={}),
            patch.object(viewer.typeset, "render_pdf", return_value=rendered),
        ):
            payload = viewer.handle_typeset(state, {"all": ["1"]})

        self.assertEqual(
            payload["records"][0]["externalLinks"],
            [
                {
                    "label": "the reference",
                    "url": "https://example.com/reference",
                    "host": "example.com",
                }
            ],
        )

    def test_local_attachments_are_derived_from_the_assistant_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "plot.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            png = root / "plot.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            csv = root / "results.csv"
            csv.write_text("name,value\na,1\n", encoding="utf-8")
            record = search.TextRecord(
                "",
                "assistant",
                f"[PDF plot]({pdf}), [PNG plot]({png}), [Results]({csv}), "
                f"and [ignore]({root / 'page.html'}).",
                42,
                "message",
            )

            payload = viewer.attachment_link_payload(root / "session.jsonl", record)

        self.assertEqual(len(payload), 3)
        self.assertEqual(
            [(item["label"], item["kind"]) for item in payload],
            [("PDF plot", "PDF"), ("PNG plot", "PNG"), ("Results", "CSV")],
        )
        self.assertIn("/open?", payload[0]["url"])
        self.assertIn("line=42", payload[0]["url"])
        self.assertIn("link=0", payload[0]["url"])

    def test_open_route_resolves_only_an_attachment_from_the_selected_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_path = root / "session.jsonl"
            pdf = root / "plot.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            session = search.Session(
                path=session_path,
                records=[
                    search.TextRecord(
                        "", "assistant", f"[Open plot]({pdf})", 42, "message"
                    )
                ],
            )
            state = SimpleNamespace()
            query = {"path": [str(session_path)], "line": ["42"], "link": ["0"]}
            with (
                patch.object(viewer, "resolve_session_path", return_value=session_path),
                patch.object(viewer, "read_session", return_value=session),
            ):
                self.assertEqual(
                    viewer.resolve_linked_attachment(state, query),
                    (pdf.resolve(), "application/pdf"),
                )
                with self.assertRaises(FileNotFoundError):
                    viewer.resolve_linked_attachment(
                        state,
                        {**query, "link": ["1"], "target": ["/etc/passwd"]},
                    )

    def test_attachment_validation_rejects_mismatched_or_active_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_png = root / "fake.png"
            fake_png.write_text("not an image", encoding="utf-8")
            svg = root / "active.svg"
            svg.write_text("<svg><script/></svg>", encoding="utf-8")

            with self.assertRaises(FileNotFoundError):
                viewer.validated_attachment(fake_png)
            self.assertEqual(viewer.local_attachment_links(f"[SVG]({svg})"), [])

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

    def test_message_navigation_marks_the_current_bubble(self) -> None:
        for script in (viewer.VIEW_JS, viewer.TYPESET_JS):
            self.assertIn("function setNavigationCurrent(message)", script)
            self.assertIn('classList.add("is-navigation-current")', script)
            self.assertIn('setAttribute("aria-current", "true")', script)
            self.assertIn("setNavigationCurrent(messages[nextIndex])", script)
            self.assertIn("setNavigationCurrent(target)", script)

        self.assertIn(".message.is-navigation-current::before", viewer.APP_CSS)
        self.assertIn(
            ".typeset-message.is-navigation-current::before", viewer.APP_CSS
        )
        self.assertIn(
            ".typeset-message.is-navigation-current .typeset-pdf-page::before",
            viewer.APP_CSS,
        )
        self.assertIn("border-left-color: var(--faint)", viewer.APP_CSS)
        typeset_body_rule = viewer.APP_CSS.split(
            ".typeset-message.is-navigation-current .typeset-pdf-page::before", 1
        )[1].split("}", 1)[0]
        self.assertIn("width: 2px", typeset_body_rule)
        typeset_container_rule = viewer.APP_CSS.split(
            ".typeset-message.is-navigation-current::before", 1
        )[1].split("}", 1)[0]
        self.assertIn("content: none", typeset_container_rule)
        navigation_rule = viewer.APP_CSS.split(
            ".message.is-navigation-current::before", 1
        )[1].split("}", 1)[0]
        self.assertIn("width: 3px", navigation_rule)
        self.assertIn("background: var(--faint)", navigation_rule)

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
                search.TextRecord(
                    "", "assistant", "```text\n/tmp/example\n```", 12, "message"
                ),
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
            payload = viewer.handle_typeset(
                state, {"line": ["12"]}, isolated=True, force=True
            )

        self.assertEqual([record["line_no"] for record in payload["records"]], [12])
        self.assertEqual(payload["isolatedLine"], 12)
        self.assertEqual(payload["debugLine"], 12)
        self.assertTrue(payload["typesetFresh"])
        self.assertIn("?fresh=", payload["records"][0]["typeset"]["pdfUrl"])
        self.assertEqual(
            payload["records"][0]["typeset"]["codeBlocks"], ["/tmp/example"]
        )
        render.assert_called_once_with(
            "```text\n/tmp/example\n```",
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
                viewer.handle_typeset(
                    state, {"line": ["12"]}, isolated=True, force=True
                )

    def test_focused_answer_uses_cached_typesetting_without_debug_mode(self) -> None:
        path = Path("/tmp/session.jsonl")
        session = search.Session(
            path=path,
            records=[search.TextRecord("", "assistant", "answer", 12, "message")],
        )
        state = SimpleNamespace(typeset_debug=False, titles={})
        result = typeset.TypesetResult(True, "a" * 64, Path("bubble.pdf"), cached=True)

        with (
            patch.object(viewer, "resolve_session_path", return_value=path),
            patch.object(viewer, "read_session", return_value=session),
            patch.object(viewer, "session_summary", return_value={"title": "Test"}),
            patch.object(viewer.typeset, "render_pdf", return_value=result) as render,
        ):
            payload = viewer.handle_typeset(
                state, {"line": ["12"]}, isolated=True
            )

        self.assertEqual([record["line_no"] for record in payload["records"]], [12])
        self.assertEqual(payload["isolatedLine"], 12)
        self.assertIsNone(payload["debugLine"])
        self.assertFalse(payload["typesetFresh"])
        self.assertNotIn("?fresh=", payload["records"][0]["typeset"]["pdfUrl"])
        render.assert_called_once_with(
            "answer",
            title="Assistant answer",
            force=False,
            header_mode="external",
            code_mode="auto",
        )

    def test_normal_typeset_api_rejects_line_targeting(self) -> None:
        path = Path("/tmp/session.jsonl")
        state = SimpleNamespace(typeset_debug=True, titles={})

        with patch.object(viewer, "resolve_session_path", return_value=path):
            with patch.object(viewer, "read_session", return_value=search.Session(path=path)):
                with self.assertRaises(FileNotFoundError):
                    viewer.handle_typeset(state, {"line": ["12"]})


if __name__ == "__main__":
    unittest.main()
