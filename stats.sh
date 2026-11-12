#!/usr/bin/env bash
# Arcade usage dashboard: fetch the aggregates from theshow-vps and write a self-contained local page.
#
#   ./stats.sh [--days N] [--open]
#
# Writes ~/scratch/arcade-stats/index.html (data embedded, works offline apart from the fonts) and prints the path.
# --open also opens it in the browser. STATS_JSON=file skips ssh and uses that JSON instead (for testing).
set -euo pipefail
cd "$(dirname "$0")"
DAYS=60 OPEN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --days) DAYS=$2; shift ;;
    --open) OPEN=1 ;;
    *) sed -n '2,7p' "$0"; exit 1 ;;
  esac
  shift
done
[[ $DAYS =~ ^[0-9]+$ ]] || { echo "--days wants a number" >&2; exit 1; }
TZNAME=$(timedatectl show -p Timezone --value 2>/dev/null || true)
[ -n "$TZNAME" ] || TZNAME=$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||' || true)
[[ $TZNAME =~ ^[A-Za-z0-9_+/-]+$ ]] || TZNAME=UTC
OUT=${STATS_OUT:-$HOME/scratch/arcade-stats}
mkdir -p "$OUT"
JSON=$(mktemp)
trap 'rm -f "$JSON"' EXIT
if [ -n "${STATS_JSON:-}" ]; then
  cp "$STATS_JSON" "$JSON"
else
  ssh theshow-vps "sudo STATE_DIRECTORY=/var/lib/private/plop-scores python3 /opt/plop-scores/plop_scores.py stats --days $DAYS --tz $TZNAME" > "$JSON"
fi
# embed the JSON in the page; '</' is escaped so a nickname can't close the script tag
python3 -I - "$JSON" stats/dashboard.html "$OUT/index.html" <<'EOF'
import json, sys
data = json.dumps(json.load(open(sys.argv[1])), separators=(",", ":")).replace("</", "<\\/")
page = open(sys.argv[2]).read().replace("/*__DATA__*/null", data, 1)
open(sys.argv[3], "w").write(page)
EOF
echo "$OUT/index.html"
if [ "$OPEN" = 1 ]; then xdg-open "$OUT/index.html" >/dev/null 2>&1 || true; fi
