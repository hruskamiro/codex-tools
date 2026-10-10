# Codex Tools

**A small local toolkit for working with Codex.** Manage separate profiles,
browse and search past conversations, create work summaries, and run
reproducible structured tasks.

Codex Tools is an independent project and is not affiliated with or endorsed by
OpenAI.

## What can I use it for?

- **Read old conversations comfortably** in a private local browser viewer.
- **Typeset technical answers** with polished math, tables, and code blocks.
- **Find something you discussed before** across local Codex JSONL history.
- **Create daily and weekly work summaries** and browse them as a static site.
- **Keep multiple Codex logins separate** while optionally sharing conversations.
- **Move a login to another machine** with passphrase-encrypted profile exports.
- **Run schema-constrained tasks** with saved prompts, results, and token usage.
- **Run structured tasks through a separately authenticated ChatGPT-plan connection.**

## Quick start

**Requirements:** Linux and Python 3.10 or newer. Model-backed summaries and
structured tasks also require an installed Codex CLI. The default `exec`
backend uses its Codex login; the optional app-server backend uses a separate
ChatGPT-plan connection.

Codex Tools is not yet published on PyPI. Install it directly from GitHub:

```bash
pipx install git+https://github.com/hruskamiro/codex-tools.git
```

You now have `codex-tools`, `codex-viewer`, and `codex-manager`.

### Update an existing installation

To reinstall the latest version from the original GitHub source:

```bash
pipx reinstall codex-tools
```

If you installed the optional `ct` alias, refresh its generated Bash
completion after updating:

```bash
codex-tools alias install --force
```

The alias itself continues to work. If the conversation viewer is already
running, restart it so the background process uses the updated code:

```bash
codex-viewer restart
```

### Optional: install `ct` and completion

The shorter `ct` command is convenient for everyday use. Its installer also
adds Bash completion for both `ct` and `codex-tools`:

```bash
codex-tools alias install
source ~/.local/share/bash-completion/completions/codex-tools
```

**Completion covers every current top-level tool and its subcommands.** New
shells load it automatically when the `bash-completion` package is available.

If an earlier uninstall left stale alias or completion files, replace them with:

```bash
codex-tools alias install --force
```

Open the conversation picker:

```bash
ct viewer
```

Without the alias, use `codex-tools viewer` instead.

## Conversation viewer

The viewer reads local Codex sessions and presents them in a searchable,
keyboard-friendly browser interface.

Inspect the Codex sessions currently loaded on this machine, read their latest
exchange, or queue a message using the same session names and IDs:

```bash
ct agents
ct agents show SESSION
ct agents send SESSION "Check the tests and report back."
ct agents ui
```

`ct agents` reads the managed Codex daemon's loaded-thread list, so it shows
only sessions that Codex currently reports as Working or Ready—not archived
transcript history. It uses `codex queue` for message delivery. `ct agents ui`
refreshes the local relay cache and opens the terminal **Agent Console**,
styled after the Codex CLI. You can switch among cached local or remote agents,
read their recent messages, search, scroll, and send or queue a message without
leaving the terminal. The conversation pane starts with an always-available
composer: type immediately, press Enter to send, and use Ctrl-J for a newline.
It expands as text wraps, supports cursor editing and multiline paste, and
keeps a separate unsent draft when you move between agents. Markdown fenced
code blocks use Pygments syntax highlighting, preserve indentation, and render
on a restrained terminal surface without showing the literal fences.

Sent messages appear in the conversation immediately, including in manual
sync mode. A muted line beneath the optimistic bubble reports whether the
message was sent locally, queued offline, delivered to a relay, or failed.
When synchronization finds the corresponding real transcript record, the
temporary bubble is reconciled away instead of appearing twice.

Press Tab to switch between the agent list and composer. Shift-Left also
selects the agent list from anywhere in a draft; use Up/Down and Right to enter
the selected agent's composer. Ctrl-P/Ctrl-N move
between draft lines, while Ctrl-Left/Ctrl-Right move by word. Page Up/Down or
the mouse wheel scrolls the conversation. Ctrl-R refreshes the selected agent,
Ctrl-S refreshes all local sessions and configured peers, Ctrl-V opens the
browser viewer, Ctrl-F searches, and Ctrl-X maximizes or restores the
conversation; Alt-M does the same without consuming a typing key. Press Ctrl-C
to quit. Agents keep a stable creation-time order instead of jumping after new
activity; the first nine are numbered and Alt-1 through Alt-9 switches to them
directly. Alt-Up and Alt-Down move the selected agent within its host group;
the custom order persists across refreshes and console restarts. With
`--no-local`, Ctrl-S skips the local refresh.

