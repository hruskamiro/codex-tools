# Structured tasks

[Back to README](../README.md)

The default backend runs an isolated `codex exec` under a codex-manager profile:

Run a prompt under a strict JSON Schema and retain reproducibility artifacts:

```bash
ct structured run \
  --prompt prompt.txt \
  --schema response.schema.json \
  --run-dir runs/example-001 \
  --model MODEL
```

## Batches and adaptive pacing

Run a batch and verify its saved artifacts:

```bash
ct structured batch batch.json --batch-dir runs/batch-001
ct structured check runs/batch-001
```

**The default is one job with adaptive pacing**, for both execution backends.
`--jobs N` sets a hard maximum. A provider rate limit pauses new requests for
60 seconds, then resumes at half the previous concurrency (rounded down, at
least one). Repeated affected requests double the cooldown up to ten minutes.
Repeated error events within one request extend its cooldown without repeatedly
cutting concurrency. Timeouts and HTTP 502/503/504 also pause new requests,
without reducing concurrency on their own.

Recovery requires **30 healthy seconds and three successful tasks** at the
current level. Each recovery step adds one slot, up to `--jobs`; subsequent
steps require fresh work and a new healthy period. Requests that started before
a limit or a recovery step do not count towards that next period. Idle time,
including time between batches, cannot increase concurrency on its own.

```bash
ct structured batch batch.json --batch-dir runs/parallel --jobs 8
ct structured batch batch.json --batch-dir runs/fixed --jobs 8 --fixed
```

**`--fixed` fixes concurrency, not cooldowns.** It retains provider pauses while
keeping the requested number of slots. Running requests finish normally in
both modes, and the scheduler never automatically replays failed tasks.
Codex itself may perform retries inside a running request.

Optional policy controls:

| Option | Default | Meaning |
|---|---|---|
| `--rate-limit-backoff SECONDS` | `60` | Initial cooldown; positive and at most 600 seconds |
| `--backoff-decrease RULE` | `0.5x` | Multiply concurrency by a factor below one, or subtract an integer (`-1`) |
| `--backoff-increase RULE` | `+1` | Add an integer, or multiply by a factor above one (`1.25x`, rounded up) |
| `--backoff-recovery-period SECONDS` | `30` | Minimum healthy period before each recovery step |

For example, use `--backoff-decrease=-1 --backoff-increase 1.25x` for an additive
cut and multiplicative recovery. `--fixed` rejects the three concurrency tuning
options rather than silently ignoring them. A single `structured run` has no
batch scheduler, so these controls apply only to `structured batch`.

The batch summary includes effective concurrency, cooldown, recovery progress,
and recent policy events. State is saved in `backoff-state.json` in the batch
directory when pacing state changes. Use `--backoff-state FILE` to share it
across **sequential** batches. The file is not a coordinator for concurrent batch
processes. Reusing it preserves the cooldown and reduced concurrency, capped at
the new `--jobs` maximum. Recovery needs fresh successful tasks. When changing
the saved policy (including switching to `--fixed`), choose a new state file.
Older version-one cooldown files are also accepted.

`--admission-deadline TIMESTAMP` stops launching tasks after an ISO timestamp
with a UTC offset. Active tasks drain normally, and untouched tasks are recorded
as `deferred-deadline`. The batch remains incomplete; no model call is made for
those deferred tasks. It can be resumed later without the expired deadline.

## App-server connection

To use the lower-overhead app-server backend, authorize Codex Tools once and
select the saved connection on each run or batch:

```bash
ct structured connection login --connection default
ct structured connection status --connection default
ct structured connection usage

ct structured run \
  --backend app-server \
  --connection default \
  --prompt prompt.txt \
  --schema response.schema.json \
  --run-dir runs/example-app-server \
  --model MODEL
```

`--profile` belongs to the `exec` backend; `--connection` belongs to the
`app-server` backend. App-server credentials are stored in a private file under
`~/.config/codex-tools/app-server/`, independently of Codex homes and
codex-manager profiles.

**App-server runs are designed for focused, low-overhead prompts.** Each task
uses a fresh ephemeral thread with a short dedicated instruction, the supplied
prompt and output schema, and an empty workspace. No project files, dynamic
tools, environments, or capability roots are supplied; reasoning summaries are
disabled. This reduces general coding-agent context around the actual task.
Recorded token usage lets you measure the overhead for your prompts and model.

**Structured runs are isolated and tool-free.** Codex Tools disables the shell
tool, supplies no dynamic tools or environments, uses an empty read-only
workspace, and rejects any unexpected tool event. Prompts, schemas, events,
results, hashes, settings, and token usage are retained in the run directory.

App-server batches also save account usage snapshots immediately before and
after execution, including the integer percentage-point change for each usage
window. These snapshots come from the authenticated `~/.codex` account by
default and can be redirected with `--usage-codex-home`; they are independent
of the app-server task connection.
