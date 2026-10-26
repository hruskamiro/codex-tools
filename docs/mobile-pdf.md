# Markdown and PDF reading modes

Use **Reading mode** in the bottom agent footer to choose **Markdown** or **PDF**.
The selection is saved per agent, with separate reading positions for each mode.
The composer, agent navigation, attachments and progress breadcrumbs stay available.
User messages remain ordinary text bubbles. The latest assistant response stays
in Markdown while the agent is working, so incomplete output is not typeset.

PDF mode requests completed responses from the relay. The server uses the same
XeLaTeX renderer, fonts, code highlighting and math support as the ct viewer, with
a single phone-portrait layout. No TeX installation is needed on the phone.
Update and restart the relay to expose the block endpoints; the server needs its
normal XeLaTeX/font dependencies and the new `markdown-it-py` package installed by
the updated Codex Tools distribution.

## Independent blocks

The server parses Markdown into complete structural nodes before typesetting.
Top-level tables and fenced/indented code become individual PDFs; surrounding
prose is rendered separately. Display math, nested lists, quotes and nested code
are preserved together. Long prose splits only at complete node boundaries.

Everything defaults to portrait width. **Render wide** on a table or code block
requests a separate wide PDF. Text keeps the portrait physical scale, while only
that block scrolls horizontally. **Fit portrait** restores the narrow version.
Wide choices are saved per response/block. Agent-switching gestures remain on
the footer, so they do not intercept table scrolling.

Small tables wrap cells; headers can hyphenate instead of overlapping. A wide
table has more room without shrinking its font. Very large/unbreakable content
can still exceed TeX limits; the app shows an error and Markdown fallback for
that block. PDF blocks are page images for reading, so use Markdown mode when
selecting text or following inline links. Local file attachments stay listed
below the original response in both modes.

## Fetching and storage

Responses and blocks load as they enter the viewport. PDF compilation requests
are queued to avoid launching a render for the entire conversation at once.
Manifests and PDFs are cached locally, scoped to connection, host, agent, response,
renderer version, block and layout. Cached content can be read offline and after
restarting the app. Native storage and browser IndexedDB each keep a bounded
64 MiB PDF cache, evicting older writes; evicted blocks require connectivity.
This cache contains conversation content and is not separately encrypted.

The client requests server-selected record hashes and block indices; it never
submits arbitrary Markdown, TeX, filesystem paths or page dimensions. Only the
two named phone layouts are accepted. Authenticated PDF downloads reject redirects,
non-PDF MIME types and incomplete/oversized responses (32 MiB limit).
The web preview allowlists and proxies these endpoints without exposing relay
credentials to PDFium. Rendering failures retain access to the Markdown text.

## Existing PDF attachments

Tap a PDF attachment to download and read it inside the app, with zoom and normal
document navigation. The original PDF layout is preserved. **Open externally**
(or **Download** in the browser) remains available. The embedded reader accepts
up to 32 MiB; the external attachment path retains its existing 128 MiB limit.

Native PDFium rendering, server XeLaTeX output, cache persistence, layout selection,
relay bounds and bridge routing have automated coverage. Android/web builds are
checked; physical phone touch behavior and iOS still need device testing.

Run `flutter test` for native regressions. With Flutter and Chrome on PATH,
`python3 tool/pdf_browser_smoke.py` checks browser IndexedDB and PDFium rendering
using only a local fixture and correctly typed package assets.
