# Splat! relay protocol

The relay (`server/splat_relay.py`) is a dumb message switch for rooms of up to 6
phones. It knows nothing about the game. One phone in each room is the **host**: it
runs the real simulation (all kids, bots, food, hits) and broadcasts snapshots. The
other phones send their inputs to the host and render what the host sends back.

## Connection

- Production: `wss://theshow-vps.pleco-rooster.ts.net:8443/splat/ws`
  (Tailscale Funnel path `/splat` proxies to `127.0.0.1:8788`).
- Local testing: `ws://127.0.0.1:8788/ws` (`PORT=8788 python3 server/splat_relay.py`).
  The game accepts `?relay=<url>` to override.
- The relay serves the same routes with and without the `/splat` prefix
  (`/ws`, `/splat/ws`, `/health`, `/splat/health`), whatever Funnel forwards.
- Text frames only, each one JSON object with a `t` field. Max 4 KB per frame,
  max 40 frames per second per client (sustained; bursts up to 80). Over the limit:
  the frame is dropped; repeated abuse closes the socket (code 1008).
- `GET /health` -> `{"rooms": n, "peers": n}`.

## Messages

Client -> relay:

| t        | fields                                   | meaning |
|----------|------------------------------------------|---------|
| `create` | `name`, `color`                          | make a room; you are host |
| `join`   | `room`, `name`, `color`, `token?`        | join by code; `token` from an earlier `welcome` rejoins with the same id |
| `send`   | `to` (peer id or `null`), `d` (any JSON) | relay `d` to one peer, or to everyone else when `to` is null |
| `leave`  |                                          | leave the room cleanly |
| `lock`   | `locked` (bool)                          | host only: locked rooms refuse new joins (rejoins with a token still work) |

Relay -> client:

| t           | fields                                              | meaning |
|-------------|-----------------------------------------------------|---------|
| `welcome`   | `room`, `you`, `token`, `host`, `peers`: [{`id`,`name`,`color`,`on`}] | after create or join; `peers` includes you and seats held for dropped players (`on:false`) |
| `peer_join` | `peer`: {`id`,`name`,`color`,`on`}                  | someone joined (or rejoined); `on` is always `true` |
| `peer_leave`| `id`, `gone` (bool)                                 | socket dropped (`gone:false`, may rejoin within 60 s) or left for good (`gone:true`) |
| `host`      | `id`                                                | the host changed (old host left or dropped); the new host is the longest-connected remaining peer |
| `msg`       | `from`, `d`                                         | a relayed `send` |
| `error`     | `code`: `no_room` \| `full` \| `locked` \| `bad` \| `rate` | request refused |

- Room codes are 4 capital letters from `BCDFGHJKLMNPQRSTVWXZ` (no vowels, so no
  words), unique among live rooms.
- Peer ids are small integers unique within a room (1..). Names: up to 12 chars,
  trimmed, any Unicode letters/digits (so "Zoë" is fine) plus space and `.'!?-`;
  other characters are removed, empty becomes "Player". Colour: `#rrggbb` (else grey).
- A rejoiner gets `welcome` (with the current `host`) before anything else; other
  peers get `host` (if it changed) and then `peer_join`.
- A socket is closed (1008) after 10 refused `create`/`join` attempts, and may create
  at most 5 rooms. Fragmented messages may have at most 16 frames; every frame,
  pings included, counts toward the rate limit. Slow readers are dropped once ~96 KB
  is queued for them.
- A dropped socket keeps its seat (id, name, colour) for 60 s for rejoin. A room with
  no connected peers is deleted after 60 s. At most 300 rooms and 2000 sockets.
- The relay sends WebSocket pings every 20 s and drops sockets silent for 60 s.

## Game-level payloads (inside `d`, defined by the game, not the relay)

Documented in `games/splat/index.html` next to the net code.
