#!/usr/bin/env bash
# Publish Frazuur Arcade. This private repo is the source; GitHub Pages serves from
# two public repos (Pages needs public repos on the free plan), so this mirrors
# the built files into them and pushes as Frazuur.
#
#   ./deploy.sh site      launcher  -> Frazuur/frazuur.github.io  (https://frazuur.github.io/)
#   ./deploy.sh plop      game      -> Frazuur/plop-catcher       (https://frazuur.github.io/plop-catcher/)
#   ./deploy.sh server    leaderboard + ideas API -> theshow-vps (/opt/plop-scores, plop-scores.service)
#   ./deploy.sh splat     game      -> Frazuur/splat              (https://frazuur.github.io/splat/)
#   ./deploy.sh relay     Splat! relay -> theshow-vps (/opt/splat-relay, splat-relay.service)
#   ./deploy.sh all       all of the above
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$PWD
WORK=${DEPLOY_WORK:-$HOME/scratch/frazuur-arcade-deploy}
MSG=${MSG:-"Deploy from frazuur-arcade $(git rev-parse --short HEAD 2>/dev/null || echo local)"}
token() { env -u GH_TOKEN gh auth token -u Frazuur; }

mirror() { # <public repo> <source dir> [server dir, published as server/]
  local repo=$1 src=$2 dir=$WORK/${1#*/}
  mkdir -p "$WORK"
  if [ ! -d "$dir/.git" ]; then git clone -q "https://github.com/$repo.git" "$dir"; fi
  git -C "$dir" pull -q --ff-only
  local stage; stage=$(mktemp -d); trap 'rm -rf "$stage"' RETURN
  rsync -a --exclude __pycache__ "$src/" "$stage/"
  if [ -n "${3:-}" ]; then rsync -a --exclude __pycache__ "$3/" "$stage/server/"; fi
  rsync -a --delete --exclude .git "$stage/" "$dir/"
  git -C "$dir" add -A
  if git -C "$dir" diff --cached --quiet; then echo "$repo: nothing to publish"; return; fi
  git -C "$dir" -c user.name=Frazuur -c user.email=Frazuur@users.noreply.github.com commit -qm "$MSG"
  git -C "$dir" push -q "https://x-access-token:$(token)@github.com/$repo.git" HEAD:main
  echo "$repo: published (live in about a minute)"
}

server() {
  python3 -m py_compile server/plop_scores.py
  scp -q server/plop_scores.py theshow-vps:/tmp/plop_scores.py
  ssh theshow-vps 'sudo install -m 644 /tmp/plop_scores.py /opt/plop-scores/plop_scores.py && rm /tmp/plop_scores.py && sudo systemctl restart plop-scores && sleep 1 && systemctl is-active plop-scores'
  curl -fsS -o /dev/null https://theshow-vps.pleco-rooster.ts.net:8443/scores && echo "server: live"
}

relay() {
  python3 -m py_compile server/splat_relay.py
  scp -q server/splat_relay.py server/splat-relay.service theshow-vps:/tmp/
  ssh theshow-vps 'sudo install -D -m 644 /tmp/splat_relay.py /opt/splat-relay/splat_relay.py && sudo install -m 644 /tmp/splat-relay.service /etc/systemd/system/splat-relay.service && rm /tmp/splat_relay.py /tmp/splat-relay.service && sudo systemctl daemon-reload && sudo systemctl enable -q splat-relay && sudo systemctl restart splat-relay && sleep 1 && systemctl is-active splat-relay'
  curl -fsS -o /dev/null https://theshow-vps.pleco-rooster.ts.net:8443/splat/health && echo "relay: live"
}

case "${1:-}" in
  site) mirror Frazuur/frazuur.github.io "$ROOT/site" ;;
  plop) mirror Frazuur/plop-catcher "$ROOT/games/plop-catcher" "$ROOT/server" ;;
  server) server ;;
  splat) mirror Frazuur/splat "$ROOT/games/splat" ;;
  relay) relay ;;
  all) server; relay; mirror Frazuur/splat "$ROOT/games/splat"; mirror Frazuur/plop-catcher "$ROOT/games/plop-catcher" "$ROOT/server"; mirror Frazuur/frazuur.github.io "$ROOT/site" ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
