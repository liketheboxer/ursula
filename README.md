# Ursula

**U**nconventional **R**eplies, **S**chedules & **U**nwarranted **L**eftist **A**phorisms: Anarres's Discord bot, named for Ursula K. Le Guin.

A Python Discord bot (discord.py 2.x) that runs under Exocomp and reports telemetry to The Magical Samurai. Ursula was forked from PlunderBot 1.6.3 and keeps six of its features (birthdays came back in 1.5.0), renamed after Le Guin's books (mostly *The Dispossessed*), plus two new ones:

| Ursula | What it is | From PlunderBot |
|---|---|---|
| **Gatherings** (`/gathering`) | Events with RSVPs, reminders, repeats, a place and a Discord Event | Voyages, without crews |
| **Customs** (`/customs`) | The server's own if-this-then-that rules | Articles |
| **Postings** (`/postings`) | Polished pages: rules, a welcome guide | Notice Board |
| **Syndicates** (`/syndicate`) | Role menus members pick from | Role menus (Colours) |
| **Salas** (`/play`, `/salas`) | Music in voice channels | Jukebox |
| **Birthdays** (`/birthday`) | Members save their own; Ursula marks the day | Birthdays (1.5.0) |
| **The Ansible** (`/pdc ansible`) | Channels mirrored to Matrix rooms, both ways, each to its own room | new |
| **Dispatches** (`/pdc dispatches`) | Reports handed over through The Magical Samurai, posted as a PDF in a private channel | new |
| **The PDC** (`/pdc`) | Settings: Production and Distribution Coordination | Admin |
| **The Analogy** | The manual, in Daisho | Manual |

