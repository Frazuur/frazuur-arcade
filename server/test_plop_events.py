#!/usr/bin/env python3
"""Tests for POST /events and the stats aggregation. Run: python3 server/test_plop_events.py"""
import http.client, json, os, sys, tempfile, threading, time, unittest

TMP = tempfile.mkdtemp()
os.environ.update(STATE_DIRECTORY=TMP, PLOP_DEV="1", PLOP_KEY="testkey")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plop_scores as P  # noqa: E402

ORIGIN = "http://localhost:8000"
PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
DESK = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
AID, SID = "0123456789abcdef0123456789abcdef", "fedcba9876543210"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        P.db().close()
        cls.srv = P.ThreadingHTTPServer(("127.0.0.1", 0), P.H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        P.buckets.clear()
        with P.db() as con:
            con.execute("delete from events")

    def post(self, body, origin=ORIGIN, ua=DESK, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        data = raw if raw is not None else json.dumps(body).encode()
        h = {"Content-Type": "text/plain;charset=UTF-8", "User-Agent": ua}
        if origin:
            h["Origin"] = origin
        c.request("POST", "/events", data, h)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, out

    def rows(self):
        with P.db() as con:
            con.row_factory = None
            return con.execute("select type, game, ver, nick, dur, score, round, cat, extra, dev, br, os from events order by id").fetchall()


def batch(*ev, **kw):
    return dict({"k": "testkey", "id": AID, "sid": SID, "ev": list(ev)}, **kw)


class Endpoint(Base):
    def test_saves_valid_batch_with_device_class(self):
        now = int(time.time() * 1000)
        st, out = self.post(batch({"t": "game_start", "g": "plop", "v": "2.1", "n": "Meg", "at": now},
                                  {"t": "game_end", "g": "plop", "v": "2.1", "n": "Meg", "ms": 61000, "s": 120, "r": 3}),
                            ua=PHONE)
        self.assertEqual((st, json.loads(out)), (200, {"ok": 2}))
        r = self.rows()
        self.assertEqual(r[0][:4], ("game_start", "plop", "2.1", "Meg"))
        self.assertEqual(r[0][7], now)
        self.assertEqual(r[1][4:7], (61000, 120, 3))
        self.assertEqual(r[1][9:], ("phone", "safari", "ios"))

    def test_never_stores_ua_or_ip(self):
        self.post(batch({"t": "visit", "g": "site"}))
        with P.db() as con:
            dump = "\n".join(con.iterdump())
        self.assertNotIn("Chrome/120", dump)
        self.assertNotIn("127.0.0.1", dump)

    def test_key_and_origin(self):
        self.assertEqual(self.post(batch({"t": "visit", "g": "site"}, k="nope"))[0], 403)
        self.assertEqual(self.post(batch({"t": "visit", "g": "site"}), origin=None)[0], 403)
        self.assertEqual(self.post(batch({"t": "visit", "g": "site"}), origin="https://evil.example")[0], 403)
        self.assertEqual(self.rows(), [])

    def test_rejects_bad_batches(self):
        ev = {"t": "visit", "g": "site"}
        for b in (batch(*[ev] * 51), batch(), batch(ev, id="NOT-AN-ID!"), batch(ev, sid=5),
                  batch({"t": "hack", "g": "site"}), batch({"t": "visit", "g": "doom"}), batch("x")):
            self.assertEqual(self.post(b)[0], 400, b)
        self.assertEqual(self.post(None, raw=b"not json")[0], 400)
        self.assertEqual(self.post(None, raw=b'{"ev":' + b"1" * 9000 + b"}")[0], 413)
        self.assertEqual(self.rows(), [])

    def test_drops_junk_events_and_clamps(self):
        st, out = self.post(batch({"t": "visit", "g": "site"}, {"t": "zzz", "g": "site"}, 7,
                                  {"t": "game_end", "g": "splat", "v": "<script>", "n": "<b>Bo\x00b</b>" * 5,
                                   "ms": 10 ** 12, "s": -5, "r": "3", "at": 5, "m": "online", "won": True,
                                   "p": 99, "evil": "x", "quit": "yes"}))
        self.assertEqual(json.loads(out), {"ok": 2})
        t, g, ver, nick, dur, score, rnd, cat, extra, *_ = self.rows()[1]
        self.assertEqual((ver, dur, score, rnd, cat), ("", 3 * 3600 * 1000, 0, None, None))
        self.assertLessEqual(len(nick), 16)
        self.assertNotIn("<", nick)
        self.assertEqual(json.loads(extra), {"m": "online", "won": True, "p": 8})

    def test_bots_are_ignored(self):
        st, out = self.post(batch({"t": "visit", "g": "site"}), ua="Googlebot/2.1")
        self.assertEqual((st, json.loads(out)), (200, {"ok": 0}))
        self.assertEqual(self.rows(), [])

    def test_rate_limited(self):
        codes = [self.post(batch({"t": "visit", "g": "site"}))[0] for _ in range(40)]
        self.assertIn(429, codes)

    def test_prunes_old_events(self):
        P.ev_pruned[0] = 0
        with P.db() as con:
            con.execute("insert into events(at, aid, sid, type, game) values(?,?,?,?,?)",
                        (int(time.time()) - 401 * 86400, AID, SID, "visit", "site"))
        self.post(batch({"t": "visit", "g": "site"}))
        self.assertEqual(len(self.rows()), 1)

    def test_daily_caps(self):
        old = P.EV_DAY_ALL, P.EV_DAY_ID
        P.EV_DAY_ALL, P.EV_DAY_ID = 3, 2
        try:
            for _ in range(3):
                P.add_events(batch({"t": "visit", "g": "site"}), PHONE)
            self.assertEqual(len(self.rows()), 2)  # per-id cap
            other = batch({"t": "visit", "g": "site"}, id="b" * 32, sid="c" * 16)
            for _ in range(3):
                P.add_events(other, PHONE)
            self.assertEqual(len(self.rows()), 3)  # global cap
        finally:
            P.EV_DAY_ALL, P.EV_DAY_ID = old


class Device(unittest.TestCase):
    def test_classes(self):
        cases = {
            PHONE: ("phone", "safari", "ios"),
            DESK: ("desktop", "chrome", "linux"),
            "Mozilla/5.0 (iPad; CPU OS 16_0 like Mac OS X) Version/16.0 Safari/604.1": ("tablet", "safari", "ios"),
            "Mozilla/5.0 (Linux; Android 14; SM-X200) Chrome/120.0 Safari/537.36": ("tablet", "chrome", "android"),
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) Chrome/120.0 Mobile Safari/537.36": ("phone", "chrome", "android"),
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36 Edg/120.0": ("desktop", "edge", "windows"),
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.0; rv:120.0) Gecko/20100101 Firefox/120.0": ("desktop", "firefox", "macos"),
            "": ("desktop", "other", "other"),
        }
        for ua, want in cases.items():
            self.assertEqual(P.device(ua), want, ua)


