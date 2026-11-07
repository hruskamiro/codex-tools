# Relay and SSH connections

[Back to README](../README.md)

**Run agents on a server; read and message them from a train or a slow connection.**
The relay caches conversations and saves outgoing prompts locally before sending.
SSH reconnects automatically; agents keep running when the client disconnects.

## Connect over SSH

**Keep the relay bound to localhost.** On the server:

```bash
ct relay init --name desktop
ct relay token  # transfer this key privately
ct relay serve
```

Verify the client's SSH alias first: `ssh desktop true`. Then pair and connect:

```bash
ct relay init --name laptop
ct relay pair desktop http://127.0.0.1:18780
ct relay connect desktop
```

Pairing prompts privately for the server's relay key. `connect` opens the
reconnecting SSH tunnel and Agent Console; the forwarded port need not be open
at pairing time. Keep it running; Ctrl-C stops the client connection, not the server.

Use `--ssh user@host` for a different SSH destination, `--remote-port PORT` for
another relay port, `--ui viewer` for the browser, or `--ui none` for transport
and delivery only. Checkout equivalents: `scripts/relay-ssh-serve` and
`scripts/relay-ssh-connect desktop`. SSH keys and jump routes stay in `~/.ssh/config`.
For travel, use an alias reachable through your VPN or jump host.

## macOS connection launcher

The checkout's `scripts/ct-connect-macos` provides an on-demand launchd controller.
Run it with the checkout's virtualenv Python, or a user-local `ct-connect` shim:

```bash
ct-connect console desktop --ssh user@desktop
ct-connect                         # console for the default profile
ct-connect status
ct-connect start desktop           # keep this connection in the background
ct-connect viewer desktop          # connect and open this peer's viewer
ct-connect stop desktop
```

**Console connections last until their final console closes.** They are scoped
to one peer and shared safely across its consoles. `start` explicitly keeps the
connection running; `viewer` also uses background transport. `stop` revokes that
choice. Closing a client never stops remote agents or the relay.

A second server needs its own forwarding port and named connection:

```bash
ct relay pair workstation http://127.0.0.1:18781
ct-connect start workstation --ssh workstation
```

Connections operate independently. Settings live in `ssh-launcher.json` under
`~/.config/codex-tools/`; per-service definitions live in `ssh-services/`.
Changing `--ssh`, `--peer`, or `--remote-port` restarts only that connection.
Legacy single-server settings migrate with a `ssh-launcher.v1.json` backup.
Services stay outside `~/Library/LaunchAgents`: run `ct-connect` again after login.

### Start the remote relay automatically over SSH

**Auto-start requires permission at both ends.** First, on the destination:

```bash
ct relay configure --allow-auto-start
```

Then opt in on the client, using the server's absolute CLI path:

```bash
ct relay connect desktop --auto-start --remote-bin /absolute/path/to/ct
# Or on macOS:
ct-connect start desktop --auto-start --remote-bin /absolute/path/to/ct
```

Inspect permission with `ct relay configure`; revoke it with `--no-auto-start`.
Use `ct-connect start desktop --no-auto-start` to revoke the client choice.
New configurations and QR pairing default to off unless explicitly opted in.

`ct relay ensure` reuses a healthy relay. It starts a missing one only with
permission, binding localhost and detaching from SSH. It neither takes over an
occupied port nor kills an existing process. Startup is locked per port; logs
are in `~/.local/state/codex-tools/relay-serve/PORT/`.

The server needs an initialized identity and current Codex Tools. Auto-start
installs nothing, rotates no keys, and changes no firewall rules. It is not a
boot/login service. Revocation prevents future starts but leaves a running relay alone.

## Delivery and reconnects

**Cached reading and composing remain available offline.** The supervisor uses
keepalives and health probes, reconnects with jittered delays up to 30 seconds,
then syncs and retries pending prompts. Sleep/wake is handled the same way.
Selected-conversation sync transfers appended JSONL records; truncation or
replacement requests a fresh snapshot. Long polling uses a shared file watcher.
See [console sync modes](agent-console.md#sync-modes) and [viewer sync](viewer-sync.md).

```bash
ct relay peers
ct relay sync desktop
ct relay outbox --json
ct relay cache refresh
ct relay cache status
```

Message IDs make normal retries idempotent. **Queued is not started:** busy
agents retain prompts in Codex's native queue. `ct agents stop SESSION` or
Ctrl-G stops only the active turn; remote stop requests are never queued for
later replay. Ctrl-R, Ctrl-S, and `relay sync` retry pending delivery.

If sending fails, check the server's Codex executable: PATH, common user install
locations, then `$CODEX_HOME/packages/app-server-daemon/current/bin/codex` are
searched. Override with `ct relay serve --codex-bin /absolute/path/to/codex`.
Missing executables are retryable errors; old messages already marked failed
by a previous 410 response are not automatically resent.

Logs live in `~/.local/state/codex-tools/relay-ssh/`. Check these and the remote
relay after connection failures. Requests can time out; interrupted PDF downloads
restart rather than resume byte by byte. Cached downloads remain available.
Conflicting local ports are reported, not taken over. Use `economy` for less traffic.

## Direct HTTPS connection

SSH is optional. To expose a TLS-protected relay and sync from another machine:

```bash
# Server, after ct relay init:
ct relay serve --host 0.0.0.0 --tls-cert relay.crt --tls-key relay.key
# Client, after ct relay init:
ct relay pair desktop https://desktop.example:8780 --ca-file relay.crt
ct relay sync desktop
ct agents ui
```

**Plain HTTP is allowed only on loopback by default.** For an already protected
VPN/tunnel address, explicitly enable `--allow-insecure-http` on both ends.
The relay does not create a VPN. Update/restart running relays after upgrades;
remote delivery targets already-running Codex sessions.
