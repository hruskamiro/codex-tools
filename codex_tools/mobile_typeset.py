"""Structural Markdown blocks for portrait conversation PDF rendering."""
from __future__ import annotations

from dataclasses import dataclass
import re

from markdown_it import MarkdownIt

VERSION = "phone-blocks-v1"
MAX_BLOCKS = 128
MAX_SOURCE = 512 * 1024


def math_block(state, start, end, silent):
    if state.sCount[start] - state.blkIndent >= 4:
        return False
    line = state.src[state.bMarks[start] + state.tShift[start]:state.eMarks[start]].strip()
    if line not in {"$$", r"\["} and not (line.startswith("$$") and line.endswith("$$") and len(line) > 4):
        return False
    stop = start
    if line in {"$$", r"\["}:
        closer = "$$" if line == "$$" else r"\]"
        stop = start + 1
        while stop < end:
            candidate = state.src[state.bMarks[stop] + state.tShift[stop]:state.eMarks[stop]].strip()
            if candidate == closer:
                break
            stop += 1
    if silent:
        return True
    stop = min(stop + 1, end)
    token = state.push("ct_math", "", 0)
    token.block = True
    token.map = [start, stop]
    state.line = stop
    return True


@dataclass(frozen=True)
class Block:
    kind: str
    text: str

    def public(self, index):
        return {"index": index, "kind": self.kind, "text": self.text,
                "wide_allowed": self.kind in {"table", "code"}}


def split_blocks(text: str) -> list[Block]:
    if len(text.encode("utf-8")) > MAX_SOURCE:
        raise ValueError("response is too large for mobile typesetting")
    # Token source maps preserve the original Markdown. Only complete top-level
    # nodes can split; fences/tables nested in lists and quotes stay with them.
    parser = MarkdownIt("commonmark").enable("table")
    parser.block.ruler.before("fence", "ct_math", math_block)
    tokens = parser.parse(text)
    lines = text.splitlines(keepends=True)
    special = [(token.map[0], token.map[1], "table" if token.type == "table_open" else "code", token)
               for token in tokens if token.level == 0 and token.map is not None
               and token.type in {"table_open", "fence", "code_block"}]
    blocks = []

    def prose(start, end):
        # Break long prose at complete top-level node boundaries, never inside
        # a paragraph, list, quote, fence or formula. Avoid giant PDF bitmaps.
        boundaries = sorted({token.map[0] for token in tokens
                             if token.level == 0 and token.map and start < token.map[0] < end}) + [end]
        begin = start
        for boundary in boundaries:
            if sum(len(line) for line in lines[begin:boundary]) >= 2000 or boundary == end:
                value = "".join(lines[begin:boundary]).strip("\r\n")
                if value:
                    blocks.append(Block("prose", value))
                begin = boundary

    cursor = 0
    for start, end, kind, token in special:
        prose(cursor, start)
        value = "".join(lines[start:end]).strip("\r\n")
        if token.type == "code_block":
            fence = '`' * max(3, 1 + max((len(run) for run in re.findall(r'`+', token.content)), default=0))
            value = fence + '\n' + token.content + '\n' + fence
        blocks.append(Block(kind, value))
        cursor = end
    prose(cursor, len(lines))
    if len(blocks) > MAX_BLOCKS:
        raise ValueError("response has too many mobile PDF blocks")
    return blocks


def manifest(text, record_key, renderer_version):
    return {"version": 1, "record_key": record_key,
            "renderer": renderer_version + ":" + VERSION,
            "layout": "phone-portrait",
            "blocks": [block.public(index) for index, block in enumerate(split_blocks(text))]}
