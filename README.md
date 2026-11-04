# wa

WhatsApp agent tools today log in to your account as a hidden extra device.
`wa` reads the WhatsApp history already saved on your Mac instead: read only, no login, no network.

```
$ wa unread --redact
6 unread in 3 chats

== Contact 1  (1 unread)
 2026-11-03 09:01  Contact 1
    [4 words]

== Group 1  (3 unread)
 2026-11-02 09:06  Contact 2
    [3 words]
 2026-11-02 09:08  Contact 3
    [document] [title]
 2026-11-02 09:10  Contact 4
    [location]

== Contact 2  (2 unread)
 2026-11-02 09:00  Contact 2
    [6 words]
 2026-11-02 09:00  Contact 2
    [image]
```

The same command without `--redact`, on the made-up test data in `tests/`:

```
$ wa unread
6 unread in 3 chats

== Priya 🌸 Nair  (1 unread)
 2026-11-03 09:01  Priya 🌸 Nair
    Landed. Call you later

== Hiking Club  (3 unread)
 2026-11-02 09:06  Maya Chen
    I'll bring snacks
 2026-11-02 09:08  Tomás Ortega
    [document] trailhead map.pdf
 2026-11-02 09:10  Lena Fischer
    [location]

== Maya Chen  (2 unread)
 2026-11-02 09:00  Maya Chen
    Bonjour, je suis à l'école 你好
 2026-11-02 09:00  Maya Chen
    [image]
```

## Why this exists

A popular post this week reviewed "wamcp", a WhatsApp MCP server whose repo
ships no source code and no licence. That is a lot of trust to hand a tool
that reads your private messages.

The well known open source WhatsApp agent tools all work the same way. They
log in as a linked device over the WhatsApp Web protocol, using Baileys or
whatsmeow. WhatsApp's terms discourage that. The README of one of them,
finereli/whatsapp-mcp, says WhatsApp could restrict or close an account that
does it.

`wa` takes a different route. WhatsApp Desktop already keeps your history in a
SQLite file on your Mac. `wa` copies that file to a private temp folder, reads
the copy, and deletes the copy when it is done. It has no server, no linked
device, no QR code and no network code at all.

To send, `wa` builds WhatsApp's own click to chat link
(`https://wa.me/<number>?text=...`). With `--open` it opens that link, which
shows a prefilled draft in WhatsApp. You read it and press Send. `wa` never
sends anything itself.

## How it is different

| | wa | lharries/whatsapp-mcp | openclaw/wacli | finereli/whatsapp-mcp |
|---|---|---|---|---|
| GitHub stars (Nov 2026) | new | 6.8k | 2.9k | 402 |
| Approach | Reads a copy of the WhatsApp Desktop database on your Mac | Linked device over the WhatsApp Web protocol (whatsmeow) | Linked device over the WhatsApp Web protocol (whatsmeow) | Linked device over the WhatsApp Web protocol (Baileys) |
| Network | None | Yes, to WhatsApp's servers | Yes, to WhatsApp's servers | Yes, to WhatsApp's servers |
| QR code or linked device | No | Yes | Yes | Yes |
| Can send | Opens a draft, you press Send | Yes, text and files | Yes | No, read only |
| Account risk named by the project | None known: it never talks to WhatsApp | | | README says WhatsApp could restrict or close the account |
| Licence | MIT | MIT | MIT | MIT |

## Install

You need macOS, Python 3.8 or newer, and WhatsApp Desktop installed, signed in
and synced. No other packages.

```
git clone https://github.com/wilsonwu-ai/whatsapp-local
cd whatsapp-local
python3 wa.py doctor
```

Or install the `wa` command:

```
pip install .
wa doctor
```

`wa doctor` finds the database, prints your WhatsApp version, the file size,
the date of the last message, and whether the database looks the way `wa`
expects. If something is wrong it says what and exits with a non zero code.

## Commands

Every command takes `--json` for scripts and agents, and `--redact` for
screenshots.

| Command | What it does |
|---|---|
| `wa chats [--unread] [--groups\|--people] [--limit N]` | List chats, newest first |
| `wa read <chat> [--last N] [--since DATE]` | Read one chat |
| `wa search "<text>" [--chat X] [--since DATE] [--from me\|them] [--limit N]` | Search message text. Ignores case, works with accents, emoji and any script |
| `wa unread` | Each chat with unread messages, its unread count and those messages. A morning brief in one command |
| `wa links <chat\|--all> [--since DATE]` | Every URL shared, with who sent it and when |
| `wa export <chat> --md out.md` | A readable Markdown transcript |
| `wa stats` | Chats, messages, groups, date range, top 10 chats |
| `wa send <phone> "<text>" [--open]` | Build a wa.me draft link. `--open` opens it in WhatsApp |
| `wa doctor` | Check the setup |

