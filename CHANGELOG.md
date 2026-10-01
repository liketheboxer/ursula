# Changelog

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
