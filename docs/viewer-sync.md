# Conversation updates

[Back to viewer](viewer.md) · [Back to README](../README.md)

**Two icons keep the navigation bar compact:** `/` expands it; `↻` or **S**
toggles automatic updates. Shortcuts do not activate while typing in a field.

| Mode | Behavior |
|---|---|
| **Manual** (default) | Muted icon; a badge counts new messages, or a dot marks edits |
| **Auto** | Highlighted icon; fetch changes and align the last message with the top |

**R refreshes in place; Shift-R refreshes and follows the last message.** L jumps
to the last displayed message, including a user message. Switch to Manual when
reading earlier content if incoming messages should not move the view.

**Background checks are quiet.** Only a content refresh spins the icons, easing
in and settling at their current angle. Reduced-motion preferences disable
rotation. Expand the bar for the named mode, last check, and offline/retry status.

Visible tabs check about every two seconds; hidden tabs stop. Failures back off
up to thirty seconds while keeping displayed content. Manual still checks for
changes, including contacting a remote peer, but does not fetch PDFs for them.
Auto is saved in the tab's URL and follows Markdown/PDF switches, not other tabs.

Auto or Shift-R in a focused PDF answer opens the full conversation in place.
Existing PDF caches are reused when content is loaded.