The first item in the agent pane opens **Settings**. Its synchronization mode
applies immediately and is saved as `agents.sync_mode` in the regular Codex
Tools configuration:

- `manual` performs no background work;
- `economy` checks the selected conversation roughly every ten seconds;
- `adaptive` keeps a low-bandwidth incremental request open and backs off when
  the connection fails;
- `live` favors the shortest update delay.

When Codex reports that the selected session is active, the console shows a
soft animated `Agent working…` indicator. Remote live sessions carry this
daemon-reported state with their incremental event updates, so delivery alone
is never mistaken for active work. While a turn is running, a transient
animated `Working…` row also appears at the end of the conversation and
disappears when the turn finishes; it is not stored in transcript history.

`adaptive` is the default. Use `ct agents ui --sync MODE` for a temporary
startup override. Automatic synchronization follows only the selected agent;
inactive conversations and attachments are not downloaded in the background.

After synchronizing a relay peer, use `ct agents ui --no-local` to hide this
machine's sessions and browse only cached remote agents.

Viewer routes explicitly identify both presentation and source:
`/v/local/SESSION` and `/t/local/SESSION` use local Codex JSONL data, while
`/v/relay/HOST/AGENT` renders a synchronized remote conversation from the
private relay cache. Remote typesetting is reserved under `/t/relay/...` but is
not implemented yet.

The relay keeps normalized sessions, synchronization cursors, and an
idempotent outgoing-message queue in a private local SQLite cache. Populate or
inspect the local-machine snapshot and outgoing messages with:

```bash
ct relay cache refresh
ct relay cache status
ct relay outbox
```

To expose one machine and pull its read-only snapshot from another:

```bash
# On the machine running Codex:
ct relay init --name desktop
ct relay token                         # transfer this key privately
ct relay serve --host 0.0.0.0 --tls-cert relay.crt --tls-key relay.key

# On the client machine:
ct relay init --name laptop
ct relay pair desktop https://desktop.example:8780 --ca-file relay.crt
ct relay sync desktop
ct agents ui
```

`ct relay pair` prompts for the remote machine's key without echoing it.
`ct relay peers` lists configured connections without exposing their keys. Each
sync refreshes the serving machine's currently loaded Codex sessions before it
returns the snapshot, then stores the result in the client's private cache for
offline viewing. Messages written in the Agent Console are committed to the
local outbox before a network attempt. If the peer is reachable, they are sent
immediately to its `codex queue`; otherwise they remain pending. Ctrl-R,
Ctrl-S, and `ct relay sync PEER` retry pending messages. `ct relay outbox`
shows pending, delivered, and permanently failed messages; add `--json` for
machine-readable status.

The receiver records each message ID before dispatch and acknowledges accepted
IDs idempotently, so an ordinary reconnect or repeated sync does not submit the
same prompt twice. Restart an already-running `ct relay serve` process after
upgrading so it exposes the authenticated write endpoint.

After the first per-agent snapshot, selected-conversation refreshes use an
authenticated byte cursor and transfer only complete JSONL records appended
since the previous refresh. Cursors are bound to the agent and file generation;
truncation or replacement requests a fresh snapshot. The relay also supports a
bounded long-poll events request, backed by one shared lightweight file watcher,
so an interactive client can receive changes promptly without repeatedly
downloading whole conversations.

Plain HTTP is accepted only on loopback by default. When an address is already
protected by a VPN or an encrypted tunnel, explicitly opt in on both ends with
`--allow-insecure-http`; the relay itself does not configure or depend on a
particular VPN. Use HTTPS for an otherwise untrusted network. Remote delivery
currently targets already-running Codex sessions; starting and controlling new
remote sessions remains future work.

```bash
ct viewer                 # choose and open a conversation
ct viewer launcher        # open the keyboard-first browser launcher
codex-viewer status       # check the background server
codex-viewer stop         # stop it
```

`ct viewer launcher` is suitable for a desktop keyboard shortcut. It starts
the background viewer when necessary, focuses the browser launcher's search
field, and preselects the newest conversation. Type to filter, use Up/Down to
move, and press Enter to open the selected conversation in the same tab. Pass
`--query TEXT` to start with a filter already applied.

