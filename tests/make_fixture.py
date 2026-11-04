#!/usr/bin/env python3
"""Build a synthetic WhatsApp Desktop database for tests and README examples.

Every name, number and message here is made up. Usage:

    python3 tests/make_fixture.py OUT_DIR

writes OUT_DIR/ChatStorage.sqlite (+ -wal, -shm) and OUT_DIR/ContactsV2.sqlite.
"""

import os
import shutil
import sqlite3
import sys
import tempfile

EPOCH = 978307200


def cd(unix):
    """Unix seconds to Core Data seconds."""
    return unix - EPOCH


# 2026-11-01 09:00:00 UTC
BASE = 1793523600
DAY = 86400

ME = "15550000000@s.whatsapp.net"

# Every string a redacted run must never print.
SENSITIVE_NAMES = [
    "Maya Chen", "Diego Ramos", "Priya 🌸 Nair", "Hiking Club",
    "Family 家庭", "Tomás Ortega", "Lena Fischer", "Sam Okafor",
]
SENSITIVE_DIGITS = ["15551230001", "15551230002", "15551230003",
                    "15551230004", "15551230005", "15551230006"]
SENSITIVE_TEXT = [
    "lunch", "école", "你好", "مرحبا", "trailhead", "Saturday", "recipe",
    "boarding", "invoice", "sunrise", "running late",
]

SCHEMA = """
CREATE TABLE ZWACHATSESSION (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZARCHIVED INTEGER, ZHIDDEN INTEGER, ZREMOVED INTEGER, ZSESSIONTYPE INTEGER,
  ZUNREADCOUNT INTEGER, ZMESSAGECOUNTER INTEGER, ZGROUPINFO INTEGER,
  ZLASTMESSAGEDATE TIMESTAMP, ZCONTACTJID VARCHAR, ZLASTMESSAGETEXT VARCHAR,
  ZPARTNERNAME VARCHAR);
CREATE TABLE ZWAMESSAGE (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZISFROMME INTEGER, ZMESSAGETYPE INTEGER, ZSTARRED INTEGER,
  ZCHATSESSION INTEGER, ZGROUPMEMBER INTEGER, ZMEDIAITEM INTEGER,
  ZPARENTMESSAGE INTEGER, ZMESSAGEDATE TIMESTAMP, ZFROMJID VARCHAR,
  ZPUSHNAME VARCHAR, ZTEXT VARCHAR, ZTOJID VARCHAR);
CREATE TABLE ZWAGROUPMEMBER (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZCHATSESSION INTEGER, ZCONTACTNAME VARCHAR, ZFIRSTNAME VARCHAR,
  ZMEMBERJID VARCHAR);
CREATE TABLE ZWAMEDIAITEM (
  Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZFILESIZE INTEGER, ZMESSAGE INTEGER, ZLATITUDE FLOAT, ZLONGITUDE FLOAT,
  ZMEDIALOCALPATH VARCHAR, ZTITLE VARCHAR);
CREATE TABLE ZWAPROFILEPUSHNAME (
  Z_PK INTEGER PRIMARY KEY, ZJID VARCHAR, ZPUSHNAME VARCHAR);
"""

CONTACTS_SCHEMA = """
CREATE TABLE ZWAADDRESSBOOKCONTACT (
  Z_PK INTEGER PRIMARY KEY, ZFULLNAME VARCHAR, ZGIVENNAME VARCHAR,
  ZBUSINESSNAME VARCHAR, ZWHATSAPPID VARCHAR, ZLID VARCHAR,
  ZPHONENUMBER VARCHAR);
"""

# chats: pk, jid, partner name, type, unread, archived
CHATS = [
    (1, "15551230001@s.whatsapp.net", "Maya Chen", 0, 2, 0),
    (2, "88776655443322@lid", None, 0, 0, 0),          # Diego via contacts
    (3, "15551230003@s.whatsapp.net", "Priya 🌸 Nair", 0, 1, 0),
    (4, "120363000000000001@g.us", "Hiking Club", 1, 3, 0),
    (5, "120363000000000002@g.us", "Family 家庭", 1, 0, 0),
    (6, "status@broadcast", None, 3, 5, 0),           # must be hidden
]

