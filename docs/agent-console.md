# Agents and the terminal console

[Back to README](../README.md)

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

## Navigation and editing

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

## Agent visibility and order

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

## Relay traffic

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

## Sync modes

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

For connection setup, see [Relay and SSH](relay.md).