The viewer resolves the desktop's default browser and asks known browsers to
open a new window. It falls back to the desktop URL opener when the browser
cannot be resolved. Diagnose startup, port, browser, and optional LaTeX support
with `ct viewer doctor`. To override browser selection, use
`ct viewer open --browser firefox` or set
`viewer.browser` in the user configuration:

```bash
ct config set viewer.browser firefox
ct config unset viewer.browser       # restore the desktop default
```

`CODEX_TOOLS_BROWSER` remains available when the setting is `default`.
Background-server failures report the private log path
under `~/.local/state/codex-tools/viewer/` instead of claiming startup
succeeded.

**Markdown is the portable default.** Press `T` inside a conversation to switch
between Markdown and the LaTeX view. In the LaTeX view, local attachments and
external links from each assistant answer are collected in a footer below it.
Relative Markdown attachment paths are resolved from the conversation's
recorded working directory and use the same validated local opener.
Hold `Shift` briefly to reveal `[1]`–`[9]` hints for footer links currently in
the viewport, then press the corresponding digit while still holding `Shift`
to open one in a new tab. The left and right arrows move toward the previous or
next message boundary, but never farther than one viewport per press.

![Codex Viewer showing the symmetry of Riemann zeta zeros](docs/assets/codex-viewer-riemann-symmetry.png)

The screenshot above was generated by the real viewer from the included
[Riemann zeta symmetry example](docs/examples/riemann-symmetry.jsonl).

### Optional LaTeX view

For high-quality equations, tables, prose, and highlighted code, install the
optional typesetting dependencies:

```bash
sudo apt install texlive-xetex texlive-latex-extra fonts-texgyre fonts-paratype fonts-dejavu-core
ct viewer doctor
ct viewer --set-default-latex
```

**Nothing breaks without LaTeX.** The viewer continues to use its Markdown
renderer when the optional toolchain is unavailable.

## Profile manager

Profiles give each account its own `CODEX_HOME`, login, and configuration.
Conversations are shared with the normal `~/.codex` profile by default.

```bash
codex-manager new work
codex-manager run work
```

Use a private conversation store when needed:

```bash
codex-manager new private --isolated
```

### Wrapper commands

Install a convenient command for a profile:

```bash
codex-manager install work
codex-work
```

**The default wrapper is `codex-<profile>`.** Choose another name with:

```bash
codex-manager install work --wrapper cw
```

Each profile has at most one registered wrapper. Rename and uninstall stay
coordinated with it:

```bash
codex-manager rename work company
codex-manager uninstall company
```

A default wrapper follows the renamed profile (`codex-work` becomes
`codex-company`). A custom wrapper such as `cw` keeps its name.

### Encrypted login transfer

Export file-backed credentials and profile configuration:

```bash
codex-manager export work
codex-manager import work.codex-profile
```

**The complete bundle is encrypted with your passphrase.** Credentials,
configuration, and—when selected—conversations are protected together.

```bash
codex-manager export work --include-conversations
codex-manager import work.codex-profile \
  --name laptop-work --isolated --install
```

Import uses the embedded profile name unless `--name` is supplied. Without
`--isolated`, an auth-only import shares the destination machine's default
conversations. A bundle that includes conversations restores them privately.
Export currently supports file-backed `auth.json` credentials, not credentials
stored only in an OS keyring.

Useful profile commands:

```bash
codex-manager list
codex-manager doctor work
codex-manager path work
codex-manager remove work
```

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

## Structured tasks

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

## Configuration and diagnostics

```bash
ct config show
ct config edit
ct config path
ct config validate
ct diagnose
```

Configuration lives in `$XDG_CONFIG_HOME/codex-tools/config.toml`, or
`~/.config/codex-tools/config.toml` when `XDG_CONFIG_HOME` is unset. The first
`config edit` creates a commented template and opens `$VISUAL` or `$EDITOR`.

## Privacy and safety

- **Conversation data stays local.** Search and viewer operations are read-only.
- **The viewer binds to `127.0.0.1` by default** and has no remote authentication.
- **Profile exports contain account credentials.** Keep the encrypted bundle and
  its passphrase private, even though the file is created with owner-only
  permissions.
- **Destructive cleanup asks first.** Profile removal uses a manager trash area
  unless explicit purge behavior is requested.

See [SECURITY.md](SECURITY.md) for security notes.

## Development

```bash
git clone https://github.com/hruskamiro/codex-tools.git
cd codex-tools
pipx install .
python3 -m unittest discover -s tests
```

The source-checkout wrappers also work directly:

```bash
./codex-tools --help
./codex-manager --help
./codex-viewer --help
```

## License

[MIT](LICENSE)
