# Maker Inventory

> Forked from [m-esm/my-inventory](https://github.com/m-esm/my-inventory), the
> upstream project this builds on. Like upstream, it is released under the GNU
> General Public License version 3 (see [LICENSE](LICENSE)).

A photo-driven inventory for a maker's tools, parts and materials. Every item
lives in a container (a cabinet, a drawer inside it, a bin, a shelf), and
containers sit in zones (work areas). Scan a container's QR label with your
phone, photograph what's inside, and later ask a Claude Code session to "tag the
queue": it identifies everything from the photos and files each item in the
container you shot it in. The dashboard can be shared read-only and edited after
logging in. The full plan is in [SPEC.md](SPEC.md).

It is deliberately tiny: **one Python file, standard library only.** No Node, no
pip, no database, no build step. The whole "stack" is `python3 server.py` and some
JSON files. It runs the same on a laptop or a $5 VPS.

The repo ships with no inventory data: `python3 server.py` starts you on an empty
inventory, and your items and photos stay out of git (see "Data and backups").

![Dashboard](docs/screenshots/dashboard.png)

Product documents for an assigned agent: [docs/GOAL.md](docs/GOAL.md), [docs/PRD.md](docs/PRD.md), and [docs/ARD.md](docs/ARD.md).

## Why

Most parts-inventory apps want you to type part numbers. This one is built around
photos because a photo is the fastest honest record of "what is actually in this
bag." You capture the part (and the label, if you want), and that image is the
source of truth. Names and specs are optional metadata you fill in later, by hand
or with an LLM agent.

## Features

- **Phone-first capture over plain HTTP.** The capture page uses the phone's own
  camera app (a file input), so it works on home Wi-Fi with no certificates.
  Several photos per item plus an optional note; the current container is always
  shown, with a searchable picker as well as QR scanning.
- **Containers, zones and QR labels.** Nest containers, assign zones, and print a
  US letter sheet of QR labels; each label opens the capture page for its
  container.
- **On-demand tagging.** Nothing is identified on the server and it holds no AI
  keys. Photos queue up until you ask a Claude Code session to tag the queue.
- **Public dashboard, private editing.** Anyone can view; only a logged-in admin
  sees the edit controls. Auth is off until you create the first admin, so local
  dev needs no login.
- **Grouped by zone and container.** Items are listed per zone, then per
  container (a drawer inside its cabinet). Filter by category, container
  (including everything nested in it), or free text, including the item id. The
  query and filters stay in the URL, so Back and a shared link restore them.
- **A unique link per item.** `/i/<id>` opens that item. The detail modal shows
  the id and a copyable link. Right-click the name on a card for the same URL.
- **Duplicate handling in the UI.** When you add a part you already have, the app
  surfaces a duplicate card so you can add to the existing stash, set a new count,
  or file it as a separate bag. No silent double entries.
- **"To buy" list.** A separate wishlist of parts to purchase, with a public create
  endpoint so an agent (or a collaborator) can request a part with no credentials.
- **Agent-friendly.** A read-only [MCP server](mcp_server.py) exposes search,
  summary, item photos, and the one public write (`request_purchase`). An optional
  bridge lets an LLM identify uploaded photos and write back specs.
- **Safe by default.** Atomic writes, one-step `.bak` files, a guard against
  zeroing out a non-empty inventory, and a backup script.

## Screenshots

| Capture | To buy |
|---|---|
| ![Capture page](docs/screenshots/capture.png) | ![To-buy page](docs/screenshots/buy.png) |

## Quick start

```bash
git clone https://github.com/m-esm/my-inventory.git
cd my-inventory
python3 server.py
# open http://localhost:8770/dashboard
```

That's it. No dependencies. Tested on Python 3.9+.

- **Dashboard:** http://localhost:8770/dashboard
- **Item permalink:** http://localhost:8770/i/<id>
- **Capture:** http://localhost:8770/capture (from the phone: `http://<your-mac>.local:8770/capture`)
- **Containers + labels:** http://localhost:8770/containers
- **To buy:** http://localhost:8770/buy

Change host/port with `INVENTORY_HOST` / `INVENTORY_PORT`.

### Create an admin (to enable editing)

While no admin exists, the app is fully open (handy for local use). To lock down
editing, create an admin either in the browser (the **Set up admin** prompt on the
dashboard) or on the CLI:

```bash
python3 server.py adduser yourname admin   # prompts for a password (hidden)
python3 server.py users                     # show users / auth state
```

Passwords are stored salted-SHA256 in `data/users.json`, which is gitignored. Basic
Auth is base64, not encryption, so put it behind HTTPS for real use (see below).

## How it works

```
phone ──scan label──> /c/<container> ──photos──> server.py ──> captures/cap-<ms>.jpg
                                                    │           + one sidecar per item
                                                    ├─ data/containers.json (zones, containers)
                                                    ├─ data/inventory.json  (the items)
                                                    └─ public/*.html        (the UI)
Claude Code ──"tag the queue"──> GET /api/queue, reads the photos, POST /api/ingest
```

The phone's camera app takes the photos; the page downscales them and uploads
each one, then writes a small JSON sidecar per item with the container id and
note. Nothing is identified until you ask a Claude Code session in this repo to
"tag the queue" (the procedure lives in [CLAUDE.md](CLAUDE.md)). Names can also
be typed by hand in the dashboard's edit modal. See CLAUDE.md for the full
architecture, data model, and API table.

## Deploy

The app is one stdlib server, so deployment is "run it behind a TLS proxy." Two
ready paths live in [`deploy/`](deploy/):

**Docker + reverse proxy**

```bash
cp deploy/admin.env.example deploy/admin.env   # set ADMIN_USER / ADMIN_PASS
# edit docker-compose.yml: set your domain in the Traefik label
docker compose up -d --build
```

**systemd + Caddy (auto-HTTPS)**

```bash
export VPS_HOST=your.server.ip
./deploy/deploy.sh                              # rsync code + data, restart service
# on the server: install deploy/inventory.service and deploy/Caddyfile
```

All deploy/sync/backup scripts read `VPS_HOST` / `VPS_USER` / `VPS_PATH` from the
environment; nothing is hardcoded. `deploy.sh` and `sync*.sh` never overwrite the
server's `users.json`.

## MCP server (optional)

`mcp_server.py` is a stdlib MCP server that lets an LLM agent query the inventory
and request purchases:

```bash
INVENTORY_URL=http://localhost:8770 python3 mcp_server.py
```

Tools: `list_all_items`, `get_items`, `inventory_summary`, `get_item_images`,
`list_wishlist`, `attach_item_model` / `get_item_model`, and `request_purchase`
(the one public write).

There is deliberately **no search tool**. A few hundred items fit in one response,
so an agent calls `list_all_items` once (compact rows: id, name, category,
quantity, location, photo count, models) and picks ids from that list, then calls
`get_items` for the full records. Keyword search made agents conclude a part was
missing when it was there under another name.

## Data and backups

- `data/inventory.json`: the items. Created empty on first start; gitignored.
- `captures/`: item photos and capture sidecars. Gitignored.
- `data/users.json`, `data/*.bak`, `data/pending.json`, `data/wishlist.json`: local
  / secret state, gitignored.
- `scripts/backup.sh` pulls `data/` + `captures/` from a running server to a local
  folder (timestamped, zipped, read-only, pruned). Schedule it with cron or launchd.

## Contributing

Issues and PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). The whole point is
that it stays small and dependency-free, so changes that add a framework or a build
step are a hard sell.

## License

[GPL-3.0](LICENSE). You can use, modify, and redistribute it; derivative works must
stay open under the same license.

`qrcodegen.py` is the QR Code generator library by Project Nayuki
(https://www.nayuki.io/page/qr-code-generator-library), vendored unmodified under
its MIT license (notice at the top of the file), which is compatible with GPL-3.0.
