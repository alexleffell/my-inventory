# my-inventory

A photo-driven inventory of small parts (electronics components, hardware, craft
supplies, anything you keep in labelled bags/bins), with a live web dashboard you
can share read-only and edit after logging in. Items are captured as photos,
optionally identified by an LLM agent from the photo, and tracked in a JSON file
rendered on a dashboard. Python standard library only. No Node, no pip, no DB.

> **Keep this file updated.** Whenever the architecture, data model, workflow, or
> conventions below change, update CLAUDE.md in the same change. It is the source
> of truth for how the project fits together.

## Run

```bash
python3 server.py        # serves on http://localhost:8770 (stdlib only, no deps)
# host/port configurable: INVENTORY_HOST=127.0.0.1 INVENTORY_PORT=8770 python3 server.py
```

Routes: `/` and `/dashboard` → the dashboard (public read-only landing page; edit
controls + a 📷 Capture link appear after admin login). `/capture` → the capture
page (camera tool). `/buy` (alias `/wishlist`) → the "To buy" page (purchase
requests). Requires only Python 3 (tested on 3.9+). No build step.

## Data safety

The inventory can take real effort to build. Treat `data/`, `captures/` and
`models/` as precious:
- `save_inv`/`save_pending` write **atomically** (temp file + rename) and keep a
  one-step `data/*.json.bak`. A write that would wipe a non-empty inventory to
  zero items drops a timestamped `*.emptied-*.bak` first.
- `scripts/backup.sh` pulls `data/` + `captures/` + `models/` from the live server to a local
  folder (timestamped + zip, read-only, pruned). Schedule it (cron/launchd).
  `scripts/backup.sh latest` does a fast in-place mirror (no timestamp/zip/prune).
  Configure with `VPS_HOST` (+ optional `VPS_USER`, `VPS_PATH`, `BACKUP_DEST`).
- Rules for any agent/script: back up before destructive ops; never `rm -rf`
  `data`/`captures`; never `git checkout`/`git clean` over them without a backup.
  `data/users.json`, `data/*.bak`, `data/pending.json`, `data/wishlist.json` are
  gitignored (secrets / local state).

## Architecture

```
server.py            # tiny stdlib HTTP server (ThreadingHTTPServer) on :8770
mcp_server.py        # optional MCP server (stdlib): read/query, request_purchase,
                     # attach_item_model / get_item_model (3D models)
public/
  capture.html       # camera capture (multi-photo per item) + live inventory panel
  dashboard.html     # editable dashboard (edit modal w/ inline camera, lightbox gallery)
  wishlist.html      # the "To buy" page
data/
  inventory.json     # the inventory: { "items": [ ... ] }
  wishlist.json      # purchase requests ("to buy"): { "wishlist": [ ... ] }  (gitignored)
models/
  model-<ms>-<name>.<ext>  # 3D model files attached to items (stl/3mf/obj/glb/
                     # gltf/step/stp/scad); gitignored, bind-mounted in Docker
captures/
  cap-<ms>.png       # raw captured frames (the photo for each item)
  cap-<ms>.json      # one sidecar per item on confirmation: {files:[...], quantity, note, location}
  review-<ms>.json   # transient marker: photos added to an EXISTING item (enrich trigger)
deploy/              # Dockerfile/compose, Caddy + systemd units, optional agent bridge
```

### Capture → log pipeline (multiple photos per item)
1. Browser opens `/capture`, uses `getUserMedia` to show a camera (e.g. an iPhone
   via macOS Continuity Camera, or any webcam).
2. User presses **Capture** one or more times → each frame is POSTed to
   `/api/capture` → saved as `captures/cap-<ms>.png` and added to the page's
   "current item" photo strip. Several photos build up ONE item.
3. User sets quantity/location and clicks **Add to inventory** → POST `/api/confirm`
   with `{files: [...], ...}` writes ONE sidecar `captures/<first-file-stem>.json`.
4. (Optional) An LLM agent watches for new sidecars, reads the photos, identifies
   the part, and appends an item with an `images` array to `data/inventory.json`.
   Without an agent, items are saved with whatever name/notes the user typed.

The sidecar (not the raw photo) is the trigger, so identification only happens once
the user has confirmed. To find unprocessed captures: list `captures/*.json` whose
`files` are not already in any item's `images` in `inventory.json`. (Legacy sidecars
may have a single `file` field instead of `files`.)

### Adding photos to an existing item (re-examine / enrich)
The dashboard edit modal has an inline camera ("Add photo") and a removable photo
strip. Saving an edit sends the full `images` array via `POST /api/update`. When the
new list contains photos the item didn't have before, the server writes a
`captures/review-<ms>.json` marker `{id, images, reason}`. An agent can watch for
these to enrich the item (fill specs/SKU from a label, resolve an `unconfirmed`
value), then delete the marker. Two trigger types share the `captures/*.json`
watcher: `cap-*.json` (new item) and `review-*.json` (enrich existing); dispatch by
filename prefix.

