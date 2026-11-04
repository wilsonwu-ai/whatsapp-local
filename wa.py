#!/usr/bin/env python3
"""wa: a read-only WhatsApp CLI for macOS.

Reads a snapshot of the local WhatsApp Desktop database. No network, no linked
device, no QR code. To send, it builds a wa.me click-to-chat link that opens a
prefilled draft; you press Send yourself.

Python 3 standard library only.
"""

import argparse
import atexit
import datetime as _dt
import json
import os
import plistlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import urllib.parse

__version__ = "0.1.0"

TESTED_WHATSAPP_VERSION = "26.44.15"
CORE_DATA_EPOCH = 978307200  # 2001-01-01T00:00:00Z as a Unix timestamp

GROUP_CONTAINER = os.path.expanduser(
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared"
)
DEFAULT_DB = os.path.join(GROUP_CONTAINER, "ChatStorage.sqlite")
DEFAULT_APP_PLIST = "/Applications/WhatsApp.app/Contents/Info.plist"

# The columns wa actually reads. doctor checks every one of them.
EXPECTED_SCHEMA = {
    "ZWAMESSAGE": [
        "Z_PK", "ZCHATSESSION", "ZISFROMME", "ZMESSAGETYPE", "ZMESSAGEDATE",
        "ZTEXT", "ZFROMJID", "ZTOJID", "ZPUSHNAME", "ZSTARRED",
        "ZGROUPMEMBER", "ZMEDIAITEM",
    ],
    "ZWACHATSESSION": [
        "Z_PK", "ZCONTACTJID", "ZPARTNERNAME", "ZUNREADCOUNT", "ZARCHIVED",
        "ZHIDDEN", "ZREMOVED", "ZSESSIONTYPE", "ZLASTMESSAGEDATE",
        "ZLASTMESSAGETEXT",
    ],
    "ZWAGROUPMEMBER": [
        "Z_PK", "ZCHATSESSION", "ZMEMBERJID", "ZCONTACTNAME", "ZFIRSTNAME",
    ],
    "ZWAMEDIAITEM": [
        "Z_PK", "ZMESSAGE", "ZMEDIALOCALPATH", "ZTITLE", "ZFILESIZE",
        "ZLATITUDE", "ZLONGITUDE",
    ],
}

# Session types seen in the wild: 0 person, 1 group, 2 broadcast list,
# 3 status, 4 community. Broadcast and status are not conversations.
CHAT_SESSION_TYPES = (0, 1, 4)

# Best-effort labels for messages with no text. The numbers are undocumented.
MESSAGE_TYPE_LABELS = {
    1: "image",
    2: "video",
    3: "audio",
    4: "contact card",
    5: "location",
    6: "group event",
    8: "document",
    11: "gif",
    14: "deleted message",
    15: "sticker",
}

URL_RE = re.compile(r"(?i)\b((?:https?://|www\.)[^\s<>\"')\]]+)")


class WaError(Exception):
    """A user-facing error. exit_code is returned by main()."""

    def __init__(self, message, exit_code=1):
        super().__init__(message)
        self.exit_code = exit_code


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

def coredata_to_unix(value):
    """Core Data stores seconds since 2001-01-01 UTC."""
    if value is None:
        return None
    return float(value) + CORE_DATA_EPOCH


def unix_to_coredata(value):
    return float(value) - CORE_DATA_EPOCH


def coredata_to_datetime(value, tz=None):
    """Return an aware datetime. tz=None means local time."""
    unix = coredata_to_unix(value)
    if unix is None:
        return None
    utc = _dt.datetime.fromtimestamp(unix, tz=_dt.timezone.utc)
    return utc.astimezone(tz) if tz is not None else utc.astimezone()


def iso(value):
    d = coredata_to_datetime(value)
    return d.isoformat(timespec="seconds") if d else None


def short_time(value):
    d = coredata_to_datetime(value)
    return d.strftime("%Y-%m-%d %H:%M") if d else "-"


def parse_since(text, now=None):
    """Parse --since. Accepts YYYY-MM-DD, 'YYYY-MM-DD HH:MM', today,
    yesterday, Nd, Nw, Nh. Returns a Core Data timestamp."""
    if text is None:
        return None
    s = text.strip().lower()
    now = now or _dt.datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    when = None
    if s == "today":
        when = midnight
    elif s == "yesterday":
        when = midnight - _dt.timedelta(days=1)
    else:
        m = re.fullmatch(r"(\d+)\s*([hdw])", s)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            delta = {"h": _dt.timedelta(hours=n), "d": _dt.timedelta(days=n),
                     "w": _dt.timedelta(weeks=n)}[unit]
            when = now - delta
        else:
            for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
                try:
                    naive = _dt.datetime.strptime(text.strip(), fmt)
                    when = naive.astimezone()
                    break
                except ValueError:
                    continue
    if when is None:
        raise WaError(
            "Could not read --since %r. Use YYYY-MM-DD, today, yesterday, "
            "or a span like 7d, 2w, 12h." % text, 2)
    return unix_to_coredata(when.timestamp())


