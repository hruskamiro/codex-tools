# Conversation updates

The Markdown and PDF conversation viewers show two controls in the collapsed
navigation bar: `/` expands navigation and `↻` toggles automatic updates. Press
`S` to toggle the same mode. Typing in an editable field does not trigger it.

Manual is the default. The muted sync icon can show a badge counting messages
not yet displayed. An edit to existing content shows a dot instead. Press `R`
to fetch changes while keeping your reading position, or `Shift+R` to fetch and
move to the start of the last message.

In Auto, the sync icon is highlighted. New or edited messages are fetched and
displayed automatically, and the last message is aligned with the top of the
view. “Latest” includes user messages, rather than only assistant answers. `L`
also moves to the last displayed message. Switch back to Manual before reading
older messages if you do not want new content to move the view.

Background checks are quiet: they never spin the sync icon or typeset PDFs.
The icon spins only during an actual content refresh. Expand the navigation bar
to see the named mode, the last successful check, or an offline/retrying status.
During a refresh, the icons accelerate gently and settle at their current angle
when it finishes. The next refresh continues from that angle. Reduced-motion
preferences disable rotation; mode and refresh status remain available.

Visible tabs normally check every two seconds. Hidden tabs stop checking, and
failed checks back off up to thirty seconds while preserving displayed content.
Manual mode still checks for changes, including contacting the peer for remote
conversations. Auto is saved in that tab's URL and follows a switch between
Markdown and PDF views; it does not change other tabs or global settings.

Enabling Auto or using `Shift+R` in a focused PDF-answer view opens the full
conversation in place so it can follow subsequent messages. Existing PDF caches
are reused when loading new content.
