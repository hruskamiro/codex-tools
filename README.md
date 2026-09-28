# Codex Tools

Local utilities for recovering, searching, viewing, and summarizing Codex work
context, plus reproducible structured Codex execution. The primary
`codex-tools` CLI provides search, summaries, diagnostics, and a browser viewer;
`codex-manager` launches Codex with separate profile homes and optionally shared
conversation stores.

The viewer, search, and summary tools read Codex state without modifying it.
The manager writes only its own profile homes, conversation stores, and wrapper
commands.

The CLI also provides an explicit structured-task runner. Unlike the context
utilities, this command calls a model and writes reproducibility artifacts only
to the run directory supplied by the caller.

## Install

Codex Tools requires Python 3.10 or newer and is currently intended for POSIX
systems such as Linux and macOS. Model-backed commands also require the `codex`
CLI to be installed and authenticated.

The optional LaTeX conversation view requires `xelatex`, the `standalone`,
`fontspec`, `xcolor`, `hyperref`, `fvextra`, `enumitem`, `tabularx`, `colortbl`,
and `tikz` packages, plus the TeX Gyre Pagella, TeX Gyre Heros, and PT Mono
fonts. Without them, the viewer remains usable and shows Markdown whenever a
PDF cannot be generated. Run `xelatex --version` to check the main executable.

Install directly from GitHub with `pipx`:

```bash
pipx install git+https://github.com/hruskamiro/codex-tools.git
```

Or install from a repository checkout:

```bash
pipx install .
```

This installs three commands:

```bash
codex-tools
codex-manager
codex-viewer
```

For development from a checkout, the local wrappers still work:

```bash
./codex-tools --help
./codex-manager --help
./codex-viewer --help
```

After changing entry points or package metadata, reinstall with:

```bash
pipx reinstall codex-tools
```

Install the short `ct` command and bash completion:

```bash
codex-tools alias install
source ~/.local/share/bash-completion/completions/ct
```

The alias installer refuses to replace an existing `ct` command or completion
file. Remove it with:

```bash
codex-tools alias remove
```

## Tools

### Search Local Codex Context

```bash
./codex-tools search "search terms"
```

Useful options:

```bash
./codex-tools search "JSONDecodeError" --include-tools --role tool
./codex-tools search "stored sessions" --context-turns 2
./codex-tools search "render failure" --context-lines 1
./codex-tools search "sidescribe price" --since 2026-09-01 --role user
./codex-tools search --list --limit 20
./codex-tools diagnose
```

Search results include both the conversation creation time and the latest parsed
transcript activity as `updated`. Multiline matches preserve their layout and
show two physical lines before and after the matching line by default;
`--context-turns` separately adds neighboring conversation messages.

### View Conversations In A Browser

Run the viewer in the foreground:

```bash
codex-viewer serve
```

Then open `http://127.0.0.1:8765`.

The viewer intentionally binds only to loopback by default because its API can
read private transcripts and has no authentication. A non-loopback `--host`
requires the explicit `--allow-remote` acknowledgment.

Pinned Markdown, syntax-highlighting, equation, and PDF rendering assets ship
with the package and are served locally by default. To use the matching jsDelivr
copies instead, start or restart with `--web-assets cdn`. Use
`--web-assets bundled` to switch back to the offline default.

Or run it like a small daemon:

```bash
ct viewer
codex-viewer start
codex-viewer restart
codex-viewer status
codex-viewer open
codex-viewer stop
```

`ct viewer` starts the daemon when it is not running; once it is running, the
same command opens the terminal picker.

For typesetter development, enable per-bubble isolated previews:

```bash
codex-viewer restart --typeset-debug
```

Fenced code blocks use Pygments highlighting when it is available. Select the
renderer for the whole server with `--typeset-code-mode auto|pygments|verbatim`,
or compare one typeset page with `?code=pygments` and `?code=verbatim`. The
default `auto` mode highlights recognized fence languages and falls back to
plain verbatim rendering for unknown languages or installations without
Pygments. Highlighting is generated in Python; XeLaTeX remains in
`-no-shell-escape` mode.

