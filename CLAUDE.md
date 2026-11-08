# Frazuur Arcade

Dean's silly browser games for friends and family. Private source repo
`Frazuur/frazuur-arcade`; push with the Frazuur GitHub account
(`GH_TOKEN=$(env -u GH_TOKEN gh auth token -u Frazuur)`, commits as
`Frazuur <Frazuur@users.noreply.github.com>`), never wellnotedhq.

## Layout

- `site/`: the launcher at https://frazuur.github.io/ (game cards, live top 3,
  the game ideas form, and the "Meggy's Arcade" pink theme you get by tapping
  the title).
- `games/plop-catcher/`: Plop Catcher, one self-contained `index.html` (canvas,
  WebAudio, no build step) plus `version.json`. Live at
  https://frazuur.github.io/plop-catcher/.
- `server/`: `plop_scores.py`, a Python stdlib + SQLite API on theshow-vps
  (`plop-scores.service`, 127.0.0.1:8787, public through Tailscale Funnel at
  https://theshow-vps.pleco-rooster.ts.net:8443). `GET/POST /scores` is the
  leaderboard; `POST /ideas` saves game ideas (never served back; read them
  with `ssh theshow-vps 'sudo STATE_DIRECTORY=/var/lib/private/plop-scores python3 /opt/plop-scores/plop_scores.py ideas'`).

## Deploying

GitHub Pages serves from two public repos (free plan), so `./deploy.sh`
mirrors this repo into them: `./deploy.sh site|plop|server|all`. Commit here
first, then deploy. Deploying is part of finishing a change Dean asked for.

The API wants an app key (`X-Plop-Key`) once `PLOP_KEY` is set in
`/etc/plop-scores.env` on the box. The plain key lives only in
`~/.config/frazuur-arcade/plop_key` (mode 600, override with `PLOP_KEY_FILE`)
and on the box, never in the repo; pages carry it XOR-obfuscated. Rollout
order: `./deploy.sh server` (no key yet, so old pages keep working), then
`./deploy.sh plop` and `./deploy.sh site`, then `./deploy.sh key`.
`./deploy.sh server` also installs `plop-scores.service` and reloads systemd.

## Rules for game changes

- Every Plop Catcher release bumps `const VERSION` in `index.html`, adds an
  entry at the top of the in-game changelog (`<details id="ver">`, short and
  plain), and sets the same version in `version.json`. Open pages notice the
  new `version.json` and offer a reload button.
- The server rejects scores above 15 points per catch (`score > catches*15`)
  and faster than 250 ms per catch. If scoring changes, keep within that or
  change the server check in the same change and deploy the server first.
- Keep it fair: the spawn code guarantees every poo is reachable and a hazard
  never forces you to miss a poo. Don't break those checks.
- Works on phones (finger drag, 390x844) and desktop. Test headless with
  Playwright using `/usr/bin/chromium` (`executablePath`), never by opening
  windows on Dean's screens. `node --check` the extracted script.
- UI work loads the Impeccable skill and passes `impeccable detect`.
- Use original art drawn in canvas, no emoji sprites, no copyrighted
  characters (for example no actual Hello Kitty).
- Don't put Dean's email or personal details in requests or shipped code.

## theshow-vps

The leaderboard service and Funnel are noted in `~/dev/theshow-tracker/CLAUDE.md`
and the secwatch watchlist. Don't touch other services on that box.
