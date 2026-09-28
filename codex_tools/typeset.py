"""Experimental Markdown-ish to XeLaTeX renderer for Codex viewer bubbles."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    from pygments import highlight
    from pygments.formatters import LatexFormatter
    from pygments.lexers import get_lexer_by_name
    from pygments.style import Style
    from pygments.token import Comment, Generic, Keyword, Name, Number, Operator, String
    from pygments.util import ClassNotFound
except ImportError:  # Pygments is optional when running directly from a checkout.
    highlight = None
    LatexFormatter = None
    get_lexer_by_name = None
    Style = object
    ClassNotFound = LookupError


RENDERER_VERSION = "typeset-v37"
DEFAULT_PARAGRAPH_MODE = "spaced"
DEFAULT_HEADER_MODE = "external"
BODY_LINE_STRETCH = "1.08"
CODE_MODES = {"auto", "pygments", "verbatim"}
PLAIN_CODE_LANGUAGES = {"", "text", "txt", "plain", "plaintext", "none"}
DEFAULT_CODE_MODE = os.environ.get("CODEX_TOOLS_TYPESET_CODE_MODE", "auto").lower()
if DEFAULT_CODE_MODE not in CODE_MODES:
    DEFAULT_CODE_MODE = "auto"


if LatexFormatter is not None:
    class CodexPygmentsStyle(Style):
        background_color = "#FFFFFF"
        default_style = "#282B27"
        styles = {
            Comment: "italic #72786F",
            Keyword: "bold #6D7046",
            Name.Builtin: "#536B75",
            Name.Class: "bold #536B75",
            Name.Function: "#536B75",
            Name.Decorator: "#7562A9",
            Name.Tag: "bold #6D7046",
            Name.Attribute: "#536B75",
            String: "#AD4A76",
            Number: "#267F8D",
            Operator: "#7562A9",
            Generic.Heading: "bold #5E673B",
            Generic.Subheading: "bold #5E673B",
            Generic.Deleted: "#9A4D45",
            Generic.Inserted: "#557153",
            Generic.Error: "bold #9A4D45",
        }
else:
    class CodexPygmentsStyle:  # pragma: no cover - import fallback marker
        pass


PYGMENTS_FORMATTER = (
    LatexFormatter(nowrap=True, commandprefix="CodexTok", style=CodexPygmentsStyle)
    if LatexFormatter is not None
    else None
)
PYGMENTS_STYLE_DEFS = (
    PYGMENTS_FORMATTER.get_style_defs() if PYGMENTS_FORMATTER is not None else ""
)

QUOTED_STRING_RE = re.compile(
    r'"(?:\\.|[^"\\])*"'
    r"|'(?:\\.|[^'\\])*'"
    r"|“[^”]*”|‘[^’]*’"
)
NUMBER_RE = re.compile(
    r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?%?"
)


@dataclass(frozen=True)
class ParagraphLayout:
    indent: str
    paragraph_sep: str
    list_top_sep: str
    code_top_sep: str
    table_top_sep: str
    quote_top_sep: str
    quote_after_sep: str


PARAGRAPH_LAYOUTS = {
    "indented": ParagraphLayout(
        "1.2em", "0pt", "0.6em", "0.35em", "0.6em", "0.6em", "0.6em"
    ),
    "spaced": ParagraphLayout(
        "0pt", "0.5em", "0pt", "0pt", "0pt", "0pt", "0.5em"
    ),
}


@dataclass(frozen=True)
class TypesetResult:
    ok: bool
    key: str
    pdf_path: Path | None
    error: str | None = None
    cached: bool = False


@dataclass(frozen=True)
class ListItem:
    indent: int
    ordered: bool
    text: str
    number: int | None = None


def user_cache_dir() -> Path:
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser() / "codex-tools"
    return Path("~/.cache/codex-tools").expanduser()


def typeset_cache_dir() -> Path:
    return user_cache_dir() / "typeset"


def latex_escape(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in value)


def latex_url_escape(value: str) -> str:
    return value.replace("\\", "/").replace("%", r"\%").replace("#", r"\#")


def render_inline(text: str) -> str:
    rendered: list[str] = []
    cursor = 0
    while cursor < len(text):
        if text.startswith("`", cursor):
            end = text.find("`", cursor + 1)
            if end > cursor + 1:
                rendered.append(
                    r"\CodexInlineCode{" + latex_escape(text[cursor + 1 : end]) + "}"
                )
                cursor = end + 1
                continue
        if text.startswith("**", cursor):
            end = text.find("**", cursor + 2)
            if end > cursor + 2:
                rendered.append(r"\textbf{" + render_inline(text[cursor + 2 : end]) + "}")
                cursor = end + 2
                continue
        if text.startswith("*", cursor):
            end = text.find("*", cursor + 1)
            if end > cursor + 1:
                rendered.append(r"\emph{" + render_inline(text[cursor + 1 : end]) + "}")
                cursor = end + 1
                continue
        link_match = re.match(r"\[([^\]]+)\]\(([^)]+)\)", text[cursor:])
        if link_match:
            label, url = link_match.groups()
            rendered.append(
                r"\href{"
                + latex_url_escape(url.strip())
                + "}{"
                + render_inline(label)
                + "}"
            )
            cursor += len(link_match.group(0))
            continue
        string_match = QUOTED_STRING_RE.match(text, cursor)
        if string_match:
            token = string_match.group(0)
            previous = text[cursor - 1] if cursor else ""
            following = text[string_match.end()] if string_match.end() < len(text) else ""
            straight_apostrophe_pair = token.startswith("'")
            touches_word = straight_apostrophe_pair and (
                previous.isalnum()
                or previous == "_"
                or following.isalnum()
                or following == "_"
            )
            if not touches_word:
                rendered.append(r"\CodexString{" + latex_escape(token) + "}")
                cursor = string_match.end()
                continue
        number_match = NUMBER_RE.match(text, cursor)
        if number_match:
            end = number_match.end()
            previous = text[cursor - 1] if cursor else ""
            following = text[end] if end < len(text) else ""
            starts_inside_identifier = text[cursor].isdigit() and (
                previous.isalnum() or previous == "_"
            )
            ends_inside_identifier = following.isalnum() or following == "_"
            if not starts_inside_identifier and not ends_inside_identifier:
                rendered.append(
                    r"\CodexNumber{" + latex_escape(number_match.group(0)) + "}"
                )
                cursor = end
                continue
        rendered.append(latex_escape(text[cursor]))
        cursor += 1
    return "".join(rendered)


def is_block_start(line: str) -> bool:
    stripped = line.strip()
    return bool(
        stripped.startswith("```")
        or re.match(r"#{1,4}\s+", stripped)
        or re.match(r"[-*+]\s+", stripped)
        or re.match(r"\d+[.)]\s+", stripped)
        or stripped.startswith(">")
    )


def resolved_code_mode(code_mode: str) -> str:
    if code_mode not in CODE_MODES:
        raise ValueError(f"unknown code mode: {code_mode}")
    if code_mode == "verbatim":
        return "verbatim"
    return "pygments" if PYGMENTS_FORMATTER is not None else "verbatim"


def render_code_block(
    lines: list[str], language: str = "", code_mode: str = DEFAULT_CODE_MODE
) -> str:
    body = "\n".join(lines).replace(r"\end{Verbatim}", r"\textbackslash{}end{Verbatim}")
    effective_mode = resolved_code_mode(code_mode)
    normalized_language = language.strip().lower().removeprefix("language-")
    if (
        effective_mode == "pygments"
        and normalized_language not in PLAIN_CODE_LANGUAGES
        and get_lexer_by_name
    ):
        try:
            lexer = get_lexer_by_name(normalized_language)
        except ClassNotFound:
            pass
        else:
            highlighted = highlight(
                "\n".join(lines), lexer, PYGMENTS_FORMATTER
            ).rstrip("\n")
            return (
                "\\begin{Verbatim}[breaklines=true,breakanywhere=true,"
                "fontsize=\\small,commandchars=\\\\\\{\\}]\n"
                + highlighted
                + "\n\\end{Verbatim}\n"
            )
    return "\\begin{Verbatim}[breaklines=true,breakanywhere=true,fontsize=\\small]\n" + body + "\n\\end{Verbatim}\n"


def parse_list_item(line: str) -> ListItem | None:
    match = re.match(r"^([ \t]*)([-*+]|\d+[.)])\s+(.+)$", line)
    if not match:
        return None
    indentation, marker, text = match.groups()
    ordered = marker[0].isdigit()
    return ListItem(
        indent=len(indentation.expandtabs(4)),
        ordered=ordered,
        text=text,
        number=int(marker.rstrip(".)")) if ordered else None,
    )


def render_list_level(
    items: list[ListItem], index: int, indent: int, level: int = 1
) -> tuple[str, int]:
    rendered: list[str] = []
    while index < len(items) and items[index].indent == indent:
        ordered = items[index].ordered
        env = "enumerate" if ordered else "itemize"
        start = items[index].number
        option = f"[start={start}]" if ordered and start != 1 else ""
        rendered.append(f"\\begin{{{env}}}{option}")
        expected_number = start
        while (
            index < len(items)
            and items[index].indent == indent
            and items[index].ordered == ordered
        ):
            if (
                ordered
                and expected_number is not None
                and items[index].number != expected_number
            ):
                counter = ("enumi", "enumii", "enumiii", "enumiv")[
                    min(level - 1, 3)
                ]
                rendered.append(
                    f"\\setcounter{{{counter}}}{{{items[index].number - 1}}}"
                )
            rendered.append(r"\item " + render_inline(items[index].text.strip()))
            if ordered:
                expected_number = items[index].number + 1
            index += 1
            if index < len(items) and items[index].indent > indent:
                child_indent = items[index].indent
                children, index = render_list_level(
                    items, index, child_indent, level + 1
                )
                rendered.append(children)
        rendered.append(f"\\end{{{env}}}")
    return "\n".join(rendered), index


def render_list(items: list[ListItem]) -> str:
    if not items:
        return ""
    base_indent = min(item.indent for item in items)
    rendered, index = render_list_level(items, 0, base_indent)
    if index < len(items):
        remainder, _ = render_list_level(items, index, items[index].indent)
        rendered += "\n" + remainder
    return rendered + "\n"


def render_quote(lines: list[str]) -> str:
    text = " ".join(line.strip() for line in lines).strip()
    return "\\begin{CodexQuote}\n" + render_inline(text) + "\n\\end{CodexQuote}\n"


def split_table_row(line: str) -> list[str]:
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    in_code = False
    for char in line.strip():
        if escaped:
            if char != "|":
                current.append("\\")
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "`":
            in_code = not in_code
            current.append(char)
        elif char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    if cells and not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells


def table_alignments(line: str) -> list[str] | None:
    cells = split_table_row(line)
    if not cells or any(not re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
        return None
    return [
        "center" if cell.startswith(":") and cell.endswith(":") else
        "right" if cell.endswith(":") else
        "left"
        for cell in cells
    ]


def render_table(header: list[str], rows: list[list[str]], alignments: list[str]) -> str:
    natural_types = {"left": "l", "center": "c", "right": "r"}
    wrapped_types = {
        "left": r">{\raggedright\arraybackslash}X",
        "center": r">{\centering\arraybackslash}X",
        "right": r">{\raggedleft\arraybackslash}X",
    }
    natural_columns = " ".join(natural_types[alignment] for alignment in alignments)
    wrapped_columns = " ".join(wrapped_types[alignment] for alignment in alignments)
    header_row = " & ".join(r"\textbf{" + render_inline(cell) + "}" for cell in header)
    body_rows = "\n".join(
        " & ".join(render_inline(cell) for cell in row) + r" \\ \hline"
        for row in rows
    )
    if body_rows:
        body_rows = "\n" + body_rows
    content = (
        r"\arrayrulecolor{CodexInk}\hline"
        + "\n"
        + header_row
        + r" \\ \hline"
        + "\n"
        + r"\arrayrulecolor{CodexTableRule}"
        + body_rows
        + "\n"
    )
    return (
        r"\begingroup" + "\n"
        + r"\setlength{\topsep}{\CodexTableTopSep}" + "\n"
        + r"\setlength{\partopsep}{0pt}" + "\n"
        + r"\begin{center}" + "\n"
        + r"{\small\renewcommand{\arraystretch}{1.2}" + "\n"
        + r"\sbox{\CodexTableBox}{%" + "\n"
        + r"\begin{tabular}{@{} " + natural_columns + r" @{}}" + "\n"
        + content
        + r"\end{tabular}}%" + "\n"
        + r"\ifdim\wd\CodexTableBox>\linewidth" + "\n"
        + r"\begin{tabularx}{\linewidth}{@{} " + wrapped_columns + r" @{}}" + "\n"
        + content
        + r"\end{tabularx}" + "\n"
        + r"\else" + "\n"
        + r"\usebox{\CodexTableBox}" + "\n"
        + r"\fi}" + "\n"
        + r"\end{center}" + "\n"
        + r"\endgroup" + "\n"
    )


def markdown_to_latex(
    markdown: str, *, code_mode: str = DEFAULT_CODE_MODE
) -> str:
    resolved_code_mode(code_mode)
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[str] = []
    noindent_next = True
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        if stripped.startswith("```"):
            language = stripped[3:].strip().split(maxsplit=1)[0] if stripped[3:].strip() else ""
            index += 1
            code_lines: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append(render_code_block(code_lines, language, code_mode))
            noindent_next = True
            continue

        if index + 1 < len(lines) and "|" in line and "|" in lines[index + 1]:
            header = split_table_row(line)
            alignments = table_alignments(lines[index + 1])
            if alignments is not None and len(header) == len(alignments):
                index += 2
                rows: list[list[str]] = []
                while index < len(lines) and "|" in lines[index]:
                    row = split_table_row(lines[index])
                    if len(row) != len(header):
                        break
                    rows.append(row)
                    index += 1
                blocks.append(render_table(header, rows, alignments))
                noindent_next = True
                continue

        heading = re.match(r"(#{1,4})\s+(.+)", stripped)
        if heading:
            level = len(heading.group(1))
            command = "section" if level == 1 else "subsection" if level == 2 else "subsubsection"
            blocks.append(f"\\{command}*{{{render_inline(heading.group(2).strip())}}}\n")
            noindent_next = True
            index += 1
            continue

        if parse_list_item(line):
            items: list[ListItem] = []
            while index < len(lines):
                item = parse_list_item(lines[index])
                if item is None:
                    break
                items.append(item)
                index += 1
            blocks.append(render_list(items))
            noindent_next = True
            continue

        if stripped.startswith(">"):
            quote_lines = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote_lines.append(lines[index].strip().lstrip(">").strip())
                index += 1
            blocks.append(render_quote(quote_lines))
            noindent_next = True
            continue

        paragraph = [stripped]
        index += 1
        while index < len(lines) and lines[index].strip() and not is_block_start(lines[index]):
            paragraph.append(lines[index].strip())
            index += 1
        prefix = r"\noindent " if noindent_next else ""
        blocks.append(prefix + render_inline(" ".join(paragraph)) + "\n\\par\n")
        noindent_next = False

    return "\n".join(blocks).strip() or latex_escape("(empty assistant message)")


def document_for(
    markdown: str,
    title: str = "Codex answer",
    *,
    paragraph_mode: str = DEFAULT_PARAGRAPH_MODE,
    header_mode: str = DEFAULT_HEADER_MODE,
    code_mode: str = DEFAULT_CODE_MODE,
) -> str:
    try:
        layout = PARAGRAPH_LAYOUTS[paragraph_mode]
    except KeyError as exc:
        raise ValueError(f"unknown paragraph mode: {paragraph_mode}") from exc
    if header_mode not in {"embedded", "external"}:
        raise ValueError(f"unknown header mode: {header_mode}")
    effective_code_mode = resolved_code_mode(code_mode)
    body = markdown_to_latex(markdown, code_mode=effective_code_mode)
    pygments_style_defs = (
        PYGMENTS_STYLE_DEFS if effective_code_mode == "pygments" else ""
    )
    escaped_title = render_inline(title)
    embedded_header = (
        rf"""{{\sffamily\small\color{{CodexAccent}} {escaped_title}}}
