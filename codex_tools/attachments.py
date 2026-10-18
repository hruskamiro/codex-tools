"""Safe attachment discovery shared by the viewer and relay transport."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

from codex_tools import search


MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
ATTACHMENT_TYPES = {
    ".pdf": ("application/pdf", "PDF"),
    ".png": ("image/png", "PNG"),
    ".jpg": ("image/jpeg", "JPEG"),
    ".jpeg": ("image/jpeg", "JPEG"),
    ".webp": ("image/webp", "WEBP"),
    ".gif": ("image/gif", "GIF"),
    ".txt": ("text/plain; charset=utf-8", "TEXT"),
    ".log": ("text/plain; charset=utf-8", "LOG"),
    ".csv": ("text/csv; charset=utf-8", "CSV"),
    ".tsv": ("text/tab-separated-values; charset=utf-8", "TSV"),
    ".json": ("application/json; charset=utf-8", "JSON"),
    ".yaml": ("text/yaml; charset=utf-8", "YAML"),
    ".yml": ("text/yaml; charset=utf-8", "YAML"),
    ".toml": ("text/plain; charset=utf-8", "TOML"),
    ".md": ("text/plain; charset=utf-8", "MARKDOWN"),
    ".rst": ("text/plain; charset=utf-8", "RST"),
    ".dart": ("text/plain; charset=utf-8", "DART"),
    ".py": ("text/plain; charset=utf-8", "PYTHON"),
    ".sh": ("text/plain; charset=utf-8", "SHELL"),
    ".bash": ("text/plain; charset=utf-8", "SHELL"),
    ".js": ("text/plain; charset=utf-8", "JAVASCRIPT"),
    ".jsx": ("text/plain; charset=utf-8", "JSX"),
    ".ts": ("text/plain; charset=utf-8", "TYPESCRIPT"),
    ".tsx": ("text/plain; charset=utf-8", "TSX"),
    ".rs": ("text/plain; charset=utf-8", "RUST"),
    ".go": ("text/plain; charset=utf-8", "GO"),
    ".java": ("text/plain; charset=utf-8", "JAVA"),
    ".kt": ("text/plain; charset=utf-8", "KOTLIN"),
    ".swift": ("text/plain; charset=utf-8", "SWIFT"),
    ".c": ("text/plain; charset=utf-8", "C"),
    ".h": ("text/plain; charset=utf-8", "C HEADER"),
    ".cpp": ("text/plain; charset=utf-8", "C++"),
    ".hpp": ("text/plain; charset=utf-8", "C++ HEADER"),
    ".css": ("text/plain; charset=utf-8", "CSS"),
    ".html": ("text/plain; charset=utf-8", "HTML"),
    ".htm": ("text/plain; charset=utf-8", "HTML"),
    ".sql": ("text/plain; charset=utf-8", "SQL"),
}
TEXT_ATTACHMENT_SUFFIXES = {
    suffix
    for suffix, (content_type, _) in ATTACHMENT_TYPES.items()
    if content_type.startswith("text/") or "json" in content_type
}


def local_attachment_links(
    markdown: str, base_directory: Path | None = None
) -> list[tuple[str, Path]]:
    links: list[tuple[str, Path]] = []
    for match in MARKDOWN_LINK_RE.finditer(markdown):
        label = match.group(1).strip()
        target = match.group(2).strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        candidate = Path(target).expanduser()
        if not candidate.is_absolute():
            if base_directory is None or not base_directory.is_absolute():
                continue
            candidate = base_directory / candidate
        if candidate.suffix.lower() not in ATTACHMENT_TYPES:
            continue
        links.append((label or candidate.name, candidate))
    return links


def validated_attachment(path: Path) -> tuple[Path, str, str]:
    candidate = path.resolve()
    suffix = candidate.suffix.lower()
    attachment_type = ATTACHMENT_TYPES.get(suffix)
    if attachment_type is None or not candidate.is_file():
        raise FileNotFoundError("attachment not found")
    try:
        with candidate.open("rb") as handle:
            sample = handle.read(8192)
    except OSError as exc:
        raise FileNotFoundError("attachment not found") from exc

    valid = False
    if suffix == ".pdf":
        valid = sample.startswith(b"%PDF-")
    elif suffix == ".png":
        valid = sample.startswith(b"\x89PNG\r\n\x1a\n")
    elif suffix in {".jpg", ".jpeg"}:
        valid = sample.startswith(b"\xff\xd8\xff")
    elif suffix == ".gif":
        valid = sample.startswith((b"GIF87a", b"GIF89a"))
    elif suffix == ".webp":
        valid = (
            len(sample) >= 12
            and sample[:4] == b"RIFF"
            and sample[8:12] == b"WEBP"
        )
    elif suffix in TEXT_ATTACHMENT_SUFFIXES and b"\x00" not in sample:
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            valid = False
        else:
            valid = True
    if not valid:
        raise FileNotFoundError("attachment not found")
    return candidate, attachment_type[0], attachment_type[1]


def resolve_session_attachment(
    session_path: Path,
    line: int,
    link_index: int,
    *,
    read_session_fn: Callable[..., Any] | None = None,
) -> tuple[Path, str, str]:
    if line <= 0 or link_index < 0:
        raise FileNotFoundError("attachment not found")
    reader = read_session_fn or search.read_session
    session = reader(session_path, include_tools=False, include_system=False)
    matching = [
        record
        for record in session.records
        if record.role == "assistant" and record.line_no == line
    ]
    if not matching:
        raise FileNotFoundError("attachment not found")
    base_directory = Path(session.cwd).expanduser() if session.cwd else None
    links = local_attachment_links(matching[-1].text, base_directory)
    if link_index >= len(links):
        raise FileNotFoundError("attachment not found")
    label, linked_path = links[link_index]
    candidate, content_type, _ = validated_attachment(linked_path)
    return candidate, content_type, label


def attachment_record_key(text: str) -> str:
    return hashlib.sha256(("assistant\0" + text).encode("utf-8")).hexdigest()


def resolve_session_attachment_by_key(
    session_path: Path,
    record_key: str,
    link_index: int,
    *,
    read_session_fn: Callable[..., Any] | None = None,
) -> tuple[Path, str, str]:
    if not re.fullmatch(r"[a-f0-9]{64}", record_key) or link_index < 0:
        raise FileNotFoundError("attachment not found")
    reader = read_session_fn or search.read_session
    session = reader(session_path, include_tools=False, include_system=False)
    matching = [
        record
        for record in session.records
        if record.role == "assistant"
        and attachment_record_key(record.text) == record_key
    ]
    if not matching:
        raise FileNotFoundError("attachment not found")
    base_directory = Path(session.cwd).expanduser() if session.cwd else None
    links = local_attachment_links(matching[-1].text, base_directory)
    if link_index >= len(links):
        raise FileNotFoundError("attachment not found")
    label, linked_path = links[link_index]
    candidate, content_type, _ = validated_attachment(linked_path)
    return candidate, content_type, label


def attachment_inline_filename(label: str, path: Path) -> str:
    normalized = unicodedata.normalize("NFC", label)
    normalized = re.sub(r"[\\/]+", " - ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    suffix = path.suffix
    if not normalized:
        normalized = path.stem or "attachment"
    if suffix and not normalized.casefold().endswith(suffix.casefold()):
        normalized += suffix
    return normalized
