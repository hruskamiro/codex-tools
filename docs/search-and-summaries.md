# Search, activity and summaries

[Back to README](../README.md)

## Search

**Find past messages locally, without calling a model.**

```bash
ct search "JSONDecodeError"
ct search "stored sessions" --context-turns 2
ct search "render failure" --here
ct search --list --work-dir ~/projects/codex-tools --limit 20
```

`--include-tools` adds command output and tool events. `--here` and `--work-dir`
match the exact canonical working directory, excluding nested directories.
These filters use JSONL; the SQLite compatibility source lacks workdir metadata.

**Activity lists conversations with actual messages in a time range:**

```bash
ct activity --last 2h --here
ct activity --from yesterday --json
ct activity --from "2026-10-04 18:00" --to "2026-10-04 19:00"
```

Times use the detected local timezone unless an offset or `--timezone` is supplied.
Durations accept `s`, `m`, `h`, `d`, and `w`, including `1h30m`. Each row is one
logical conversation; `TURNS` counts user messages. The observed range is not
continuous working time. Source metadata is available in JSON output.

## Summaries

**Turn recent activity into Markdown and a browsable static site.**

```bash
ct summary today
ct summary yesterday
ct summary week --last-week
ct summary site --open
ct summary today --show-context  # preview context without a model call
```

**Daily and weekly settings are independent:**

```bash
ct config set summary.daily.words 250
ct config set summary.weekly.words 400
ct config show
```