Inside the code the PlunderBot names stay (a gathering is a `Voyage`, a custom an `Article`, a posting a `Page`, a syndicate a `RoleMenu`), so the Daisho module and the tests could be carried over. What members and the PDC read always uses the Ursula names.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/ursula` | Everyone | Meet Ursula: the version, and an aphorism |
| `/timezone set` | Everyone | Save your time zone (Pacific, ET, Europe/London…) so the times you type are read correctly. Also `show` and `clear` |
| `/gathering call` | Everyone | Call a gathering: title, date (friday, tomorrow, 10/3), time (8pm, or 8pm ET), **place** (a voice channel, an address, a link), an optional **role** to tag (only roles the server lets anyone mention), places (empty for no limit), details, reminders (default 1 day and 1 hour before), repeat and its last date, length (default 2 hours), a picture, when to tag the role, and `lobby`: when its lobby thread opens (the server's default, none, or 15 minutes to a day before) |
| `/gathering edit` | Whoever called it, or mods | Change any of that, including place and role (`remove_role` stops tagging); for a repeating gathering, how it repeats, its last date, and skip or unskip a date |
| `/gathering series` | Everyone | A repeating gathering's coming dates |
| `/gathering cancel` | Whoever called it, or mods | Call it off, or with `whole_series:True` the whole series |
| `/gathering list` | Everyone | What's on the board |
| `/play` | Everyone in voice | Play a song name or a link in your voice channel |
| `/salas queue` | Everyone | What's playing and next. Also `nowplaying`, `skip`, `pause`, `resume`, `stop`, `clear`, `remove`, `move`, `shuffle`, `repeat`, `seek`, `volume`, `lyrics`, `leave` |
| `/syndicate create` | Manage Roles | Role menus: `create`, `add`, `remove`, `move`, `edit`, `post`, `preview`, `import` (copy another bot's reaction-role message), `list`, `delete` |
| `/postings create` | Manage Server | Pages: `create`, `section add/edit/image/remove/move`, `import`, `starter` (a draft *Welcome to Anarres* to edit), `post`, `preview`, `list`, `delete` |
| `/customs new` | Manage Server | Rules: `new`, then `reply`, `react`, `role`, `count`, `repost`, `pin`; also `edit`, `remove`, `limits`, `where`, `on`, `off`, `show`, `list`, `delete` |
| `/pdc settings` | Manage Server | This server's settings, including the Ansible's state |
| `/pdc timezone` | Manage Server | The server's time zone |
| `/pdc gatherings channel` | Manage Server | Where gathering cards go (empty: wherever `/gathering call` is used) |
| `/pdc gatherings lobby` | Manage Server | When each gathering's lobby thread opens (default 1 hour before; Off for none). Each gathering can pick its own |
| `/pdc regions auto` | Manage Server | Match region roles to time zones. Also `set`, `clear`, `list` |
| `/birthday set` | Everyone | Save your birthday: month and day, never the year. Also `mine`, `remove`, and `upcoming` (the next ten, without pinging anyone) |
| `/pdc birthdays channel` | Manage Server | Where birthdays are announced (this switches it on). Also `hour` (in the server's time zone), `role` (worn for the day) and `off` |
| `/pdc dispatches channel` | Manage Server | Where dispatches are posted (make it private; Ursula needs Attach Files there). Also `off` |
| `/pdc salas status` | Manage Server | How music is set up. Also `enable`, `youtube`, `djrole`, `channel`, `settings` |
| `/pdc status set` | Manage Server | The line under Ursula's name, how it's worded (Playing, Listening to...) and her dot. Also `music` (Now Playing: song while Salas plays; on by default) and `show` |
| `/pdc ansible link` | Manage Server | Mirror a channel and a Matrix room, and switch it on. Run it again for each channel you want mirrored; linking a channel that's already mirrored moves it to the new room |
| `/pdc ansible unlink` | Manage Server | Stop mirroring a channel (pick the channel, or type the room if the channel's been deleted). Its webhook is removed; Ursula stays in the Matrix room |
| `/pdc ansible enable` | Manage Server | Switch mirroring on or off, for one channel or (with no channel) all of them. The links are kept |
| `/pdc ansible status` | Manage Server | Every mirrored channel and room, which are on, and anything wrong |

### How Gatherings work

- A card goes up with **Going**, **Maybe** and **Can't make it** buttons, and a matching Discord Event (its location is the place). Whoever called it is Going. With places set, a full gathering has a waitlist, and the first in line is told when a place opens.
- Reminders go to everyone Going or Maybe. When it starts, they're all pinged with the place, and the Discord Event starts. It's over after its length, and the Event ends. If Ursula was offline right through a start, nobody is pinged hours late.
- A role is tagged when the card goes up, or also at each reminder and the start, or never. Only roles the server lets anyone mention can be tagged, unless you may mention any role; never @everyone or an app's role. One tagged post per member every 15 minutes.
- Repeats: every week, every 2 to 12 weeks, monthly on the date, or a weekday like the 2nd Saturday or the last Friday, with an optional last date and skipped dates. The next one is posted when one starts (or is called off).

### How the Ansible works

- Any number of **bridges**, each one Discord channel and one Matrix room (for Anarres: #potent-potables and `!oDGEuyxATmlQSlftFv:magicalsamurai.com`, and whatever else you link). Each channel has its own webhook and can be switched off on its own. A room is only ever in one bridge, so a Matrix message always has exactly one place to go.
- **Discord to Matrix:** Ursula's Matrix account (`@ursula:magicalsamurai.com`) posts **Name**: text. Mentions, custom emoji and timestamps are written out as words. Files up to 8 MB go across as Matrix files; bigger ones are named. Replies point at the right message, edits become Matrix edits, and deleting on Discord redacts it on Matrix.
- **Matrix to Discord:** a webhook in the channel posts under the Matrix member's display name, with every mention switched off (nobody on Matrix can ping @everyone or a role). Pictures and files up to 8 MB are carried. Replies show a link to the message they answer; edits and redactions follow.
- Only new messages are carried: linking (or changing the room) starts from now, and after downtime messages older than 6 hours are left. Linking another channel doesn't interrupt the ones already mirrored.
- A bridge that can't work (Ursula was removed from its room, its channel was deleted) is skipped and shown in `/pdc ansible status`; the others carry on. Ursula ignores her own Matrix messages and her webhook's Discord ones, so nothing loops. Which copy is which is kept for 30 days.
- A Matrix member's message deleted on Discord is redacted on Matrix only if Ursula is a moderator in the room; otherwise it stays there.
- **Encrypted rooms** work: Ursula has her own Matrix device and keeps its keys in `/data/matrix` (keep the data volume across refits; losing it means a new Matrix login and token). Element shows her device as unverified; that's expected, and messages still reach her. She can't read anything sent before her device existed. A message whose key is late waits up to 10 minutes before it's given up on.
- Not carried (yet): reactions, threads, and Discord embeds (the link inside them is carried).
- If Matrix refuses Ursula's token, the Ansible stops until a refit and `/pdc settings` says why. If the homeserver can't be reached, it keeps trying, waiting longer each time (up to 5 minutes).

### How Customs work

The same as PlunderBot's Articles: **when** something happens (words said, a member joining, leaving, getting or losing a role, boosting, a reaction, a schedule), Ursula **does** things (reply or post, react, give or take a role, count, repost, pin) within **limits** (cooldown, chance, required role, channels). Replies can use `{member}`, `{name}`, `{author}`, `{count}`, `{nth}`, `{server}`, `{channel}`, `{role}`, `{cuss}` and `{aphorism}`.

### How Birthdays work

- Members save their own with `/birthday set`: a month and a day. No year is ever stored, so nobody's age is on file. A date can be fixed within the hour; after that it's locked for 30 days, so nobody has a birthday every week. February 29 is marked on February 28 in other years.
- At the set hour (9 AM by default, in the server's time zone) Ursula announces the day's birthdays in the PDC's chosen channel, pinging only the people celebrating, and gives them the birthday role until the next day. If she was offline at that hour, she announces later that day, never twice.
- With no channel set nothing is announced, but dates are still saved. Someone who leaves the server has their date deleted.

### How Dispatches work

- Something outside Discord that has a **dispatch key** (KRANG's morning and evening briefings, sent from Boxer's PC) hands The Magical Samurai a finished report as a PDF. Keys are made on Ursula's **Dispatches** screen there, and can do nothing else.
- Daisho queues it like any other change. Within about 15 seconds Ursula fetches the PDF with her module token, checks it's exactly the file the change names (its SHA-256) and a real PDF under Discord's 10 MB, and posts it to the dispatch channel: the title in bold, a few lines of text, and the PDF attached. If the dispatch names who it's for (a Discord user ID), Ursula tags that one person, as long as they can see the channel; every other mention is switched off, so nothing in a report can ping anyone else.
- No channel set, or no **Attach Files** there: the dispatch fails with the reason, and can be sent again from the Dispatches screen once that's fixed. Ursula keeps nothing; the PDF goes straight from Daisho to Discord.
- Make the channel **private**. Reports carry calendars and to-do lists; `/pdc dispatches channel` warns when the channel is open to everyone.

### How Salas works

The same as PlunderBot's music: SoundCloud, Bandcamp, Twitch, radio, plain audio links, Spotify links (looked up elsewhere), and YouTube when the PDC switches it on (with a throwaway account's cookies). A DJ role can be set; the Salas screen in Daisho steers it too. Links must be on the public internet.

### How the Daisho screens work

Ursula's module in The Magical Samurai shows Settings (with the Ansible), Syndicates, Customs, Postings, Gatherings and Salas. Ursula keeps her own data and sends Daisho a copy; changes made there are applied within about 15 seconds with the same checks as the slash commands. Members can sign in, call gatherings and answer them as themselves. Linking the Ansible from the screens goes through the same steps as `/pdc ansible link`. The Settings screen's one Ansible channel and room are the first bridge; the snapshot's `settings.ansible_bridges` lists them all, and the changes `ansible.link` (`channel_id`, `room`, optional `id` to move one), `ansible.unlink` (`id`) and `ansible.enable` (`on`, optional `id`) manage the rest.

## Setting it up

### 1. Discord application

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application named **Ursula** and give her an avatar.
2. **Bot** tab: turn on **Server Members Intent** and **Message Content Intent** (Customs, imports and the Ansible need it). Leave Presence off.
3. **Bot** tab: **Reset Token** and keep it for Exocomp. Never paste it in chat or commit it.
4. **OAuth2 › URL Generator**: scopes `bot` and `applications.commands`; permissions **View Channels**, **Send Messages**, **Embed Links**, **Attach Files** (dispatches), **Read Message History**, **Add Reactions**, **Manage Roles**, **Manage Events**, **Manage Webhooks** (the Ansible), **Manage Messages** (customs that pin), **Create Public Threads** and **Send Messages in Threads** (lobby threads), **Connect** and **Speak** (Salas). Open the URL and add Ursula to Anarres.
5. **Server Settings › Roles**: drag Ursula's role above any role she should hand out (Syndicates and Customs).

### 2. Ursula's Matrix account

On the Anarres homeserver (Synapse, `anarres.magicalsamurai.com`; server name `magicalsamurai.com`):

1. Create the user `ursula` (not an admin).
2. Sign in as her once and get an **access token** (Element: *Settings › Help & About › Access Token*, then don't sign that session out; or the login API).
3. Invite `@ursula:magicalsamurai.com` to the room. `/pdc ansible link` makes her join. Make her a moderator if Discord deletions should redact Matrix members' messages too.

### 3. GitHub and Exocomp

Repo `liketheboxer/ursula` (start it empty). In Exocomp, **Fabricate unit**:

| Field | Value |
|---|---|
| Unit name / short name | Ursula / `ursula` |
| GitHub repo | `liketheboxer/ursula`, branch `main` |
| How is it built? | Repo has its own Dockerfile |
| Settings | `MATRIX_HOMESERVER=https://anarres.magicalsamurai.com`; `DEV_GUILD_ID=<Anarres's server ID>` for the first run; optional `DEFAULT_TIMEZONE`, `LOG_LEVEL` |
| Secrets | `DISCORD_TOKEN`, `MATRIX_ACCESS_TOKEN`; optional `YOUTUBE_COOKIES`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` |
| Memory limit | 1024 MB (music needs the room) |

The database is `/data/ursula.db`; the container runs as user 10001 (`ursula`).

### 4. First run

```
/pdc timezone America/Los_Angeles
/pdc gatherings channel #gatherings
/pdc ansible link channel:#potent-potables room:!oDGEuyxATmlQSlftFv:magicalsamurai.com
/pdc ansible link channel:#another-channel room:#another-room:magicalsamurai.com   (as many as you like)
/pdc settings
```

## Telemetry

Gauges `voyages_scheduled`, `ansible_up` (1 while the mirror is syncing), `ansible_bridges` (how many channels are being mirrored) and `birthdays_on_file`. Background errors are reported as `voyage-clock`, `articles-clock`, `ansible`, `birthday-announcer` and `daisho` (which includes dispatches).

## Developing

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m pytest -q
cp .env.example .env    # a test bot's token and your test server's ID as DEV_GUILD_ID
python bot.py
```

