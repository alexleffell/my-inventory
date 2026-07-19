#!/bin/bash
# Restore a local backup back UP to the live VPS (inverse of backup.sh).
# Pushes data/, captures/ and models/ from a chosen backup dir to the server.
# The VPS is normally the source of truth, so this is a recovery tool — use it
# only to roll the server back to a known-good local backup.
#
#   export VPS_HOST=1.2.3.4 [VPS_USER=root] [VPS_PATH=/opt/my-inventory]
#   export BACKUP_DEST="$HOME/my-inventory-backups"   # optional
#   ./scripts/restore.sh [SOURCE_DIR]
#
# SOURCE_DIR is a backup-* dir or the `latest` mirror. Defaults to
# "$BACKUP_DEST/latest". It must contain data/ and captures/ (models/ optional).
#
# SAFETY: never pushes data/users.json or data/*.bak — those are server-owned
# secrets/state and must not be clobbered by a restore. Requires typing `yes`.
set -euo pipefail

: "${VPS_HOST:?set VPS_HOST (your server IP/hostname)}"
VPS="${VPS_USER:-root}@${VPS_HOST}"
VPSPATH="${VPS_PATH:-/opt/my-inventory}"
DEST="${BACKUP_DEST:-$HOME/my-inventory-backups}"
SRC="${1:-$DEST/latest}"

[ -d "$SRC/data" ]     || { echo "error: $SRC/data not found — is $SRC a valid backup?" >&2; exit 1; }
[ -d "$SRC/captures" ] || { echo "error: $SRC/captures not found — is $SRC a valid backup?" >&2; exit 1; }

echo "About to PUSH this local backup UP to the LIVE server:"
echo "  source : $SRC"
echo "  target : $VPS:$VPSPATH"
echo "  dirs   : data/ (excluding users.json + *.bak), captures/, models/"
echo
printf "This overwrites live server data. Type 'yes' to proceed: "
read -r reply
[ "$reply" = "yes" ] || { echo "aborted."; exit 1; }

# data/: never overwrite server-owned users.json or *.bak snapshots.
rsync -az -e "ssh -o BatchMode=yes" \
  --exclude 'users.json' --exclude '*.bak' \
  "$SRC/data/" "$VPS:$VPSPATH/data/"
rsync -az -e "ssh -o BatchMode=yes" "$SRC/captures/" "$VPS:$VPSPATH/captures/"
if [ -d "$SRC/models" ]; then
  rsync -az -e "ssh -o BatchMode=yes" "$SRC/models/" "$VPS:$VPSPATH/models/"
fi

echo "$(date '+%Y-%m-%d %H:%M:%S')  restored $SRC -> $VPS:$VPSPATH"
echo "Note: restart the container if needed:  ssh $VPS 'cd $VPSPATH && docker compose restart'"