# group members: pk, chat, name, first, jid
MEMBERS = [
    (1, 4, "Tomás Ortega", "Tomás", "15551230004@s.whatsapp.net"),
    (2, 4, None, None, "99887766554433@lid"),         # resolves by push name
    (3, 4, "Maya Chen", "Maya", "15551230001@s.whatsapp.net"),
    (4, 5, "Sam Okafor", "Sam", "15551230006@s.whatsapp.net"),
    (5, 4, None, None, "55554444333322@lid"),         # no name anywhere
]

PUSHNAMES = [(1, "99887766554433@lid", "Lena Fischer")]

CONTACTS = [
    (1, "Maya Chen", "Maya", None, "15551230001@s.whatsapp.net",
     "11112222333344@lid", "+1 555 123 0001"),
    (2, "Diego Ramos", "Diego", None, "15551230002@s.whatsapp.net",
     "88776655443322@lid", "+1 555 123 0002"),
    (3, "Sam Okafor", "Sam", None, "15551230006@s.whatsapp.net",
     "22223333444455@lid", "+1 555 123 0006"),
]

# media: pk, message, size, lat, lon, path, title
MEDIA = [
    (1, 5, 204800, None, None, "Media/maya/photo.jpg", None),
    (2, 13, 51200, None, None, "Media/hike/route.pdf", "trailhead map.pdf"),
    (3, 14, None, 44.27, -71.30, None, None),
]

# messages: pk, chat, from_me, type, starred, member, media, t_offset, text
MESSAGES = [
    (1, 1, 0, 0, 0, None, None, 0, "Are we still on for lunch tomorrow?"),
    (2, 1, 1, 0, 0, None, None, 60, "Yes! 12:30 at the usual place 🍜"),
    (3, 1, 0, 7, 1, None, None, 120,
     "Here is the recipe https://example.com/recipes/dumplings?id=42."),
    (4, 1, 0, 0, 0, None, None, DAY, "Bonjour, je suis à l'école 你好"),
    (5, 1, 0, 1, 0, None, 1, DAY + 30, None),
    (6, 2, 0, 0, 0, None, None, 200, "مرحبا Diego here, invoice attached soon"),
    (7, 2, 1, 0, 0, None, None, 260, "Thanks Diego, got it"),
    (8, 3, 0, 0, 0, None, None, 2 * DAY, "Boarding at gate 12 ✈️"),
    (9, 4, 1, 0, 0, None, None, 300, "Who is in for Saturday?"),
    (10, 4, 0, 0, 0, 1, None, 360, "Me! Meet at the trailhead at 7"),
    (11, 4, 0, 0, 0, 2, None, 420, "Count me in, see www.trails.example/route"),
    (12, 4, 0, 0, 0, 3, None, DAY + 400, "I'll bring snacks"),
    (20, 4, 0, 0, 0, 5, None, 380, "Running late, start without me"),
    (13, 4, 0, 8, 0, 1, 2, DAY + 500, None),
    (14, 4, 0, 5, 0, 2, 3, DAY + 600, None),
    (15, 4, 0, 6, 0, None, None, 100, None),          # group event
    (16, 5, 0, 0, 0, 4, None, 50, "Happy birthday Grandma! 🎂"),
    (17, 5, 1, 0, 1, None, None, 70, "Sunrise photos tomorrow"),
    (18, 6, 0, 0, 0, None, None, 80, "status update should never show"),
]

# These rows are written after the base file and stay in the WAL only.
WAL_MESSAGES = [
    (19, 3, 0, 0, 0, None, None, 2 * DAY + 60, "Landed. Call you later"),
]


def _jid_for(chat_pk):
    return next(c[1] for c in CHATS if c[0] == chat_pk)