# --------------------------------------------------------------------------
# Snapshot: copy the live files, open the copy read-only, delete on exit
# --------------------------------------------------------------------------

_LAST_SNAPSHOT_DIR = None  # exposed for tests


def db_path():
    return os.environ.get("WA_DB") or DEFAULT_DB


def contacts_path(main_db):
    env = os.environ.get("WA_CONTACTS_DB")
    if env:
        return env
    return os.path.join(os.path.dirname(main_db), "ContactsV2.sqlite")


def _copy_set(src_main, dest_dir):
    """Copy a SQLite file and its -wal/-shm siblings. Reads only."""
    name = os.path.basename(src_main)
    dest_main = os.path.join(dest_dir, name)
    shutil.copyfile(src_main, dest_main)
    for suffix in ("-wal", "-shm"):
        side = src_main + suffix
        if os.path.exists(side):
            shutil.copyfile(side, dest_main + suffix)
    return dest_main


class Snapshot:
    """Context manager. Never opens the live database file with SQLite."""

    def __init__(self, path=None):
        self.src = path or db_path()
        self.dir = None
        self.conn = None
        self.has_contacts = False

    def __enter__(self):
        global _LAST_SNAPSHOT_DIR
        if not os.path.isfile(self.src):
            raise WaError(
                "No WhatsApp database at %s\n"
                "Install WhatsApp Desktop, sign in and let it sync, "
                "or set WA_DB to the file." % self.src, 1)
        self.dir = tempfile.mkdtemp(prefix="wa-snapshot-")
        os.chmod(self.dir, stat.S_IRWXU)  # 0700
        _LAST_SNAPSHOT_DIR = self.dir
        atexit.register(shutil.rmtree, self.dir, True)
        copy = _copy_set(self.src, self.dir)
        uri = "file:%s?mode=ro" % urllib.parse.quote(copy)
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row
        csrc = contacts_path(self.src)
        if os.path.isfile(csrc):
            sub = os.path.join(self.dir, "contacts")
            os.mkdir(sub, 0o700)
            ccopy = _copy_set(csrc, sub)
            try:
                self.conn.execute(
                    "ATTACH DATABASE ? AS k",
                    ("file:%s?mode=ro" % urllib.parse.quote(ccopy),))
                self.conn.execute(
                    "SELECT 1 FROM k.ZWAADDRESSBOOKCONTACT LIMIT 1")
                self.has_contacts = True
            except sqlite3.Error:
                self.has_contacts = False
        return self

    def __exit__(self, *exc):
        if self.conn is not None:
            self.conn.close()
        if self.dir and os.path.isdir(self.dir):
            shutil.rmtree(self.dir, ignore_errors=True)
        return False


def schema_problems(conn):
    missing = []
    for table, cols in EXPECTED_SCHEMA.items():
        have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if not have:
            missing.append(table)
            continue
        for c in cols:
            if c not in have:
                missing.append("%s.%s" % (table, c))
    return missing


def _table_exists(conn, name, schema="main"):
    row = conn.execute(
        "SELECT 1 FROM %s.sqlite_master WHERE type='table' AND name=?" % schema,
        (name,)).fetchone()
    return row is not None


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

class Redactor:
    """Stable within one run: the same person is always 'Contact 3'."""

    def __init__(self, on):
        self.on = on
        self._names = {}
        self._counts = {"Contact": 0, "Group": 0}

    def name(self, key, display, is_group=False):
        if not self.on or display is None:
            return display
        if display in ("me", "system"):
            return display
        key = key or display
        if key not in self._names:
            kind = "Group" if is_group else "Contact"
            self._counts[kind] += 1
            self._names[key] = "%s %d" % (kind, self._counts[kind])
        return self._names[key]

    def digits(self, s):
        if not self.on or s is None:
            return s
        ds = [i for i, ch in enumerate(s) if ch.isdigit()]
        keep = set(ds[-2:]) if len(ds) > 4 else set()
        return "".join(
            ("•" if (ch.isdigit() and i not in keep) else ch)
            for i, ch in enumerate(s))

    def jid(self, jid):
        if not self.on or jid is None:
            return jid
        user, _, server = jid.partition("@")
        return "%s@%s" % (re.sub(r"[0-9A-Za-z\-]", "•", user)[-6:], server)

    def text(self, t):
        if not self.on or t is None:
            return t
        n = len(t.split())
        return "[%d word%s]" % (n, "" if n == 1 else "s")

    def url(self, u):
        if not self.on or u is None:
            return u
        p = urllib.parse.urlsplit(u if "://" in u else "https://" + u)
        return "%s://%s/…" % (p.scheme or "https", p.hostname or "?")

    def title(self, t):
        if not self.on or t is None:
            return t
        return "[title]"


# --------------------------------------------------------------------------
# Names and chats
# --------------------------------------------------------------------------

def jid_user(jid):
    return (jid or "").partition("@")[0]


