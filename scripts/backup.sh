#!/bin/bash
# Backup the LIVE (VPS) inventory data + photos to a local folder.
# The VPS is the source of truth, so we pull from it. Keeps the last N
# backups, read-only, plus a zip each run. Schedule it (e.g. cron/launchd).
#   export VPS_HOST=1.2.3.4 [VPS_USER=root] [VPS_PATH=/opt/my-inventory]
#   export BACKUP_DEST="$HOME/my-inventory-backups"   # optional
set -euo pipefail

: "${VPS_HOST:?set VPS_HOST (your server IP/hostname)}"
VPS="${VPS_USER:-root}@${VPS_HOST}"
VPSPATH="${VPS_PATH:-/opt/my-inventory}"
DEST="${BACKUP_DEST:-$HOME/my-inventory-backups}"
KEEP=48                       # ~4 days at one backup every 2 hours
TS="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/backup-$TS"

# `latest` mode: fast in-place mirror of the live VPS data, refreshed after each
# item Claude processes. No timestamped folder, no zip, no prune, stays writable
# so rsync can overwrite it. The 2-hourly LaunchAgent still runs the full
# timestamped+zip rotation (default mode), so this can't evict that history.
if [ "${1:-}" = "latest" ]; then
  LATEST="$DEST/latest"
  mkdir -p "$LATEST/data" "$LATEST/captures" "$LATEST/models"
  rsync -az --delete -e "ssh -o BatchMode=yes" "$VPS:$VPSPATH/data/"     "$LATEST/data/"
  rsync -az          -e "ssh -o BatchMode=yes" "$VPS:$VPSPATH/captures/" "$LATEST/captures/"
  rsync -az          -e "ssh -o BatchMode=yes" "$VPS:$VPSPATH/models/"   "$LATEST/models/" 2>/dev/null || true
  echo "$(date '+%Y-%m-%d %H:%M:%S')  refreshed live mirror -> $LATEST"
  exit 0
fi

mkdir -p "$OUT/data" "$OUT/captures" "$OUT/models"
rsync -az -e "ssh -o BatchMode=yes" "$VPS:$VPSPATH/data/"     "$OUT/data/"
rsync -az -e "ssh -o BatchMode=yes" "$VPS:$VPSPATH/captures/" "$OUT/captures/"
rsync -az -e "ssh -o BatchMode=yes" "$VPS:$VPSPATH/models/"   "$OUT/models/" 2>/dev/null || true

# single zip alongside, for easy off-machine copying
( cd "$OUT" && zip -qr "../inventory-$TS.zip" . ) || true

# read-only (protect against accidental deletion/edits)
chmod -R a-w "$OUT" 2>/dev/null || true

# prune old backups, keeping the most recent $KEEP
ls -1dt "$DEST"/backup-* 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r d; do chmod -R u+w "$d" 2>/dev/null || true; rm -rf "$d"; done
ls -1t "$DEST"/inventory-*.zip 2>/dev/null | tail -n +$((KEEP + 1)) | xargs rm -f 2>/dev/null || true

echo "$(date '+%Y-%m-%d %H:%M:%S')  backed up VPS -> $OUT"
