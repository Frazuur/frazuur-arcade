#!/usr/bin/env python3
"""Splat! multiplayer relay: rooms of up to 6 phones, a dumb JSON message switch.

Python stdlib only. Speaks HTTP/1.1 + RFC 6455 WebSocket itself. Contract:
games/splat/PROTOCOL.md. Nothing is stored on disk; logs go to stdout.
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import struct
import time

PORT = int(os.environ.get("PORT", "8788"))
HOST = "127.0.0.1"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

MAX_FRAME = 4096          # max message payload (bytes)
MAX_DISCARD = 1 << 16     # frames bigger than this close the socket (1009)
RATE = 40.0               # frames per second, sustained
BURST = 80.0
ABUSE_LIMIT = 200         # dropped frames (decaying) before close 1008
ROOM_SIZE = 6
MAX_ROOMS = 300
MAX_SOCKETS = 2000
SEAT_HOLD = 60.0          # seconds a dropped seat is kept
EMPTY_ROOM = 60.0         # seconds a room with nobody connected lives
PING_EVERY = 20.0
SILENT_DROP = 60.0
SEND_QUEUE = 256          # queued outgoing frames before a slow client is dropped
WRITE_TIMEOUT = 10.0
CODE_LETTERS = "BCDFGHJKLMNPQRSTVWXZ"
NAME_BAD = re.compile(r"[^A-Za-z0-9 .'!?\-]")
COLOR_OK = re.compile(r"^#[0-9a-fA-F]{6}$")


def log(*a):
    print(*a, flush=True)


def allowed_origin(origin):
    if origin is None:
        return True
    if origin == "https://frazuur.github.io":
        return True
    return bool(re.fullmatch(r"http://(127\.0\.0\.1|localhost)(:\d{1,5})?", origin))


def clean_name(v):
    if not isinstance(v, str):
        return "Player"
    v = NAME_BAD.sub("", v).strip()[:12].strip()
    return v or "Player"


def clean_color(v):
    return v.lower() if isinstance(v, str) and COLOR_OK.match(v) else "#888888"


def encode_frame(opcode, payload=b""):
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + payload


class Closed(Exception):
    pass


class Seat:
    def __init__(self, sid, name, color):
        self.id = sid
        self.name = name
        self.color = color
        self.token = secrets.token_urlsafe(16)
        self.client = None
        self.connected_at = 0.0
        self.dropped_at = 0.0

    def info(self):
        return {"id": self.id, "name": self.name, "color": self.color}


class Room:
    def __init__(self, code):
        self.code = code
        self.seats = {}
        self.next_id = 1
        self.host = None
        self.locked = False
        self.empty_since = None

    def connected(self):
        return [s for s in self.seats.values() if s.client is not None]

    def broadcast(self, obj, skip=None):
        data = json.dumps(obj, separators=(",", ":"))
        for s in self.connected():
            if s is not skip:
                s.client.send_text(data)


class Relay:
    def __init__(self):
        self.rooms = {}
        self.clients = set()

    # ---------- rooms ----------
    def new_code(self):
        while True:
            code = "".join(secrets.choice(CODE_LETTERS) for _ in range(4))
            if code not in self.rooms:
                return code

    def elect_host(self, room):
        """Pick the longest-connected peer if the current host is gone; announce it."""
        cur = room.seats.get(room.host)
        if cur is not None and cur.client is not None:
            return
        live = room.connected()
        if not live:
            return  # keep the old host id; re-elected when someone comes back
        new = min(live, key=lambda s: (s.connected_at, s.id))
        if new.id != room.host:
            room.host = new.id
            room.broadcast({"t": "host", "id": new.id})

    def seat_client(self, room, seat, client):
        seat.client = client
        seat.connected_at = time.monotonic()
        client.room, client.seat = room, seat
        room.empty_since = None

    def welcome(self, room, seat):
        seat.client.send({"t": "welcome", "room": room.code, "you": seat.id, "token": seat.token,
                          "host": room.host, "peers": [s.info() for s in room.seats.values()]})

    def do_create(self, client, m):
        if len(self.rooms) >= MAX_ROOMS:
            return client.error("full")
        room = Room(self.new_code())
        self.rooms[room.code] = room
        seat = Seat(room.next_id, clean_name(m.get("name")), clean_color(m.get("color")))
        room.next_id += 1
        room.seats[seat.id] = seat
        room.host = seat.id
        self.seat_client(room, seat, client)
        log(f"room {room.code} created ({len(self.rooms)} rooms)")
        self.welcome(room, seat)

    def do_join(self, client, m):
        code = m.get("room")
        room = self.rooms.get(code.strip().upper()) if isinstance(code, str) else None
        if room is None:
            return client.error("no_room")
        token = m.get("token")
        seat = None
        if isinstance(token, str) and token:
            seat = next((s for s in room.seats.values()
                         if secrets.compare_digest(s.token, token)), None)
        if seat is not None:  # rejoin
            old = seat.client
            if old is not None and old is not client:
                old.room = old.seat = None
                old.close(1000)
            seat.name = clean_name(m.get("name")) if "name" in m else seat.name
            seat.color = clean_color(m.get("color")) if "color" in m else seat.color
            self.seat_client(room, seat, client)
        else:
            if room.locked:
                return client.error("locked")
            if len(room.seats) >= ROOM_SIZE:
                return client.error("full")
            seat = Seat(room.next_id, clean_name(m.get("name")), clean_color(m.get("color")))
            room.next_id += 1
            room.seats[seat.id] = seat
            self.seat_client(room, seat, client)
        self.elect_host(room)
        self.welcome(room, seat)
        room.broadcast({"t": "peer_join", "peer": seat.info()}, skip=seat)

    def detach(self, client, gone):
        """Socket left its room: gone=True removes the seat, else holds it for rejoin."""
        room, seat = client.room, client.seat
        client.room = client.seat = None
        if room is None or seat is None or seat.client is not client:
            return
        seat.client = None
        seat.dropped_at = time.monotonic()
        if gone:
            room.seats.pop(seat.id, None)
        room.broadcast({"t": "peer_leave", "id": seat.id, "gone": gone})
        if not room.connected():
            room.empty_since = time.monotonic()
        if not room.seats:
            self.delete_room(room)
        else:
            self.elect_host(room)

    def delete_room(self, room):
        if self.rooms.pop(room.code, None) is not None:
            log(f"room {room.code} deleted ({len(self.rooms)} rooms)")

    def handle(self, client, m):
        t = m.get("t")
        if t == "create" or t == "join":
            if client.room is not None:
                return client.error("bad")
            return self.do_create(client, m) if t == "create" else self.do_join(client, m)
        room, seat = client.room, client.seat
        if room is None:
            return client.error("bad")
        if t == "send":
            if "d" not in m:
                return client.error("bad")
            to = m.get("to")
            data = json.dumps({"t": "msg", "from": seat.id, "d": m["d"]}, separators=(",", ":"))
            if to is None:
                for s in room.connected():
                    if s is not seat:
                        s.client.send_text(data)
            elif isinstance(to, int) and not isinstance(to, bool):
                target = room.seats.get(to)
                if target is not None and target.client is not None:
                    target.client.send_text(data)
            else:
                client.error("bad")
        elif t == "leave":
            self.detach(client, gone=True)
        elif t == "lock":
            if room.host != seat.id or not isinstance(m.get("locked"), bool):
                return client.error("bad")
            room.locked = m["locked"]
        else:
            client.error("bad")

    async def janitor(self):
        while True:
            await asyncio.sleep(1.0)
            try:
                now = time.monotonic()
                for room in list(self.rooms.values()):
                    for s in list(room.seats.values()):
                        if s.client is None and now - s.dropped_at >= SEAT_HOLD:
                            room.seats.pop(s.id, None)
                            room.broadcast({"t": "peer_leave", "id": s.id, "gone": True})
                    if not room.seats or (room.empty_since is not None
                                          and now - room.empty_since >= EMPTY_ROOM):
                        self.delete_room(room)
                for c in list(self.clients):
                    if now - c.last_rx >= SILENT_DROP:
                        c.close(1001)
                    elif now - c.last_ping >= PING_EVERY:
                        c.last_ping = now
                        c.queue_raw(encode_frame(0x9, b"p"))
            except Exception as e:  # never let the janitor die
                log(f"janitor error: {e!r}")

    # ---------- connections ----------
    async def on_connect(self, reader, writer):
        try:
            await self.serve(reader, writer)
        except (Closed, ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        except Exception as e:
            log(f"connection error: {e!r}")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def serve(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except asyncio.LimitOverrunError:
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            return
        method, path = parts[0], parts[1].split("?", 1)[0]
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if path.startswith("/splat/"):
            path = path[len("/splat"):]
        if method == "GET" and path == "/health":
            peers = sum(len(r.connected()) for r in self.rooms.values())
            return await self.http(writer, 200, json.dumps({"rooms": len(self.rooms), "peers": peers}))
        if path != "/ws":
            return await self.http(writer, 404, '{"error":"not found"}')
        if (method != "GET" or "websocket" not in headers.get("upgrade", "").lower()
                or "sec-websocket-key" not in headers):
            return await self.http(writer, 400, '{"error":"websocket only"}')
        if not allowed_origin(headers.get("origin")):
            return await self.http(writer, 403, '{"error":"origin"}')
        if len(self.clients) >= MAX_SOCKETS:
            return await self.http(writer, 503, '{"error":"busy"}')
        accept = base64.b64encode(hashlib.sha1(
            (headers["sec-websocket-key"] + GUID).encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
        client = Client(self, reader, writer)
        self.clients.add(client)
        try:
            await client.run()
        finally:
            self.clients.discard(client)
            self.detach(client, gone=False)
            await client.shutdown()

    async def http(self, writer, code, body):
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                  503: "Service Unavailable"}[code]
        b = body.encode()
        writer.write(f"HTTP/1.1 {code} {reason}\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(b)}\r\nCache-Control: no-store\r\n"
                     f"Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n".encode() + b)
        await asyncio.wait_for(writer.drain(), 5)


class Client:
    def __init__(self, relay, reader, writer):
        self.relay = relay
        self.reader = reader
        self.writer = writer
        self.room = None
        self.seat = None
        self.queue = asyncio.Queue(SEND_QUEUE)
        self.closing = False
        self.last_rx = self.last_ping = time.monotonic()
        self.tokens = BURST
        self.tok_at = time.monotonic()
        self.abuse = 0.0
        self.rate_told = 0.0
        self.writer_task = asyncio.ensure_future(self.write_loop())

    # ----- outgoing -----
    def queue_raw(self, frame):
        if self.closing:
            return
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:  # slow client: drop it
            self.closing = True
            self.writer_task.cancel()
            self.writer.transport.abort()

    def send_text(self, s):
        self.queue_raw(encode_frame(0x1, s.encode()))

    def send(self, obj):
        self.send_text(json.dumps(obj, separators=(",", ":")))

    def error(self, code):
        self.send({"t": "error", "code": code})

    def close(self, code=1000):
        if self.closing:
            return
        self.queue_raw(encode_frame(0x8, struct.pack("!H", code)))
        self.queue_raw(None)
        self.closing = True

    async def write_loop(self):
        try:
            while True:
                frame = await self.queue.get()
                if frame is None:
                    break
                self.writer.write(frame)
                await asyncio.wait_for(self.writer.drain(), WRITE_TIMEOUT)
            self.closing = True
            self.writer.close()  # flushes what is buffered, then closes
            return
        except (asyncio.CancelledError, Exception):
            pass
        self.closing = True
        try:
            self.writer.transport.abort()
        except Exception:
            pass

    async def shutdown(self):
        """Let queued frames (e.g. a close frame) flush for up to 2 s, then drop the socket."""
        self.closing = True
        if self.writer_task.done():
            return
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            self.writer_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(self.writer_task), 2)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            self.writer_task.cancel()

    # ----- incoming -----
    def allow(self):
        now = time.monotonic()
        self.tokens = min(BURST, self.tokens + (now - self.tok_at) * RATE)
        self.abuse = max(0.0, self.abuse - (now - self.tok_at) * RATE / 4)
        self.tok_at = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        self.abuse += 1
        if self.abuse >= ABUSE_LIMIT:
            self.close(1008)
        elif now - self.rate_told >= 1.0:
            self.rate_told = now
            self.error("rate")
        return False

    async def read_frame(self):
        b1, b2 = await self.reader.readexactly(2)
        fin, op = b1 & 0x80, b1 & 0x0F
        if b1 & 0x70 or not b2 & 0x80:  # reserved bits, or unmasked client frame
            raise ProtocolErr(1002)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", await self.reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", await self.reader.readexactly(8))[0]
        if op >= 0x8 and (n > 125 or not fin):
            raise ProtocolErr(1002)
        if n > MAX_DISCARD:
            raise ProtocolErr(1009)
        mask = await self.reader.readexactly(4)
        data = await self.reader.readexactly(n)
        if n:
            data = bytes(b ^ mask[i & 3] for i, b in enumerate(data)) if n <= MAX_FRAME else b""
        return bool(fin), op, data, n

    async def run(self):
        msg, msg_len, msg_op = [], 0, None
        try:
            while not self.closing:
                fin, op, data, n = await self.read_frame()
                self.last_rx = time.monotonic()
                if op == 0x8:
                    self.close(struct.unpack("!H", data[:2])[0] if len(data) >= 2 and
                               struct.unpack("!H", data[:2])[0] in (1000, 1001) else 1000)
                    return
                if op == 0x9:
                    self.queue_raw(encode_frame(0xA, data))
                    continue
                if op == 0xA:
                    continue
                if op == 0x2 or (msg_op == 0x2 and op == 0x0):
                    raise ProtocolErr(1003)
                if op == 0x1:
                    if msg_op is not None:
                        raise ProtocolErr(1002)
                    msg, msg_len, msg_op = [], 0, 0x1
                elif op == 0x0:
                    if msg_op is None:
                        raise ProtocolErr(1002)
                else:
                    raise ProtocolErr(1002)
                msg_len += n
                if msg_len <= MAX_FRAME:
                    msg.append(data)
                if not fin:
                    if msg_len > MAX_DISCARD:
                        raise ProtocolErr(1009)
                    continue
                payload, total = b"".join(msg), msg_len
                msg, msg_len, msg_op = [], 0, None
                if not self.allow() or total > MAX_FRAME:
                    continue  # over the rate or size limit: drop the frame
                self.on_text(payload)
        except ProtocolErr as e:
            self.close(e.code)

    def on_text(self, payload):
        try:
            m = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self.error("bad")
        if not isinstance(m, dict):
            return self.error("bad")
        try:
            self.relay.handle(self, m)
        except Exception as e:
            log(f"handler error: {e!r}")
            self.error("bad")


class ProtocolErr(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


async def main(port=PORT):
    relay = Relay()
    server = await asyncio.start_server(relay.on_connect, HOST, port, limit=8192)
    asyncio.ensure_future(relay.janitor())
    log(f"splat relay on {HOST}:{port}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
