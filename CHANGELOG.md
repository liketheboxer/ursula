# Changelog

## 1.4.0 (2026-10-09): the Ansible mirrors more than one channel

- **Any number of channels.** `/pdc ansible link` adds another bridge each time you run it: one Discord channel, one Matrix room, carried both ways as before. Linking a channel that's already mirrored moves it to the new room. A room is only ever in one bridge (Ursula says which channel has it).
- **`/pdc ansible unlink`** (new) stops mirroring a channel and takes its webhook down. Pick the channel, or type the room if the channel has been deleted. Ursula stays in the Matrix room.
- **`/pdc ansible enable`** takes an optional channel: switch one bridge off and leave the rest running. With no channel it switches them all.
- `/pdc ansible status` and `/pdc settings` list every bridge, which are off, which rooms are encrypted, and any bridge being skipped.
- One sync covers every room. Linking another channel keeps the others' place, so nothing in them is lost; the new room starts from now.
- A bridge that can't work (Ursula was removed from the room, the room's alias is gone, the channel was deleted) is skipped and its reason shown, instead of stalling the whole mirror.
- **Your existing link carries over:** the database migration moves the one channel and room (and its webhook and on/off state) into the first bridge. Nothing to redo after the refit.
- **Daisho:** the Settings screen's Ansible fields keep working, as the first bridge. The settings snapshot adds `ansible_bridges` (every bridge, with its id, channel, room, on/off, encrypted and any problem), and three new changes manage them: `ansible.link`, `ansible.unlink` and `ansible.enable`. The Magical Samurai 1.24.0 (Ursula module 1.3.0) lists every bridge on the Settings screen, with Switch on/off, Unlink and Link a channel.
- New gauge `ansible_bridges`: how many channels are being mirrored.
- The welcome page starter (`/postings starter`) names every mirrored channel.

## 1.3.0 (2026-10-02): the lobby thread

- **A lobby for each gathering.** A while before it starts (an hour, by default), Ursula opens a thread on the gathering's card and tags everyone Going or Maybe in it, with the Going / Maybe / Can't make it buttons, so the maybes can decide and last-minute plans stay out of the main channel. It takes the place of the reminder due then; later reminders and the start ping (with the place) go into the thread. Anyone who answers after it opened is added. When the gathering is over or called off the thread gets a last line and is archived.
- **When:** `/pdc gatherings lobby` sets the server's time (or Off); each gathering can pick its own or none with `lobby` on `/gathering call` and `/gathering edit`, or on Daisho. If the role is tagged at reminders, one line in the channel tags it with a link to the lobby.
- If the thread can't be made (Ursula needs **Create Public Threads** and **Send Messages in Threads** in the gatherings channel), the reminder it replaced goes out in the channel as before; a gathering with no reminders isn't pinged. Moving a gathering out of its lobby window parks the thread until the new time.
- Tests: two reminder tests no longer depend on the time of day they run.

## 1.2.0 (2026-10-01): the status under her name

- **Ursula's status in Discord** (the dot and the line under her name) is a setting: the line (up to 128 characters), how Discord words it (just the text, Playing, Listening to, Watching, Competing in) and the dot (Online, Idle, Do Not Disturb, Invisible). Set it on Daisho's Settings screen (**Bot status**) or with `/pdc status set`; `/pdc status show` says what's showing.
- **Now Playing:** while Salas plays, the line reads `Now Playing: <song>`, and goes back when the music stops. On by default; `/pdc status music` or the screen switches it off.
- Discord limits how often a bot changes its status, so changes are spaced at least 5 seconds apart, and only sent when what shows actually changes.

## 1.1.0 (2026-10-01)

- **The Ansible works in encrypted rooms.** Ursula reads and writes end-to-end encrypted Matrix rooms (matrix-nio with vodozemac), so #potent-potables can stay mirrored to the encrypted Anarres room instead of a second, unencrypted one. Files go across encrypted too.
- Her encryption keys live in `/data/matrix` and must survive refits. If they're lost, she needs a new Matrix login (a new device and token) to read new messages again.
- A message whose room key hasn't arrived yet waits up to 10 minutes (Ursula asks the sender's device for it once) and is carried late rather than dropped. `/pdc settings` shows a count of any she had to give up on.
- Messages sent before Ursula's device joined the room can't be read, as for any new Matrix device.
- Unencrypted rooms work as before. A token without a device stops the Ansible with a clear reason instead of retrying.

## 1.0.0 (2026-10-01)

Ursula's first version, forked from PlunderBot 1.6.3 for Anarres.

- **Gatherings** (`/gathering call`, `edit`, `series`, `cancel`, `list`): PlunderBot's voyages without crews or games. A gathering has a **place** and an optional **role** to tag (only roles the server lets anyone mention, unless the member may mention any). At the start everyone Going or Maybe is pinged with the place; it's over after its length. The Discord Event's location is the place.
- **Customs** (`/customs`): PlunderBot's Articles, renamed. Replies can also use `{aphorism}`.
- **Postings** (`/postings`): the Notice Board without the Game Index or following games. `starter` drafts *Welcome to Anarres*.
- **Syndicates** (`/syndicate`): role menus, without the Gangplank's onboarding.
- **Salas** (`/play`, `/salas`): the jukebox, unchanged inside.
- **The Ansible** (new): one Discord channel and one Matrix room mirrored both ways. Ursula's own Matrix client (no SDK); Matrix messages arrive through a webhook under the sender's name with mentions off; files up to 8 MB both ways; replies, edits and deletions follow. Set up with `/pdc ansible link`, `enable` and `status`, or on the Daisho Settings screen. Secrets `MATRIX_HOMESERVER` and `MATRIX_ACCESS_TOKEN`.
- **The PDC** (`/pdc`): settings, timezone, gatherings channel, regions, Salas, the Ansible.
- Daisho sync: the same snapshot keys as PlunderBot's (without crews and the ledger), the API at `/api/m/ursula/v1`, Ansible settings and status.
- Left behind: Crew Call, birthdays, the Gangplank, the Ship's Log, the Crow's Nest, Parley and the Ship's Ledger. Their old database tables stay (migrations are append-only) but nothing uses them.
- A new voice: an Odonian who'd rather everyone just helped each other, with original cusses and aphorisms.