\vspace{{0.8em}}
\hrule height 0.4pt
\vspace{{1.1em}}
"""
        if header_mode == "embedded"
        else ""
    )
    document_border = (
        "0.75cm"
        if header_mode == "embedded"
        else "0.75cm 0.375cm 0.75cm 0.375cm"
    )
    return rf"""\documentclass[10pt,border={{{document_border}}}]{{standalone}}
\usepackage{{fontspec}}
\usepackage[dvipsnames]{{xcolor}}
\usepackage{{hyperref}}
\usepackage{{fvextra}}
\usepackage{{enumitem}}
\usepackage{{tabularx}}
\usepackage{{colortbl}}
\usepackage{{tikz}}
\setmainfont{{TeX Gyre Pagella}}
\setsansfont{{TeX Gyre Heros}}
\setmonofont[Scale=MatchLowercase]{{PT Mono}}
\linespread{{{BODY_LINE_STRETCH}}}
\definecolor{{CodexInk}}{{HTML}}{{282B27}}
\definecolor{{CodexAccent}}{{HTML}}{{5E673B}}
\definecolor{{CodexRule}}{{HTML}}{{D7D7CA}}
\definecolor{{CodexTableRule}}{{HTML}}{{E8E8E2}}
\definecolor{{CodexCode}}{{HTML}}{{E8E7E1}}
\definecolor{{CodexNumberColor}}{{HTML}}{{267F8D}}
\definecolor{{CodexStringColor}}{{HTML}}{{AD4A76}}
\definecolor{{CodexQuoteText}}{{HTML}}{{5E625B}}
\definecolor{{CodexQuoteRule}}{{HTML}}{{C4C7B7}}
{pygments_style_defs}
\hypersetup{{colorlinks=true,linkcolor=CodexAccent,urlcolor=CodexAccent}}
\pagestyle{{empty}}
\newlength{{\CodexListTopSep}}
\newlength{{\CodexCodeTopSep}}
\newlength{{\CodexTableTopSep}}
\newlength{{\CodexQuoteTopSep}}
\newlength{{\CodexQuoteAfterSep}}
\setlength{{\CodexListTopSep}}{{{layout.list_top_sep}}}
\setlength{{\CodexCodeTopSep}}{{{layout.code_top_sep}}}
\setlength{{\CodexTableTopSep}}{{{layout.table_top_sep}}}
\setlength{{\CodexQuoteTopSep}}{{{layout.quote_top_sep}}}
\setlength{{\CodexQuoteAfterSep}}{{{layout.quote_after_sep}}}
\setlist{{leftmargin=*,itemsep=0.12em,parsep=0pt,topsep=\CodexListTopSep,partopsep=0pt}}
\setlist[enumerate]{{font=\color{{CodexNumberColor}}}}
\setlist[itemize,1]{{labelindent=0.8em,leftmargin=*}}
\setlist[enumerate,1]{{labelindent=0.8em,leftmargin=*}}
\renewcommand{{\familydefault}}{{\rmdefault}}
\newsavebox{{\CodexTableBox}}
\newsavebox{{\CodexInlineCodeBox}}
\newcommand{{\CodexNumber}}[1]{{{{\color{{CodexNumberColor}}#1}}}}
\newcommand{{\CodexString}}[1]{{{{\color{{CodexStringColor}}#1}}}}
\newenvironment{{CodexQuote}}{{%
  \begin{{list}}{{}}{{%
    \setlength{{\leftmargin}}{{2.5em}}%
    \setlength{{\rightmargin}}{{2.5em}}%
    \setlength{{\topsep}}{{\CodexQuoteTopSep}}%
    \setlength{{\partopsep}}{{0pt}}%
    \setlength{{\parsep}}{{0pt}}%
    \setlength{{\itemsep}}{{0pt}}%
    \setlength{{\labelwidth}}{{0pt}}%
    \setlength{{\labelsep}}{{0pt}}%
  }}%
  \item\relax
  \noindent
  {{\color{{CodexQuoteRule}}\vrule width 1.2pt}}%
  \hspace{{0.8em}}%
  \begin{{minipage}}[t]{{\dimexpr\linewidth-0.8em-1.2pt\relax}}%
  \color{{CodexQuoteText}}\ignorespaces
}}{{%
  \end{{minipage}}%
  \end{{list}}%
  \addvspace{{\CodexQuoteAfterSep}}%
}}
\newcommand{{\CodexInlineCode}}[1]{{%
  \sbox{{\CodexInlineCodeBox}}{{%
    \tikz[baseline=(CodexCodeText.base)]{{%
      \node[
        fill=CodexCode,
        rounded corners=1pt,
        inner xsep=1.1pt,
        inner ysep=0.2pt,
        outer sep=0pt
      ] (CodexCodeText) {{\texttt{{#1}}}};
    }}%
  }}%
  \usebox{{\CodexInlineCodeBox}}%
}}
\RecustomVerbatimEnvironment{{Verbatim}}{{Verbatim}}{{%
  frame=none,
  xleftmargin=0.8em,
  xrightmargin=0.8em,
  vspace=\CodexCodeTopSep,
  formatcom=\color{{CodexInk}}
}}
\begin{{document}}
\begin{{minipage}}{{150mm}}
\color{{CodexInk}}
{embedded_header}%
\setlength{{\parindent}}{{{layout.indent}}}
\setlength{{\parskip}}{{{layout.paragraph_sep}}}
{body}
\end{{minipage}}
\end{{document}}
"""


def cache_key(
    markdown: str,
    title: str = "",
    *,
    paragraph_mode: str = DEFAULT_PARAGRAPH_MODE,
    header_mode: str = DEFAULT_HEADER_MODE,
    code_mode: str = DEFAULT_CODE_MODE,
) -> str:
    effective_code_mode = resolved_code_mode(code_mode)
    digest = hashlib.sha256()
    digest.update(RENDERER_VERSION.encode("utf-8"))
    digest.update(b"\0")
    digest.update(header_mode.encode("utf-8"))
    digest.update(b"\0")
    if header_mode == "embedded":
        digest.update(title.encode("utf-8", errors="replace"))
        digest.update(b"\0")
    digest.update(paragraph_mode.encode("utf-8"))
    digest.update(b"\0")
    digest.update(effective_code_mode.encode("utf-8"))
    digest.update(b"\0")
    digest.update(markdown.encode("utf-8", errors="replace"))
    return digest.hexdigest()


def render_pdf(
    markdown: str,
    title: str = "Codex answer",
    *,
    force: bool = False,
    paragraph_mode: str = DEFAULT_PARAGRAPH_MODE,
    header_mode: str = DEFAULT_HEADER_MODE,
    code_mode: str = DEFAULT_CODE_MODE,
) -> TypesetResult:
    key = cache_key(
        markdown,
        title,
        paragraph_mode=paragraph_mode,
        header_mode=header_mode,
        code_mode=code_mode,
    )
    cache_dir = typeset_cache_dir() / key[:2] / key
    pdf_path = cache_dir / "bubble.pdf"
    if pdf_path.exists() and not force:
        return TypesetResult(ok=True, key=key, pdf_path=pdf_path, cached=True)

    if shutil.which("xelatex") is None:
        return TypesetResult(ok=False, key=key, pdf_path=None, error="xelatex was not found")

    cache_dir.mkdir(parents=True, exist_ok=True)
    tex_path = cache_dir / "bubble.tex"
    tex_path.write_text(
        document_for(
            markdown,
            title,
            paragraph_mode=paragraph_mode,
            header_mode=header_mode,
            code_mode=code_mode,
        ),
        encoding="utf-8",
    )
    command = [
        "xelatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-no-shell-escape",
        "-output-directory",
        str(cache_dir),
        str(tex_path),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=cache_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return TypesetResult(ok=False, key=key, pdf_path=None, error=str(exc))

    if completed.returncode != 0 or not pdf_path.exists():
        log = completed.stdout.strip().splitlines()
        tail = "\n".join(log[-8:])
        return TypesetResult(
            ok=False,
            key=key,
            pdf_path=None,
            error=tail or f"xelatex exited with {completed.returncode}",
        )

    return TypesetResult(ok=True, key=key, pdf_path=pdf_path, cached=False)