def is_group_jid(jid):
    return bool(jid) and jid.endswith("@g.us")


def _printable(s):
    return bool(s) and s.strip() != "" and s.isprintable()


class Directory:
    """Resolves JIDs to display names and phone numbers."""

    def __init__(self, snap):
        conn = snap.conn
        self.contact_name = {}   # jid or lid -> name
        self.lid_phone = {}      # lid -> phone jid
        self.push = {}           # jid -> push name
        if _table_exists(conn, "ZWAPROFILEPUSHNAME"):
            for r in conn.execute("SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME"):
                if r[0] and _printable(r[1]):
                    self.push[r[0]] = r[1]
        if snap.has_contacts:
            cols = {r[1] for r in conn.execute(
                "PRAGMA k.table_info(ZWAADDRESSBOOKCONTACT)")}
            want = [c for c in ("ZFULLNAME", "ZGIVENNAME", "ZBUSINESSNAME",
                                "ZWHATSAPPID", "ZLID") if c in cols]
            if "ZWHATSAPPID" in want or "ZLID" in want:
                sql = "SELECT %s FROM k.ZWAADDRESSBOOKCONTACT" % ", ".join(want)
                for r in conn.execute(sql):
                    d = dict(zip(want, r))
                    nm = next((d.get(c) for c in ("ZFULLNAME", "ZGIVENNAME",
                               "ZBUSINESSNAME") if _printable(d.get(c))), None)
                    wid, lid = d.get("ZWHATSAPPID"), d.get("ZLID")
                    if nm:
                        if wid:
                            self.contact_name[wid] = nm
                        if lid:
                            self.contact_name[lid] = nm
                    if lid and wid:
                        self.lid_phone[lid] = wid

    def phone(self, jid):
        """Phone digits for a JID, or None (an @lid with no contact match)."""
        if not jid:
            return None
        if jid.endswith("@s.whatsapp.net"):
            return "+" + jid_user(jid)
        if jid.endswith("@lid") and jid in self.lid_phone:
            return "+" + jid_user(self.lid_phone[jid])
        return None

    def name_for(self, jid, *fallbacks):
        for f in fallbacks:
            if _printable(f):
                return f
        if jid in self.contact_name:
            return self.contact_name[jid]
        if jid in self.push:
            return self.push[jid]
        ph = self.phone(jid)
        if ph:
            return ph
        if jid and jid.endswith("@lid"):
            # A privacy id with no contact match: not a phone number.
            return "unknown …%s" % jid_user(jid)[-4:]
        return jid_user(jid) or "unknown"


def load_chats(snap, d):
    sql = """
        SELECT s.Z_PK, s.ZCONTACTJID, s.ZPARTNERNAME, s.ZUNREADCOUNT,
               s.ZARCHIVED, s.ZSESSIONTYPE, s.ZLASTMESSAGEDATE,
               s.ZLASTMESSAGETEXT,
               (SELECT COUNT(*) FROM ZWAMESSAGE m WHERE m.ZCHATSESSION = s.Z_PK)
                 AS n
        FROM ZWACHATSESSION s
        WHERE s.ZSESSIONTYPE IN (%s)
          AND COALESCE(s.ZREMOVED, 0) = 0
          AND COALESCE(s.ZHIDDEN, 0) = 0
        ORDER BY s.ZLASTMESSAGEDATE DESC
    """ % ",".join(str(t) for t in CHAT_SESSION_TYPES)
    chats = []
    for r in snap.conn.execute(sql):
        jid = r["ZCONTACTJID"]
        chats.append({
            "pk": r["Z_PK"],
            "jid": jid,
            "name": d.name_for(jid, r["ZPARTNERNAME"]),
            "is_group": is_group_jid(jid),
            "unread": max(0, r["ZUNREADCOUNT"] or 0),
            "archived": bool(r["ZARCHIVED"]),
            "last_date": r["ZLASTMESSAGEDATE"],
            "last_text": r["ZLASTMESSAGETEXT"],
            "messages": r["n"],
        })
    return chats