Assistant bubbles in the normal conversation view then show a `Typeset debug`
link. It opens a dedicated `/debug/typeset/...` page; refreshing that page
recompiles only the selected bubble and bypasses its PDF cache.

Typeset views use an HTML bubble header by default, leaving the generated PDF
to contain only the assistant answer. Add `?header=embedded` to a typeset or
isolated-debug URL to compare the earlier PDF-embedded header mode.

Pick a recent conversation from a terminal list and open it directly:

```bash
codex-viewer pick
```

`codex-tools viewer ...` is an alias for the same viewer commands. With no
subcommand, `codex-tools viewer` and `codex-viewer` start the background viewer
when needed, then open the terminal picker on later runs.

Markdown is the initial view for conversations selected in the browser or
terminal picker. Set a persistent per-user default with either command:

```bash
codex-tools viewer --set-default-latex
codex-tools viewer --set-default-markdown
```

The equivalent `codex-viewer` commands work as well. The preference is stored
at `$XDG_CONFIG_HOME/codex-tools/viewer.json`, or
`~/.config/codex-tools/viewer.json` when `XDG_CONFIG_HOME` is unset, and takes
effect without restarting the viewer. Press `T` in an open conversation to
switch its view without changing the saved default.

Viewer URLs open in a new browser window by default for Brave, Chrome, Chromium,
and Firefox. Override the browser with `--browser` or `CODEX_TOOLS_BROWSER`
(`CODEX_VIEWER_BROWSER` remains supported), or pass `--same-window` to skip the
`--new-window` flag.

The viewer renders recent JSONL conversations with Markdown, code highlighting,
language detection for unlabeled code blocks, equation support, semantic
highlighting, a browser conversation picker, and a terminal picker. It is
read-only.

Press `/` to expand the navigation bar. Its `LaTeX` action opens the
XeLaTeX-rendered conversation, while `Markdown` switches back without losing
the conversation identity.

Equations may use `\(...\)` or `$...$` for inline math and `\[...\]` or
`$$...$$` for display math. LaTeX delimiters inside code spans and fenced code
blocks remain literal.

Semantic highlighting currently colors numbers, quoted strings, and path-like
tokens in rendered prose and code blocks. Segment colors apply only to absolute
paths such as `/home/user/projects/codex-tools` and file-like names such as
`sample_data.jsonl`; ordinary words with `_` or `-` are left alone. The
first-pass theme lives in CSS variables in `codex_tools/viewer.py` so future
themes can replace the palette without changing the tokenizer.

The live conversation view does not auto-refresh. Press `R` or use either
refresh button to fetch new messages while keeping the currently visible
message at the same screen position. The viewer shows refresh progress and does
not move to the latest message unless you choose `Latest` or press `L`.

### Manage Codex Profiles

Create a Codex profile with separate login/config state but shared conversations:

```bash
codex-manager new codex-work
codex-manager run codex-work
```

By default, new profiles share conversations with the existing default
`~/.codex` home while keeping auth and config isolated. To create a profile with
its own conversation store:

```bash
codex-manager new codex-private --isolated
```

To share conversations with another managed profile:

```bash
codex-manager new codex-client --share codex-private
```

Install a short command for a profile:

```bash
codex-manager install codex-work
codex-work
```

Useful inspection commands:

```bash
codex-manager list
codex-manager doctor codex-work
codex-manager repair codex-work --force
codex-manager remove codex-work
codex-manager path codex-work
```

`repair --force` reseeds derived local state such as `state_5.sqlite` from the
shared profile and keeps a timestamped backup of the old files.
`remove` moves the managed profile to `~/.codex-manager/trash` by default; use
`--purge` to delete immediately, and `--remove-store` only for private stores.

The manager can also be reached as `codex-tools manager ...`.

### Build Summaries

Extract today's transcript context without calling a model:

```bash
./codex-tools summary today --show-context
```

Inspect the complete prompt that would be sent to the model:

```bash
./codex-tools summary today --show-prompt
./codex-tools summary week --last-week --show-prompt
```

Print the raw template with its placeholders intact:

```bash
./codex-tools summary today --show-template
./codex-tools summary week --show-template
```

Ask `codex exec` to summarize the extracted context:

```bash
./codex-tools summary today
```

Other summary commands:

```bash
./codex-tools summary yesterday
./codex-tools summary day 2026-09-11
./codex-tools summary week --last-week
./codex-tools summary site --open
./codex-tools summary clean
```

`summary site --open` opens the archive in a new browser window by default.
Use `--same-window` to reuse the current window, or `--browser` to select a
specific browser command. `CODEX_TOOLS_BROWSER` provides the same override for
both summaries and the conversation viewer.

The built-in model instructions are ordinary Markdown templates:

- `codex_tools/templates/daily_summary.md`
- `codex_tools/templates/weekly_summary.md`

Daily templates receive `$weekday`, `$day`, `$timezone`, and `$context`.
Weekly templates receive `$start`, `$end`, and `$context`. Override either for
one run with `--prompt-template PATH`; the template is rendered before any
model invocation.

Daily and weekly commands detect the local IANA timezone from `TZ` and the
system timezone configuration. Pass `--timezone AREA/CITY` to override it;
systems without a recognizable local zone fall back to `UTC`.

A weekly run first refreshes missing or stale daily summaries for dates that
contain Codex activity, then summarizes the saved daily notes. Freshness is
determined from a small `.md.json` sidecar containing hashes of the extracted
context and daily prompt template, plus the latest source timestamp. Continued
work, edited source context, or a changed template therefore causes only the
affected day to be regenerated. Existing summaries without metadata are
treated as stale once. `--show-context` stays model-free and only displays
context from daily summaries that already exist.

The default policy is `--refresh-dailies auto`. It can be overridden when
needed:

```bash
codex-tools summary week --refresh-dailies missing
codex-tools summary week --refresh-dailies all
codex-tools summary week --refresh-dailies none
```

`missing` preserves the earlier behavior, `all` regenerates every active day,
and `none` uses only saved daily summaries. Use `--daily-prompt-template PATH`
when automatic daily refreshes should use a custom daily template.
Daily summary filenames include the weekday, and the static site groups daily
summaries by ISO week. By default, generated summaries and sites live under:

- `~/.local/share/codex-tools/summaries/daily/`
- `~/.local/share/codex-tools/summaries/weekly/`
- `~/.local/share/codex-tools/summaries/site/`

Set `XDG_DATA_HOME` to move the whole `codex-tools` data root, or use command
flags such as `--output-dir`, `--daily-summaries-dir`, `--weekly-summaries-dir`,
and `--site-dir` for one-off locations.

The site builder only replaces an existing output directory when it contains
the marker written by an earlier `codex-tools summary site` run. This prevents a
mistyped `--site-dir` from deleting an unrelated directory.

`summary clean` shows what it will remove and asks for confirmation. It removes
daily summaries, weekly summaries, and the derived static site by default. Use
`--daily`, `--weekly`, or `--site` to narrow the selection, and `--yes` only for
non-interactive use. Removing daily or weekly summaries also removes the site
so it cannot continue showing stale entries.

### Run Structured Tasks

Run a self-contained prompt under a strict JSON Schema:

```bash
codex-tools structured run \
  --prompt prompt.txt \
  --schema response.schema.json \
  --run-dir runs/example-001 \
  --model gpt-5.6-sol \
  --reasoning-effort medium \
  --profile default
```

Use `--prompt -` to read the prompt from stdin. The run is ephemeral,
read-only, and isolated from user configuration and project rules. Structured
tasks must not use tools; attempted tool actions cause the run to fail.

`--profile NAME` selects a profile created by `codex-manager` by setting its
managed home as `CODEX_HOME`. The default is the manager's `default` profile.
Structured execution still ignores profile configuration for reproducibility;
the selected profile supplies authentication and account identity.

Each run directory retains `prompt.txt`, `schema.json`, `events.jsonl`,
`stderr.log`, `response.txt`, `result.json`, and `run.json`. The run
metadata records status, timestamps, duration, model settings, input hashes,
token usage, and any policy violations. Existing nonempty run directories are
never overwritten, and failed runs are not retried automatically.

The Python API is available as `codex_tools.structured.run_task`. Callers remain
responsible for constructing domain-specific prompts and performing semantic
validation after reading `result.json`.

