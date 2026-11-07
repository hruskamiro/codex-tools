# Structured tasks

[Back to README](../README.md)

**Run focused prompts under a strict JSON Schema.** Prompts, settings, events,
results, hashes, and token usage are saved for reproducibility. Runs are isolated
and tool-free; unexpected tool events are rejected.

```bash
ct structured run --prompt prompt.txt --schema response.schema.json \
  --run-dir runs/example-001 --model MODEL
```

The default backend uses `codex exec` under a managed profile. See below for
the separately authenticated app-server backend.

## Batches and adaptive pacing

**One job and adaptive pacing are the defaults**, for both backends:

```bash
ct structured batch batch.json --batch-dir runs/batch-001
ct structured check runs/batch-001
```

`--jobs N` is a hard maximum. A rate limit pauses new requests for **60 seconds**,
then resumes at half the previous concurrency, rounded down to at least one.
Repeated affected tasks double the pause up to ten minutes; repeated events from
one task extend the pause without repeatedly cutting concurrency. Timeouts and
HTTP 502/503/504 pause admissions without reducing concurrency on their own.

**Recovery needs 30 healthy seconds and three successful tasks at the current
level.** Each step adds one slot, up to `--jobs`, then requires fresh work and a
new observation period. Older requests and idle time alone do not prove recovery.
Running requests finish normally; the scheduler never replays failed tasks.
Codex may retry inside a running request.

```bash
ct structured batch batch.json --batch-dir runs/parallel --jobs 8
ct structured batch batch.json --batch-dir runs/fixed --jobs 8 --fixed
```

**`--fixed` keeps concurrency constant while retaining cooldowns.** Tune the
adaptive policy only when needed:

| Option | Default | Meaning |
|---|---|---|
| `--rate-limit-backoff SECONDS` | `60` | Initial cooldown; positive, at most 600 seconds |
| `--backoff-decrease RULE` | `0.5x` | Multiply by a factor below one, or subtract an integer (`-1`) |
| `--backoff-increase RULE` | `+1` | Add an integer, or multiply by a factor above one (`1.25x`, rounded up) |
| `--backoff-recovery-period SECONDS` | `30` | Minimum healthy period before each increase |

Example: `--backoff-decrease=-1 --backoff-increase 1.25x`. Fixed mode rejects
concurrency tuning options. These controls apply to batches, not single runs.

**State survives sequential batches.** The summary shows concurrency, cooldown,
recovery progress, and recent events. `backoff-state.json` is saved in the batch
directory; use `--backoff-state FILE` to share it across sequential runs, not
concurrent processes. Reduced concurrency is capped at the new `--jobs` limit;
recovery needs fresh successes. Use a new state file when changing policy,
including switching to Fixed. Version-one state files are accepted.

`--admission-deadline TIMESTAMP` stops new work after an ISO timestamp with a UTC
offset. Active tasks finish; untouched tasks become `deferred-deadline` and the
batch remains incomplete. Resume later without the expired deadline.

## App-server connection

**Compact instructions, no tools, and an empty workspace reduce prompt overhead.**
Each task starts a fresh ephemeral thread with your prompt and schema; project
files, dynamic tools, environments, and capability roots are not supplied.
Reasoning summaries are disabled; recorded token usage lets you measure overhead.

```bash
ct structured connection login --connection default
ct structured connection status --connection default
ct structured connection usage
ct structured run --backend app-server --connection default \
  --prompt prompt.txt --schema response.schema.json \
  --run-dir runs/example-app-server --model MODEL
```

`--connection` selects the app-server login; `--profile` selects the exec profile.
App-server credentials stay under `~/.config/codex-tools/app-server/`, separate
from Codex homes. The shell tool is disabled; workspace access is read-only.

App-server batches also record account usage before/after execution, including
integer percentage-point deltas. These snapshots use authenticated `~/.codex`
by default (`--usage-codex-home` overrides it), independently of the task connection.