def _insert_messages(conn, rows):
    for pk, chat, me, mtype, star, member, media, off, text in rows:
        cj = _jid_for(chat)
        conn.execute(
            "INSERT INTO ZWAMESSAGE (Z_PK, Z_ENT, Z_OPT, ZISFROMME, "
            "ZMESSAGETYPE, ZSTARRED, ZCHATSESSION, ZGROUPMEMBER, ZMEDIAITEM, "
            "ZMESSAGEDATE, ZFROMJID, ZTOJID, ZPUSHNAME, ZTEXT) "
            "VALUES (?,9,1,?,?,?,?,?,?,?,?,?,?,?)",
            (pk, me, mtype, star, chat, member, media, cd(BASE + off),
             None if me else cj, cj if me else ME,
             None, text))


def build(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="wa-fixture-")
    try:
        path = os.path.join(work, "ChatStorage.sqlite")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        for pk, jid, name, stype, unread, arch in CHATS:
            conn.execute(
                "INSERT INTO ZWACHATSESSION (Z_PK, Z_ENT, Z_OPT, ZCONTACTJID, "
                "ZPARTNERNAME, ZSESSIONTYPE, ZUNREADCOUNT, ZARCHIVED, ZHIDDEN, "
                "ZREMOVED) VALUES (?,4,1,?,?,?,?,?,0,0)",
                (pk, jid, name, stype, unread, arch))
        conn.executemany(
            "INSERT INTO ZWAGROUPMEMBER (Z_PK, Z_ENT, Z_OPT, ZCHATSESSION, "
            "ZCONTACTNAME, ZFIRSTNAME, ZMEMBERJID) VALUES (?,6,1,?,?,?,?)",
            MEMBERS)
        conn.executemany(
            "INSERT INTO ZWAPROFILEPUSHNAME (Z_PK, ZJID, ZPUSHNAME) "
            "VALUES (?,?,?)", PUSHNAMES)
        conn.executemany(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, Z_ENT, Z_OPT, ZMESSAGE, "
            "ZFILESIZE, ZLATITUDE, ZLONGITUDE, ZMEDIALOCALPATH, ZTITLE) "
            "VALUES (?,8,1,?,?,?,?,?,?)", MEDIA)
        _insert_messages(conn, MESSAGES)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # Now write rows that stay in the WAL: no checkpoint, copy while open.
        conn.execute("PRAGMA wal_autocheckpoint=0")
        _insert_messages(conn, WAL_MESSAGES)
        _update_last(conn)
        conn.commit()
        for suffix in ("", "-wal", "-shm"):
            src = path + suffix
            dst = os.path.join(out_dir, "ChatStorage.sqlite" + suffix)
            if os.path.exists(dst):
                os.remove(dst)
            if os.path.exists(src):
                shutil.copyfile(src, dst)
        conn.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    cpath = os.path.join(out_dir, "ContactsV2.sqlite")
    if os.path.exists(cpath):
        os.remove(cpath)
    c = sqlite3.connect(cpath)
    c.executescript(CONTACTS_SCHEMA)
    c.executemany(
        "INSERT INTO ZWAADDRESSBOOKCONTACT (Z_PK, ZFULLNAME, ZGIVENNAME, "
        "ZBUSINESSNAME, ZWHATSAPPID, ZLID, ZPHONENUMBER) VALUES (?,?,?,?,?,?,?)",
        CONTACTS)
    c.commit()
    c.close()
    return os.path.join(out_dir, "ChatStorage.sqlite")


def _update_last(conn):
    for (pk,) in conn.execute("SELECT Z_PK FROM ZWACHATSESSION").fetchall():
        row = conn.execute(
            "SELECT ZMESSAGEDATE, ZTEXT FROM ZWAMESSAGE WHERE ZCHATSESSION=? "
            "ORDER BY ZMESSAGEDATE DESC LIMIT 1", (pk,)).fetchone()
        if row:
            conn.execute(
                "UPDATE ZWACHATSESSION SET ZLASTMESSAGEDATE=?, "
                "ZLASTMESSAGETEXT=? WHERE Z_PK=?", (row[0], row[1], pk))


def build_broken(out_dir, drop_column="ZTEXT"):
    """A database whose ZWAMESSAGE lacks one expected column."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "ChatStorage.sqlite")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    schema = SCHEMA.replace(" ZTEXT VARCHAR,", "") if drop_column == "ZTEXT" \
        else SCHEMA
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return path


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "fixture"
    print(build(target))
