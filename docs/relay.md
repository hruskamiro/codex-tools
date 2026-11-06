# Relay and SSH connections

[Back to README](../README.md)

The relay keeps normalized sessions, synchronization cursors, and an
idempotent outgoing-message queue in a private local SQLite cache. Populate or
inspect the local-machine snapshot and outgoing messages with:

```bash
ct relay cache refresh
ct relay cache status
ct relay outbox
```

## Direct HTTPS connection

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

## Connect over SSH

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

## macOS connection launcher

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

Automatic startup requires both destination permission and an opted-in client
connection. QR pairing leaves it disabled unless the saved client profile
already explicitly enables it.

First allow automatic startup **on the destination computer**:

```bash
ct relay configure --allow-auto-start
```

New and existing relay configurations without this explicit permission default
to disabled. `ct relay ensure` can reuse a running relay, but cannot start a
missing one until this destination setting allows it. `ct relay serve` is an
explicit manual start and remains available.

Then enable it on the client:

```bash
ct-connect start desktop --auto-start --remote-bin /home/example/.local/bin/ct
ct-connect status desktop
ct-connect start desktop --no-auto-start  # turn it off again
```

To inspect or revoke destination permission later, use `ct relay configure`
or `ct relay configure --no-auto-start`. Revocation prevents new automatic
starts; a relay that was explicitly started can continue serving connections.

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

## Delivery and reconnects

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
Console uses its configured incremental sync mode; the browser viewer offers Manual update badges and Auto follow
([viewer sync](viewer-sync.md)). If the first connection arrives after the
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
