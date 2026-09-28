from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from codex_tools import typeset


class TypesetTests(unittest.TestCase):
    def test_code_blocks_load_package_with_line_breaking_options(self) -> None:
        document = typeset.document_for("```text\na very long line\n```")

        self.assertIn(r"\usepackage{fvextra}", document)
        self.assertIn(r"\usepackage{enumitem}", document)
        self.assertNotIn(r"\usepackage{fancyvrb}", document)
        self.assertIn(r"\setmonofont[Scale=MatchLowercase]{PT Mono}", document)
        self.assertIn(r"\linespread{1.08}", document)
        self.assertIn(r"\usepackage{tikz}", document)
        self.assertNotIn(r"\usepackage{varwidth}", document)
        self.assertIn(r"\begin{minipage}{150mm}", document)
        self.assertNotIn(r"\begin{varwidth}", document)
        self.assertIn(r"\definecolor{CodexCode}{HTML}{E8E7E1}", document)
        self.assertIn(r"\definecolor{CodexCodeRule}{HTML}{DDDED6}", document)
        self.assertIn(r"\definecolor{CodexNumberColor}{HTML}{267F8D}", document)
        self.assertIn(r"\definecolor{CodexStringColor}{HTML}{AD4A76}", document)
        self.assertIn(r"\definecolor{CodexQuoteText}{HTML}{5E625B}", document)
        self.assertIn(r"\definecolor{CodexQuoteRule}{HTML}{C4C7B7}", document)
        self.assertIn(
            r"\newcommand{\CodexNumber}[1]{{\color{CodexNumberColor}#1}}",
            document,
        )
        self.assertIn(
            r"\newcommand{\CodexString}[1]{{\color{CodexStringColor}#1}}",
            document,
        )
        self.assertIn(r"rounded corners=1pt", document)
        self.assertIn(r"inner ysep=0.2pt", document)
        self.assertIn(r"\usebox{\CodexInlineCodeBox}", document)
        self.assertNotIn(r"\raisebox{0pt}[\ht\strutbox][\dp\strutbox]", document)
        self.assertNotIn(r"\fcolorbox", document)
        self.assertNotIn(r"\strut\texttt", document)
        self.assertNotIn(r"\hrule height 0.4pt", document)
        self.assertNotIn("Codex answer", document)
        self.assertIn(
            r"\documentclass[10pt,border={0.75cm 0.375cm 0.75cm 0.375cm}]{standalone}",
            document,
        )
        self.assertIn(r"\setlength{\parindent}{0pt}", document)
        self.assertIn(r"\setlength{\parskip}{0.5em}", document)
        self.assertIn(r"\setlength{\CodexQuoteTopSep}{0pt}", document)
        self.assertIn(r"\setlength{\CodexQuoteAfterSep}{0.5em}", document)
        self.assertIn(
            r"\setlist{leftmargin=*,itemsep=0.12em,parsep=0pt,topsep=\CodexListTopSep,partopsep=0pt}",
            document,
        )
        self.assertIn(
            r"\setlist[itemize,1]{labelindent=0.8em,leftmargin=*}",
            document,
        )
        self.assertIn(
            r"\setlist[enumerate,1]{labelindent=0.8em,leftmargin=*}",
            document,
        )
        self.assertIn(
            r"\setlist[enumerate]{font=\color{CodexNumberColor}}",
            document,
        )
        self.assertIn(
            r"\begin{Verbatim}[breaklines=true,breakanywhere=true,fontsize=\small]",
            document,
        )
        self.assertIn("frame=lines", document)
        self.assertIn("framerule=0.25pt", document)
        self.assertIn("framesep=0.45em", document)
        self.assertIn(r"rulecolor=\color{CodexCodeRule}", document)
        self.assertIn("xleftmargin=0.8em", document)
        self.assertIn("xrightmargin=0.8em", document)
        self.assertIn(r"vspace=\CodexCodeTopSep", document)
        self.assertIn("https://codex-tools.invalid/code/0", document)
        self.assertIn(r"\newcommand{\CodexCopyTarget}", document)

        indented = typeset.document_for("One.\n\nTwo.", paragraph_mode="indented")
        self.assertIn(r"\setlength{\parindent}{1.2em}", indented)
        self.assertIn(r"\setlength{\parskip}{0pt}", indented)
        self.assertIn(r"\setlength{\CodexCodeTopSep}{0.35em}", indented)
        self.assertIn(r"\setlength{\CodexQuoteTopSep}{0.6em}", indented)
        self.assertIn(r"\setlength{\CodexQuoteAfterSep}{0.6em}", indented)

        embedded = typeset.document_for("Text", "Visible title", header_mode="embedded")
        self.assertIn("Visible title", embedded)
        self.assertIn(r"\hrule height 0.4pt", embedded)
        self.assertIn(r"\documentclass[10pt,border={0.75cm}]{standalone}", embedded)

    def test_fenced_code_blocks_preserve_exact_contents_and_order(self) -> None:
        markdown = (
            "Before.\n\n"
            "```bash\n  printf '%s\\n' hello  \n```\n\n"
            "```text\n/tmp/example\nsecond line\n```"
        )

        self.assertEqual(
            typeset.fenced_code_blocks(markdown),
            ["  printf '%s\\n' hello  ", "/tmp/example\nsecond line"],
        )
        latex = typeset.markdown_to_latex(markdown)
        self.assertIn("https://codex-tools.invalid/code/0", latex)
        self.assertIn("https://codex-tools.invalid/code/1", latex)
        self.assertNotIn("https://codex-tools.invalid/code/2", latex)

    def test_unknown_paragraph_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown paragraph mode"):
            typeset.document_for("Text", paragraph_mode="unknown")

    def test_unknown_header_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown header mode"):
            typeset.document_for("Text", header_mode="unknown")

    def test_unknown_code_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown code mode"):
            typeset.document_for("Text", code_mode="unknown")

    def test_pygments_highlights_recognized_fenced_languages(self) -> None:
        document = typeset.document_for(
            "```bash\n# Comment\nprintf '%s' 42\n```",
            code_mode="pygments",
        )

        self.assertIn(r"commandchars=\\\{\}", document)
        self.assertIn(r"\def\CodexTok", document)
        self.assertIn(r"\CodexTok{c+c1}", document)
        self.assertIn(r"\CodexTok{l+m}{42}", document)
        self.assertIn("-no-shell-escape", Path(typeset.__file__).read_text())

    def test_verbatim_mode_and_unknown_languages_fall_back_cleanly(self) -> None:
        verbatim = typeset.document_for(
            "```bash\nprintf 42\n```", code_mode="verbatim"
        )
        unknown = typeset.document_for(
            "```not-a-real-language\nprintf 42\n```", code_mode="pygments"
        )

        self.assertNotIn(r"\def\CodexTok", verbatim)
        self.assertNotIn("commandchars", verbatim)
        self.assertNotIn("commandchars", unknown)
        self.assertIn("printf 42", unknown)

    def test_code_mode_is_part_of_cache_identity(self) -> None:
        highlighted = typeset.cache_key("```bash\nprintf 42\n```", code_mode="pygments")
        plain = typeset.cache_key("```bash\nprintf 42\n```", code_mode="verbatim")

        self.assertNotEqual(highlighted, plain)

    def test_blockquote_uses_the_styled_quote_environment(self) -> None:
        latex = typeset.markdown_to_latex(
            "> Each fragment carries either shared or fragment-specific context."
        )

        self.assertEqual(
            latex,
            "\\begin{CodexQuote}\n"
            "Each fragment carries either shared or fragment-specific context.\n"
            "\\end{CodexQuote}",
        )
        document = typeset.document_for("> Quoted text")
        self.assertIn(r"\newenvironment{CodexQuote}", document)
        self.assertIn(r"\vrule width 1.2pt", document)
        self.assertIn(r"\setlength{\topsep}{\CodexQuoteTopSep}", document)
        self.assertIn(r"\addvspace{\CodexQuoteAfterSep}", document)
        self.assertNotIn(r"\begin{quote}", document)

    def test_prose_numbers_use_the_number_color_command(self) -> None:
        latex = typeset.markdown_to_latex(
            "Values: 12, -3.5, 6e2, 1,024, and 40%. Keep v25 and item_2 plain. "
            "Keep `code42 99` literal."
        )

        self.assertIn(r"\CodexNumber{12}", latex)
        self.assertIn(r"\CodexNumber{-3.5}", latex)
        self.assertIn(r"\CodexNumber{6e2}", latex)
        self.assertIn(r"\CodexNumber{1,024}", latex)
        self.assertIn(r"\CodexNumber{40\%}", latex)
        self.assertIn("v25", latex)
        self.assertIn(r"item\_2", latex)
        self.assertNotIn(r"v\CodexNumber{25}", latex)
        self.assertNotIn(r"item\_\CodexNumber{2}", latex)
        self.assertIn(r"\CodexInlineCode{code42 99}", latex)

    def test_prose_quote_pairs_use_the_string_color_command(self) -> None:
        latex = typeset.markdown_to_latex(
            'Use "double", \'single\', “curly double”, and ‘curly single’. '
            "Don't color an apostrophe or `\"quoted code\"`."
        )

        self.assertIn(r'\CodexString{"double"}', latex)
        self.assertIn(r"\CodexString{'single'}", latex)
        self.assertIn(r"\CodexString{“curly double”}", latex)
        self.assertIn(r"\CodexString{‘curly single’}", latex)
        self.assertIn("Don't color", latex)
        self.assertIn(r'\CodexInlineCode{"quoted code"}', latex)
        self.assertNotIn(r'\CodexInlineCode{\CodexString', latex)

    def test_external_header_does_not_affect_cache_identity(self) -> None:
        external_a = typeset.cache_key("Text", "First", header_mode="external")
        external_b = typeset.cache_key("Text", "Second", header_mode="external")
        embedded_a = typeset.cache_key("Text", "First", header_mode="embedded")
        embedded_b = typeset.cache_key("Text", "Second", header_mode="embedded")

        self.assertEqual(external_a, external_b)
        self.assertNotEqual(embedded_a, embedded_b)

    def test_display_blocks_suppress_the_next_paragraph_indent(self) -> None:
        latex = typeset.markdown_to_latex(
            "Opening paragraph.\n\n"
            "Second paragraph.\n\n"
            "- List item\n\n"
            "After list.\n\n"
            "```text\ncode\n```\n\n"
            "After code.\n\n"
            "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
            "After table."
        )

        self.assertIn(r"\noindent Opening paragraph.", latex)
        self.assertNotIn(r"\noindent Second paragraph.", latex)
        self.assertIn(r"\noindent After list.", latex)
        self.assertIn(r"\noindent After code.", latex)
        self.assertIn(r"\noindent After table.", latex)

    def test_nested_lists_follow_markdown_indentation(self) -> None:
        latex = typeset.markdown_to_latex(
            "- Parent\n"
            "  - Child\n"
            "    1. Grandchild\n"
            "  - Second child\n"
            "- Second parent"
        )

        self.assertEqual(
            latex,
            "\\begin{itemize}\n"
            "\\item Parent\n"
            "\\begin{itemize}\n"
            "\\item Child\n"
            "\\begin{enumerate}\n"
            "\\item Grandchild\n"
            "\\end{enumerate}\n"
            "\\item Second child\n"
            "\\end{itemize}\n"
            "\\item Second parent\n"
            "\\end{itemize}",
        )

    def test_nested_list_can_switch_marker_type_at_one_level(self) -> None:
        latex = typeset.markdown_to_latex(
            "1. Ordered parent\n"
            "   - Bullet child\n"
            "   1. Numbered child\n"
            "2. Next parent"
        )

        self.assertIn(
            "\\item Ordered parent\n"
            "\\begin{itemize}\n"
            "\\item Bullet child\n"
            "\\end{itemize}\n"
            "\\begin{enumerate}\n"
            "\\item Numbered child\n"
            "\\end{enumerate}\n"
            "\\item Next parent",
            latex,
        )

    def test_separate_ordered_lists_preserve_explicit_start_numbers(self) -> None:
        latex = typeset.markdown_to_latex(
            "1. First item\n\n"
            "An unindented paragraph outside the list.\n\n"
            "2. Second item"
        )

        lines = latex.splitlines()
        self.assertEqual(lines.count(r"\begin{enumerate}"), 1)
        self.assertEqual(lines.count(r"\begin{enumerate}[start=2]"), 1)
        self.assertIn("An unindented paragraph outside the list.\n\\par", latex)

    def test_ordered_list_preserves_an_explicit_numbering_gap(self) -> None:
        latex = typeset.markdown_to_latex("3. Third\n5. Fifth")

        self.assertIn(r"\begin{enumerate}[start=3]", latex)
        self.assertIn(r"\setcounter{enumi}{4}", latex)

    def test_pipe_table_chooses_between_natural_and_wrapped_layouts(self) -> None:
        latex = typeset.markdown_to_latex(
            "| Name | Description | Cost |\n"
            "|:---|:---:|---:|\n"
            "| **Basic** | A long description | $10 |"
        )

        self.assertIn(r"\begin{tabular}{@{} l c r @{}}", latex)
        self.assertTrue(latex.startswith(r"\begingroup"))
        self.assertIn(r"\begin{center}", latex)
        self.assertTrue(latex.endswith(r"\endgroup"))
        self.assertIn(r"\ifdim\wd\CodexTableBox>\linewidth", latex)
        self.assertIn(r"\begin{tabularx}{\linewidth}", latex)
        self.assertIn(r"\usebox{\CodexTableBox}", latex)
        self.assertIn(r">{\raggedright\arraybackslash}X", latex)
        self.assertIn(r">{\centering\arraybackslash}X", latex)
        self.assertIn(r">{\raggedleft\arraybackslash}X", latex)
        self.assertIn(r"\textbf{Basic}", latex)
        self.assertIn(r"\$\CodexNumber{10}", latex)
        self.assertEqual(latex.count(r"\arrayrulecolor{CodexInk}\hline"), 2)
        self.assertEqual(latex.count(r"\arrayrulecolor{CodexTableRule}"), 2)

        document = typeset.document_for("| A | B |\n|---|---|\n| 1 | 2 |")
        self.assertIn(r"\usepackage{colortbl}", document)
        self.assertIn(r"\definecolor{CodexTableRule}{HTML}{E8E8E2}", document)

    def test_table_cells_allow_pipes_in_code_and_escaped_pipes(self) -> None:
        latex = typeset.markdown_to_latex(
            "| Input | Meaning |\n"
            "|---|---|\n"
            "| `a|b` | one \\| two |"
        )

        self.assertIn(r"\CodexInlineCode{a|b}", latex)
        self.assertIn("one | two", latex)

    def test_plain_pipe_text_is_not_treated_as_a_table(self) -> None:
        latex = typeset.markdown_to_latex("alpha | beta\nnot a separator")

        self.assertNotIn(r"\begin{tabularx}", latex)

    def test_forced_render_ignores_existing_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_root = Path(temporary)
            key = typeset.cache_key("hello", "Test")
            output_dir = cache_root / "codex-tools" / "typeset" / key[:2] / key
            output_dir.mkdir(parents=True)
            pdf_path = output_dir / "bubble.pdf"
            pdf_path.write_bytes(b"old")

            def compile_pdf(command: list[str], **kwargs: object) -> object:
                pdf_path.write_bytes(b"new")
                return type("Completed", (), {"returncode": 0, "stdout": ""})()

            with (
                patch.dict("os.environ", {"XDG_CACHE_HOME": temporary}),
                patch.object(typeset.shutil, "which", return_value="/usr/bin/xelatex"),
                patch.object(typeset.subprocess, "run", side_effect=compile_pdf) as run,
            ):
                cached = typeset.render_pdf("hello", title="Test")
                fresh = typeset.render_pdf("hello", title="Test", force=True)

            self.assertTrue(cached.cached)
            self.assertFalse(fresh.cached)
            self.assertEqual(pdf_path.read_bytes(), b"new")
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
