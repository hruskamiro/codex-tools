#!/usr/bin/env python3
"""Build a static website archive from saved Codex daily and weekly summaries."""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from codex_tools import browser, paths

DEFAULT_DAILY_SUMMARIES_DIR = paths.DAILY_SUMMARIES_DIR
DEFAULT_WEEKLY_SUMMARIES_DIR = paths.WEEKLY_SUMMARIES_DIR
DEFAULT_SITE_DIR = paths.SUMMARY_SITE_DIR
SITE_MARKER = ".codex-tools-summary-site"
DAILY_SUMMARY_RE = re.compile(
    r"^codex-daily-summary-(?P<day>\d{4}-\d{2}-\d{2})-"
    r"(?:(?P<weekday>[a-z]+)-)?"
    r"(?P<stamp>\d{8}T\d{6}[+-]\d{4})\.md$"
)
WEEKLY_SUMMARY_RE = re.compile(
    r"^codex-weekly-summary-(?P<start>\d{4}-\d{2}-\d{2})_to_"
    r"(?P<end>\d{4}-\d{2}-\d{2})-(?P<stamp>\d{8}T\d{6}[+-]\d{4})\.md$"
)


@dataclass(frozen=True)
class SummaryEntry:
    markdown_path: Path
    markdown_name: str
    html_name: str
    label: str
    timestamp: datetime
    title: str
    excerpt: str
    day: date | None = None
    week_start: date | None = None
    week_end: date | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-tools summary site",
        description="Build a static HTML archive for Codex daily and weekly summaries."
    )
    parser.add_argument(
        "--daily-summaries-dir",
        type=Path,
        default=DEFAULT_DAILY_SUMMARIES_DIR,
        help=(
            "Directory containing saved daily Markdown summaries. "
            f"Default: {DEFAULT_DAILY_SUMMARIES_DIR}"
        ),
    )
    parser.add_argument(
        "--weekly-summaries-dir",
        type=Path,
        default=DEFAULT_WEEKLY_SUMMARIES_DIR,
        help=(
            "Directory containing saved weekly Markdown summaries. "
            f"Default: {DEFAULT_WEEKLY_SUMMARIES_DIR}"
        ),
    )
    parser.add_argument(
        "--site-dir",
        type=Path,
        default=DEFAULT_SITE_DIR,
        help=f"Output directory for the static site. Default: {DEFAULT_SITE_DIR}",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated site index in a new browser window.",
    )
    browser.add_browser_args(parser)
    return parser.parse_args(argv)


