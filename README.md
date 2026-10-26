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

**Requirements:** Linux or macOS and Python 3.10 or newer. The Agent Console,
relay client, and Markdown viewer also run on Intel macOS Monterey; they do not
require Homebrew or LaTeX. The optional typeset viewer needs XeLaTeX locally
or on its relay peer.
Model-backed summaries and
structured tasks also require an installed Codex CLI. The default `exec`
backend uses its Codex login; the optional app-server backend uses a separate
ChatGPT-plan connection.

Codex Tools is not yet published on PyPI. Install it directly from GitHub:

```bash
pipx install git+https://github.com/hruskamiro/codex-tools.git
```

You now have `codex-tools`, `codex-viewer`, and `codex-manager`.

On older macOS releases, use prebuilt wheels for native Python dependencies
to avoid compiling them and downloading build toolchains:

```bash
pipx install --pip-args='--only-binary=cryptography,cffi,rpds-py' git+https://github.com/hruskamiro/codex-tools.git
```

Monterey's bundled Python 3.9 is too old; select an installed Python 3.10 or
newer with `pipx install --python /path/to/python3` when needed. The default browser opens through
macOS's `open` command. Mouse-wheel-down support depends on the curses library
provided by Python; Page Down remains available when that support is missing.

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
ct agents stop SESSION
ct agents ui
```

`ct agents` reads the managed Codex daemon's loaded-thread list, so it shows
only sessions that Codex currently reports as Working or Ready—not archived
transcript history. It uses `codex queue` for message delivery. `ct agents ui`
refreshes the local relay cache and opens the terminal **Agent Console**,
styled after the Codex CLI. You can switch among cached local or remote agents,
read their recent messages, search, scroll, and send or queue a message without
leaving the terminal. The conversation pane starts with an always-available
composer: type immediately, press Enter to send, Tab to queue the draft, and
use Ctrl-J for a newline. Queued drafts appear in the conversation with their
delivery status; remote drafts remain safely queued if the connection is down.
Enter also starts the oldest waiting input when the agent is idle, including
after an interruption. Tab only queues, without explicitly restarting stopped
work. A busy agent is never interrupted by either key.
It expands as text wraps, supports cursor editing and multiline paste, and
keeps a separate unsent draft when you move between agents. Markdown fenced
code blocks use Pygments syntax highlighting, preserve indentation, and render
on a restrained terminal surface without showing the literal fences.

Sent messages appear in the conversation immediately, including in manual
sync mode. A muted line beneath the message reports whether the
message is waiting to sync, awaiting a delivery retry, accepted into Codex's
queue ("Queued in Codex"), or failed. Queued does not mean the agent has started that message;
the thread's separate working indicator is not attributed to a queued draft.
Delivery receipts are stored in the private local relay cache, including for
local sends, so statuses survive refreshes and console restarts. When sync
finds the corresponding real transcript record, the status stays attached to
that message, marked “Delivered · in conversation,” instead of disappearing
or creating a duplicate bubble. This is delivery/observation status, not a
claim that the assistant's turn completed. Existing messages sent before
receipt tracking was enabled are not retroactively assigned a status.

Settings → Deliveries clears failed deliveries for the last selected conversation
or for all conversations. In a `ct-connect console PROFILE` console, the latter
action applies only to that server. Clearing removes failed local messages and
their delivery receipts; pending deliveries and conversation history are retained.
Use Left/Right to switch settings tabs, Up/Down to choose an action, and Enter to apply it.

Beside the model and reasoning effort, separated by a vertical divider, the
console shows the sync mode and a steady health dot: green for a recent
successful sync, yellow for manual/cached, stale, or not-yet-checked state,
and red after a failed sync until a newer success. Routine checks do not
change the label or animate the dot. The last-success time is still stored
locally, including successful incremental polls with no new messages.
Manual refresh progress and its latest result appear directly below the sync
metadata, with a local completion time. Failures use the sync error color.
Conversation refresh results stay with that agent; refresh-all results apply
to the console. Routine automatic polling does not replace this feedback.

The conversation shows up to three muted tool-activity breadcrumbs, such as
“Running a command” or “Editing files,” alongside the working indicator.
Markdown `**bold**` is displayed with the terminal's bold attribute, without
the asterisks, including across wrapped lines and table cells. Inline code and
fenced code keep literal asterisks. No additional font installation is required.
The latest assistant message has a muted, indented `HH:MM:SS` beneath it, without a bullet,
separated by a blank line, using its recorded timestamp in the console machine's local timezone.
These compact summaries sync through the relay; tool arguments and output
are not included. In manual sync mode they update only when you refresh.
The composer wraps at word boundaries (splitting only overlong words/URLs),
preserves your actual draft text, and leaves a cell of background padding on
the right. Explicit newlines and cursor navigation follow the visual layout.

Press Tab with an empty draft to switch between the agent list and composer;
Shift-Tab switches panes even with a draft. Alt-Up/Down selects the previous/next agent without
reordering, wrapping from the first to the last and vice versa (also Command-Up/Down
in macOS Terminal when Cmd/Option are swapped).
Shift-Left selects the agent list from anywhere in a draft; use Up/Down and Right or Shift-Right to enter
the selected agent's composer. Ctrl-P/Ctrl-N move between agents in the agent
list, or between draft lines in the composer. Ctrl-Left/Ctrl-Right move by word. Page Up/Down or
the mouse wheel scrolls the conversation. Ctrl-R refreshes the selected agent,
Ctrl-S refreshes all local sessions and configured peers, Ctrl-V opens the
browser viewer, Ctrl-T opens the typeset viewer, Ctrl-F searches, and Ctrl-X
maximizes or restores the conversation; Alt-M does the same without consuming
a typing key. Ctrl-C stops the selected agent's active turn without discarding
its queued prompts; when the selected agent is idle and no messages are waiting,
Ctrl-C quits like Ctrl-D. Escape and Ctrl-G remain stop-only shortcuts.
Press Ctrl-D to quit the console regardless of agent activity.
Remote submissions are saved locally and shown immediately; delivery runs in a
background worker, even in manual sync mode. The message's receipt updates when
the peer acknowledges it, without blocking typing or agent selection. If the
connection fails, the durable message remains queued for the usual sync retries.
When opened through `ct-connect console`, the selected server disconnects when
its last console closes, unless explicitly kept running with `ct-connect start`.
Pending deliveries require confirmation before disconnecting; cached conversations
and queued messages remain saved for the next connection. Remote agents keep
running independently. Agents keep a stable creation-time
order instead of jumping after new
activity; the first nine are numbered and Alt-1 through Alt-9 switches to them
directly. Alt-Up and Alt-Down select the previous or next agent, while
Alt-Shift-Up and Alt-Shift-Down reorder it in the single agent list, across hosts;
the custom order persists across refreshes and console restarts. With
`--no-local`, Ctrl-S skips the local refresh.

Press **Ctrl-B** to choose which available agents appear in the console:

- **Up/Down** selects an agent; **Space/Enter** shows or hides it.
- **Alt-Up/Down** reorders the selected agent, including across hosts.
- **a** shows all currently available agents.
- **Esc/Ctrl-B** returns to the console; **Ctrl-D** quits.

The console follows this one flat list. No named groups, workdir grouping,
membership rules, or group filters are involved. New agents appear by default;
unchecked agents can always be restored from the same chooser. Hiding an agent
does not stop it, delete its conversation, or change relay access or syncing.

Visibility and order persist per client in
`~/.config/codex-tools/agent-selection.json`, using stable host/session IDs.
Missing agents are not shown, but retain their preferences if they return.
The first run copies explicit Hidden choices and order from the previous group
setup, but discards group filters. The original `agent-groups.json` and
`agent-order.json` files are left untouched as backups. Group commands and
group-filter CLI flags were removed.

**Settings** has separate **Sync**, **Agents**, and **Deliveries** tabs. With the Settings pane
focused, use **Left/Right** to select a tab and **Up/Down** to choose an option;
**Enter** applies it. **Tab** still switches panes, and **Shift-Left** selects
the sidebar. **Left** from the first (**Sync**) tab also returns to the sidebar.
**Agents** opens the same simple chooser. Preferences apply on
future console starts, including `ct-connect console PROFILE`.

Settings also shows approximate downloaded/uploaded relay payload bytes
since opening this console, with separate **Console/sync** and **Viewer/files**
totals and a combined total. Viewer traffic includes remote conversation refreshes,
typeset PDFs, and attachments. Serving an already cached file locally does not
count as another transfer. A profiled console counts only its selected route;
an unscoped console counts all routes. Background SSH-supervisor sync and message
delivery are included once that supervisor has been restarted with this version.
Concurrent consoles using the same route share its traffic. This is not an SSH
wire counter: HTTP/SSH headers, encryption, retransmissions, failed requests,
and bootstrap traffic before the console opens are excluded. Locally generated
PDFs and external websites/assets fetched directly by the browser are not counted.
Restart the viewer and reopen the console after upgrading to enable both categories.
Totals reset for each console launch; no message contents or credentials are logged.

The first item in the agent pane opens **Settings**. The **Sync** tab's mode
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
private relay cache. `/t/relay/HOST/AGENT` can typeset locally or request PDFs
from the authenticated relay peer. Attachments referenced by an assistant record are fetched from the
authenticated peer only when opened, validated by record and link index, and
kept in the private relay attachment cache for later offline viewing. Arbitrary
remote paths are never accepted from the browser.

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

For intermittent connections, keep the relay on localhost and connect through
SSH. On the serving machine, leave this running in a terminal or your existing
service supervisor:

```bash
scripts/relay-ssh-serve  # initializes an identity if needed; binds 127.0.0.1:8780
# Equivalent after initialization: ct relay serve
```

Configure a normal SSH host alias such as `desktop` in `~/.ssh/config` (including
any key or jump host). Connect once with `ssh desktop true` to verify its host
key and authentication. On the client, pair once with the forwarded address:

```bash
ct relay init --name laptop                         # first installation only
ct relay pair desktop http://127.0.0.1:18780          # enter the serving machine's relay key
ct relay connect desktop                           # opens the Agent Console immediately
```

The equivalent checkout script is `scripts/relay-ssh-connect desktop`.
Use `--ssh user@host` if the SSH destination differs from the peer name,
`--remote-port PORT` if its relay uses another port, or `--ui viewer` to open
the latest cached peer conversation in the local browser. `--ui none` maintains
only the tunnel and queued-message delivery. Keep this command running;
Ctrl-C stops its connection supervisor and SSH child. The serving relay is
independent and continues running. No additional SSH packages are needed.

On macOS, the checkout also includes `scripts/ct-connect-macos`, an on-demand
per-user launchd controller. Run it with the checkout's virtualenv Python, or
through a user-local `ct-connect` shim pointing to that interpreter and script:

```bash
ct-connect console desktop --ssh user@desktop --auto-start --remote-bin /home/user/.local/bin/ct
ct-connect                         # open the default profile's scoped console
ct-connect status                  # list all configured connections
ct-connect console desktop         # connect only to this server while the console is open
ct-connect start desktop           # explicitly keep its connection in the background
ct-connect viewer desktop          # ensure connected, then open this peer's viewer
ct-connect stop desktop
```

Console connections are scoped to the selected peer: the sidebar, agent chooser,
automatic sync, and Ctrl-S refresh do not include other cached servers. The
connection retries while the console is open and stops after the last console
for that profile closes. Two consoles can share it without disconnecting each
other. An explicit `start` opts that profile into background operation, including
delivery retries after closing the console; `stop` disables that background
choice. `viewer` also uses a background connection because browser-tab lifetime
is independent of the CLI. Closing a console never stops the remote relay or agents.
Standalone consoles can be restricted with `ct agents ui --peer NAME`.

For a second server, pair a second peer using a different local forwarding
port, then start its named connection. SSH login keys and jump-host routes
remain in `~/.ssh/config`; relay pairing keys remain in `relay.json`.

```bash
ct relay pair workstation http://127.0.0.1:18781
ct-connect start workstation --ssh your-workstation-ssh-alias
ct-connect status workstation
```

Both services can run together. Stopping or reconfiguring one does not restart
the other. The saved launcher configuration contains a `connections` table,
with each entry storing its `ssh`, `peer`, and `remote_port`. It also records
the default connection used when no name is supplied. Existing single-server
settings migrate automatically on the next start, with a private backup in
`ssh-launcher.v1.json`. Each named service has separate status/log files and
a private definition under `~/.config/codex-tools/ssh-services/`.

Closing the console or terminal does not stop this background service. It
reconnects independently; launchd also restarts it if the helper crashes.
The service definition is kept outside `~/Library/LaunchAgents`, so it does
not start automatically at login. Run `ct-connect` again after logging in.
Saved SSH destination/peer settings are in
`~/.config/codex-tools/ssh-launcher.json`; changing them with `--ssh`, `--peer`,
or `--remote-port` restarts only the selected service with the new settings. For travel, use
an SSH alias reachable through your chosen VPN or jump host instead of a
home-network-only IP address.

### Start the remote relay automatically over SSH

Remote auto-start is opt-in per connection. Save it on the Mac with:

```bash
ct-connect start desktop --auto-start --remote-bin /home/hruskam/.local/bin/ct
ct-connect status desktop
ct-connect start desktop --no-auto-start  # turn it off again
```

The launcher saves `auto_start` and `remote_bin` alongside the profile's SSH
settings. Without the Mac launcher, the equivalent one-run command is:

```bash
ct relay connect desktop --ssh your-ssh-alias --auto-start --remote-bin /absolute/path/to/ct
```

Before each tunnel connection/reconnection, SSH invokes `ct relay ensure` on
the serving machine. A healthy relay is reused. Only a refused localhost
connection is treated as absent; occupied, unresponsive, or incompatible
endpoints are reported and left untouched. Startup is locked per port, and
a recorded live startup PID prevents duplicate launches during retries.

New relays bind only to `127.0.0.1`, detach from SSH, and stay running after
the client disconnects. No existing process is killed or restarted. This is
on-demand startup, not an OS boot/login service; the next connect can start
the relay again after a reboot or crash. Logs and startup metadata are private
under `~/.local/state/codex-tools/relay-serve/PORT/` on the serving machine.

The serving machine needs an updated codex-tools installation and an already
initialized relay identity (`ct relay init`). Auto-start never generates or
rotates pairing keys, installs software, or changes network/firewall settings.
Prefer an absolute executable path because noninteractive SSH may have a
different PATH. SSH aliases and `ProxyJump` continue to work normally. Cached
viewing and the console remain usable while SSH or remote startup is unavailable.

For message delivery, the relay resolves `codex` to an absolute executable path.
It checks PATH first, then common user install directories (`~/.local/bin`,
`~/.volta/bin`, `~/bin`, `~/.cargo/bin`, and `~/.npm-global/bin`), then the managed
native CLI at `$CODEX_HOME/packages/app-server-daemon/current/bin/codex`
(`~/.codex` by default). The native fallback does not require NVM/Node on PATH. Unusual installs
can use `ct relay serve --codex-bin /absolute/path/to/codex`. A missing executable
is a retryable server error, not “410 Gone”; delivery errors include the server's
explanation. Existing relays need restarting to load this change. Messages already
marked failed by an older 410 response are not automatically resent.

The tunnel binds only `127.0.0.1` on the client and forwards to `127.0.0.1` on
the server; HTTP travels inside encrypted SSH. The connection supervisor:

- detects broken SSH connections with keepalives and small relay health probes;
- retries with jittered delays up to 30 seconds, including after sleep/wake;
- synchronizes a snapshot after reconnection and retries persisted pending messages;
- leaves cached viewing and the local composer usable while offline.

Between reconnects it does not repeatedly fetch full snapshots. The Agent
Console uses its configured incremental sync mode; the standalone viewer uses
its existing refresh controls. If the first connection arrives after the
console opened, newly cached agents appear automatically. Existing port
conflicts are reported without taking over other tunnels. Per-connection
status and private SSH logs live below `~/.local/state/codex-tools/relay-ssh/`.

SSH is a reconnectable transport, not seamless roaming: requests in progress
can time out on a poor connection. Completed downloads remain cached; an
interrupted PDF download must be retried, rather than resumed byte by byte.
If reconnecting fails, check the SSH log and confirm the remote relay is still
running. For less conversation traffic, select the Agent Console's `economy`
sync mode.

`ct relay peers` lists configured connections without exposing their keys. Each
sync refreshes the serving machine's currently loaded Codex sessions before it
returns the snapshot, then stores the result in the client's private cache for
offline viewing. Messages written in the Agent Console are committed to the
local outbox before a network attempt. If the peer is reachable, they are sent
immediately to its `codex queue`; otherwise they remain pending. Ctrl-R,
Ctrl-S, and `ct relay sync PEER` retry pending messages. `ct relay outbox`
shows pending, delivered, and permanently failed messages; add `--json` for
machine-readable status.

Prompt delivery and execution control are intentionally separate. Codex's
native per-thread queue is the execution source of truth: a prompt sent to a
busy agent waits for its turn. `ct agents stop SESSION` or Ctrl-G interrupts
only the turn currently running. A remote stop is an immediate authenticated
request and is never saved for later replay, because a delayed stop could
otherwise interrupt the wrong future turn.

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

In either conversation view, **Shift-L** opens the chooser in a new tab;
lowercase **L** still jumps to the latest response. The chooser includes local
and cached relay conversations. From a relay conversation it initially shows
that same peer, with **All sources** to broaden the selection. It preserves
Markdown/typeset mode and reads the local cache without contacting the peer,
so cached conversations remain selectable offline.

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
between Markdown and the LaTeX view. In both views, local or synchronized
remote attachments and external links from each assistant answer are collected
in a footer below it.
Relative Markdown attachment paths are resolved from the conversation's
recorded working directory and use the same validated local opener.
Hold `Shift` briefly to reveal `1`–`9` keycap hints for footer links currently in
the viewport, then press the corresponding digit while still holding `Shift`
to open one in a new tab. The left and right arrows move toward the previous or
next message boundary, but never farther than one viewport per press.

![Codex Viewer showing the symmetry of Riemann zeta zeros](docs/assets/codex-viewer-riemann-symmetry.png)

The screenshot above was generated by the real viewer from the included
[Riemann zeta symmetry example](docs/examples/riemann-symmetry.jsonl).

### Optional LaTeX view

For relay conversations, a thin client can use the serving machine's XeLaTeX
installation. The default `auto` backend renders locally when `xelatex` is
available, and otherwise requests rendered assistant answers from the peer.
Local conversations always use the local renderer.

```bash
ct config set viewer.typeset_backend remote  # always render relay answers on the peer
ct viewer restart
```

The other choices are `local` and `auto`. Use `ct viewer start
--typeset-backend remote` for a command-specific override, or add
`?backend=remote` to a typeset-view URL. Restart an already-running relay after
upgrading it to enable the remote rendering endpoint.

Only the referenced assistant answer and validated rendering options are used;
the relay does not accept arbitrary Markdown, TeX, or filesystem paths for
rendering. The server reuses its content-addressed PDFs, and the client downloads
each requested answer once into a private PDF cache. Cached views work offline;
uncached answers show a rendering/connection error with Markdown fallback.
Attachments and external-link keyboard shortcuts remain available below the PDFs.

For high-quality equations, tables, prose, and highlighted code, install the
optional typesetting dependencies on the rendering machine:

```bash
sudo apt install texlive-xetex texlive-latex-extra fonts-texgyre fonts-paratype fonts-dejavu-core
ct viewer doctor
ct viewer --set-default-latex
```

**Nothing breaks without LaTeX.** The viewer continues to use its Markdown
renderer when the optional toolchain is unavailable.

## Optional voice transcription

`ct voice` provides durable transcription jobs for the mobile console using a
separately installed whisper.cpp `whisper-server`. Voice and agent connections
are independent. Normal installation adds no Whisper libraries or models, and
the agent relay does not start voice workers.

```bash
ct voice doctor                       # find whisper-server and existing Iris/models
ct voice models list
ct voice install large                # explicit model install; large means large-v3
ct voice configure                    # guided model/engine setup and identity
ct voice serve                        # follows the saved engine choice
# In another terminal:
ct voice status
ct voice token                        # key for the mobile voice connection
```

Accepted jobs survive SSH disconnects and service restarts; completed results
can be retrieved later. The phone retains unuploaded audio and returns text to
the original draft. `ct voice status` shows the verified accelerator and recent
processing time; `ct voice benchmark recording.wav --model large --device both`
compares CPU/GPU locally without changing setup. See [voice installation and recovery](docs/voice.md) for
engine discovery, CPU/GPU settings, background service setup and migration.

## Phone pairing

Pair the mobile console with one temporary QR invitation, including the SSH
hosts, ordered jump route and authorization of the phone's own public key:

```bash
ct connect pair-phone PROFILE --address COMPUTER_IP
```

The command requires the optional QR encoder (`pipx inject codex-tools qrcode`).
See [phone pairing](docs/phone-pairing.md) for fingerprint confirmation,
reachability, expiry and recovery.

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