`<chat>` is part of a chat name, a phone number or a JID. If a name matches
more than one chat, `wa` lists them and asks you to pick.

`DATE` is `2026-11-01`, `today`, `yesterday`, or a span such as `7d`, `2w` or
`12h`.

```
$ wa search "école"
1 result for "école"

 2026-11-02 09:00  Maya Chen  in Maya Chen
    Bonjour, je suis à l'école 你好

$ wa send "+1 555 010 2030" "Running 10 min late, sorry!"
https://wa.me/15550102030?text=Running%2010%20min%20late%2C%20sorry%21
Not opened. Add --open to open this draft in WhatsApp. wa never sends.
```

`WA_DB` points `wa` at a different database file. The tests use it.

## Redaction

`--redact` makes output safe to screenshot or share:

* names become `Contact 1`, `Group 2` and so on. The same person keeps the
  same label for the whole run
* phone numbers and IDs are masked
* message text becomes its word count, like `[6 words]`
* file names, locations and media paths are hidden, and links show only the
  domain

The tests check every command with `--redact` against every name, number and
message in the test data.

## Agents

`skill/whatsapp-local/SKILL.md` is an agent skill. It tells an agent when to
use `wa`, gives one example per command, and sets three rules: read only,
always `--redact` when output will be shown to anyone else, and never run
`send --open` until the user has approved the exact text.

## Safety design

* **Never opens the live file.** `wa` copies `ChatStorage.sqlite` and its
  `-wal` and `-shm` files into a new temp folder that only you can read
  (mode 0700), opens the copy with SQLite's read only mode, and deletes the
  folder on exit. A test checks that the original files have the same bytes
  and the same modified time after every command.
* **No network.** `wa` imports no network module. A test runs every command
  with Python's socket functions patched to fail, and they all still work.
* **Never sends.** `send` only builds a link. `--open` hands it to macOS,
  which opens WhatsApp with a draft. A person presses Send.
* **Standard library only.** One Python file, no dependencies, easy to read
  before you run it.
* **Fails loudly on change.** If WhatsApp changes its database, `wa doctor`
  names the missing columns and other commands stop with a clear message.

## Limits

* macOS only. It needs WhatsApp Desktop installed, signed in and synced.
* It only sees what your phone has synced to the desktop app. Old history may
  be missing.
* The database format is undocumented and can change with any WhatsApp
  update. `wa` was tested on WhatsApp 26.44.15.
* Some people appear only as a private ID, not a phone number. When WhatsApp
  has no name for them, `wa` shows `unknown …1234` (the last four characters
  of that ID).
* Labels for photos, voice notes and other media types are best guesses. Some
  newer message types show as `[type 59]` and similar.
* The copy is taken while WhatsApp may be writing. In rare cases the very
  latest message can be missing from one run.
* If you let an AI agent run `wa`, that agent sees your chats. Use `--redact`
  for anything you will share.

## Proof on a real Mac

`evidence/real_run_2026-11-03.json` holds counts and timings from one run on a
real database: 682 chats, 62,653 messages, 314 groups, history from 2018 to
2026. A search for a common word took about 90 ms for the first 50 results
and about 350 ms for all 16,434 matches, including the time to copy the 57 MB
database. The live files were unchanged afterwards. The file has no names,
numbers or message text.

## Tests

```
python3 -m unittest discover -s tests
```

`tests/make_fixture.py` builds a made-up database with the same tables, with
people, groups, unread messages, links, media, starred messages, emoji and
Chinese, Arabic and accented text. `TEST.md` lists what each test proves.

## Credits

* WhatsApp's click to chat links (`wa.me`) do the sending.
* lharries/whatsapp-mcp, openclaw/wacli and finereli/whatsapp-mcp showed how
  much people want their agents to read WhatsApp.
* Not affiliated with WhatsApp or Meta. WhatsApp is a trademark of its owner.

## Licence

MIT. Copyright (c) 2026 Wilson Wu. See `LICENSE`.
