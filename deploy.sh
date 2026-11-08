#!/usr/bin/env bash
# Publish Frazuur Arcade. This private repo is the source; GitHub Pages serves from
# three public repos (Pages needs public repos on the free plan), so this mirrors
# the built files into them and pushes as Frazuur.
#
#   ./deploy.sh site      launcher  -> Frazuur/frazuur.github.io  (https://frazuur.github.io/)
#   ./deploy.sh plop      game      -> Frazuur/plop-catcher       (https://frazuur.github.io/plop-catcher/)
#   ./deploy.sh server    leaderboard + ideas API -> theshow-vps (/opt/plop-scores, plop-scores.service)
#   ./deploy.sh key       copy the local app key (~/.config/frazuur-arcade/plop_key) to the box and restart
#   ./deploy.sh splat     game      -> Frazuur/splat              (https://frazuur.github.io/splat/)
#   ./deploy.sh relay     Splat! relay -> theshow-vps (/opt/splat-relay, splat-relay.service)
#   ./deploy.sh all       all of the above except key
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$PWD
WORK=${DEPLOY_WORK:-$HOME/scratch/frazuur-arcade-deploy}
MSG=${MSG:-"Deploy from frazuur-arcade $(git rev-parse --short HEAD 2>/dev/null || echo local)"}
token() { env -u GH_TOKEN gh auth token -u Frazuur; }

mirror() { # <public repo> <source dir> [files published under server/ ...]
  local repo=$1 src=$2 dir=$WORK/${1#*/}
  mkdir -p "$WORK"
  if [ ! -d "$dir/.git" ]; then git clone -q "https://github.com/$repo.git" "$dir"; fi
  git -C "$dir" pull -q --ff-only
  local stage; stage=$(mktemp -d); trap 'rm -rf "$stage"; trap - RETURN' RETURN
  rsync -a --exclude __pycache__ "$src/" "$stage/"
  if [ $# -gt 2 ]; then mkdir -p "$stage/server"; cp "${@:3}" "$stage/server/"; fi
  rsync -a --delete --exclude .git "$stage/" "$dir/"
  git -C "$dir" add -A
  if git -C "$dir" diff --cached --quiet; then echo "$repo: nothing to publish"; return; fi
  git -C "$dir" -c user.name=Frazuur -c user.email=Frazuur@users.noreply.github.com commit -qm "$MSG"
  git -C "$dir" push -q "https://x-access-token:$(token)@github.com/$repo.git" HEAD:main
  echo "$repo: published (live in about a minute)"
}

API=https://theshow-vps.pleco-rooster.ts.net:8443
# The plain app key lives only in this local file and in /etc/plop-scores.env on the box.
KEY_FILE=${PLOP_KEY_FILE:-$HOME/.config/frazuur-arcade/plop_key}

health() { # GET /scores, with the key header when we have a key (fed on stdin, so it isn't in ps)
  local h=""
  if [ -r "$KEY_FILE" ]; then h="X-Plop-Key: $(tr -d '\r\n' < "$KEY_FILE")"; fi
  printf '%s\n' "$h" | curl -fsS -o /dev/null -H @- "$API/scores"
  echo "server: live"
}

server() {
  python3 -m py_compile server/plop_scores.py
  scp -q server/plop_scores.py server/plop-scores.service theshow-vps:/tmp/
  ssh theshow-vps 'set -e
    sudo install -m 644 /tmp/plop_scores.py /opt/plop-scores/plop_scores.py
    sudo install -m 644 /tmp/plop-scores.service /etc/systemd/system/plop-scores.service
    rm /tmp/plop_scores.py /tmp/plop-scores.service
    [ -e /etc/plop-scores.env ] || sudo install -m 600 -o root -g root /dev/null /etc/plop-scores.env
    sudo systemctl daemon-reload
    sudo systemctl restart plop-scores
    sleep 1
    systemctl is-active plop-scores'
  health
}

key() { # copy the local key into /etc/plop-scores.env on the box, restart, check
  if [ ! -s "$KEY_FILE" ]; then echo "No key at $KEY_FILE" >&2; exit 1; fi
  printf 'PLOP_KEY=%s\n' "$(tr -d '\r\n' < "$KEY_FILE")" | ssh theshow-vps 'set -e
    sudo sh -c "umask 077; cat > /etc/plop-scores.env.new"
    sudo chown root:root /etc/plop-scores.env.new
    sudo chmod 600 /etc/plop-scores.env.new
    sudo mv /etc/plop-scores.env.new /etc/plop-scores.env
    sudo systemctl restart plop-scores
    sleep 1
    systemctl is-active plop-scores'
  health
}

plop() { # the public game repo gets only the leaderboard server files, not the relay
  mirror Frazuur/plop-catcher "$ROOT/games/plop-catcher" "$ROOT/server/plop_scores.py" "$ROOT/server/plop-scores.service"
}

relay() {
  python3 -m py_compile server/splat_relay.py
  scp -q server/splat_relay.py server/splat-relay.service theshow-vps:/tmp/
  ssh theshow-vps 'sudo install -D -m 644 /tmp/splat_relay.py /opt/splat-relay/splat_relay.py && sudo install -m 644 /tmp/splat-relay.service /etc/systemd/system/splat-relay.service && rm /tmp/splat_relay.py /tmp/splat-relay.service && sudo systemctl daemon-reload && sudo systemctl enable -q splat-relay && sudo systemctl restart splat-relay && sleep 1 && systemctl is-active splat-relay'
  curl -fsS -o /dev/null https://theshow-vps.pleco-rooster.ts.net:8443/splat/health && echo "relay: live"
}

case "${1:-}" in
  site) mirror Frazuur/frazuur.github.io "$ROOT/site" ;;
  plop) plop ;;
  server) server ;;
  key) key ;;
  splat) mirror Frazuur/splat "$ROOT/games/splat" ;;
  relay) relay ;;
  all) server; relay; mirror Frazuur/splat "$ROOT/games/splat"; plop; mirror Frazuur/frazuur.github.io "$ROOT/site" ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
