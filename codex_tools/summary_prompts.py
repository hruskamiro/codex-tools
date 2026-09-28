"""Load and render packaged summary prompt templates."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from string import Template


TEMPLATE_FILES = {
    "daily": "daily_summary.md",
    "weekly": "weekly_summary.md",
}


def template_text(kind: str, override: Path | None = None) -> str:
    if override is not None:
        return override.read_text(encoding="utf-8")
    try:
        filename = TEMPLATE_FILES[kind]
    except KeyError as exc:
        raise ValueError(f"unknown summary template kind: {kind}") from exc
    return (
        resources.files("codex_tools.templates")
        .joinpath(filename)
        .read_text(encoding="utf-8")
    )


def render_template(
    kind: str,
    values: dict[str, str],
    override: Path | None = None,
) -> str:
    source = template_text(kind, override)
    try:
        return Template(source).substitute(values)
    except (KeyError, ValueError) as exc:
        location = str(override) if override is not None else TEMPLATE_FILES.get(kind, kind)
        raise ValueError(f"invalid summary prompt template {location}: {exc}") from exc
