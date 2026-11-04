# Test plan

All tests run on a synthetic database built by `tests/make_fixture.py`. No test
reads the real WhatsApp database. Run them with:

```
python3 -m unittest discover -s tests
```

## Fixture

`tests/make_fixture.py` builds `ChatStorage.sqlite` plus `-wal` and `-shm`, and
`ContactsV2.sqlite`, with the same tables and the columns `wa` reads. Some rows
live only in the WAL file, so a test fails if `wa` forgets to copy it. It holds:

* 3 people chats (one `@s.whatsapp.net`, one `@lid` that maps to a phone through
  ContactsV2, one with emoji in the name), 2 groups, 1 status session that must
  never show up
* a group with members, where one member has no contact name and resolves from
  the push name table
* unread counts, starred messages, links, an image, a document with a title, a
  location, a group event
* emoji, Chinese, Arabic and accented text
* known timestamps so date conversion can be checked exactly

## What each test proves

| Area | Test |
|---|---|
| Core Data dates | 0 maps to 2001-01-01T00:00:00Z; a known fixture value maps to the expected UTC time; `--since` parses YYYY-MM-DD, today, 7d |
| chats | default list, `--unread`, `--groups`, `--people`, `--limit`; status session hidden; JSON keys |
| read | by name substring, by JID, by phone; `--last`; `--since`; ambiguous name exits 2 and lists choices; unknown chat exits 2; group sender names, including the @lid member |
| search | case-insensitive, accented (`ÉCOLE` finds `école`), Chinese, emoji; `--chat`; `--from me/them`; `--since`; `--limit` |
| unread | totals match the fixture; only incoming messages; per-chat cap with "more" |
| links | every URL in one chat and with `--all`, with sender and date; `www.` links; trailing punctuation stripped |
| export | writes Markdown with day headings; refuses to write inside the WhatsApp folder |
| stats | counts, groups, date range, top chats |
| send | wa.me URL: digits only, text percent-encoded (spaces, `&`, `?`, `#`, accents, emoji, newline); bad numbers rejected; `open` is only called with `--open` (mocked) |
| doctor | ok on the fixture with a fake Info.plist version; exits non-zero with the missing column named when the schema lacks a column; exits non-zero when the file is missing |
| JSON shapes | every command's `--json` parses and has the documented keys |
| Redaction | every command with `--redact` (human and JSON): no fixture name, phone digits, JID user part or message text appears in the output |
| Safety: original untouched | SHA-256 and mtime of the db, `-wal`, `-shm` and contacts db are identical before and after every command |
| Safety: temp copy removed | the snapshot directory is mode 0700 while open and gone after the command |
| Safety: no network | every command runs with `socket.socket`, `socket.create_connection` and `socket.getaddrinfo` patched to raise, and still succeeds |
| Read only | the snapshot connection refuses writes (`attempt to write a readonly database`) |
| CLI | `python3 wa.py --help` and `python3 wa.py stats --json` work as a subprocess |
