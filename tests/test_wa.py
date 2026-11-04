import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import plistlib
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import make_fixture as fx  # noqa: E402
import wa  # noqa: E402


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def run(argv):
    """Run wa.main in-process. Returns (exit code, stdout, stderr)."""
    o, e = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
        code = wa.main(argv)
    return code, o.getvalue(), e.getvalue()


def run_json(argv):
    code, out, err = run(argv + ["--json"])
    assert code == 0, (argv, code, err)
    return json.loads(out)


EVERY_COMMAND = [
    ["chats"], ["chats", "--unread"], ["chats", "--groups"],
    ["chats", "--people", "--limit", "2"],
    ["read", "Maya"], ["read", "Hiking", "--last", "3"],
    ["read", "Diego", "--since", "2026-10-01"],
    ["search", "the"], ["search", "lunch", "--chat", "Maya"],
    ["search", "e", "--from", "me"],
    ["unread"], ["links", "--all"], ["links", "Hiking"],
    ["stats"], ["send", "+1 555 010 2030", "hello there"],
    ["doctor"],
]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="wa-test-")
        cls.db = fx.build(os.path.join(cls.tmp, "wa"))
        cls.plist = os.path.join(cls.tmp, "Info.plist")
        with open(cls.plist, "wb") as f:
            plistlib.dump({"CFBundleShortVersionString": "26.44.15"}, f)
        cls.env = mock.patch.dict(os.environ, {
            "WA_DB": cls.db, "WA_APP_PLIST": cls.plist, "TZ": "UTC"})
        cls.env.start()
        if hasattr(__import__("time"), "tzset"):
            __import__("time").tzset()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        if hasattr(__import__("time"), "tzset"):
            __import__("time").tzset()
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestDates(Base):
    def test_epoch(self):
        d = wa.coredata_to_datetime(0, tz=dt.timezone.utc)
        self.assertEqual(d.isoformat(), "2001-01-01T00:00:00+00:00")

    def test_known_value(self):
        d = wa.coredata_to_datetime(fx.cd(fx.BASE), tz=dt.timezone.utc)
        self.assertEqual(d.isoformat(), "2026-11-01T09:00:00+00:00")
        self.assertEqual(wa.unix_to_coredata(wa.coredata_to_unix(123.5)), 123.5)

    def test_real_valued_dates(self):
        d = wa.coredata_to_datetime(1.5, tz=dt.timezone.utc)
        self.assertEqual(d.microsecond, 500000)

    def test_since(self):
        now = dt.datetime(2026, 11, 3, 15, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(wa.parse_since("7d", now),
                         wa.unix_to_coredata((now - dt.timedelta(days=7)).timestamp()))
        today = wa.parse_since("today", now)
        self.assertEqual(wa.coredata_to_datetime(today, dt.timezone.utc).hour, 0)
        self.assertIsNotNone(wa.parse_since("2026-11-02"))
        with self.assertRaises(wa.WaError):
            wa.parse_since("next tuesday")

    def test_json_dates_are_utc_iso(self):
        msgs = run_json(["read", "Maya"])["messages"]
        self.assertEqual(msgs[0]["at"], "2026-11-01T09:00:00+00:00")


class TestChats(Base):
    def test_default(self):
        data = run_json(["chats"])
        names = [c["name"] for c in data["chats"]]
        self.assertEqual(len(names), 5)
        self.assertIn("Diego Ramos", names)          # @lid via ContactsV2
        self.assertNotIn("status@broadcast", [c["jid"] for c in data["chats"]])
        self.assertEqual(names[0], "Priya 🌸 Nair")  # newest first (WAL row)
        self.assertEqual(set(data["chats"][0]),
                         {"jid", "name", "type", "unread", "messages",
                          "archived", "last_message_at", "last_message"})

    def test_filters(self):
        self.assertEqual({c["name"] for c in run_json(["chats", "--unread"])["chats"]},
                         {"Maya Chen", "Priya 🌸 Nair", "Hiking Club"})
        self.assertTrue(all(c["type"] == "group"
                            for c in run_json(["chats", "--groups"])["chats"]))
        people = run_json(["chats", "--people", "--limit", "2"])["chats"]
        self.assertEqual(len(people), 2)
        self.assertTrue(all(c["type"] == "person" for c in people))

    def test_human(self):
        code, out, _ = run(["chats"])
        self.assertEqual(code, 0)
        self.assertIn("Hiking Club (group)", out)
        self.assertTrue(all(len(line) <= 100 for line in out.splitlines()))


class TestRead(Base):
    def test_by_name_jid_phone(self):
        a = run_json(["read", "maya"])
        b = run_json(["read", "15551230001@s.whatsapp.net"])
        c = run_json(["read", "+1 555 123 0001"])
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(len(a["messages"]), 5)
        self.assertEqual(set(a["messages"][0]),
                         {"id", "chat", "chat_jid", "from_me", "sender",
                          "sender_jid", "at", "kind", "text", "starred", "media"})

    def test_group_senders(self):
        msgs = run_json(["read", "Hiking"])["messages"]
        senders = [m["sender"] for m in msgs]
        self.assertIn("Tomás Ortega", senders)
        self.assertIn("Lena Fischer", senders)      # @lid member via push name
        self.assertIn("me", senders)
        self.assertIn("system", senders)
        self.assertIn("unknown …3322", senders)   # unresolved @lid, not a phone
        doc = next(m for m in msgs if m["kind"] == "document")
        self.assertEqual(doc["media"]["title"], "trailhead map.pdf")
        loc = next(m for m in msgs if m["kind"] == "location")
        self.assertAlmostEqual(loc["media"]["lat"], 44.27)

    def test_last_and_since(self):
        msgs = run_json(["read", "Hiking", "--last", "2"])["messages"]
        self.assertEqual([m["id"] for m in msgs], [13, 14])
        msgs = run_json(["read", "Maya", "--since", "2026-11-02"])["messages"]
        self.assertEqual([m["id"] for m in msgs], [4, 5])

    def test_wal_rows_are_read(self):
        msgs = run_json(["read", "Priya"])["messages"]
        self.assertEqual(msgs[-1]["text"], "Landed. Call you later")

    def test_ambiguous_and_missing(self):
        code, _, err = run(["read", "a"])
        self.assertEqual(code, 2)
        self.assertIn("matches", err)
        self.assertIn("Maya Chen", err)
        self.assertIn("Family 家庭", err)
        code, _, err = run(["read", "nobody-here"])
        self.assertEqual(code, 2)
        self.assertIn("No chat", err)

    def test_human(self):
        code, out, _ = run(["read", "Maya"])
        self.assertEqual(code, 0)
        self.assertIn("[image]", out)
        self.assertIn("*2026-11-01 09:02", out)       # starred marker


class TestSearch(Base):
    def test_case_and_unicode(self):
        self.assertEqual([m["id"] for m in run_json(["search", "ÉCOLE"])["results"]], [4])
        self.assertEqual([m["id"] for m in run_json(["search", "你好"])["results"]], [4])
        self.assertEqual([m["id"] for m in run_json(["search", "مرحبا"])["results"]], [6])
        self.assertEqual([m["id"] for m in run_json(["search", "🍜"])["results"]], [2])
        self.assertEqual([m["id"] for m in run_json(["search", "LUNCH"])["results"]], [1])

    def test_filters(self):
        r = run_json(["search", "the", "--chat", "Hiking"])["results"]
        self.assertTrue(all(m["chat"] == "Hiking Club" for m in r))
        self.assertTrue(r)
        me = run_json(["search", "e", "--from", "me"])["results"]
        self.assertTrue(me and all(m["from_me"] for m in me))
        them = run_json(["search", "e", "--from", "them"])["results"]
        self.assertTrue(them and not any(m["from_me"] for m in them))
        self.assertEqual(len(run_json(["search", "e", "--limit", "2"])["results"]), 2)
        late = run_json(["search", "e", "--since", "2026-11-03"])["results"]
        self.assertEqual({m["id"] for m in late}, {8, 19})

    def test_like_wildcards_are_literal(self):
        self.assertEqual(run_json(["search", "%"])["results"], [])
        self.assertEqual(run_json(["search", "_"])["results"], [])


class TestUnread(Base):
    def test_digest(self):
        d = run_json(["unread"])
        self.assertEqual(d["total_unread"], 6)
        self.assertEqual(d["chats_with_unread"], 3)
        hike = next(c for c in d["chats"] if c["name"] == "Hiking Club")
        self.assertEqual(hike["unread"], 3)
        self.assertEqual([m["id"] for m in hike["messages"]], [12, 13, 14])
        self.assertTrue(all(not m["from_me"] for c in d["chats"] for m in c["messages"]))

    def test_cap(self):
        d = run_json(["unread", "--per-chat", "1"])
        hike = next(c for c in d["chats"] if c["name"] == "Hiking Club")
        self.assertEqual(len(hike["messages"]), 1)
        self.assertEqual(hike["more"], 2)
        code, out, _ = run(["unread", "--per-chat", "1"])
        self.assertIn("… 2 more", out)


class TestLinks(Base):
    def test_all(self):
        links = run_json(["links", "--all"])["links"]
        urls = {l["url"] for l in links}
        self.assertEqual(urls, {"https://example.com/recipes/dumplings?id=42",
                                "www.trails.example/route"})
        rec = next(l for l in links if "example.com" in l["url"])
        self.assertEqual(rec["sender"], "Maya Chen")
        self.assertEqual(rec["at"], "2026-11-01T09:02:00+00:00")

    def test_one_chat_and_since(self):
        links = run_json(["links", "Hiking"])["links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["sender"], "Lena Fischer")
        self.assertEqual(run_json(["links", "--all", "--since", "2026-11-02"])["links"], [])

    def test_needs_target(self):
        self.assertEqual(run(["links"])[0], 2)


class TestExport(Base):
    def test_markdown(self):
        out = os.path.join(self.tmp, "hike.md")
        d = run_json(["export", "Hiking", "--md", out])
        self.assertEqual(d["messages"], 8)
        text = slurp(out)
        self.assertTrue(text.startswith("# Hiking Club"))
        self.assertIn("## 2026-11-01", text)
        self.assertIn("## 2026-11-02", text)
        self.assertIn("**09:06** Tomás Ortega: Me! Meet at the trailhead at 7", text)

    def test_refuses_whatsapp_folder(self):
        target = os.path.join(os.path.dirname(self.db), "evil.md")
        code, _, err = run(["export", "Hiking", "--md", target])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(target))


class TestStats(Base):
    def test_stats(self):
        s = run_json(["stats"])
        self.assertEqual(s["chats"], 5)
        self.assertEqual(s["groups"], 2)
        self.assertEqual(s["people"], 3)
        self.assertEqual(s["messages"], 19)            # status message excluded
        self.assertEqual(s["all_messages_including_status"], 20)
        self.assertEqual(s["sent_by_me"], 4)
        self.assertEqual(s["starred"], 2)
        self.assertEqual(s["first_message_at"][:10], "2026-11-01")
        self.assertEqual(s["last_message_at"][:10], "2026-11-03")
        self.assertEqual(s["top_chats"][0], {"jid": "120363000000000001@g.us",
                                             "name": "Hiking Club", "type": "group",
                                             "messages": 8})


class TestSend(Base):
    def test_url_encoding(self):
        url = wa.build_wa_me_url("+1 (555) 010-2030", "Hi & bye? café 🎉 #1\nok")
        self.assertEqual(
            url, "https://wa.me/15550102030?text=Hi%20%26%20bye%3F%20caf%C3%A9"
                 "%20%F0%9F%8E%89%20%231%0Aok")
        self.assertEqual(wa.build_wa_me_url("15550102030", ""),
                         "https://wa.me/15550102030")

    def test_bad_numbers(self):
        for bad in ("", "123", "abc", "+1234567890123456"):
            with self.assertRaises(wa.WaError):
                wa.build_wa_me_url(bad, "x")

    def test_open_only_with_flag(self):
        with mock.patch.object(wa.subprocess, "run") as r:
            code, out, _ = run(["send", "+15550102030", "hi"])
            self.assertEqual(code, 0)
            r.assert_not_called()
            self.assertIn("https://wa.me/15550102030?text=hi", out)
            code, out, _ = run(["send", "+15550102030", "hi", "--open"])
            self.assertEqual(code, 0)
            r.assert_called_once_with(
                ["open", "https://wa.me/15550102030?text=hi"], check=True)

    def test_json(self):
        self.assertEqual(run_json(["send", "+15550102030", "hi"]),
                         {"url": "https://wa.me/15550102030?text=hi", "opened": False})


class TestDoctor(Base):
    def test_ok(self):
        d = run_json(["doctor"])
        self.assertTrue(d["ok"])
        self.assertEqual(d["whatsapp_version"], "26.44.15")
        self.assertTrue(d["schema_ok"])
        self.assertEqual(d["last_message_at"][:10], "2026-11-03")

    def test_schema_mismatch(self):
        broken = fx.build_broken(os.path.join(self.tmp, "broken"))
        with mock.patch.dict(os.environ, {"WA_DB": broken}):
            code, out, _ = run(["doctor"])
            self.assertEqual(code, 1)
            self.assertIn("ZWAMESSAGE.ZTEXT", out)
            self.assertIn("MISMATCH", out)
            code, _, err = run(["chats"])
            self.assertEqual(code, 3)
            self.assertIn("wa doctor", err)

    def test_missing_db(self):
        with mock.patch.dict(os.environ, {"WA_DB": os.path.join(self.tmp, "nope.sqlite")}):
            code, out, _ = run(["doctor"])
            self.assertEqual(code, 1)
            self.assertIn("No WhatsApp database", out)
            code, _, err = run(["stats"])
            self.assertEqual(code, 1)
            self.assertIn("WA_DB", err)


class TestRedaction(Base):
    def _check(self, text):
        low = text.casefold()
        for s in fx.SENSITIVE_NAMES + fx.SENSITIVE_TEXT:
            self.assertNotIn(s.casefold(), low, "leaked %r" % s)
        for s in fx.SENSITIVE_DIGITS + ["88776655443322", "99887766554433",
                                        "120363000000000001", "55554444333322", "3322",
                                        "trails.example/route",
                                        "dumplings", "Media/"]:
            self.assertNotIn(s, text, "leaked %r" % s)

    def test_every_command(self):
        for argv in EVERY_COMMAND:
            if argv[0] in ("doctor",):
                continue
            for extra in ([], ["--json"]):
                code, out, err = run(argv + ["--redact"] + extra)
                self.assertEqual(code, 0, (argv, err))
                self._check(out)

    def test_export_redacted(self):
        path = os.path.join(self.tmp, "r.md")
        code, out, _ = run(["export", "Hiking", "--md", path, "--redact"])
        self.assertEqual(code, 0)
        self._check(out)
        self._check(slurp(path))

    def test_stable_labels(self):
        d = run_json(["read", "Hiking", "--redact"])
        self.assertEqual(d["chat"]["name"], "Group 1")
        senders = [m["sender"] for m in d["messages"]]
        self.assertEqual(senders.count(senders[2]),
                         [m["sender"] for m in run_json(["read", "Hiking"])["messages"]]
                         .count("Tomás Ortega"))
        self.assertTrue(all(s in ("me", "system") or s.startswith("Contact ")
                            for s in senders))
        self.assertTrue(all(m["text"] is None or m["text"].endswith("words]")
                            or m["text"].endswith("word]") for m in d["messages"]))

    def test_global_flag_position(self):
        code, out, _ = run(["--redact", "chats"])
        self.assertEqual(code, 0)
        self._check(out)


class TestSafety(Base):
    def _fingerprint(self):
        fp = {}
        d = os.path.dirname(self.db)
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    fp[name] = (hashlib.sha256(f.read()).hexdigest(),
                                os.stat(p).st_mtime_ns)
        return fp

    def test_original_unchanged(self):
        before = self._fingerprint()
        self.assertIn("ChatStorage.sqlite-wal", before)
        self.assertIn("ChatStorage.sqlite-shm", before)
        self.assertIn("ContactsV2.sqlite", before)
        for argv in EVERY_COMMAND:
            for extra in ([], ["--json"], ["--redact"]):
                run(argv + extra)
                self.assertEqual(before, self._fingerprint(), argv + extra)
        run(["export", "Maya", "--md", os.path.join(self.tmp, "m.md")])
        self.assertEqual(before, self._fingerprint())

    def test_no_network(self):
        def boom(*a, **k):
            raise AssertionError("network used")
        with mock.patch.object(socket, "socket", boom), \
                mock.patch.object(socket, "create_connection", boom), \
                mock.patch.object(socket, "getaddrinfo", boom), \
                mock.patch.object(wa.subprocess, "run") as r:
            for argv in EVERY_COMMAND + [["export", "Maya", "--md",
                                          os.path.join(self.tmp, "n.md")]]:
                for extra in ([], ["--json"], ["--redact"]):
                    code, _, err = run(argv + extra)
                    self.assertEqual(code, 0, (argv, err))
            run(["send", "+15550102030", "hi", "--open"])
            r.assert_called_once()

    def test_snapshot_dir_private_and_removed(self):
        with wa.Snapshot(self.db) as snap:
            mode = stat.S_IMODE(os.stat(snap.dir).st_mode)
            self.assertEqual(mode, 0o700)
            self.assertNotEqual(os.path.dirname(self.db), snap.dir)
            with self.assertRaises(sqlite3.OperationalError) as cm:
                snap.conn.execute("DELETE FROM ZWAMESSAGE")
            self.assertIn("readonly", str(cm.exception))
            d = snap.dir
        self.assertFalse(os.path.exists(d))
        run(["stats"])
        self.assertFalse(os.path.exists(wa._LAST_SNAPSHOT_DIR))

    def test_source_has_no_socket_import(self):
        src = slurp(os.path.join(ROOT, "wa.py"))
        for bad in ("import socket", "urllib.request", "http.client", "requests"):
            self.assertNotIn(bad, src)


class TestCLI(Base):
    def test_subprocess(self):
        env = dict(os.environ)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "wa.py"), "--help"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("unread", r.stdout)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "wa.py"), "stats", "--json"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["messages"], 19)


if __name__ == "__main__":
    unittest.main()