The Ansible's tests run against a small fake homeserver (`tests/test_ansible.py`); `tests/test_ansible_e2e.py` adds an encrypted room with a second member on a real matrix-nio client.

## Layout

| Path | What |
|---|---|
| `bot.py` | Start file: logging, telemetry, run |
| `ursula/bot.py` | The client, command sync, error replies, health-check file |
| `ursula/db.py` | SQLite in `/data` and its migrations (append only; the early ones are PlunderBot's and stay) |
| `ursula/voice.py` | Every member-facing line in Ursula's voice, the cusses and the aphorisms |
| `ursula/matrix.py` | The Matrix client (matrix-nio for encryption; its own HTTP for media and redactions) |
| `ursula/ansible_logic.py` | Turning a Discord message into a Matrix event and back |
| `ursula/voyage_logic.py` | Gatherings: dates, times, repeats, reminders, the card |
| `ursula/articles_logic.py` | Customs: matching words, schedules, reply templates |
| `ursula/birthday_logic.py` | Birthdays: valid dates, February 29, the change lock, when to announce |
| `ursula/cogs/` | `core`, `admin` (the PDC), `voyages` (Gatherings), `regions`, `colours` (Syndicates), `noticeboard` (Postings), `articles` (Customs), `music` (Salas), `ansible`, `birthdays`, `daisho` |
| `exocomp_telemetry.py` | Exocomp's telemetry helper, vendored |

## House rules

- Member-facing text lives in `voice.py`: a dry, warm Odonian. Allusions to Le Guin are fine; quotes from her books (or anyone's) are not. Admin replies and logs stay plain English.
- Ursula never pings @everyone. Role pings are opt-in roles on gatherings; reminders and starts ping only the people who answered. Nothing from Matrix pings anyone.
- Every check lives in one place and every way in uses it (slash commands, buttons and the Daisho screens): `Voyages.launch`/`plan_edit`/`apply_series`/`role_problem`, `Ansible.link`, `Music.control`/`may_steer`, `Colours.apply_as`, `self_serve_problem` plus `above_their_reach` for roles.
- Anything a member types that Ursula fetches goes through `netguard.public_url` first. Matrix files come only from the configured homeserver.
- Schema changes are new entries at the end of `MIGRATIONS`. Version in `VERSION`, changes in `CHANGELOG.md`, a `vX.Y.Z` tag per release.
