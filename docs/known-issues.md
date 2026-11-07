# Known issues

[Back to README](../README.md)

## Missing Unicode superscripts in typeset PDFs

**Fixed in renderer `typeset-v48`.** Older `typeset-v47` PDFs could lose the
superscript minus/six in scientific thresholds such as **E≤10⁻⁶**. Source text
and LaTeX were intact; the body font lacked those glyphs during PDF generation.

The renderer now falls back to DejaVu Serif for missing body-text glyphs, as it
already did for inline code. Regression tests check plain, bold, italic, quoted,
and table text against missing-character logs and extracted PDF output.

**Update the renderer and install DejaVu Serif on each rendering machine.**
The changed renderer version gives PDFs new cache keys; there is no need to
clear the whole cache.
