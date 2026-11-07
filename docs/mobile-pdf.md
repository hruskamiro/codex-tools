# Markdown and PDF reading modes

[Back to README](../README.md)

**Read typeset agent answers on a phone, with cached PDFs for unreliable links.**
This supports the upcoming mobile Agent Console and is still in development.
Choose **Reading mode → Markdown / PDF** in the bottom agent footer. Each agent
keeps its choice and reading position; composing and navigation stay available.

User messages remain text. The latest assistant response stays in Markdown while
working; completed responses can be rendered by the relay. The phone needs no
TeX installation. Update/restart the relay with its XeLaTeX/fonts and current
Codex Tools dependencies, including `markdown-it-py`.

## Independent blocks

**Prose stays portrait; tables and code can go wide.** Markdown is split at
complete structural nodes, preserving math, nested lists, quotes, and nested code.

| Control | Result |
|---|---|
| Render wide | A separate wide table/code PDF, keeping text scale and scrolling horizontally |
| Fit portrait | Restore that block's narrow layout |
| Markdown mode | Select text or follow inline links |

Layout choices are saved per response/block. Agent-switch gestures stay on the
footer. Small tables wrap cells; oversized/unbreakable content can still hit TeX
limits, with an error and Markdown fallback. PDF blocks are reading images;
attachments remain listed below the original response in both modes.

## Fetching and storage

**Blocks load as they enter view**, with compilation queued rather than rendering
an entire conversation. Manifests/PDFs are cached per connection, host, agent,
response, renderer, block, and layout. Native and browser caches each hold up to
64 MiB, evicting older writes; evicted blocks need connectivity. Cached content
survives restart but has no separate at-rest encryption.

Requests use server-selected record hashes and block indices, not arbitrary
Markdown, TeX, paths, or dimensions. Only two named layouts are accepted. PDF
requests reject redirects, incorrect MIME types, and incomplete/oversized downloads
(32 MiB). Browser proxying keeps relay credentials out of PDFium.

## Existing PDF attachments

**Open attachments inside the app**, with zoom and document navigation, or use
**Open externally / Download**. The embedded reader allows 32 MiB; the external
attachment route retains its 128 MiB limit and the PDF's original layout.

Native rendering, server output, caches, layouts, and routing have automated
coverage. Physical touch behavior and iOS still need device testing. In the mobile
repository, run `flutter test`; with Flutter and Chrome, also run
`python3 tool/pdf_browser_smoke.py` for the local browser fixture.
