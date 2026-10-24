# Known issues

## Missing Unicode superscripts in typeset PDFs

Status: fixed by the body-text Unicode fallback in renderer `typeset-v48`;
the older Mac checkout was still using `typeset-v47` when this was reported.

Reported in "Explore fragment mass tolerance":

<http://127.0.0.1:8765/t/relay/7d10e6f2-d5a8-4f90-852e-379f549ef9e7/01a13e6a-7233-7553-92db-6c025fe3954a?tail=24>

The answer dated 2026-10-21 at 07:33:42 UTC contains:

> Correct-match examples: peaks matching b/y ladders from confident reference
> identifications, with E≤10⁻⁶.

The source text and generated LaTeX retained the superscript minus `⁻` (U+207B)
and superscript six `⁶` (U+2076), but PDF generation reported both as missing
characters in TeX Gyre Pagella Regular. This could change the apparent numerical
threshold. The issue occurred during PDF generation, not browser display.

The fix uses DejaVu Serif when the body font lacks a Unicode glyph, extending
the fallback that previously applied only to inline code. The PDF regression
test checks scientific thresholds in plain, bold, italic, quoted, and table
text, including both missing-character logs and extracted PDF text.

Clients and rendering peers must have the updated renderer and DejaVu Serif
installed. The renderer version changes PDF cache keys, so older cached PDFs
are not reused after upgrading; there is no need to delete the whole cache.
