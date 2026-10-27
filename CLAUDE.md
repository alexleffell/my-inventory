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
controls + a 📷 Capture link appear after admin login). `/i/<id>` → the same
dashboard with that item's detail modal open (stable public permalink; the
dashboard also pushStates here when you open an item). `/capture` → the capture
page (camera tool). `/buy` (alias `/wishlist`) → the "To buy" page (purchase
requests). Requires only Python 3 (tested on 3.9+). No build step.

HTML pages are served with `Last-Modified` + `Cache-Control: no-store`, and the
server answers `HEAD` for the page routes. The dashboard polls `HEAD` on itself
every 10s and self-reloads when `Last-Modified` changes (skipped while an edit
modal is open) — deploys show up in open tabs without a manual refresh.

The design system (shared across all three pages): black/orange palette as
semantic CSS custom properties in `:root` (surfaces `--bg/--panel*/--line*`,
text `--text/--dim/--dim2/--faint`, accent families
`--orange/--orange2/--ember/--orangefg/--orangebg/--orangeline` +
`--on-orange` for text on orange fills and `--amber/--amberfg/--amberbg/
--amberline`, status `--okfg/--warnfg/--redfg` + `*bg`/`*line` tints,
`--focus`, radii `--r-ctl/--r-card/--r-modal`, shadows `--shadow-1/2`).
Orange owns actions/selection/brand; amber owns the physical storage layer
(location chips, storage map); green/red/yellow are status-only. Every page carries the 2px `body::before` ember strip. New UI
must use these tokens, not ad-hoc hex. The dashboard has a left sidebar
(brand, stats, page nav, category filter via `activeCat`/`renderSideCats()`,
admin actions) that collapses to a drawer under 920px, and an item detail
modal (`openDetail(id)`) opened by clicking a card or visiting `/i/<id>`.
Each item's name is an `<a href="/i/<id>">`; opening the modal pushStates
that path, closing it returns to the list (`/` or `/dashboard`). The address
bar also keeps the dashboard view: `q` (name, id, category, location, notes),
`cat`, `loc`, and `sort`. Typing in the search box replaces the current
history entry; changing a filter, the sort, or which item is open pushes one.
Back and forward restore the box, the filters, and whether the detail modal
is open. An exact item-id query shows that one item even when the category
or location filter would hide it. Copy link copies the bare `/i/<id>`
permalink. Dialogs use
`role="dialog"` + background `inert` via `showOverlay()/hideOverlay()`
(stacked focus restore) — route any new overlay through those helpers.

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
- `scripts/restore.sh [SOURCE_DIR]` is the inverse: pushes a local backup's
  `data/`/`captures/`/`models/` back UP to the VPS (recovery only). It **excludes
  `users.json` and `*.bak`** and requires typing `yes`. Defaults SOURCE_DIR to
  `$BACKUP_DEST/latest`.
- Rules for any agent/script: back up before destructive ops; never `rm -rf`
  `data`/`captures`; never `git checkout`/`git clean` over them without a backup.
  `data/inventory.json`, `data/containers.json`, `captures/*`, `data/users.json`,
  `data/*.bak`, `data/pending.json`, `data/wishlist.json` are gitignored: the
  inventory and photos are personal data and never go into a commit. The repo
  ships empty; tests run against `tests/fixtures/` in a throwaway copy.

## Architecture

