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

Run an independent batch in parallel, then verify its saved artifacts:

```bash
ct structured batch batch.json --batch-dir runs/batch-001 --jobs 4
ct structured check runs/batch-001
```

App-server batches can opt into provider backoff:

```bash
ct structured batch batch.json --batch-dir runs/book --backend app-server \
  --jobs 2 --rate-limit-backoff 60 --backoff-state runs/book-backoff.json
```

HTTP 429, transient server errors and request timeouts pause new admissions and
reduce concurrency to one. The initial pause doubles for subsequent affected
requests, up to ten minutes; repeated signals within one request extend its quiet
period without increasing the incident count. Active requests finish normally,
and failed tasks are not automatically replayed. Sharing `--backoff-state` carries
the cooldown and reduced concurrency across sequential batches. Live state is
included in the batch summary. Backoff is disabled unless requested.

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

**Structured runs are isolated and tool-free.** Codex Tools disables the shell
tool, supplies no dynamic tools or environments, uses an empty read-only
workspace, and rejects any unexpected tool event. Prompts, schemas, events,
results, hashes, settings, and token usage are retained in the run directory.

App-server batches also save account usage snapshots immediately before and
after execution, including the integer percentage-point change for each usage
window. These snapshots come from the authenticated `~/.codex` account by
default and can be redirected with `--usage-codex-home`; they are independent
of the app-server task connection.
