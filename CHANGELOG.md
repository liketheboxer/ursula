# Changelog

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
