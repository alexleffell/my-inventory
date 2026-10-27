#!/usr/bin/env python3
"""Print unprocessed uploads on the VPS (run with cwd = app root).

For the later remote setup (Raspberry Pi / rented server) only. When the server
runs on the laptop, the agent reads GET /api/queue instead ("Tag the queue" in
CLAUDE.md). Sidecars carry a container id; this script never needs it.

Two kinds, both need Claude to identify them:
  - review-*.json    : item added on the dashboard ("Add item") with photos
  - cap-*.json        : item confirmed on the capture page (files not yet logged)
Used by deploy/vps-bridge.sh to decide when to wake Claude.

Default output: the marker/sidecar paths (one per line) — the wake signal.
With --png: the image basenames of those unprocessed items (one per line), so the
bridge can pull ONLY the new photos via rsync --files-from instead of re-scanning
the whole captures dir (which grows with the inventory).
"""
import json
import glob
import sys

inv = json.load(open("data/inventory.json"))
logged = set(img for i in inv.get("items", []) for img in (i.get("images") or []))
try:
    pend = json.load(open("data/pending.json"))
    logged |= set(img for p in pend.get("pending", []) for img in p.get("candidate", {}).get("images", []))
except Exception:
    pass

hits = []   # marker/sidecar paths (wake signal)
pngs = []   # image basenames of unprocessed items (for a targeted pull)

for f in glob.glob("captures/review-*.json"):
    hits.append(f)
    try:
        pngs += json.load(open(f)).get("images", [])
    except Exception:
        pass

for f in glob.glob("captures/cap-*.json"):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    files = d.get("files") or ([d["file"]] if d.get("file") else [])
    if files and not any(x in logged for x in files):
        hits.append(f)
        pngs += files

if "--png" in sys.argv:
    print("\n".join(sorted(set(pngs))))
else:
    print("\n".join(hits))