def compact_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def first_heading(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or fallback
    return fallback


def first_excerpt(markdown: str, title: str, limit: int = 180) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        text = compact_space(stripped.lstrip("- ").strip())
        if not text or text == title:
            continue
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."
    return ""


def weekday_slug(day: date) -> str:
    return day.strftime("%A").lower()


def weekday_label(day: date) -> str:
    return day.strftime("%A")


def week_start_for(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_label(start: date) -> str:
    end = start + timedelta(days=6)
    return f"{start.isoformat()} to {end.isoformat()}"


def daily_site_names(day: date, stamp: str) -> tuple[str, str]:
    markdown_name = (
        f"codex-daily-summary-{day.isoformat()}-{weekday_slug(day)}-{stamp}.md"
    )
    return markdown_name, markdown_name.removesuffix(".md") + ".html"


def normalize_daily_title(title: str, day: date) -> str:
    weekday = weekday_label(day)
    iso = day.isoformat()
    if weekday in title:
        return title
    if title == f"Daily Summary: {iso}":
        return f"Daily Summary: {weekday}, {iso}"
    if iso in title:
        return title.replace(iso, f"{weekday}, {iso}", 1)
    return f"{weekday}, {iso}: {title}"


def normalized_markdown_for_entry(entry: SummaryEntry) -> str:
    markdown = entry.markdown_path.read_text(encoding="utf-8", errors="replace")
    if entry.day is None:
        return markdown
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# "):
            lines[index] = f"# {entry.title}"
            return "\n".join(lines) + ("\n" if markdown.endswith("\n") else "")
    return f"# {entry.title}\n\n{markdown}"


def read_daily_entries(summaries_dir: Path) -> list[SummaryEntry]:
    entries: list[SummaryEntry] = []
    if not summaries_dir.exists():
        return entries

    for path in summaries_dir.glob("*.md"):
        match = DAILY_SUMMARY_RE.match(path.name)
        if not match:
            continue
        try:
            day = date.fromisoformat(match.group("day"))
            stamp = match.group("stamp")
            timestamp = datetime.strptime(stamp, "%Y%m%dT%H%M%S%z")
        except ValueError:
            continue
        markdown = path.read_text(encoding="utf-8", errors="replace")
        markdown_name, html_name = daily_site_names(day, stamp)
        title = normalize_daily_title(
            first_heading(markdown, f"Daily Summary: {weekday_label(day)}, {day.isoformat()}"),
            day,
        )
        entries.append(
            SummaryEntry(
                markdown_path=path,
                markdown_name=markdown_name,
                html_name=html_name,
                label=f"{weekday_label(day)}, {day.isoformat()}",
                timestamp=timestamp,
                title=title,
                excerpt=first_excerpt(markdown, title),
                day=day,
                week_start=week_start_for(day),
                week_end=week_start_for(day) + timedelta(days=6),
            )
        )

    entries.sort(key=lambda entry: (entry.day or date.min, entry.timestamp), reverse=True)
    return entries


def read_weekly_entries(summaries_dir: Path) -> list[SummaryEntry]:
    entries: list[SummaryEntry] = []
    if not summaries_dir.exists():
        return entries

    for path in summaries_dir.glob("*.md"):
        match = WEEKLY_SUMMARY_RE.match(path.name)
        if not match:
            continue
        try:
            timestamp = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%S%z")
        except ValueError:
            continue
        markdown = path.read_text(encoding="utf-8", errors="replace")
        label = f"{match.group('start')} to {match.group('end')}"
        title = first_heading(markdown, f"Weekly Summary: {label}")
        entries.append(
            SummaryEntry(
                markdown_path=path,
                markdown_name=path.name,
                html_name=path.with_suffix(".html").name,
                label=label,
                timestamp=timestamp,
                title=title,
                excerpt=first_excerpt(markdown, title),
                week_start=date.fromisoformat(match.group("start")),
                week_end=date.fromisoformat(match.group("end")),
            )
        )

    entries.sort(key=lambda entry: (entry.label, entry.timestamp), reverse=True)
    return entries


def ensure_clean_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlinked site directory: {path}")
    resolved = path.expanduser().resolve()
    protected = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}
    protected.update(Path.home().resolve().parents)
    protected.update(Path.cwd().resolve().parents)
    if resolved in protected:
        raise ValueError(f"refusing to replace unsafe site directory: {path}")
    if path.exists() and not path.is_dir():
        raise ValueError(f"site path is not a directory: {path}")
    if path.exists() and any(path.iterdir()) and not (path / SITE_MARKER).is_file():
        raise ValueError(
            f"refusing to replace nonempty directory not created by codex-tools: {path}"
        )
    if path.exists():
        shutil.rmtree(path)
    paths.ensure_private_dir(path)
    paths.write_private_text(path / SITE_MARKER, "codex-tools summary site\n")


def stylesheet() -> str:
    return """
:root {
  color-scheme: light;
  --bg: #f8f7f3;
  --surface: #fffdfa;
  --surface-soft: #f0eee8;
  --text: #24231f;
  --muted: #706b61;
  --faint: #9a9285;
  --line: #ded8cc;
  --line-strong: #c9bfaf;
  --accent: #2d6f75;
  --accent-dark: #174c52;
  --accent-soft: #dfefed;
  --code: #efede6;
  --mark: #9b4e6f;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  background:
    radial-gradient(circle at 20% 0%, rgba(255, 253, 250, 0.9), rgba(255, 253, 250, 0) 28%),
    linear-gradient(135deg, #fbfaf6 0%, #f0eee7 55%, #f8f6f1 100%);
  color: var(--text);
  font-family: Inter, Aptos, "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  line-height: 1.62;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
}

a {
  color: var(--accent-dark);
  text-decoration-thickness: 1px;
  text-underline-offset: 4px;
}

.shell {
  max-width: 1120px;
  margin: 0 auto;
  padding: 46px 24px 72px;
}

.topbar {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: flex-end;
  padding-bottom: 26px;
  border-bottom: 1px solid var(--line);
}

h1 {
  margin: 0;
  max-width: 720px;
  font-family: "Iowan Old Style", Charter, "Source Serif 4", Georgia, serif;
  font-size: clamp(38px, 6vw, 68px);
  font-weight: 640;
  line-height: 0.98;
  letter-spacing: 0;
}

.deck {
  max-width: 660px;
  margin: 18px 0 0;
  color: var(--muted);
  font-family: "Iowan Old Style", Charter, "Source Serif 4", Georgia, serif;
  font-size: 21px;
  line-height: 1.45;
}

.meta {
  color: var(--muted);
  font-size: 14px;
  font-variant-numeric: tabular-nums;
  text-align: right;
  white-space: nowrap;
}

.nav-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 9px;
  margin-top: 24px;
}

.nav-tabs a {
  display: inline-flex;
  align-items: center;
  min-height: 36px;
  padding: 6px 12px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  color: var(--text);
  font-size: 14px;
  text-decoration: none;
}

.nav-tabs a:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.archive-section {
  margin-top: 42px;
}

.archive-section h2 {
  margin: 0;
  color: var(--accent-dark);
  font-size: 12px;
  font-weight: 780;
  letter-spacing: 0;
  text-transform: uppercase;
}

.week-group {
  margin-top: 30px;
  padding-top: 22px;
  border-top: 1px solid var(--line);
}

.week-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 14px;
}

.week-heading h3 {
  margin: 0;
  font-family: "Iowan Old Style", Charter, "Source Serif 4", Georgia, serif;
  font-size: 28px;
  font-weight: 640;
  line-height: 1.15;
}

.week-heading .meta {
  font-size: 13px;
}

.archive {
  display: grid;
  gap: 14px;
  margin-top: 14px;
}

.entry {
  display: grid;
  grid-template-columns: 170px minmax(0, 1fr);
  gap: 22px;
  padding: 18px 0 19px;
  border-bottom: 1px solid rgba(222, 216, 204, 0.82);
}

.entry time {
  display: grid;
  gap: 4px;
  color: var(--faint);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.weekday {
  color: var(--accent-dark);
  font-size: 15px;
  font-weight: 760;
}

.entry h3 {
  margin: 0 0 7px;
  font-family: "Iowan Old Style", Charter, "Source Serif 4", Georgia, serif;
  font-size: 22px;
  font-weight: 620;
  line-height: 1.22;
}

.entry p {
  margin: 0;
  color: var(--muted);
  max-width: 760px;
}

.links {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 11px;
  font-size: 13px;
  font-weight: 650;
}

.summary {
  max-width: 880px;
  margin: 0 auto;
  padding: 42px 24px 76px;
}

.summary-nav {
  margin-bottom: 24px;
  color: var(--muted);
  font-size: 14px;
}

.summary article {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: clamp(24px, 5vw, 48px);
  box-shadow: 0 20px 58px rgba(75, 59, 34, 0.08);
}

.summary h1 {
  margin: 0 0 24px;
  font-size: clamp(32px, 5vw, 52px);
}

.summary h2 {
  margin-top: 34px;
  padding-top: 22px;
  border-top: 1px solid var(--line);
  font-family: "Iowan Old Style", Charter, "Source Serif 4", Georgia, serif;
  font-size: 27px;
  font-weight: 640;
}

.summary p,
.summary li {
  font-size: 18px;
}

.summary li {
  margin: 8px 0;
}

code {
  background: var(--code);
  padding: 0.13em 0.34em;
  border-radius: 4px;
  font-size: 0.88em;
}

pre {
  overflow-x: auto;
  background: var(--code);
  padding: 16px;
  border-radius: 8px;
}

@media (max-width: 680px) {
  .topbar,
  .entry,
  .week-heading {
    display: block;
  }

  .meta {
    margin-top: 12px;
    text-align: left;
    white-space: normal;
  }

  .entry time {
    margin-bottom: 9px;
  }

  .summary article {
    padding: 20px;
  }
}
""".strip()


def html_page(title: str, body: str, css_href: str = "styles.css") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="{html.escape(css_href)}">
</head>
<body>
{body}
</body>
</html>
"""


def render_markdown(markdown_path: Path, html_path: Path, title: str) -> None:
    if shutil.which("pandoc"):
        completed = subprocess.run(
            [
                "pandoc",
                "--from",
                "gfm",
                "--to",
                "html5",
                "--metadata",
                f"title={title}",
                str(markdown_path),
                "-o",
                str(html_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            html_path.chmod(0o600)
            return
        print(
            f"warning: pandoc failed for {markdown_path}: {completed.stderr}",
            file=sys.stderr,
        )

    escaped = html.escape(markdown_path.read_text(encoding="utf-8", errors="replace"))
    paths.write_private_text(html_path, f"<pre>{escaped}</pre>\n")


def build_summary_page(entry: SummaryEntry, summaries_out: Path) -> None:
    raw_out = summaries_out / entry.markdown_name
    html_out = summaries_out / entry.html_name
    shutil.copy2(entry.markdown_path, raw_out)
    raw_out.chmod(0o600)
    rendered_body = summaries_out / f".{entry.html_name}.body"
    rendered_source = summaries_out / f".{entry.html_name}.source.md"
    paths.write_private_text(rendered_source, normalized_markdown_for_entry(entry))
    render_markdown(rendered_source, rendered_body, entry.title)
    rendered_source.unlink(missing_ok=True)
    body = rendered_body.read_text(encoding="utf-8", errors="replace")
    rendered_body.unlink(missing_ok=True)

    page = html_page(
        entry.title,
        f"""<main class="summary">
  <nav class="summary-nav"><a href="../index.html">Back to archive</a> · <a href="{html.escape(entry.markdown_name)}">Raw Markdown</a></nav>
  <article>
{body}
  </article>
</main>
""",
        css_href="../styles.css",
    )
    paths.write_private_text(html_out, page)


def render_entry(entry: SummaryEntry, section_dir: str) -> str:
    if entry.day is not None:
        date_label = entry.day.isoformat()
        weekday = weekday_label(entry.day)
        datetime_value = entry.day.isoformat()
        time_label = entry.timestamp.strftime("%H:%M %z")
    else:
        date_label = entry.label
        weekday = "Weekly"
        datetime_value = entry.timestamp.isoformat()
        time_label = entry.timestamp.strftime("%Y-%m-%d %H:%M %z")
    return f"""<section class="entry">
  <time datetime="{html.escape(datetime_value)}">
    <span class="weekday">{html.escape(weekday)}</span>
    <span>{html.escape(date_label)}</span>
    <span>{html.escape(time_label)}</span>
  </time>
  <div>
    <h3><a href="{html.escape(section_dir)}/{html.escape(entry.html_name)}">{html.escape(entry.title)}</a></h3>
    <p>{html.escape(entry.excerpt)}</p>
    <div class="links">
      <a href="{html.escape(section_dir)}/{html.escape(entry.html_name)}">Open rendered summary</a>
      <a href="{html.escape(section_dir)}/{html.escape(entry.markdown_name)}">Open Markdown</a>
    </div>
  </div>
</section>"""


def group_daily_by_week(entries: list[SummaryEntry]) -> list[tuple[date, list[SummaryEntry]]]:
    grouped: dict[date, list[SummaryEntry]] = {}
    for entry in entries:
        if entry.week_start is None:
            continue
        grouped.setdefault(entry.week_start, []).append(entry)
    return [
        (start, sorted(group, key=lambda entry: (entry.day or date.min, entry.timestamp), reverse=True))
        for start, group in sorted(grouped.items(), reverse=True)
    ]


def render_daily_archive(entries: list[SummaryEntry]) -> str:
    if not entries:
        return '<p class="meta">No saved daily summaries found.</p>'

    groups = []
    for start, group_entries in group_daily_by_week(entries):
        items = "\n".join(render_entry(entry, "summaries") for entry in group_entries)
        unique_days = len({entry.day for entry in group_entries if entry.day is not None})
        entry_count = len(group_entries)
        count_label = (
            f"{entry_count} summaries across {unique_days} day{'s' if unique_days != 1 else ''}"
        )
        groups.append(
            f"""<section class="week-group">
  <header class="week-heading">
    <h3>Week of {html.escape(start.strftime('%B %-d, %Y'))}</h3>
    <div class="meta">{html.escape(week_label(start))} · {html.escape(count_label)}</div>
  </header>
  <div class="archive">
{items}
  </div>
</section>"""
        )
    return "\n".join(groups)


def render_archive_section(
    title: str, anchor: str, entries: list[SummaryEntry], section_dir: str
) -> str:
    items = "\n".join(render_entry(entry, section_dir) for entry in entries)
    if not items:
        items = f'<p class="meta">No saved {html.escape(title.lower())} found.</p>'
    return f"""<section class="archive-section" id="{html.escape(anchor)}">
  <h2>{html.escape(title)}</h2>
  <div class="archive">
{items}
  </div>
</section>"""


def render_daily_archive_section(entries: list[SummaryEntry]) -> str:
    return f"""<section class="archive-section" id="daily">
  <h2>Daily Summaries By Week</h2>
{render_daily_archive(entries)}
</section>"""


def build_index(
    daily_entries: list[SummaryEntry], weekly_entries: list[SummaryEntry], site_dir: Path
) -> None:
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")
    total = len(daily_entries) + len(weekly_entries)
    daily_section = render_daily_archive_section(daily_entries)
    weekly_section = render_archive_section(
        "Weekly Summaries", "weekly", weekly_entries, "weekly"
    )

    body = f"""<main class="shell">
  <header class="topbar">
    <div>
      <h1>Codex Summaries</h1>
      <p class="deck">A quiet archive of daily and weekly Codex work notes, grouped for recall instead of rummaging.</p>
    </div>
    <div class="meta">{total} summaries<br>built {html.escape(generated_at)}</div>
  </header>
  <nav class="nav-tabs" aria-label="Summary archive sections">
    <a href="#daily">Daily</a>
    <a href="#weekly">Weekly</a>
  </nav>
{daily_section}
{weekly_section}
</main>
"""
    paths.write_private_text(
        site_dir / "index.html", html_page("Codex Summaries", body)
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.site_dir = args.site_dir.expanduser().resolve()
    daily_entries = read_daily_entries(args.daily_summaries_dir)
    weekly_entries = read_weekly_entries(args.weekly_summaries_dir)
    try:
        ensure_clean_dir(args.site_dir)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    daily_out = args.site_dir / "summaries"
    weekly_out = args.site_dir / "weekly"
    paths.ensure_private_dir(daily_out)
    paths.ensure_private_dir(weekly_out)
    paths.write_private_text(args.site_dir / "styles.css", stylesheet() + "\n")

    for entry in daily_entries:
        build_summary_page(entry, daily_out)
    for entry in weekly_entries:
        build_summary_page(entry, weekly_out)
    build_index(daily_entries, weekly_entries, args.site_dir)

    index = args.site_dir / "index.html"
    print(f"Built static summary site: {index}")
    if args.open:
        browser.open_browser(index.resolve().as_uri(), args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