class Stats(Base):
    def test_aggregates(self):
        os.environ["TZ"] = "UTC"
        time.tzset()
        day = 86400
        now = 1_760_000_000 - 1_760_000_000 % day + 12 * 3600  # noon UTC
        a, b = "a" * 32, "b" * 32
        P.add_events(batch({"t": "visit", "g": "site"}, id=a, sid="1" * 16), PHONE, now - 2 * day)
        P.add_events(batch({"t": "visit", "g": "site"}, {"t": "card", "g": "plop"},
                           {"t": "game_start", "g": "plop", "n": "Meg"},
                           {"t": "game_end", "g": "plop", "n": "Meg", "ms": 60000, "s": 90},
                           id=a, sid="2" * 16), PHONE, now)
        P.add_events(batch({"t": "game_start", "g": "splat", "m": "online"},
                           {"t": "game_end", "g": "splat", "ms": 30000, "s": 2, "quit": True},
                           {"t": "game_start", "g": "plop", "n": "Bo"},
                           {"t": "game_end", "g": "plop", "ms": 20000, "s": 40, "n": "Bo"},
                           id=b, sid="3" * 16), DESK, now)
        with P.db() as con:
            s = P.stats(con, now=now, days=7, tz="UTC")
        json.dumps(s)  # must be serialisable
        self.assertEqual(len(s["daily"]), 7)
        last, two_ago = s["daily"][-1], s["daily"][-3]
        self.assertEqual((last["users"], last["new"], last["returning"], last["sessions"], last["plays"]), (2, 1, 1, 2, 3))
        self.assertEqual((two_ago["users"], two_ago["new"]), (1, 1))
        g = {x["game"]: x for x in s["games"]}
        self.assertEqual((g["plop"]["plays"], g["plop"]["players"], g["plop"]["best"], g["plop"]["play_ms"]), (2, 2, 90, 80000))
        self.assertEqual((g["splat"]["quit"], g["splat"]["best"], g["splat"]["modes"]), (1, None, {"online": 1}))
        self.assertEqual(s["totals"]["ids"], 2)
        self.assertEqual(s["totals"]["plays"], 3)
        self.assertEqual(s["devices"]["dev"], {"phone": 1, "desktop": 1})
        wd = time.gmtime(now).tm_wday
        self.assertEqual(s["heat"][wd][12], 4)  # a: visit + plop start, b: two starts (cards and ends don't count)
        pa = next(p for p in s["players"] if p["id"] == a[:8])
        self.assertEqual((pa["nicks"], pa["sessions"], pa["plays"], pa["best"]), (["Meg"], 2, {"plop": 1}, {"plop": 90}))
        self.assertEqual(s["sessions"][0]["player"] in (a[:8], b[:8]), True)
        self.assertEqual(len(s["sessions"]), 3)

    def test_empty(self):
        with P.db() as con:
            s = P.stats(con, days=3)
        self.assertEqual((s["totals"]["ids"], s["players"], len(s["daily"])), (0, [], 3))


if __name__ == "__main__":
    unittest.main(verbosity=1)