def resolve_chat(chats, query):
    q = query.strip()
    if "@" in q:
        hits = [c for c in chats if c["jid"] == q]
        if hits:
            return hits[0]
        raise WaError("No chat with JID %s." % q, 2)
    digits = re.sub(r"\D", "", q)
    if digits and len(digits) >= 6 and re.fullmatch(r"[\d\s+\-().]+", q):
        hits = [c for c in chats if jid_user(c["jid"]) == digits]
        if hits:
            return hits[0]
    ql = q.casefold()
    exact = [c for c in chats if c["name"].casefold() == ql]
    if len(exact) == 1:
        return exact[0]
    hits = exact or [c for c in chats if ql in c["name"].casefold()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise WaError('No chat matches "%s". Try: wa chats' % query, 2)
    return AmbiguousChat(hits)


class AmbiguousChat:
    def __init__(self, hits):
        self.hits = hits


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

MSG_SELECT = """
    SELECT m.Z_PK, m.ZCHATSESSION, m.ZISFROMME, m.ZMESSAGETYPE,
           m.ZMESSAGEDATE, m.ZTEXT, m.ZFROMJID, m.ZPUSHNAME, m.ZSTARRED,
           gm.ZMEMBERJID, gm.ZCONTACTNAME, gm.ZFIRSTNAME,
           mi.ZTITLE, mi.ZFILESIZE, mi.ZLATITUDE, mi.ZLONGITUDE,
           mi.ZMEDIALOCALPATH
    FROM ZWAMESSAGE m
    LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
    LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
"""


def build_message(r, chat, d):
    from_me = bool(r["ZISFROMME"])
    if from_me:
        sender, sender_jid = "me", None
    elif (r["ZMESSAGETYPE"] or 0) == 6 and not r["ZMEMBERJID"]:
        sender, sender_jid = "system", None
    elif chat["is_group"]:
        # ZWAMESSAGE.ZPUSHNAME is opaque in WhatsApp 26.x (base64-shaped,
        # not plain text), so it is never used as a name.
        sender_jid = r["ZMEMBERJID"] or None
        if sender_jid:
            sender = d.name_for(sender_jid, r["ZCONTACTNAME"], r["ZFIRSTNAME"])
        else:
            sender = "unknown"
    else:
        sender, sender_jid = chat["name"], chat["jid"]
    mtype = r["ZMESSAGETYPE"] or 0
    media = None
    if r["ZMEDIALOCALPATH"] or r["ZTITLE"] or r["ZFILESIZE"] or r["ZLATITUDE"]:
        media = {
            "kind": MESSAGE_TYPE_LABELS.get(mtype, "media"),
            "title": r["ZTITLE"],
            "bytes": r["ZFILESIZE"] or None,
            "local_path": r["ZMEDIALOCALPATH"],
        }
        if r["ZLATITUDE"] or r["ZLONGITUDE"]:
            media["lat"], media["lon"] = r["ZLATITUDE"], r["ZLONGITUDE"]
    return {
        "id": r["Z_PK"],
        "chat": chat["name"],
        "chat_jid": chat["jid"],
        "is_group": chat["is_group"],
        "from_me": from_me,
        "sender": sender,
        "sender_jid": sender_jid,
        "date": r["ZMESSAGEDATE"],
        "type": mtype,
        "kind": "text" if r["ZTEXT"] and mtype in (0, 7) else
                MESSAGE_TYPE_LABELS.get(mtype, "text" if r["ZTEXT"] else
                                        "type %d" % mtype),
        "text": r["ZTEXT"],
        "starred": bool(r["ZSTARRED"]),
        "media": media,
    }


def display_text(msg):
    t = msg["text"]
    if t:
        return t
    label = "[%s]" % msg["kind"]
    if msg["media"] and msg["media"].get("title"):
        label += " " + msg["media"]["title"]
    return label


def query_messages(snap, chats_by_pk, d, where="", params=(), order="DESC",
                   limit=None):
    sql = MSG_SELECT + (" WHERE " + where if where else "")
    sql += " ORDER BY m.ZMESSAGEDATE %s, m.Z_PK %s" % (order, order)
    if limit:
        sql += " LIMIT %d" % int(limit)
    out = []
    for r in snap.conn.execute(sql, params):
        chat = chats_by_pk.get(r["ZCHATSESSION"])
        if chat is None:
            continue
        out.append(build_message(r, chat, d))
    return out


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

def term_width():
    try:
        w = shutil.get_terminal_size((80, 24)).columns
    except OSError:
        w = 80
    return max(48, min(w, 100))


def clip(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


class Out:
    def __init__(self, args):
        self.json = getattr(args, "json", False)
        self.r = Redactor(getattr(args, "redact", False))
        self.stream = sys.stdout

    def emit_json(self, obj):
        self.stream.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")

    def line(self, s=""):
        self.stream.write(s + "\n")

    # redacted views ---------------------------------------------------
    def chat_name(self, chat):
        return self.r.name(chat["jid"], chat["name"], chat["is_group"])

    def chat_obj(self, chat):
        return {
            "jid": self.r.jid(chat["jid"]),
            "name": self.chat_name(chat),
            "type": "group" if chat["is_group"] else "person",
        }

    def sender(self, msg):
        if msg["from_me"]:
            return "me"
        return self.r.name(msg["sender_jid"] or msg["sender"], msg["sender"])

    def msg_obj(self, msg):
        media = None
        if msg["media"]:
            media = dict(msg["media"])
            media["title"] = self.r.title(media.get("title"))
            if self.r.on:
                media["local_path"] = None
                media.pop("lat", None)
                media.pop("lon", None)
        return {
            "id": msg["id"],
            "chat": self.r.name(msg["chat_jid"], msg["chat"], msg["is_group"]),
            "chat_jid": self.r.jid(msg["chat_jid"]),
            "from_me": msg["from_me"],
            "sender": self.sender(msg),
            "sender_jid": self.r.jid(msg["sender_jid"]),
            "at": iso(msg["date"]),
            "kind": msg["kind"],
            "text": self.r.text(msg["text"]),
            "starred": msg["starred"],
            "media": media,
        }

    def msg_text(self, msg):
        if msg["text"]:
            return self.r.text(msg["text"])
        label = "[%s]" % msg["kind"]
        if msg["media"] and msg["media"].get("title"):
            label += " " + self.r.title(msg["media"]["title"])
        return label


def print_messages(out, msgs, show_chat=False, width=None):
    width = width or term_width()
    for m in msgs:
        star = "*" if m["starred"] else " "
        who = clip(out.sender(m), 16)
        head = "%s%s  %s" % (star, short_time(m["date"]), who)
        if show_chat:
            head += "  in " + clip(out.r.name(m["chat_jid"], m["chat"],
                                             m["is_group"]), 22)
        out.line(head)
        body = out.msg_text(m)
        for chunk in _wrap(body, width - 4):
            out.line("    " + chunk)


def _wrap(text, width):
    import textwrap
    lines = []
    for para in (text or "").splitlines() or [""]:
        lines.extend(textwrap.wrap(para, width) or [""])
    return lines


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _load(snap):
    d = Directory(snap)
    chats = load_chats(snap, d)
    return d, chats, {c["pk"]: c for c in chats}


def _require_schema(snap):
    missing = schema_problems(snap.conn)
    if missing:
        raise WaError(
            "The WhatsApp database schema changed (missing: %s).\n"
            "Run: wa doctor" % ", ".join(missing[:6]), 3)


def _pick_chat(out, chats, query):
    c = resolve_chat(chats, query)
    if isinstance(c, AmbiguousChat):
        names = [out.chat_name(h) for h in c.hits[:15]]
        msg = '"%s" matches %d chats. Be more specific or use a JID:\n' % (
            query, len(c.hits))
        msg += "\n".join("  %s  (%s)" % (n, out.r.jid(h["jid"]))
                         for n, h in zip(names, c.hits[:15]))
        raise WaError(msg, 2)
    return c


def cmd_chats(args, snap, out):
    d, chats, _ = _load(snap)
    rows = chats
    if args.unread:
        rows = [c for c in rows if c["unread"] > 0]
    if args.groups:
        rows = [c for c in rows if c["is_group"]]
    if args.people:
        rows = [c for c in rows if not c["is_group"]]
    rows = rows[: args.limit] if args.limit else rows
    if out.json:
        out.emit_json({"chats": [dict(out.chat_obj(c), **{
            "unread": c["unread"],
            "messages": c["messages"],
            "archived": c["archived"],
            "last_message_at": iso(c["last_date"]),
            "last_message": out.r.text(c["last_text"]),
        }) for c in rows]})
        return 0
    w = term_width()
    out.line("%6s  %-16s  %s" % ("UNREAD", "LAST", "CHAT"))
    for c in rows:
        tag = " (group)" if c["is_group"] else ""
        out.line("%6s  %-16s  %s" % (
            c["unread"] or "", short_time(c["last_date"]),
            clip(out.chat_name(c) + tag, w - 27)))
    out.line("%d chats" % len(rows))
    return 0


def cmd_read(args, snap, out):
    d, chats, by_pk = _load(snap)
    chat = _pick_chat(out, chats, args.chat)
    where, params = "m.ZCHATSESSION = ?", [chat["pk"]]
    since = parse_since(args.since)
    if since is not None:
        where += " AND m.ZMESSAGEDATE >= ?"
        params.append(since)
    msgs = query_messages(snap, by_pk, d, where, params, "DESC", args.last)
    msgs.reverse()
    if out.json:
        out.emit_json({"chat": out.chat_obj(chat),
                       "messages": [out.msg_obj(m) for m in msgs]})
        return 0
    out.line("%s  (%d of %d messages)" % (out.chat_name(chat), len(msgs),
                                          chat["messages"]))
    out.line("")
    print_messages(out, msgs)
    return 0


def cmd_search(args, snap, out):
    d, chats, by_pk = _load(snap)
    needle = args.text.casefold()
    snap.conn.create_function(
        "wa_contains", 1,
        lambda t: 1 if t is not None and needle in t.casefold() else 0,
        deterministic=True)
    where = ["m.ZTEXT IS NOT NULL", "wa_contains(m.ZTEXT)"]
    params = []
    if args.chat:
        chat = _pick_chat(out, chats, args.chat)
        where.append("m.ZCHATSESSION = ?")
        params.append(chat["pk"])
    since = parse_since(args.since)
    if since is not None:
        where.append("m.ZMESSAGEDATE >= ?")
        params.append(since)
    if args.from_ == "me":
        where.append("m.ZISFROMME = 1")
    elif args.from_ == "them":
        where.append("m.ZISFROMME = 0")
    msgs = query_messages(snap, by_pk, d, " AND ".join(where), params,
                          "DESC", args.limit)
    if out.json:
        out.emit_json({"query": out.r.text(args.text),
                       "results": [out.msg_obj(m) for m in msgs]})
        return 0
    out.line('%d result%s for "%s"' % (len(msgs), "" if len(msgs) == 1 else "s",
                                       out.r.text(args.text)))
    out.line("")
    print_messages(out, msgs, show_chat=True)
    return 0


def cmd_unread(args, snap, out):
    d, chats, by_pk = _load(snap)
    rows = [c for c in chats if c["unread"] > 0]
    if args.limit:
        rows = rows[: args.limit]
    digest = []
    for c in rows:
        take = min(c["unread"], args.per_chat)
        msgs = query_messages(
            snap, by_pk, d,
            "m.ZCHATSESSION = ? AND m.ZISFROMME = 0 AND m.ZMESSAGETYPE != 6",
            [c["pk"]], "DESC", take)
        msgs.reverse()
        digest.append((c, msgs, max(0, c["unread"] - len(msgs))))
    total = sum(c["unread"] for c in chats)
    if out.json:
        out.emit_json({
            "total_unread": total,
            "chats_with_unread": sum(1 for c in chats if c["unread"] > 0),
            "chats": [dict(out.chat_obj(c), unread=c["unread"], more=more,
                           messages=[out.msg_obj(m) for m in msgs])
                      for c, msgs, more in digest],
        })
        return 0
    out.line("%d unread in %d chats" % (
        total, sum(1 for c in chats if c["unread"] > 0)))
    for c, msgs, more in digest:
        out.line("")
        out.line("== %s  (%d unread)" % (out.chat_name(c), c["unread"]))
        print_messages(out, msgs)
        if more:
            out.line("    … %d more" % more)
    return 0


def _links_from(msgs):
    found = []
    for m in msgs:
        for u in URL_RE.findall(m["text"] or ""):
            u = u.rstrip(".,;:!?")
            found.append((u, m))
    return found


def cmd_links(args, snap, out):
    d, chats, by_pk = _load(snap)
    if not args.all and not args.chat:
        raise WaError("Give a chat or --all.", 2)
    where = ["m.ZTEXT IS NOT NULL",
             "(m.ZTEXT LIKE '%http%' OR m.ZTEXT LIKE '%www.%')"]
    params = []
    if args.chat and not args.all:
        chat = _pick_chat(out, chats, args.chat)
        where.append("m.ZCHATSESSION = ?")
        params.append(chat["pk"])
    since = parse_since(args.since)
    if since is not None:
        where.append("m.ZMESSAGEDATE >= ?")
        params.append(since)
    msgs = query_messages(snap, by_pk, d, " AND ".join(where), params,
                          "DESC", None)
    links = _links_from(msgs)
    if args.limit:
        links = links[: args.limit]
    if out.json:
        out.emit_json({"links": [{
            "url": out.r.url(u),
            "chat": out.r.name(m["chat_jid"], m["chat"], m["is_group"]),
            "sender": out.sender(m),
            "at": iso(m["date"]),
            "message_id": m["id"],
        } for u, m in links]})
        return 0
    w = term_width()
    for u, m in links:
        out.line("%s  %s  in %s" % (
            short_time(m["date"]), clip(out.sender(m), 16),
            clip(out.r.name(m["chat_jid"], m["chat"], m["is_group"]), 24)))
        out.line("    " + clip(out.r.url(u), w - 4))
    out.line("%d link%s" % (len(links), "" if len(links) == 1 else "s"))
    return 0


def _refuse_inside_container(path):
    real = os.path.realpath(path)
    for guarded in (os.path.dirname(os.path.realpath(db_path())),
                    os.path.realpath(GROUP_CONTAINER)):
        if real == guarded or real.startswith(guarded + os.sep):
            raise WaError("Refusing to write inside the WhatsApp folder: %s"
                          % guarded, 2)


def cmd_export(args, snap, out):
    d, chats, by_pk = _load(snap)
    chat = _pick_chat(out, chats, args.chat)
    if not args.md:
        raise WaError("Give an output file: wa export <chat> --md out.md", 2)
    _refuse_inside_container(args.md)
    where, params = "m.ZCHATSESSION = ?", [chat["pk"]]
    since = parse_since(args.since)
    if since is not None:
        where += " AND m.ZMESSAGEDATE >= ?"
        params.append(since)
    msgs = query_messages(snap, by_pk, d, where, params, "ASC", None)
    name = out.chat_name(chat)
    lines = ["# %s" % name, ""]
    lines.append("%d messages, exported %s by wa. Read-only snapshot." % (
        len(msgs), _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")))
    if out.r.on:
        lines.append("Names, numbers and message text are redacted.")
    day = None
    for m in msgs:
        dt = coredata_to_datetime(m["date"])
        dkey = dt.strftime("%Y-%m-%d") if dt else "unknown"
        if dkey != day:
            day = dkey
            lines += ["", "## %s" % dkey, ""]
        body = out.msg_text(m).replace("\n", "  \n  ")
        star = " ⭐" if m["starred"] else ""
        lines.append("- **%s** %s%s: %s" % (
            dt.strftime("%H:%M") if dt else "--:--", out.sender(m), star, body))
    with open(args.md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if out.json:
        out.emit_json({"chat": out.chat_obj(chat), "path": args.md,
                       "messages": len(msgs)})
    else:
        out.line("Wrote %d messages from %s to %s" % (len(msgs), name, args.md))
    return 0


def cmd_stats(args, snap, out):
    d, chats, _ = _load(snap)
    conn = snap.conn
    in_chats = ",".join(str(c["pk"]) for c in chats) or "NULL"
    row = conn.execute(
        "SELECT COUNT(*), MIN(ZMESSAGEDATE), MAX(ZMESSAGEDATE), "
        "SUM(ZISFROMME = 1), SUM(ZSTARRED = 1) "
        "FROM ZWAMESSAGE WHERE ZCHATSESSION IN (%s)" % in_chats).fetchone()
    all_msgs = conn.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0]
    all_sessions = conn.execute(
        "SELECT COUNT(*) FROM ZWACHATSESSION").fetchone()[0]
    top = sorted(chats, key=lambda c: c["messages"], reverse=True)[:10]
    stats = {
        "chats": len(chats),
        "groups": sum(1 for c in chats if c["is_group"]),
        "people": sum(1 for c in chats if not c["is_group"]),
        "messages": row[0],
        "sent_by_me": row[3] or 0,
        "starred": row[4] or 0,
        "unread": sum(c["unread"] for c in chats),
        "chats_with_unread": sum(1 for c in chats if c["unread"] > 0),
        "first_message_at": iso(row[1]),
        "last_message_at": iso(row[2]),
        "all_sessions_including_status": all_sessions,
        "all_messages_including_status": all_msgs,
        "top_chats": [dict(out.chat_obj(c), messages=c["messages"])
                      for c in top],
    }
    if out.json:
        out.emit_json(stats)
        return 0
    out.line("chats      %d  (%d people, %d groups)" % (
        stats["chats"], stats["people"], stats["groups"]))
    out.line("messages   %d  (%d sent by me, %d starred)" % (
        stats["messages"], stats["sent_by_me"], stats["starred"]))
    out.line("unread     %d in %d chats" % (stats["unread"],
                                            stats["chats_with_unread"]))
    out.line("range      %s to %s" % (short_time(row[1])[:10],
                                      short_time(row[2])[:10]))
    out.line("")
    out.line("top chats by messages")
    for c in top:
        out.line("  %7d  %s" % (c["messages"], clip(out.chat_name(c), 50)))
    return 0


def build_wa_me_url(phone, text):
    digits = re.sub(r"\D", "", phone or "")
    if not 7 <= len(digits) <= 15:
        raise WaError(
            "Phone must be a full international number, 7 to 15 digits, "
            "for example +1 555 010 2030.", 2)
    url = "https://wa.me/%s" % digits
    if text:
        url += "?text=" + urllib.parse.quote(text, safe="")
    return url


def cmd_send(args, out):
    url = build_wa_me_url(args.phone, args.text)
    opened = False
    if args.open:
        subprocess.run(["open", url], check=True)
        opened = True
    shown = url
    if out.r.on:
        digits = re.sub(r"\D", "", args.phone)
        shown = "https://wa.me/%s%s" % (
            out.r.digits(digits),
            "?text=" + urllib.parse.quote(out.r.text(args.text), safe="")
            if args.text else "")
    if out.json:
        out.emit_json({"url": shown, "opened": opened})
    else:
        out.line(shown)
        if opened:
            out.line("Opened a draft in WhatsApp. Review it and press Send "
                     "yourself.")
        else:
            out.line("Not opened. Add --open to open this draft in WhatsApp. "
                     "wa never sends.")
    return 0


def whatsapp_version():
    plist = os.environ.get("WA_APP_PLIST", DEFAULT_APP_PLIST)
    try:
        with open(plist, "rb") as f:
            info = plistlib.load(f)
        return info.get("CFBundleShortVersionString") or info.get(
            "CFBundleVersion")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def cmd_doctor(args, out):
    path = db_path()
    report = {
        "ok": False,
        "db_path": path,
        "db_found": os.path.isfile(path),
        "db_bytes": None,
        "wal_bytes": None,
        "whatsapp_version": whatsapp_version(),
        "tested_version": TESTED_WHATSAPP_VERSION,
        "contacts_db_found": os.path.isfile(contacts_path(path)),
        "last_message_at": None,
        "schema_ok": False,
        "schema_missing": [],
        "problem": None,
    }
    if report["db_found"]:
        report["db_bytes"] = os.path.getsize(path)
        if os.path.exists(path + "-wal"):
            report["wal_bytes"] = os.path.getsize(path + "-wal")
        try:
            with Snapshot(path) as snap:
                missing = schema_problems(snap.conn)
                report["schema_missing"] = missing
                report["schema_ok"] = not missing
                if not missing:
                    last = snap.conn.execute(
                        "SELECT MAX(ZMESSAGEDATE) FROM ZWAMESSAGE").fetchone()[0]
                    report["last_message_at"] = iso(last)
        except sqlite3.DatabaseError as e:
            report["problem"] = "Could not read the database copy: %s" % e
    else:
        report["problem"] = ("No WhatsApp database found. Install WhatsApp "
                             "Desktop, sign in and let it sync, or set WA_DB.")
    if report["db_found"] and not report["schema_ok"] and not report["problem"]:
        report["problem"] = (
            "Schema mismatch. WhatsApp changed its database (missing: %s). "
            "wa was tested on WhatsApp %s." % (
                ", ".join(report["schema_missing"]), TESTED_WHATSAPP_VERSION))
    report["ok"] = report["db_found"] and report["schema_ok"]
    if out.json:
        out.emit_json(report)
    else:
        mb = lambda b: "%.1f MB" % (b / 1e6) if b is not None else "-"
        out.line("database   %s" % path)
        out.line("found      %s" % ("yes" if report["db_found"] else "no"))
        if report["db_found"]:
            out.line("size       %s  (wal %s)" % (mb(report["db_bytes"]),
                                                   mb(report["wal_bytes"])))
        out.line("contacts   %s" % ("found" if report["contacts_db_found"]
                                    else "not found (names may be missing)"))
        out.line("whatsapp   %s  (tested on %s)" % (
            report["whatsapp_version"] or "not found", TESTED_WHATSAPP_VERSION))
        out.line("last msg   %s" % (report["last_message_at"] or "-"))
        out.line("schema     %s" % ("ok" if report["schema_ok"] else "MISMATCH"))
        if report["problem"]:
            out.line("")
            out.line("problem: " + report["problem"])
        else:
            out.line("")
            out.line("All good. wa can read this database.")
    return 0 if report["ok"] else 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    common.add_argument("--redact", action="store_true",
                        default=argparse.SUPPRESS,
                        help="hide names, numbers and message text")

    p = argparse.ArgumentParser(
        prog="wa",
        description="Read-only WhatsApp CLI. Reads a snapshot of the local "
                    "WhatsApp Desktop database. No network.")
    p.add_argument("--version", action="version",
                   version="wa %s" % __version__)
    p.add_argument("--json", action="store_true", default=False,
                   help=argparse.SUPPRESS)
    p.add_argument("--redact", action="store_true", default=False,
                   help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", metavar="command")

    s = sub.add_parser("chats", parents=[common], help="list chats")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--groups", action="store_true")
    g.add_argument("--people", action="store_true")
    s.add_argument("--unread", action="store_true")
    s.add_argument("--limit", type=int, default=20)

    s = sub.add_parser("read", parents=[common], help="read one chat")
    s.add_argument("chat", help="name substring, phone or JID")
    s.add_argument("--last", type=int, default=20)
    s.add_argument("--since")

    s = sub.add_parser("search", parents=[common], help="search message text")
    s.add_argument("text")
    s.add_argument("--chat")
    s.add_argument("--since")
    s.add_argument("--from", dest="from_", choices=["me", "them"])
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("unread", parents=[common],
                       help="digest of unread messages")
    s.add_argument("--per-chat", type=int, default=10)
    s.add_argument("--limit", type=int, default=25, help="max chats")

    s = sub.add_parser("links", parents=[common], help="URLs shared in chats")
    s.add_argument("chat", nargs="?")
    s.add_argument("--all", action="store_true")
    s.add_argument("--since")
    s.add_argument("--limit", type=int, default=0)

    s = sub.add_parser("export", parents=[common],
                       help="export a chat as Markdown")
    s.add_argument("chat")
    s.add_argument("--md", required=True, metavar="FILE")
    s.add_argument("--since")

    sub.add_parser("stats", parents=[common], help="counts and top chats")

    s = sub.add_parser("send", parents=[common],
                       help="build a wa.me draft link (never sends)")
    s.add_argument("phone")
    s.add_argument("text", nargs="?", default="")
    s.add_argument("--open", action="store_true",
                   help="open the draft in WhatsApp; you press Send")

    sub.add_parser("doctor", parents=[common], help="check setup and schema")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 2
    out = Out(args)
    try:
        if args.cmd == "send":
            return cmd_send(args, out)
        if args.cmd == "doctor":
            return cmd_doctor(args, out)
        handler = {
            "chats": cmd_chats, "read": cmd_read, "search": cmd_search,
            "unread": cmd_unread, "links": cmd_links, "export": cmd_export,
            "stats": cmd_stats,
        }[args.cmd]
        with Snapshot() as snap:
            _require_schema(snap)
            return handler(args, snap, out)
    except WaError as e:
        sys.stderr.write("wa: %s\n" % e)
        return e.exit_code
    except sqlite3.DatabaseError as e:
        sys.stderr.write("wa: could not read the database copy (%s). "
                         "Run: wa doctor\n" % e)
        return 3
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
