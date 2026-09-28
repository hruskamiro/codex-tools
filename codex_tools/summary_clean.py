"""Remove generated summary data after explicit confirmation."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from codex_tools import paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-tools summary clean",
        description="Remove saved summaries and their generated static site.",
    )
    selection = parser.add_argument_group("selection")
    selection.add_argument(
        "--daily", action="store_true", help="Remove saved daily summaries."
    )
    selection.add_argument(
        "--weekly", action="store_true", help="Remove saved weekly summaries."
    )
    selection.add_argument(
        "--site", action="store_true", help="Remove only the generated static site."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    parser.add_argument(
        "--daily-summaries-dir",
        type=Path,
        default=paths.DAILY_SUMMARIES_DIR,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--weekly-summaries-dir",
        type=Path,
        default=paths.WEEKLY_SUMMARIES_DIR,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--site-dir",
        type=Path,
        default=paths.SUMMARY_SITE_DIR,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def summary_files(directory: Path, pattern: str) -> list[Path]:
    if not directory.is_dir() or directory.is_symlink():
        return []
    return sorted(path for path in directory.glob(pattern) if path.is_file())


def validate_site_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise ValueError(f"refusing to clean symlinked site directory: {directory}")
    resolved = directory.expanduser().resolve()
    protected = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}
    if resolved in protected:
        raise ValueError(f"refusing to clean unsafe site directory: {directory}")
    if directory.exists() and not directory.is_dir():
        raise ValueError(f"site path is not a directory: {directory}")


def validate_summary_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise ValueError(f"refusing to clean symlinked summary directory: {directory}")


def remove_empty_directory(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    explicit_selection = args.daily or args.weekly or args.site
    clean_daily = args.daily or not explicit_selection
    clean_weekly = args.weekly or not explicit_selection
    # Any summary change invalidates the derived site archive.
    clean_site = args.site or clean_daily or clean_weekly

    try:
        if clean_daily:
            validate_summary_directory(args.daily_summaries_dir)
        if clean_weekly:
            validate_summary_directory(args.weekly_summaries_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    daily_files = (
        summary_files(args.daily_summaries_dir, "codex-daily-summary-*.md")
        if clean_daily
        else []
    )
    daily_metadata_files = (
        summary_files(args.daily_summaries_dir, "codex-daily-summary-*.md.json")
        if clean_daily
        else []
    )
    weekly_files = (
        summary_files(args.weekly_summaries_dir, "codex-weekly-summary-*.md")
        if clean_weekly
        else []
    )

    site_exists = clean_site and (args.site_dir.exists() or args.site_dir.is_symlink())
    if site_exists:
        try:
            validate_site_directory(args.site_dir)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if not daily_files and not daily_metadata_files and not weekly_files and not site_exists:
        print("Nothing to clean.")
        return 0

    details: list[str] = []
    if clean_daily:
        details.append(
            f"{len(daily_files)} daily summary file(s) and "
            f"{len(daily_metadata_files)} metadata sidecar(s) in "
            f"{args.daily_summaries_dir}"
        )
    if clean_weekly:
        details.append(
            f"{len(weekly_files)} weekly summary file(s) in {args.weekly_summaries_dir}"
        )
    if site_exists:
        details.append(f"generated static site at {args.site_dir}")

    print("The following generated summary data will be removed:")
    for detail in details:
        print(f"  - {detail}")

    if not args.yes:
        try:
            confirmed = input("Continue? [y/N] ").strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            confirmed = False
            print()
        if not confirmed:
            print("Cancelled.")
            return 0

    for path in daily_files + daily_metadata_files + weekly_files:
        path.unlink()
    if clean_daily:
        remove_empty_directory(args.daily_summaries_dir)
    if clean_weekly:
        remove_empty_directory(args.weekly_summaries_dir)
    if site_exists:
        shutil.rmtree(args.site_dir)

    removed = [
        f"{len(daily_files)} daily summary file(s)",
        f"{len(daily_metadata_files)} metadata sidecar(s)",
        f"{len(weekly_files)} weekly summary file(s)",
    ]
    if site_exists:
        removed.append("the generated site")
    print("Removed " + ", ".join(removed) + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
