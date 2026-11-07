# Agents and the terminal console

[Back to README](../README.md)

**Keep agents running on a server and work through an unreliable connection.**
The Agent Console keeps a separate draft for each agent, caches conversations,
and saves outgoing messages before delivery. See [relay and SSH setup](relay.md).

```bash
ct agents
ct agents show SESSION
ct agents send SESSION "Check the tests and report back."
ct agents stop SESSION
ct agents ui
```

`ct agents` lists sessions loaded by the managed Codex daemon (Working or Ready).
Use the [viewer](viewer.md) or [search](search-and-summaries.md) for archived history.

**Enter sends; Tab queues; Ctrl-J adds a newline.** Enter can wake the oldest
waiting input when an agent is idle. Tab only queues. Neither cancels active work.
Drafts support wrapping, cursor editing, multiline paste, and highlighted code.

**Delivery status survives reconnects and restarts.** A message can be waiting,
retrying, queued in Codex, delivered into the transcript, or failed. Queued does
not mean started; the separate working indicator follows Codex's reported turn.
Up to three tool-activity breadcrumbs summarize work without syncing tool output.
Assistant timestamps use the console computer's local timezone.

## Navigation and editing

| Key | Action |
|---|---|
| Tab on an empty draft / Shift-Tab | Switch panes / switch even with a draft |
| Shift-Left | Select the agent list |
| Right / Shift-Right in the list | Enter the selected composer |
| Alt-Up/Down | Select previous/next agent, wrapping at the ends |
| Alt-1…9 | Select a numbered agent |
| Ctrl-P/N | Move between agents, or draft lines in the composer |
| Ctrl-Left/Right | Move by word |
| Page Up/Down / mouse wheel | Scroll the conversation |
| Ctrl-R / Ctrl-S | Refresh selected conversation / all configured sources |
| Ctrl-V / Ctrl-T | Open browser / typeset viewer |
| Ctrl-F | Search |
| Ctrl-X / Alt-M | Maximize or restore the conversation |
| Ctrl-C | Stop active work; quit if idle with no waiting messages |
| Esc / Ctrl-G | Stop only |
| Ctrl-D | Quit regardless of agent activity |

On macOS Terminal with Cmd/Option swapped, Command-Up/Down also selects agents.
`--no-local` excludes local sessions from the list and Ctrl-S refresh.

## Agent visibility and order

**Ctrl-B opens one flat agent list.** Up/Down selects; Space/Enter shows or
hides; Alt-Up/Down reorders; `a` shows all; Esc/Ctrl-B returns. In the console,
Alt-Shift-Up/Down also reorders across hosts. Activity does not shuffle agents.

Visibility and order persist in `~/.config/codex-tools/agent-selection.json`.
New agents appear by default; missing agents retain preferences for their return.
Hiding an agent changes neither its work nor relay access. Legacy selections
migrate on first use, preserving old group/order files as backups; group flags
have been removed.

**Settings has Sync, Agents, and Deliveries tabs.** Left/Right switches tabs,
Up/Down selects, Enter applies; Left from Sync returns to the sidebar. Agents
opens the same chooser. Deliveries clears failed messages/receipts for the
selected conversation or scoped server, preserving pending work and history.

## Sync modes

**Adaptive is the default.** Choose Settings → Sync or override one launch:

```bash
ct agents ui --sync economy
ct agents ui --no-local --peer desktop
```

| Mode | Incoming updates |
|---|---|
| `manual` | Refresh on request |
| `economy` | Check the selected conversation roughly every ten seconds |
| `adaptive` | Incremental long polling, with backoff on connection failures |
| `live` | Favor the shortest update delay |

Only the selected conversation is followed; inactive conversations and attachments
are not downloaded automatically. The saved choice is `agents.sync_mode`.
Outgoing delivery runs in the background even in Manual; pending messages retry
on sync/reconnect. A steady dot shows healthy (green), cached/stale (yellow), or
failed (red). Manual refresh results remain visible beside local completion times.

A `ct-connect console PROFILE` session is scoped to that peer and disconnects
when its last console closes, unless kept running with `ct-connect start`.
Pending deliveries require confirmation before disconnecting. Remote agents,
cached history, and queued messages remain intact.

## Relay traffic

**Settings shows approximate payload bytes** for Console/sync and Viewer/files,
reset for each launch. Consoles sharing a route share its traffic; scoped consoles
count only that route. Cached local reads are not new transfers. SSH/HTTP overhead,
retransmissions, failed requests, bootstrap traffic, and external web assets are
excluded. Restart supervisors/viewers after upgrading to include their traffic.