```
server.py            # tiny stdlib HTTP server (ThreadingHTTPServer) on :8770
mcp_server.py        # optional MCP server (stdlib): list/detail reads, request_purchase,
                     # attach_item_model / get_item_model (3D models)
public/
  capture.html       # camera capture (multi-photo per item) + live inventory panel
  dashboard.html     # editable dashboard: sidebar (nav/stats/category filter), item detail
                     # modal, edit modal w/ inline camera, lightbox gallery, 3D model viewer
                     # modal and per-item model attach/view/delete
  wishlist.html      # the "To buy" page
  vendor/o3dv/       # vendored Online3DViewer (MIT) + occt-import-js WASM for the 3D
                     # viewer. Served at /vendor/ (stdlib static route). CDN-free.
data/
  inventory.json     # the inventory: { "schema": 2, "items": [ ... ] }
  containers.json    # zones + containers (where items live): { "zones": [...], "containers": [...] }
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

### Adding photos to an existing item (re-examine / enrich / identify)
The dashboard edit modal has an inline camera ("Add photo") and a removable photo
strip. Saving an edit sends the full `images` array via `POST /api/update`. When the
new list contains photos the item didn't have before, the server writes a
`captures/review-<ms>.json` marker `{id, images, reason}`. The ✨ Identify button
(`POST /api/identify`) writes the same marker with reason
`identify requested from dashboard`. An agent watches for these to enrich or
re-identify the item (fill specs/SKU from a label, resolve an `unconfirmed`
value), then deletes the marker. Two trigger types share the `captures/*.json`
watcher: `cap-*.json` (new item) and `review-*.json` (enrich/identify existing);
dispatch by filename prefix. The `reason` field says why the marker exists.

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
| `GET /openapi.json`  | Public OpenAPI 3.1 description of the public API, for third-party LLM tool layers (ChatGPT Actions, LangChain, n8n). See "Third-party agents" below. |
| `GET /api/summary`   | Public overview: totals + category / container-path / zone breakdown + wishlist counts. |
| `GET /api/containers` | Public: `{zones, containers}`; each container with `path`, `effective_zone`, `zone_name`, `item_count`, `children`. |
| `POST /api/containers` | **Admin**: `{action: create\|update\|delete, id?, name, type, parent, zone}`. Rejects cycles and a zone on a nested container; delete is 409 while anything is inside. |
| `POST /api/zones`    | **Admin**: `{action: create\|rename\|delete, id?, name}`. |
| `GET /api/items`     | Public. No args: one compact summary row per item for the WHOLE inventory (`id, name, category, quantity, container, location` path, `images` count, `models`, `undelivered`) via the pure `item_summary()` helper. `?ids=a,b,c`: the full records for those ids, in the order asked, plus a `missing` list. This is the agent entry point — there is no search endpoint (see "Agent access contract"). |
| `GET /api/queue`     | Public: uploads awaiting identification + items with an outstanding `review-*.json` marker (a "⏳ Identifying…" strip). |
| `GET /api/identify/status` | Public: reports `{available: true, mode: "watcher"}` — identification runs via the watcher/bridge, no server-side API key. |
| `POST /api/identify` | **Admin**: queue an item for (re-)identification. Writes a `captures/review-*.json` marker (reason `identify requested from dashboard`) for the watcher; dedupes if one is already pending. Never calls an LLM API itself. |
| `POST /api/capture`  | `{image: dataURL}` → saves PNG, returns `{file}`.           |
| `POST /api/confirm`  | `{files:[...], quantity, note, location}` → writes one sidecar for the item. |
| `POST /api/ingest`   | Upsert an item by `id` (an agent's identified items). No review marker. |
| `POST /api/add`      | Create an item (dashboard manual add); server assigns `id`. |
| `POST /api/update`   | `{id, ...fields}` → merge fields. New `images` ⇒ a `review-*.json` marker. Pass `stock` to re-split the item across locations. |
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

**Identify button (watcher-based).** The ✨ Identify button in an admin's item
detail modal does NOT call an LLM from the server. `POST /api/identify` drops a
`captures/review-*.json` marker (reason `identify requested from dashboard`),
i.e. the same trigger `deploy/find_unprocessed.py` + `deploy/vps-bridge.sh`
already watch. The watching agent pulls the photos, identifies the part, writes
the result back via `/api/update` (appending a dated change note), checks
duplicates via `/api/pending`, and deletes the marker. While the marker exists
the item shows in the "⏳ Identifying…" queue strip and the button reads
"Identification queued"; the open detail modal updates in place when the agent
writes the result. No `ANTHROPIC_API_KEY` or model env vars are needed.

### Agent access contract: summaries first, then details

**There is no search endpoint and no search MCP tool, deliberately.** Ranked
keyword search made agents (ours and third-party ones) conclude a part was
absent when it was in stock under another name, and it hid the rest of the
inventory behind whatever words the agent happened to try.

The inventory is a few hundred items, so the whole list fits in one response:

1. `GET /api/items` (MCP `list_all_items`) — every item, one compact row each.
   The agent scans that list itself.
2. `GET /api/items?ids=a,b,c` (MCP `get_items`) — full records (notes, SKUs,
   specs, photo filenames) for the handful that matter.
3. `GET /api/inventory` (MCP `list_all_items` with `full: true`) — everything in
   full, when that is genuinely what you want.
4. MCP `get_item_images` takes ids from step 1 when a name is not enough and the
   part has to be seen.

`GET /api/summary` stays as the tiny "what do I have overall" call. If a step-2
lookup ever gets reintroduced as fuzzy matching, this is the failure it brings
back.

### Third-party agents (`/openapi.json`)

Claude reaches the inventory through `mcp_server.py`. Everything else (ChatGPT
Actions / custom GPTs, LangChain, n8n, OpenAI Assistants) speaks OpenAPI, so the
server publishes an OpenAPI 3.1 document at `GET /openapi.json`, generated by
`openapi_spec(base_url)` in `server.py`. Point a GPT Action at the URL, auth
"None", and it works with no glue code.

It describes ONLY the unauthenticated surface: `GET /api/summary`, `/api/items`,
`/api/inventory`, `/api/containers`, `/api/queue`, `/api/wishlist`, and `POST /api/wishlist`. Admin
endpoints stay out on purpose. The `servers[0].url` is derived per-request from
`X-Forwarded-Proto`/`X-Forwarded-Host` (falling back to `Host`), so the same code
serves the right base URL on localhost and behind Traefik.

The descriptions in the spec are written FOR a model to read (they say to pull
`/api/items` once and scan it rather than guess keywords, that details come from
a second `?ids=` call, and that `POST /api/wishlist` is the way to ask for a
missing part). Keep them that way; they are the whole reason the document is hand-written rather
than derived.

`tests/test_openapi.py` is the anti-drift gate: it parses the route table out of
`server.py`, decides which routes are public (a block calling `_require` or
sitting after the blanket admin gate in `_do_post` is not), and fails if a public
`/api/` route is neither documented nor listed in `OPENAPI_INTERNAL`. Adding a
route therefore forces a deliberate choice. It also rejects documented-but-
nonexistent paths, duplicate `operationId`s, and operations missing a
summary/description.

## Data model

`inventory.json` is `{"schema": 2, "items": [...]}`. `containers.json` holds the
places items live in:

```json
{
  "zones": [{"id": "z-4f7k2q", "name": "Electronics bench"}],
  "containers": [
    {"id": "c-k3x9qa", "name": "Red cabinet", "type": "cabinet", "parent": null, "zone": "z-4f7k2q"},
    {"id": "c-p2m8rt", "name": "Drawer 3", "type": "drawer", "parent": "c-k3x9qa", "zone": null}
  ]
}
```

**Containers and zones.** A zone is a named work area. A container is a physical
place (`type` is one of `CONTAINER_TYPES` in `server.py`: cabinet, drawer,
small-parts drawer, divided box, bin, bag, tray, shelf, wall/pegboard, rack, case,
other). Containers nest through `parent`. Only a TOP-LEVEL container stores a
`zone`; a nested one inherits its root's zone (`container_zone()`), and giving a
container a parent clears its own zone, so a drawer never sits in a different zone
from its cabinet. The display path is derived: `Red cabinet › Drawer 3`
(`container_path()`). Ids are `c-`/`z-` + 6 random chars, short so QR labels stay
small. A container cannot be deleted while an item, a stock row, a child container
or a queued capture references it (409). Deleting a zone just unassigns its
containers.

**Item:**

```json
{
  "id": "cap-<ms>",            // stable id; for captured items = first image filename stem
  "name": "Active 5V Buzzer 3kHz",
  "category": "Audio",
  "quantity": 5,               // TOTAL units, across every place
  "container": "c-k3x9qa",     // where it is; the primary place when split; absent = unfiled
  "stock": [                   // optional: only when split across 2+ places
    {"container": "c-k3x9qa", "quantity": 3},
    {"container": "c-p2m8rt", "quantity": 2}
  ],
  "condition": "New (sealed bag)",
  "notes": "SKU #106611. ...",
  "images": ["cap-<ms>.jpg", ...], // one or more photos; omitted/empty for manual adds
  "models": ["model-<ms>-holder.stl", ...],  // optional 3D models (files in models/)
  "undelivered": true          // optional: ordered but not delivered yet (see below)
}
```

**`location` is derived, never stored.** Every read (`/api/inventory`,
`/api/items`, `/api/queue`, `/api/pending`) adds `"location": "<container path>"`
to each item and each stock row (`with_places()`), so readers that predate
containers (the MCP server, OpenAPI agents) keep working, and renaming a container
never leaves stale strings. Writes take `container` ids. A legacy `location`
string in a write payload is resolved by `resolve_places()`: a container whose
path matches (ignoring case) is used, else a new top-level container with that
name is created. When both are sent, `container` wins. An unknown container id
is a 400.

**Schema migration.** `migrate_to_v2()` runs at startup when `inventory.json` has
no `schema` (or `schema < 2`). It copies `inventory.json` / `pending.json` to
`data/*.pre-v2-<ts>.bak`, turns each distinct location string into a top-level
container (type guessed from the first word: `Bag 3` → bag), rewrites items, stock
rows, pending candidates and queued `cap-*.json` sidecars (old string kept as
`legacy_location`), writes `containers.json` first and `inventory.json` last, and
is a no-op on the next start (`tests/test_migration.py`). Any future change to the
data format follows the same pattern: bump `SCHEMA`, back up, migrate on startup.

**One item, several places (`stock`).** A part is often split: some in a bag,
the rest on a shelf. `stock` is `[{container, quantity}, ...]` and exists ONLY
while the item sits in two or more places. It is then the source of truth, and
the server derives the other two fields from it on every write: `quantity` is
the sum and `container` mirrors the first row. An item in one place carries no
`stock` at all.

Rules enforced by `normalize_stock()` / `apply_scalar_stock_edit()` in
`server.py` (covered by `tests/test_stock_locations.py`):
- Sending `stock` with one row (or none) DROPS the array and leaves a plain
  single-place item. Reducing to one place is just an edit, not a special case.
- A bare `quantity`/`container` edit on a split item (the card's +/- buttons, an
  agent that predates `stock`) lands on the FIRST row, keeping sum and rows in step.
- `/api/summary` counts a split item once per place it is stored; its `total_units`
  still counts each unit once.
- Summary rows from `/api/items` carry `stock` when present, so an agent scanning
  the list sees both places without a second call.

In the dashboard, a split item shows one amber location chip per place (each with
its own count), a per-location breakdown in the detail modal, and a repeater in the
edit modal ("＋ Add another location"). The card's +/- quick-quantity buttons are
replaced by a static total for split items: which container you took it from is a
decision, not a guess.

**Ordered-but-not-delivered items.** Items added from a purchase order before the
package arrives carry `"undelivered": true` and (deliberately) no `container`.
The dashboard shows a "🚚 not delivered" flag on the card and detail modal.
`POST /api/update` clears the flag automatically when an update gives a
previously unfiled item a `container` (it arrived and got filed), whether that
came from `container` or from a `stock` row, unless the same payload sets
`undelivered` explicitly. Notes should carry the order ID and order date.

## 3D models on items

An item can carry 3D model files (a printable holder/case, or the part's own CAD
model): allowed types `stl 3mf obj glb gltf step stp scad`. Files live in
`models/` (gitignored, bind-mounted, backed up). Upload via `POST /api/model`
(admin) with base64 data; download publicly via `GET /models/<file>`; detach via
`POST /api/model/delete`. The item's `models` array mirrors `images`.

**Dashboard 3D viewer.** Cards with models show a `🧊 3D` badge; clicking it (public,
no login) opens a viewer modal that renders the model inline via vendored
Online3DViewer (`public/vendor/o3dv/`, served by the stdlib `/vendor/` static
route in `server.py`). STEP/STP render through occt-import-js WASM (bundled, no CDN).
The edit modal (admin) has a "3D models" section to attach / view / delete. Gotchas
worth remembering:
- **o3dv needs ABSOLUTE model URLs** (`location.origin + '/models/…'`). Its STEP path
  hands the URL to a Blob `Worker`, and blob workers throw "URL is invalid" on
  root-relative paths. Relative URLs fail silently for meshes too.
- The occt CDN base is **hardcoded** in `o3dv.min.js`; the vendored copy is patched
  (`sed`) to `location.origin+"/vendor/o3dv/"` so it stays CDN-free and CSP-safe.
- Cloudflare caches `/vendor/*.js` (4h). Bump the `?v=` query on the o3dv `<script>`
  when you re-patch it; the dashboard HTML itself is not edge-cached.
**MCP tools**: `attach_item_model` (uploads a local file; needs
`INVENTORY_ADMIN_USER`/`INVENTORY_ADMIN_PASS` in the MCP server env) and
`get_item_model` (downloads an item's models to a local dir, default a temp dir;
also returns each model's public URL). Which items have models is visible in the
`list_all_items` rows (`models` filenames), so there is no separate model-listing
tool. `tests/test_models_roundtrip.py` covers the attach/list/download/delete
cycle against a throwaway server copy.

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

## Importing purchase orders (Temu or any web shop)

Batch-import ordered items into the inventory BEFORE they arrive, with product
photos, so arrivals just need a location. Refined over real imports; follow this
instead of rediscovering it.

### Process

1. **Scrape the orders with the browser extension (Claude for Chrome), not an
   API.** Temu: orders list at `bgt_orders.html`, per-order detail at
   `bgt_order_detail.html?parent_order_sn=<order-id>`. Both pages render the
   order rows lazily: right after `navigate` the text holds only the nav and
   "Buy this again" recommendations, so wait ~3-4s (and scroll a few hundred px
   on the list) before reading, or you will conclude there are no orders. On the
   list, `Order Time:` / `Order ID:` label pairs give the order IDs and dates;
   the "N items" figure is total PIECES, not lines. On a detail page the item
   lines live between the `Delivery time` and `Payment details` text markers:
   run a small `javascript_tool` snippet that slices `document.body.innerText`
   between those markers instead of dumping the whole page (order pages are 90%
   recommendations by volume). Parse lines by finding each `\d+,\d{2}€` price:
   the preceding line is the name, the next is the variant, and the following
   `×N` is the quantity. Return short (~70 char) names, and page through the
   result in slices; a full dump of a 17-line order hits the tool output cap.
   "One-click pay" items appear in a separate block at the top of the goods
   list; they are part of the order. A "Pre-order" badge on the LIST page is not
   a per-line delivery state, so check the detail page before trusting it.
2. **Diff against the live inventory first.** `GET /api/inventory`, match by
   name/keywords. Skip orders already catalogued (photo-watcher imports usually
   covered anything already delivered and filed). Import only what's missing.
3. **Create items via `POST /api/add`** (server assigns `item-<ms>` ids), one
   item per order line:
   - `undelivered: true` for anything not yet delivered; omit/false for
     delivered lines.
   - **No `location`** — the user files the item into a bag when it arrives,
     and that edit auto-clears `undelivered` (see the data-model section).
   - `condition: "New (ordered)"`.
   - `notes`: pack/variant details, then the order ID + order date + a dated
     "Added <date> from <shop> order <id>" line. The order ID is the link back
     to the shop when something goes missing.
   - Quantity: total pieces for identical parts (`2 packs of 5pcs` → 10);
     assortment kits count as kits (qty = number of kits).
   - The same product in two different orders → two items, each noting the
     other's order ID, so each tracks its own delivery. Merge after arrival if
     wanted.
4. **Product images from the shop CDN.** On each order detail page, extract
   product-image URLs with `javascript_tool`: iterate `document.images`, keep
   CDN images (Temu: `img.kwcdn.com`) larger than ~50px, climb to the row
   element containing a price (`/\d+,\d{2}€/`) to pair image↔product, and
   **strip query strings from URLs before returning them** — the extension
   redacts strings that look like cookie/query data (`[BLOCKED: ...]`). The
   first N de-duplicated hits are the N order lines, in on-page order;
   everything after is recommendations. Then locally: `curl -A <browser UA>`
   each URL, `sips -Z 800` to downscale, name `<shop>-<item-digits>-<n>.jpg`
   matching the inventory id.
   **If the order page carries no product images** (common on B2C shop
   platforms like Magento), go via the shop's own catalogue instead: search
   each SKU, open the product page, and take its gallery. Do the whole loop
   with `javascript_tool` `fetch()` from the already-authenticated tab rather
   than navigating once per product — same-origin fetch carries the session
   cookies, and one call can walk all N SKUs. Return only the URLs, in slices,
   or the tool output cap truncates the result mid-JSON.
5. **Verify the image↔item mapping with ONE contact sheet**, not 37 image
   reads: PIL-montage all thumbs with index labels into a single <2000px grid,
   read that once, eyeball it.
6. **Upload + attach:** rsync the images into `captures/` on the server, then
   attach each via **`POST /api/ingest` `{id, images:[...]}` — NOT
   `/api/update`**. `/api/update` writes a `review-*.json` marker per item
   (queues them all for re-identification); `/api/ingest` does not. These items
   are already identified — their names came from the order.
7. **Re-syncing an order already imported.** A later "sync the orders" pass is
   mostly a delivery-status pass, not an import: diff by pulling the order IDs
   out of each item's `notes` (`PO-...`), compare the per-order item count to
   the detail page's line count, and import only the gap. For every order whose
   detail page now shows `Delivered on <date>`, clear the flag with
   `POST /api/update {id, undelivered: false, notes: <existing + dated line>}`
   — passing `undelivered` explicitly is required, since the automatic clear
   only fires when the same payload sets a `location`. Items stay unfiled
   (location empty) until the user puts them in a bag; that is the correct
   state, not a missing step. Non-component lines (a tool, a storage cabinet)
   are a judgement call: import tools, skip furniture, and say which you skipped.
8. **Afterwards:** refresh the local backup mirror, and verify one item
   end-to-end (image serves, flag present, flag clears when a location is set —
   then revert the test edit).

### Shop specifics

- **Temu**: orders `bgt_orders.html`, detail `bgt_order_detail.html?parent_order_sn=<id>`,
  images `img.kwcdn.com`. ~6 months of history. "One-click pay" block at the top
  of the goods list is part of the order.
- **Partco** (partco.fi): orders `/tili/tilaukset`, detail
  `/tili/tilausvahvistus/<order-number>` (needs login — ask the user to log in
  in the tab, credentials are never entered by Claude). Item lines sit between
  the "seuraavat tuotteet" and "Palaa sivun" text markers: SKU under each name,
  `N kpl` quantity. Product images are right on the confirmation page
  (`partco.fi/images/product/...`), downloadable with a plain browser-UA curl.
  **Order status "In processing" is unreliable** — delivered orders keep it;
  judge delivery by whether the order's items are already filed in the
  inventory, or ask the user.
- **Uraltone** (uraltone.com, Magento): orders `/sales/order/history/`, detail
  `/sales/order/view/order_id/<internal-id>/` (needs login; the `order_id` in
  the URL is Magento's internal id, not the printed `ORDER # 0000xxxxx`).
  `get_page_text` on the detail page returns the whole goods table in English
  with no lazy-render wait: name, product number (the SKU), unit price,
  `Ordered N` / `Posted N`, subtotal. Header carries the order date and
  status; `COMPLETE` + a "Pick up from store" delivery method means delivered,
  so import without the `undelivered` flag.
  **The order page has no product images.** Get them from the catalogue:
  `fetch('/catalogsearch/result/?q=<SKU>')` returns the product page as the
  first `li.product-item`; on that product page the full-size gallery URLs are
  in the inline JSON as `"full":"<url>"` (de-duplicate, and unescape `\/`).
  `meta[property="og:image"]` gives just the main shot. Product descriptions
  are empty on most parts — the `#product-attribute-specs-table` holds only
  `Tuotekoodi` (SKU) and `Barcode`, so write notes from the order line's
  English name plus the SKU/barcode, don't hunt for a spec sheet. Images
  download with a plain browser-UA `curl`. Product URL slugs are Finnish and
  sometimes disagree with the SKU (a `x10` SKU under a `x20kpl` slug); trust
  the SKU and flag the discrepancy to the user.
  **Searching for stock: query the Finnish noun, never the English part
  number.** Product titles are Finnish and the search does prefix matching on
  them, so `?q=ACS712` returns nothing and silently redirects to the home page
  (looks identical to "not stocked"), while `virta-anturi` returns all three
  ACS712T boards. A `/catalogsearch/result/` URL hit directly also redirects
  home — drive the header search box instead. Before concluding a Finnish shop
  does not carry a part, retry with the Finnish category word
  (virta-anturi, vastus, kondensaattori, liitin, …).

### Gotchas (learned the hard way)

- **Cloudflare blocks non-browser user agents** on the public domain with
  `403 error code: 1010`. Every scripted call to the live server (curl,
  urllib) needs a browser-like `User-Agent` header. A 403 with that body is
  Cloudflare, not the app's auth.
- `/api/login` reads the `Authorization: Basic` **header**, not a JSON body.
- Clicking "View order details" in the extension can silently not navigate;
  going straight to the detail URL works.
- Admin creds: `deploy/admin.env` on the server seeds the account only on
  first boot; treat it as the source of truth for scripts but confirm with
  `/api/login` before batch work. Fetch creds to a scratch file, never echo
  them, delete the file when done.
- **Shop galleries mix marketing banners with product shots.** A tool or
  gadget listing often leads with a captioned lifestyle render ("Tiltable
  Angle", arrows, text overlay). Those are not the part. Order the `images`
  array with the clean product shot first (the card thumbnail) and the banners
  after, per the part-first convention. The contact sheet is what tells you
  which is which — check it before attaching, not after.

## Process learning: keeping these instructions current

These sections exist because sessions kept re-solving the same problems. Keep
the loop going:

- **Capture in the same session.** When an import/batch/deploy step fails and
  you find the fix (a header, a flag, an endpoint quirk), fold it into the
  relevant section of this file before the session ends. Evidence first: only
  write down what you actually verified working.
- **Edit, don't append.** Update the existing section (Process/Gotchas above,
  API table, Conventions) rather than adding a new near-duplicate section. If
  a step in the documented process turned out wrong, fix the step — don't add
  a footnote contradicting it.
- **Generalize before writing.** Write the pattern ("shops' order pages bury
  goods lines in recommendation noise; slice between text markers"), keep
  shop-specific selectors/URLs as the concrete example under it.
- **This repo is public.** No credentials, hostnames, IPs, order IDs, or
  personal data in this file — machine-specific values live in the user's
  private global config and the environment (`VPS_HOST` etc.). Placeholders
  like `<order-id>` are fine.
- **Where things go:** project-invariant process → this file. User-specific
  habits (backup cadence, "commit straight to main") → the user's private
  memory/config, not here. One fact, one home; link rather than duplicate.

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
