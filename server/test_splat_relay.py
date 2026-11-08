#!/usr/bin/env python3
"""Stdlib tests for splat_relay.py. Run: python3 server/test_splat_relay.py"""
import asyncio
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WS:
    """Tiny WebSocket client (masked frames, text only)."""

    @classmethod
    async def open(cls, port, path="/ws", origin=None):
        self = cls()
        self.r, self.w = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n")
        if origin:
            req += f"Origin: {origin}\r\n"
        self.w.write((req + "\r\n").encode())
        head = await asyncio.wait_for(self.r.readuntil(b"\r\n\r\n"), 3)
        self.status = int(head.split(b" ")[1])
        return self

    def frame(self, op, payload, fin=True):
        n = len(payload)
        b1 = (0x80 if fin else 0) | op
        if n < 126:
            head = struct.pack("!BB", b1, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", b1, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", b1, 0x80 | 127, n)
        mask = os.urandom(4)
        self.w.write(head + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(payload)))

    def send(self, obj):
        self.frame(0x1, json.dumps(obj).encode())

    async def recv_frame(self, timeout=2):
        async def rd():
            b1, b2 = await self.r.readexactly(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", await self.r.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", await self.r.readexactly(8))[0]
            return b1 & 0x0F, await self.r.readexactly(n)
        return await asyncio.wait_for(rd(), timeout)

    async def recv(self, timeout=2):
        while True:
            op, data = await self.recv_frame(timeout)
            if op == 0x1:
                return json.loads(data)
            if op == 0x8:
                return {"t": "_close", "code": struct.unpack("!H", data[:2])[0] if data else None}

    async def recv_type(self, t, timeout=2):
        while True:
            m = await self.recv(timeout)
            if m["t"] == t:
                return m

    async def quiet(self, timeout=0.3):
        try:
            m = await self.recv(timeout)
            return m
        except asyncio.TimeoutError:
            return None

    def kill(self):
        self.w.transport.abort()


class RelayTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.proc = subprocess.Popen([sys.executable, os.path.join(HERE, "splat_relay.py")],
                                    env={**os.environ, "PORT": str(cls.port)},
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        out, _ = cls.proc.communicate(timeout=5)
        if "Traceback" in out.decode():
            print(out.decode())

    async def ws(self, **kw):
        c = await WS.open(self.port, **kw)
        self.assertEqual(c.status, 101)
        return c

    async def room(self, n):
        host = await self.ws()
        host.send({"t": "create", "name": "Host", "color": "#ff0000"})
        w = await host.recv_type("welcome")
        peers = [(host, w)]
        for i in range(n - 1):
            c = await self.ws()
            c.send({"t": "join", "room": w["room"], "name": f"P{i}", "color": "#00ff00"})
            peers.append((c, await c.recv_type("welcome")))
        for c, _ in peers:  # drain peer_join noise
            while await c.quiet(0.1):
                pass
        return w["room"], peers

    async def test_create_join_welcome(self):
        host = await self.ws()
        host.send({"t": "create", "name": "  Dino<>! ", "color": "#AABBCC"})
        w = await host.recv()
        self.assertEqual(w["t"], "welcome")
        self.assertRegex(w["room"], r"^[BCDFGHJKLMNPQRSTVWXZ]{4}$")
        self.assertEqual(w["you"], 1)
        self.assertEqual(w["host"], 1)
        self.assertEqual(w["peers"], [{"id": 1, "name": "Dino!", "color": "#aabbcc", "on": True}])
        j = await self.ws()
        j.send({"t": "join", "room": w["room"].lower(), "name": "Zoë 7", "color": "#000000"})
        w2 = await j.recv()
        self.assertEqual((w2["you"], w2["host"], len(w2["peers"])), (2, 1, 2))
        pj = await host.recv()
        self.assertEqual(pj, {"t": "peer_join",
                              "peer": {"id": 2, "name": "Zoë 7", "color": "#000000", "on": True}})

    async def test_welcome_flags_dropped_seat_and_comes_first(self):
        code, [(a, wa), (b, _)] = await self.room(2)
        a.kill()  # host drops; b becomes host
        self.assertEqual(await b.recv(), {"t": "peer_leave", "id": 1, "gone": False})
        self.assertEqual(await b.recv(), {"t": "host", "id": 2})
        b.kill()  # nobody connected; host id stays 2
        await asyncio.sleep(0.2)
        a2 = await self.ws()
        a2.send({"t": "join", "room": code, "name": "Host", "color": "#ff0000", "token": wa["token"]})
        w = await a2.recv()
        self.assertEqual((w["t"], w["you"], w["host"]), ("welcome", 1, 1))
        self.assertEqual({p["id"]: p["on"] for p in w["peers"]}, {1: True, 2: False})
        self.assertIsNone(await a2.quiet())

    async def test_fragment_flood_closed(self):
        x = await self.ws()
        x.frame(0x1, b"", fin=False)
        for _ in range(16):  # the 17th frame of one message is over the cap
            x.frame(0x0, b"", fin=False)
        self.assertEqual(await x.recv(), {"t": "_close", "code": 1009})

    async def test_join_guessing_closed(self):
        x = await self.ws()
        for _ in range(10):
            x.send({"t": "join", "room": "ZZZZ", "name": "A", "color": "#123456"})
        for _ in range(10):
            self.assertEqual(await x.recv(), {"t": "error", "code": "no_room"})
        self.assertEqual(await x.recv(), {"t": "_close", "code": 1008})

    async def test_create_limit(self):
        x = await self.ws()
        for _ in range(5):
            x.send({"t": "create", "name": "A", "color": "#123456"})
            self.assertEqual((await x.recv())["t"], "welcome")
            x.send({"t": "leave"})
        x.send({"t": "create", "name": "A", "color": "#123456"})
        self.assertEqual(await x.recv(), {"t": "error", "code": "full"})

    async def test_broadcast_and_direct(self):
        _, [(a, _), (b, _), (c, _)] = await self.room(3)
        a.send({"t": "send", "to": None, "d": {"x": 1}})
        self.assertEqual(await b.recv(), {"t": "msg", "from": 1, "d": {"x": 1}})
        self.assertEqual(await c.recv(), {"t": "msg", "from": 1, "d": {"x": 1}})
        self.assertIsNone(await a.quiet())
        b.send({"t": "send", "to": 3, "d": [2]})
        self.assertEqual(await c.recv(), {"t": "msg", "from": 2, "d": [2]})
        self.assertIsNone(await a.quiet())

    async def test_full_room(self):
        code, _ = await self.room(6)
        x = await self.ws()
        x.send({"t": "join", "room": code, "name": "Seven", "color": "#123456"})
        self.assertEqual(await x.recv(), {"t": "error", "code": "full"})

    async def test_no_room(self):
        x = await self.ws()
        x.send({"t": "join", "room": "ZZZZ", "name": "A", "color": "#123456"})
        self.assertEqual(await x.recv(), {"t": "error", "code": "no_room"})

    async def test_locked_and_rejoin(self):
        code, [(a, _), (b, wb)] = await self.room(2)
        b.send({"t": "lock", "locked": True})  # not host
        self.assertEqual(await b.recv(), {"t": "error", "code": "bad"})
        a.send({"t": "lock", "locked": True})
        await asyncio.sleep(0.1)
        x = await self.ws()
        x.send({"t": "join", "room": code, "name": "Late", "color": "#123456"})
        self.assertEqual(await x.recv(), {"t": "error", "code": "locked"})
        b.kill()
        self.assertEqual(await a.recv(), {"t": "peer_leave", "id": 2, "gone": False})
        b2 = await self.ws()
        b2.send({"t": "join", "room": code, "name": "B", "color": "#00ff00", "token": wb["token"]})
        w = await b2.recv()
        self.assertEqual((w["t"], w["you"], w["host"]), ("welcome", 2, 1))
        self.assertEqual((await a.recv())["peer"]["id"], 2)

    async def test_host_migration(self):
        _, [(a, _), (b, _), (c, _)] = await self.room(3)
        a.kill()
        self.assertEqual(await b.recv(), {"t": "peer_leave", "id": 1, "gone": False})
        self.assertEqual(await b.recv(), {"t": "host", "id": 2})
        await c.recv_type("host")
        c.send({"t": "leave"})
        self.assertEqual(await b.recv(), {"t": "peer_leave", "id": 3, "gone": True})

    async def test_oversized_dropped(self):
        _, [(a, _), (b, _)] = await self.room(2)
        a.send({"t": "send", "to": None, "d": "x" * 5000})
        self.assertIsNone(await b.quiet())
        a.send({"t": "send", "to": None, "d": "ok"})
        self.assertEqual((await b.recv())["d"], "ok")

    async def test_fragmented(self):
        _, [(a, _), (b, _)] = await self.room(2)
        raw = json.dumps({"t": "send", "to": None, "d": "frag"}).encode()
        a.frame(0x1, raw[:5], fin=False)
        a.frame(0x9, b"hi")  # control frame between fragments
        a.frame(0x0, raw[5:])
        op, data = await a.recv_frame()
        self.assertEqual((op, data), (0xA, b"hi"))
        self.assertEqual((await b.recv())["d"], "frag")

    async def test_rate_limit(self):
        _, [(a, _), (b, _)] = await self.room(2)
        for i in range(150):
            a.send({"t": "send", "to": None, "d": i})
        self.assertEqual(await a.recv_type("error"), {"t": "error", "code": "rate"})
        got = 0
        while await b.quiet(0.3):
            got += 1
        self.assertLess(got, 120)
        self.assertGreaterEqual(got, 79)

    async def test_bad_json_keeps_socket(self):
        x = await self.ws()
        x.frame(0x1, b"{not json")
        self.assertEqual(await x.recv(), {"t": "error", "code": "bad"})
        x.send({"t": "create", "name": "A", "color": "#123456"})
        self.assertEqual((await x.recv())["t"], "welcome")

    async def test_binary_rejected(self):
        x = await self.ws()
        x.frame(0x2, b"\x00\x01")
        self.assertEqual(await x.recv(), {"t": "_close", "code": 1003})

    async def test_origin(self):
        self.assertEqual((await WS.open(self.port, origin="https://evil.example")).status, 403)
        await self.ws(origin="https://frazuur.github.io")
        await self.ws(origin="http://localhost:5173", path="/splat/ws")

    def test_health(self):
        for p in ("/health", "/splat/health"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{p}", timeout=3) as r:
                d = json.load(r)
            self.assertEqual(set(d), {"rooms", "peers"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
