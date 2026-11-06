# Search, activity and summaries

[Back to README](../README.md)

## Search

Search ordinary user and assistant messages across local conversations:

```bash
ct activity --from yesterday
ct search "JSONDecodeError"
ct search "stored sessions" --context-turns 2
ct search "render failure" --here
ct search --list --work-dir ~/projects/codex-tools
ct search --list --limit 20
```

List conversations that had actual user or assistant messages inside a local
time range:

```bash
ct activity
ct activity --last 10m
ct activity --last 2h --here
ct activity --from "2026-10-04 18:00" --to "2026-10-04 19:00"
ct activity --from yesterday --here
ct activity --from yesterday --json
```

Activity is local, read-only, and deterministic: no model is called. A table
row represents one logical conversation, `TURNS` counts user messages in the
selected interval, and `WORKDIR` is the session's recorded working directory.
The displayed activity range is the first and last observed message, not a
claim that the conversation was continuously active. Times without an explicit
offset use the detected local timezone; override it with
`--timezone AREA/LOCATION`. Recent durations accept `s`, `m`, `h`, `d`, and
`w`, including combinations such as `--last 1h30m`. Recorded source metadata is
available in `--json` output but is omitted from the table because it describes
the conversation's origin, not an application that is currently running.

**Search is local and read-only.** Add `--include-tools` when command output and
tool events should be searched too. Use `--here` to select conversations started
from the current directory, or `--work-dir PATH` to select another directory.
Directory matching uses canonical paths and is exact, so nested working
directories are not included. These filters require the default JSONL source;
the SQLite compatibility source does not contain working-directory metadata.

## Summaries

Build Markdown summaries from recent Codex activity:

```bash
ct summary today
ct summary yesterday
ct summary week --last-week
ct summary site --open
```

Preview collected context without calling a model:

```bash
ct summary today --show-context
```

**Daily and weekly settings are independent.** Inspect or change them through
the unified configuration:

```bash
ct config show
ct config set summary.daily.words 250
ct config set summary.weekly.words 400
```