Run several independent tasks from a versioned JSON manifest:

```json
{
  "version": 1,
  "defaults": {
    "model": "gpt-5.6-sol",
    "reasoning_effort": "medium",
    "timeout": 1800
  },
  "tasks": [
    {
      "id": "part-01",
      "prompt": "prompts/part-01.txt",
      "schema": "schemas/result.json"
    },
    {
      "id": "part-02",
      "prompt": "prompts/part-02.txt",
      "schema": "schemas/result.json",
      "metadata": {"source": "chapter-02"}
    }
  ]
}
```

Prompt and schema paths are resolved relative to the manifest. Each task may
override `model`, `reasoning_effort`, or `timeout`. Task IDs are stable
directory names and may contain letters, numbers, dots, underscores, and
hyphens.

```bash
codex-tools structured batch batch.json \
  --batch-dir runs/book-001 \
  --jobs 4
```

The batch runner executes at most `--jobs` tasks concurrently, continues after
individual failures, and writes each task as an ordinary structured run under
`tasks/<id>/`. Its `summary.json` records per-task status and aggregate token
usage. Re-running the same manifest skips successful tasks when their prompt and
schema hashes still match. Existing failed, incomplete, or changed task runs
are reported but never retried or overwritten automatically.

Override the model, reasoning effort, or timeout for every selected task from
the command line:

```bash
codex-tools structured batch batch.json \
  --batch-dir runs/model-comparison \
  --model gpt-6-astra \
  --reasoning-effort high \
  --timeout 2400 \
  --profile codex-work
```

Configuration precedence is CLI override, then task-specific manifest value,
then batch default. Reasoning effort otherwise defaults to `medium`, timeout
to 1,800 seconds, and a model must be supplied at one of those three levels.
Resolved settings are saved in every task's `run.json`; CLI overrides are also
recorded in the batch `summary.json`.

Daily and weekly model-backed summaries accept the same manager profile:

```bash
codex-tools summary today --profile codex-work
codex-tools summary week --last-week --profile codex-work
```

Context-only and static-site summary operations do not invoke a model.

Select a zero-based, half-open range of manifest tasks with `--idxs START STOP`.
For example, this runs task indexes 10 through 19:

```bash
codex-tools structured batch batch.json \
  --batch-dir runs/book-001 \
  --jobs 4 \
  --idxs 10 20
```

Completed tasks outside the selected range remain part of the batch accounting.
In `summary.json`, `usage` is the cumulative usage of all completed tasks,
while `invocation_usage` contains only tokens consumed by tasks actually
started in the latest invocation. Each task's `run.json` retains its original
input, cached-input, output, and reasoning token counts.

Verify either one run or a whole batch:

```bash
codex-tools structured check runs/example-001
codex-tools structured check runs/book-001
```

Checking verifies artifacts, hashes, JSON parsing, schema validity, process
status, and absence of tool events. It intentionally does not attempt
domain-specific factual or quality validation.

## Current Structure

The CLI is intentionally thin. It routes into modules that own their domain:

- `codex_tools/cli.py`: top-level command routing.
- `codex_tools/search.py`: transcript reading, matching, filtering, grouping,
  diagnostics, and snippets.
- `codex_tools/summary.py`: summary command routing.
- `codex_tools/summary_common.py`: shared model execution for summaries.
- `codex_tools/summary_prompts.py`: packaged prompt loading and substitution.
- `codex_tools/templates/`: editable daily and weekly prompt templates.
- `codex_tools/summary_clean.py`: confirmed cleanup of generated summaries.
- `codex_tools/summarize_daily.py`: daily context extraction and summary
  generation.
- `codex_tools/summarize_weekly.py`: weekly summary generation from saved daily
  summaries.
- `codex_tools/summary_site.py`: static summary archive builder.
- `codex_tools/viewer.py`: local read-only browser viewer.
- `codex_tools/manager.py`: Codex profile homes, conversation sharing, and
  profile wrapper installation.
- `codex_tools/codex_exec.py`: low-level non-interactive Codex execution.
- `codex_tools/structured.py`: schema-constrained task execution and artifacts.