### Optional: remote server + agent bridge
Captures need a camera (usually a laptop/phone). The app can also run on a remote
server as a shared read-only mirror. Items added on the remote create a nameless
item + `review-*.json` there. `deploy/vps-bridge.sh` (run on the camera machine)
polls the remote via `deploy/find_unprocessed.py`, pulls the new **photos only**,
and lets a local agent identify them and write back. `deploy/sync.sh` does a
two-way merge (unions inventory/pending by `id`, copies photos both ways). All of
these read `VPS_HOST` / `VPS_USER` / `VPS_PATH` from the environment. Nothing is
hardcoded.

## API (all POST take/return JSON)

| Endpoint             | Purpose                                                     |
|----------------------|-------------------------------------------------------------|
| `GET /api/inventory` | Returns `inventory.json` (full).                            |
| `GET /api/summary`   | Public overview: totals + category/location breakdown + wishlist counts. |
| `GET /api/query`     | Public search/filter: `?search=&category=&location=&limit=`.|
| `GET /api/queue`     | Public: uploads awaiting identification (a "⏳ Identifying…" strip). |
| `POST /api/capture`  | `{image: dataURL}` → saves PNG, returns `{file}`.           |
| `POST /api/confirm`  | `{files:[...], quantity, note, location}` → writes one sidecar for the item. |
| `POST /api/ingest`   | Upsert an item by `id` (an agent's identified items). No review marker. |
| `POST /api/add`      | Create an item (dashboard manual add); server assigns `id`. |
| `POST /api/update`   | `{id, ...fields}` → merge fields. New `images` ⇒ a `review-*.json` marker. |
| `POST /api/delete`   | `{id}` → remove the item.                                   |
| `GET /api/pending`   | Returns pending duplicate decisions (`pending.json`).       |
| `POST /api/pending`  | `{matches:[...], candidate}` → queue a duplicate for the user. |
| `POST /api/resolve`  | `{pending_id, action}` → apply add/set/separate/dismiss from the UI. |
| `POST /api/model`    | **Admin**: attach a 3D model to an item: `{id, filename, data:base64}` → saves `models/model-<ms>-<name>.<ext>`, appends to the item's `models`. |
| `POST /api/model/delete` | **Admin**: `{id, file}` → detach from the item and delete the file. |
| `GET /models/<f>`    | Public download of a model file (Content-Disposition: attachment). |
| `GET /api/wishlist`  | Public: purchase requests (the "to buy" list).              |
| `POST /api/wishlist` | **PUBLIC create** of a request (so a no-creds agent can ask for a part). |
| `POST /api/wishlist/resolve` | **Admin**: manage a request (status/edit/delete).   |
| `GET /captures/<f>`  | Serves a captured image.                                    |

`/api/ingest` (admin auth) is the safe way for an agent to write identified items.
Prefer it over editing `inventory.json` directly, which races with the server.

## Data model (`inventory.json` item)

```json
{
  "id": "cap-<ms>",            // stable id; for captured items = first image filename stem
  "name": "Active 5V Buzzer 3kHz",
  "category": "Audio",
  "quantity": 5,
  "location": "Bag 1",         // "<Bag|Bin> <number>"
  "condition": "New (sealed bag)",
  "notes": "SKU #106611. ...",
  "images": ["cap-<ms>.png", ...], // one or more photos; omitted/empty for manual adds
  "models": ["model-<ms>-holder.stl", ...]  // optional 3D models (files in models/)
}
```

## 3D models on items

An item can carry 3D model files (a printable holder/case, or the part's own CAD
model): allowed types `stl 3mf obj glb gltf step stp scad`. Files live in
`models/` (gitignored, bind-mounted, backed up). Upload via `POST /api/model`
(admin) with base64 data; download publicly via `GET /models/<file>`; detach via
`POST /api/model/delete`. The item's `models` array mirrors `images`.
**MCP tools**: `attach_item_model` (uploads a local file; needs
`INVENTORY_ADMIN_USER`/`INVENTORY_ADMIN_PASS` in the MCP server env) and
`get_item_model` (downloads an item's models to a local dir, default a temp dir).

## Duplicate handling (resolved in the WEB APP, never the terminal)

When an item is identified, check whether it already exists (match by normalized
name / SKU). If it does, do NOT silently create a second entry and do NOT ask in
chat. **Post a pending decision** so the user resolves it in the browser:

```
POST /api/pending { matches:[<every similar item id>], candidate:{ name, category,
                    quantity, location, condition, notes, images:[...] } }
```

Both pages poll `GET /api/pending` and render a rich duplicate card with editable
name/quantity/bag, one row per matching stash (Add qty / Set qty), Create-new, and
Skip. Resolving (`POST /api/resolve`) attaches photos to the chosen stash and removes
the pending record. Model: one entry per (item, bag) stash. Pending records live in
`data/pending.json` (transient).

## Wishlist / purchase requests ("to buy")

A separate list of parts to **buy and add**, distinct from the inventory. The point:
an agent working through the MCP can ask for a component a project needs but that
isn't in stock.

- Stored in `data/wishlist.json` = `{ "wishlist": [ ... ] }`. `status` ∈
  `requested | ordered | bought`. `part` is the only required field.
- **Creating a request is PUBLIC** (`POST /api/wishlist`) on purpose, so an agent
  needs no credentials. Append-only, low-stakes. **Managing** (status/edit/delete via
  `POST /api/wishlist/resolve`) is admin-only.
- **MCP tools** (`mcp_server.py`): `request_purchase` and `list_wishlist`.
- **Page `/buy`** (`public/wishlist.html`): public read-only with a status badge;
  admin gets a + Request form, per-row status dropdown, and delete.
- **Closing the loop:** in the Add-item modal, typing a name that matches an open
  request shows a one-click ✓ Mark bought. An identify routine should do the same:
  flip a matched request to `bought` via `POST /api/wishlist/resolve`.

## Conventions

- **Name is optional.** This is photo-driven: an item can be saved with photos and
  no name (a blank-name item with photos can trigger an enrich/identify step). The
  UI shows "⏳ Identifying…" until a name exists. Only block a save with neither a
  name nor a photo.
- **Quantity comes from the label, not a guess.** On the capture page a blank Qty
  means "read the printed pack count off the label". A filled value is an explicit
  override. Never invent a count.
- **Location format** is `"<Bag|Bin> <number>"` (e.g. `Bag 1`, `Bin 3`). Both pages
  share a **last-used location** in `localStorage['lastLocation']`.
- **Image order: the part comes first.** The FIRST image in `images` must clearly
  show the actual physical part (it's the card thumbnail). Packaging/label/barcode
  shots go AFTER.
- **IDs**: captured items use the first photo's filename stem (`cap-<ms>`); manual
  adds get `item-<ms>` from the server.
- **Uncertain data** is flagged with the word `unconfirmed` in `name`/`notes`, which
  the dashboard surfaces as a "check" badge.
- **Both the agent and the dashboard write `inventory.json`.** Re-read before editing
  directly (it may have changed via the UI), or go through the API.

## Authentication: public viewing, log in to edit

**Viewing is public:** all GET endpoints are open. **Editing requires admin login:**
every POST is gated to `admin`. The dashboard has a Log in / Log out button; once
logged in, edit controls appear. Credentials are checked via `POST /api/login` and
sent as an `Authorization: Basic` header on writes, persisted in `localStorage`
(`authHeader`).

The gate is **active only once an admin account exists.** While `data/users.json` is
absent/empty, auth is DISABLED and writes are open (so local dev needs no login).
Create the first admin via the in-browser **Set up admin** flow (`/api/setup`, works
only while no users exist), the **👥 Users** modal (admin), or the CLI:

```bash
python3 server.py adduser <name> admin     # prompts for password (hidden)
python3 server.py users                     # list users / show auth state
python3 server.py deluser <name>
```

Passwords are stored salted-SHA256 in `data/users.json` (gitignored, never
synced/committed). Basic Auth is base64 (not encryption), so use HTTPS for real use
(see `deploy/Caddyfile`).

> ⚠️ **Testing auth locally:** `server.py` hardcodes `DATA = ROOT/data`. Hitting
> `/api/setup` or `/api/users` against a local server writes the REAL
> `data/users.json`. Test against a throwaway copy of the repo if you don't want a
> local admin (which flips local dev out of "open" mode).

## Deployment

The app is a single stdlib server, so deployment is just "run `server.py` behind a
TLS-terminating proxy." `deploy/` has two ready paths:

- **Docker + reverse proxy**: `Dockerfile` + `docker-compose.yml` (the compose file
  carries Traefik v1 labels; edit `inventory.example.com` to your domain).
  `deploy/docker-entrypoint.sh` seeds the admin account from `ADMIN_USER` /
  `ADMIN_PASS` (put them in `deploy/admin.env`; copy from `deploy/admin.env.example`,
  gitignored).
- **systemd + Caddy**: `deploy/inventory.service` (binds `127.0.0.1:8770`, hardened
  writes) + `deploy/Caddyfile` (auto-HTTPS via Let's Encrypt). `deploy/deploy.sh`
  rsyncs code + data and restarts the service.

Both read `VPS_HOST` / `VPS_USER` / `VPS_PATH` from the environment. Data persists
via the `./data` + `./captures` bind mounts (Docker) or in `WorkingDirectory`
(systemd). `deploy/deploy.sh` and `deploy/sync*.sh` never overwrite the server's
`users.json` or `*.bak` files.
