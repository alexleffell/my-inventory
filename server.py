#!/usr/bin/env python3
"""Tiny local server for the electronics inventory.

- Serves the camera-capture page (uses the iPhone via Continuity Camera) and
  the live dashboard.
- Receives captured frames from the browser and writes them to ./captures so
  Claude can read each new image, identify the part, and append it to
  ./data/inventory.json (which the dashboard polls).

Standard library only. Run:  python3 server.py
"""
import json
import base64
import email.utils
import time
import os
import sys
import shutil
import tempfile
import hashlib
import hmac
import secrets
import glob
import threading
from urllib.parse import urlparse, parse_qs, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
DATA = os.path.join(ROOT, "data")
CAPTURES = os.path.join(ROOT, "captures")
MODELS = os.path.join(ROOT, "models")
INVENTORY = os.path.join(DATA, "inventory.json")
PENDING = os.path.join(DATA, "pending.json")
WISHLIST = os.path.join(DATA, "wishlist.json")
CONTAINERS = os.path.join(DATA, "containers.json")

# inventory.json schema: 1 = free-text `location` strings, 2 = container ids.
SCHEMA = 2

for d in (DATA, CAPTURES, MODELS):
    os.makedirs(d, exist_ok=True)
if not os.path.exists(INVENTORY):
    with open(INVENTORY, "w") as f:
        json.dump({"schema": SCHEMA, "items": []}, f)
if not os.path.exists(CONTAINERS):
    with open(CONTAINERS, "w") as f:
        json.dump({"zones": [], "containers": []}, f)
if not os.path.exists(PENDING):
    with open(PENDING, "w") as f:
        json.dump({"pending": []}, f)
if not os.path.exists(WISHLIST):
    with open(WISHLIST, "w") as f:
        json.dump({"wishlist": []}, f)

# Configurable for deployment. Behind a reverse proxy (Caddy), bind localhost:
#   INVENTORY_HOST=127.0.0.1 INVENTORY_PORT=8770 python3 server.py
HOST = os.environ.get("INVENTORY_HOST", "0.0.0.0")
PORT = int(os.environ.get("INVENTORY_PORT", "8770"))
USERS = os.path.join(DATA, "users.json")

# Serialize all writes (this server is threaded) so concurrent POSTs can't
# clobber each other's read-modify-write of inventory.json.
_WRITE_LOCK = threading.Lock()


def _atomic_write(path, text):
    """Write a file safely: keep a one-step .bak of the previous version, then
    write to a temp file and atomically rename over the target. This makes it
    very hard to lose or corrupt the inventory on a crash or partial write."""
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def load_inv():
    with open(INVENTORY) as f:
        return json.load(f)


SUMMARY_FIELDS = ("id", "name", "category", "quantity", "container", "location")


def normalize_stock(item):
    """Keep an item's multi-location `stock` list and its top-level
    `container`/`quantity` consistent.

    `stock` is `[{"container": "c-k3x9qa", "quantity": 5}, ...]` and is the
    source of truth whenever the item is split across two or more places.
    `quantity` then holds the TOTAL and `container` mirrors the first entry, so
    a reader that only looks at one place (cards, /api/summary, agents) still
    sees the primary stash.

    An item in a single place carries no `stock` at all: one place is just
    `container` + `quantity`. An unfiled item has no `container`. `location`
    is never stored; reads derive it from the container (see with_places).
    """
    item.pop("location", None)
    if not item.get("container"):
        item.pop("container", None)
    stock = item.get("stock")
    if not isinstance(stock, list):
        if "stock" in item:
            item.pop("stock", None)
        return item
    clean = []
    for row in stock:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("container") or "").strip() or None
        try:
            qty = int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0
        if not cid and qty <= 0:
            continue
        clean.append({"container": cid, "quantity": max(0, qty)})
    if len(clean) < 2:
        item.pop("stock", None)
        if clean:
            _set_container(item, clean[0]["container"])
            item["quantity"] = clean[0]["quantity"]
        return item
    item["stock"] = clean
    item["quantity"] = sum(r["quantity"] for r in clean)
    _set_container(item, clean[0]["container"])
    return item


def _set_container(item, cid):
    if cid:
        item["container"] = cid
    else:
        item.pop("container", None)


def apply_scalar_stock_edit(item, payload):
    """Land a plain `quantity`/`container` edit on a split item's FIRST stash.

    The card's +/- buttons and any agent that does not know about `stock` send
    a single total. Without this the split rows and the total would disagree.
    """
    stock = item.get("stock")
    if "stock" in payload or not isinstance(stock, list) or len(stock) < 2:
        return
    if "container" in payload:
        stock[0]["container"] = payload.get("container") or None
    if "quantity" in payload:
        try:
            total = max(0, int(payload.get("quantity") or 0))
        except (TypeError, ValueError):
            return
        rest = sum(int(r.get("quantity") or 0) for r in stock[1:])
        stock[0]["quantity"] = max(0, total - rest)


def stock_rows(item):
    """Every (container, quantity) stash of an item, split or not."""
    stock = item.get("stock")
    if isinstance(stock, list) and stock:
        return [{"container": r.get("container") or None, "quantity": int(r.get("quantity") or 0)}
                for r in stock if isinstance(r, dict)]
    return [{"container": item.get("container") or None, "quantity": int(item.get("quantity") or 0)}]


# ---- containers and zones ---------------------------------------------------
# data/containers.json = {"zones": [{id, name}], "containers": [{id, name, type,
# parent, zone}]}. Containers nest through `parent`. Only a top-level container
# stores a `zone`; a nested one inherits its root's zone, so a drawer can never
# sit in a different zone from its cabinet.
CONTAINER_TYPES = (
    "cabinet", "drawer", "small-parts drawer", "divided box", "bin", "bag", "tray",
    "shelf", "wall/pegboard", "rack", "case", "other",
)
# Short random ids keep the QR code on a label small. No 0/o/1/l/i look-alikes.
ID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def new_id(prefix, taken):
    while True:
        ident = prefix + "".join(secrets.choice(ID_ALPHABET) for _ in range(6))
        if ident not in taken:
            return ident


def load_containers():
    try:
        with open(CONTAINERS) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("zones", [])
    doc.setdefault("containers", [])
    return doc


def save_containers(doc):
    _atomic_write(CONTAINERS, json.dumps(doc, indent=2))


def container_index(doc):
    return {c.get("id"): c for c in doc.get("containers", [])}


def _ancestry(cid, by_id):
    """The container and its parents, innermost first. Stops on a cycle."""
    chain, seen = [], set()
    while cid and cid in by_id and cid not in seen:
        seen.add(cid)
        chain.append(by_id[cid])
        cid = by_id[cid].get("parent")
    return chain


def container_path(cid, by_id):
    """Display path, outermost first: "Red cabinet › Drawer 3". "" if unknown."""
    return " › ".join(c.get("name") or c.get("id") for c in reversed(_ancestry(cid, by_id)))


def container_zone(cid, by_id):
    """The zone id a container sits in: its top-level ancestor's zone."""
    chain = _ancestry(cid, by_id)
    return chain[-1].get("zone") if chain else None


def guess_container_type(name):
    words = name.split()
    first = words[0].lower() if words else ""
    return first if first in CONTAINER_TYPES else "other"


def container_for_name(doc, name):
    """The container whose display path matches `name` (ignoring case), else a
    new top-level container with that name. Returns (id, created).

    Used by the v2 migration and for legacy `location` strings in writes. A
    path match first means an agent that echoes back a derived location
    ("Red cabinet › Drawer 3") lands in that drawer, not a new container.
    """
    by_id = container_index(doc)
    want = name.strip().lower()
    for c in doc["containers"]:
        if container_path(c.get("id"), by_id).lower() == want:
            return c["id"], False
    cid = new_id("c-", by_id)
    doc["containers"].append({"id": cid, "name": name.strip(),
                              "type": guess_container_type(name),
                              "parent": None, "zone": None})
    return cid, True


def resolve_places(payload, doc):
    """Turn a write payload's place fields into checked container ids, in place.

    Accepts `container` (an id) and `stock` rows of `{container, quantity}`. A
    legacy `location` string (older agents, scripts) is resolved through
    container_for_name; when both are given, `container` wins. Returns
    (error, created): error is a message for a 400, created says `doc` gained
    containers and must be saved.
    """
    created = False

    def resolve(obj):
        nonlocal created
        if "location" in obj:
            loc = str(obj.pop("location") or "").strip()
            if "container" not in obj:
                cid = None
                if loc:
                    cid, new = container_for_name(doc, loc)
                    created = created or new
                obj["container"] = cid
        if "container" in obj:
            cid = str(obj.get("container") or "").strip() or None
            if cid and cid not in container_index(doc):
                return "unknown container id %r (GET /api/containers lists them)" % cid
            obj["container"] = cid
        return None

    err = resolve(payload)
    if err:
        return err, created
    if isinstance(payload.get("stock"), list):
        rows = []
        for row in payload["stock"]:
            if not isinstance(row, dict):
                continue
            row = dict(row)
            err = resolve(row)
            if err:
                return err, created
            rows.append(row)
        payload["stock"] = rows
    return None, created


def with_places(it, by_id):
    """A copy of an item with the derived, human-readable `location` (the
    container path) on the item and on each stock row. Never stored."""
    out = dict(it)
    out["location"] = container_path(it.get("container"), by_id)
    if isinstance(it.get("stock"), list):
        out["stock"] = [dict(r, location=container_path(r.get("container"), by_id))
                        for r in it["stock"] if isinstance(r, dict)]
    return out


def containers_view(doc, items):
    """containers.json for readers: each container with its path, effective
    zone, how many items it holds directly, and how many children it has."""
    by_id = container_index(doc)
    zone_names = {z.get("id"): z.get("name") for z in doc["zones"]}
    counts, children = {}, {}
    for it in items:
        for cid in {r["container"] for r in stock_rows(it) if r["container"]}:
            counts[cid] = counts.get(cid, 0) + 1
    for c in doc["containers"]:
        if c.get("parent"):
            children[c["parent"]] = children.get(c["parent"], 0) + 1
    rows = []
    for c in doc["containers"]:
        zid = container_zone(c.get("id"), by_id)
        rows.append(dict(c, path=container_path(c.get("id"), by_id),
                         effective_zone=zid, zone_name=zone_names.get(zid) or "",
                         item_count=counts.get(c.get("id"), 0),
                         children=children.get(c.get("id"), 0)))
    return {"zones": doc["zones"], "containers": rows}


def queued_sidecars(inv, pending):
    """(path, sidecar) for every cap-*.json whose photos are not yet in an item
    or a pending duplicate decision: the identification queue."""
    logged = set(img for i in inv.get("items", []) for img in (i.get("images") or []))
    logged |= set(img for p in pending.get("pending", [])
                  for img in ((p.get("candidate") or {}).get("images") or []))
    out = []
    for f in sorted(glob.glob(os.path.join(CAPTURES, "cap-*.json"))):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        files = d.get("files") or ([d["file"]] if d.get("file") else [])
        if files and not any(x in logged for x in files):
            out.append((f, d))
    return out


def migrate_to_v2():
    """Schema 1 -> 2: free-text `location` strings become container records.

    Runs at startup, before the server binds. Backs up inventory.json and
    pending.json first, makes one top-level container per distinct location
    string (reusing a container of the same name), and rewrites items, stock
    rows, pending candidates and queued sidecars to container ids. Writes
    containers.json first and inventory.json (with `schema`) last, so a crash
    in between just re-runs the migration, which finds the containers by name.
    Returns a summary dict, or None when there was nothing to do.
    """
    inv = load_inv()
    if int(inv.get("schema") or 1) >= SCHEMA:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(INVENTORY, "%s.pre-v2-%s.bak" % (INVENTORY, stamp))
    if os.path.exists(PENDING):
        shutil.copy2(PENDING, "%s.pre-v2-%s.bak" % (PENDING, stamp))
    doc = load_containers()
    before = len(doc["containers"])

    def to_id(loc):
        loc = str(loc or "").strip()
        return container_for_name(doc, loc)[0] if loc else None

    def migrate(obj):
        if "location" in obj and "container" not in obj:
            obj["container"] = to_id(obj.get("location"))
        if isinstance(obj.get("stock"), list):
            obj["stock"] = [{"container": r["container"] if "container" in r else to_id(r.get("location")),
                             "quantity": r.get("quantity")}
                            for r in obj["stock"] if isinstance(r, dict)]
        normalize_stock(obj)

    pending = load_pending()
    sidecars = queued_sidecars(inv, pending)   # decide before items change
    items = inv.get("items", [])
    for it in items:
        migrate(it)
    for p in pending.get("pending", []):
        if isinstance(p.get("candidate"), dict):
            migrate(p["candidate"])
    rewritten = []
    for path, d in sidecars:
        if "location" in d:
            d["legacy_location"] = d.pop("location")
            d["container"] = to_id(d["legacy_location"])
            rewritten.append((path, d))

    save_containers(doc)
    save_pending(pending)
    for path, d in rewritten:
        with open(path, "w") as f:
            json.dump(d, f)
    inv["schema"] = SCHEMA
    save_inv(inv)
    return {"containers_created": len(doc["containers"]) - before,
            "items": len(items), "sidecars": len(rewritten)}



def item_summary(it):
    """One compact row per item.

    The agent-facing contract is "pull every summary in one call, then ask for
    the details you actually need", so a row carries only what you need to
    decide whether an item is interesting: identity, how many, where it lives,
    whether it has photos/models. Notes, condition and image filenames come
    from the full item.
    """
    row = {k: it.get(k) for k in SUMMARY_FIELDS}
    if it.get("stock"):
        # Split across several places: `location`/`quantity` stay the primary
        # stash and the total, `stock` says where the rest of the units are.
        row["stock"] = [dict(r) for r in it["stock"]]
    row["images"] = len(it.get("images") or [])
    models = it.get("models") or []
    if models:
        row["models"] = list(models)
    if it.get("undelivered"):
        row["undelivered"] = True
    return row


def items_by_ids(items, ids):
    """Full items for the given ids, in the order asked for, plus the misses."""
    by_id = {it.get("id"): it for it in items}
    found = [by_id[i] for i in ids if i in by_id]
    missing = [i for i in ids if i not in by_id]
    return found, missing


def save_inv(inv):
    # Safety net: if a write would wipe a previously non-empty inventory to
    # zero items, stash an extra timestamped emergency copy first (never lose data).
    try:
        prev = load_inv().get("items", [])
        if prev and not inv.get("items"):
            shutil.copy2(INVENTORY, INVENTORY + ".emptied-%d.bak" % int(time.time()))
    except (OSError, ValueError):
        pass
    _atomic_write(INVENTORY, json.dumps(inv, indent=2))


def load_pending():
    with open(PENDING) as f:
        return json.load(f)


def save_pending(p):
    _atomic_write(PENDING, json.dumps(p, indent=2))


def load_wishlist():
    try:
        with open(WISHLIST) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"wishlist": []}


def save_wishlist(w):
    _atomic_write(WISHLIST, json.dumps(w, indent=2))


IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def pending_review_ids():
    """Item ids that already have a review-*.json marker waiting for the watcher."""
    ids = set()
    for f in glob.glob(os.path.join(CAPTURES, "review-*.json")):
        try:
            item_id = json.load(open(f)).get("id")
        except Exception:
            continue
        if item_id:
            ids.add(item_id)
    return ids


def item_id_from_path(path, prefix="/i/"):
    """Item id encoded in `/i/<id>`, or None if this is not that route.

    IDs are `cap-<ms>` / `item-<ms>` (letters, digits, `_`, `.`, `-`). Anything
    with a slash, `..`, or other punctuation is rejected so `/i/` cannot be
    used as a path-traversal gadget. `prefix` reuses the same check for other
    id routes (`/c/<container-id>`).
    """
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    if not rest or "/" in rest or rest in (".", ".."):
        return None
    ident = unquote(rest)
    if not ident or "/" in ident or len(ident) > 80:
        return None
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if ident[0] in ".-" or any(c not in allowed for c in ident):
        return None
    return ident


def container_id_from_path(path):
    """Container id in `/c/<id>` (the URL a QR label encodes), or None."""
    return item_id_from_path(path, "/c/")


def image_ext(blob):
    """File extension for an uploaded photo, from its first bytes; None if the
    bytes are not a PNG, JPEG or WebP image."""
    if blob[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp"
    return None


# 3D model files an item can carry (models/ dir, item field "models": [...]).
MODEL_EXTS = (".stl", ".3mf", ".obj", ".glb", ".gltf", ".step", ".stp", ".scad")
MODEL_TYPES = {
    ".stl": "model/stl",
    ".3mf": "model/3mf",
    ".obj": "model/obj",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".step": "application/step",
    ".stp": "application/step",
    ".scad": "text/plain; charset=utf-8",
}

# Allowed status values for a purchase request, in lifecycle order.
WISH_STATUSES = ("requested", "ordered", "bought")
# Fields an admin can edit on a wishlist entry via /api/wishlist/resolve.
WISH_FIELDS = ("project", "part", "type", "reason", "quantity", "status", "notes", "requested_by")


# ---- authentication (viewer / admin) ---------------------------------------
# Auth is DISABLED while data/users.json is absent/empty (local dev stays open).
# Create accounts with:  python3 server.py adduser <name> <password> <role>
def load_users():
    try:
        with open(USERS) as f:
            return json.load(f).get("users", {})
    except (OSError, ValueError):
        return {}


def _hash_pw(salt, password):
    return hashlib.sha256((salt + password).encode()).hexdigest()


def add_user(username, password, role):
    if role not in ("admin", "viewer"):
        raise SystemExit("role must be 'admin' or 'viewer'")
    users = load_users()
    salt = secrets.token_hex(16)
    users[username] = {"salt": salt, "hash": _hash_pw(salt, password), "role": role}
    _atomic_write(USERS, json.dumps({"users": users}, indent=2))
    print("user '%s' (%s) saved." % (username, role))


def delete_user(username):
    users = load_users()
    if username not in users:
        return False
    del users[username]
    _atomic_write(USERS, json.dumps({"users": users}, indent=2))
    return True


def set_role(username, role):
    users = load_users()
    if username not in users:
        return False
    users[username]["role"] = role
    _atomic_write(USERS, json.dumps({"users": users}, indent=2))
    return True


def check_credentials(auth_header):
    """Return the role for a valid 'Authorization: Basic ...' header, else None."""
    users = load_users()
    if not users:
        return "admin"  # auth disabled: full access (local dev)
    if not auth_header or not auth_header.startswith("Basic "):
        return None
    try:
        raw = base64.b64decode(auth_header[6:]).decode()
        username, password = raw.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    u = users.get(username)
    if not u:
        return None
    if hmac.compare_digest(u["hash"], _hash_pw(u["salt"], password)):
        return u["role"]
    return None


# Public routes deliberately kept OUT of the OpenAPI document: they exist for the
# app's own pages, not for third-party agents. tests/test_openapi.py requires every
# other public /api/ route to be documented, so adding one forces a choice here.
OPENAPI_INTERNAL = {
    "/api/identify/status",   # UI probe: is the identify watcher wired up
    "/api/pending",           # duplicate decisions, resolved in the dashboard UI
    "/api/login",             # auth handshake for the dashboard
    "/api/setup",             # first-run admin creation
}

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Stable item id."},
        "name": {"type": "string", "description": "Part name. Empty while an item is still being identified from its photos."},
        "category": {"type": "string", "description": "e.g. Sensors, Power, Resistors, Modules."},
        "quantity": {"type": "integer", "description": "Total units in stock, across every location."},
        "container": {"type": "string", "description": "Id of the container the item is stored in (see /api/containers). Absent while the item is unfiled. When the item is split across places this is the first/primary one."},
        "location": {"type": "string", "description": "Read-only, human-readable path of that container, outermost first, e.g. 'Red cabinet › Drawer 3'. Empty when unfiled."},
        "stock": {"type": "array", "description": "Present only when the item is split across two or more places: where the units actually are. Absent means everything sits in 'container'.",
                  "items": {"type": "object", "properties": {
                      "container": {"type": "string"},
                      "location": {"type": "string"},
                      "quantity": {"type": "integer"}}}},
        "condition": {"type": "string"},
        "notes": {"type": "string", "description": "Free text: SKUs, specs, provenance. The word 'unconfirmed' means the value is a guess."},
        "images": {"type": "array", "items": {"type": "string"},
                   "description": "Photo filenames; fetch at <server>/captures/<filename>. The first one shows the actual part."},
        "models": {"type": "array", "items": {"type": "string"},
                   "description": "Attached 3D model filenames; download at <server>/models/<filename>."},
    },
}


def openapi_spec(base_url):
    """OpenAPI 3.1 description of the PUBLIC (unauthenticated) API, for LLM tool
    use: ChatGPT Actions, LangChain, n8n, anything that speaks OpenAPI. Admin
    endpoints are deliberately absent — they need Basic auth and are not for
    third-party agents. tests/test_openapi.py fails if a public route added to
    the handlers is missing here."""
    def _items_response(desc):
        return {"200": {"description": desc, "content": {"application/json": {"schema": {
            "type": "object",
            "properties": {"count": {"type": "integer"},
                           "items": {"type": "array", "items": ITEM_SCHEMA}},
        }}}}}

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Inventory",
            "version": "1.0.0",
            "description": (
                "A photo-driven inventory of a maker's tools, parts and materials. Every item "
                "is stored in a container (drawer, bin, shelf...); containers nest and sit in "
                "zones (work areas). Read endpoints are public.\n\n"
                "There is no search endpoint on purpose. The inventory is a few hundred items, "
                "so the reliable way to answer any question about it is: GET /api/items once "
                "(one compact row per item, the entire inventory), then pick the ids you care "
                "about and GET /api/items?ids=... for their full records. Do not guess whether "
                "a part exists from a keyword match; read the list. If a needed part really is "
                "not there, POST /api/wishlist to ask for it to be bought."
            ),
        },
        "servers": [{"url": base_url}],
        "paths": {
            "/api/summary": {"get": {
                "operationId": "getSummary",
                "summary": "Overview of the whole inventory.",
                "description": "Item/unit totals plus a breakdown by category, by container (its "
                               "path) and by zone, and how many purchase requests are open. Tiny "
                               "response — useful for 'what do I have?' questions; for anything "
                               "item-specific go straight to /api/items.",
                "responses": {"200": {"description": "Totals and breakdowns.", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "total_items": {"type": "integer"},
                        "total_units": {"type": "integer"},
                        "categories": {"type": "array", "items": {"type": "object", "properties": {
                            "name": {"type": "string"}, "items": {"type": "integer"}}}},
                        "locations": {"type": "array", "items": {"type": "object", "properties": {
                            "name": {"type": "string"}, "items": {"type": "integer"}}}},
                        "zones": {"type": "array", "items": {"type": "object", "properties": {
                            "name": {"type": "string"}, "items": {"type": "integer"}}}},
                        "wishlist_total": {"type": "integer"},
                        "wishlist_open": {"type": "integer"},
                    },
                }}}}},
            }},
            "/api/items": {"get": {
                "operationId": "listItems",
                "summary": "Every item as a compact summary, or full records for specific ids.",
                "description": "Call with no parameters to get one short row per item (id, name, "
                               "category, quantity, container + location path, photo count, attached 3D models) for "
                               "the WHOLE inventory in a single response. That list is the ground "
                               "truth: scan it yourself instead of searching. Then call again with "
                               "ids= to get the complete records (notes, SKUs, specs, photo "
                               "filenames) for the handful of items you actually need.",
                "parameters": [
                    {"name": "ids", "in": "query", "required": False, "schema": {"type": "string"},
                     "description": "Comma-separated item ids from a previous call. Returns those "
                                    "items in full, in the order given, plus a 'missing' list for "
                                    "ids that do not exist."},
                ],
                "responses": {"200": {"description": "Summary rows, or full items when ids= is given.",
                                      "content": {"application/json": {"schema": {
                                          "type": "object",
                                          "properties": {"count": {"type": "integer"},
                                                         "items": {"type": "array", "items": ITEM_SCHEMA},
                                                         "missing": {"type": "array", "items": {"type": "string"}}},
                                      }}}}},
            }},
            "/api/inventory": {"get": {
                "operationId": "getFullInventory",
                "summary": "Every item, unfiltered.",
                "description": "Every item with every field. Use it when you need the full detail of the whole inventory at once; otherwise /api/items is the cheaper first call.",
                "responses": {"200": {"description": "All items.", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"items": {"type": "array", "items": ITEM_SCHEMA}}}}}}},
            }},
            "/api/containers": {"get": {
                "operationId": "listContainers",
                "summary": "Every container and zone: where things are stored.",
                "description": "Zones are work areas (e.g. 'Electronics bench'). Containers are the "
                               "physical places items live in (cabinet, drawer, bin, shelf...); they "
                               "nest through 'parent', and a nested container sits in its top-level "
                               "ancestor's zone ('effective_zone'). Each container carries its display "
                               "'path' and how many items it holds directly. Item rows from /api/items "
                               "reference containers by id.",
                "responses": {"200": {"description": "Zones and containers.", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "zones": {"type": "array", "items": {"type": "object", "properties": {
                            "id": {"type": "string"}, "name": {"type": "string"}}}},
                        "containers": {"type": "array", "items": {"type": "object", "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": list(CONTAINER_TYPES)},
                            "parent": {"type": ["string", "null"], "description": "Enclosing container id, or null at top level."},
                            "zone": {"type": ["string", "null"], "description": "Zone id, stored on top-level containers only."},
                            "effective_zone": {"type": ["string", "null"]},
                            "zone_name": {"type": "string"},
                            "path": {"type": "string", "description": "e.g. 'Red cabinet › Drawer 3'."},
                            "item_count": {"type": "integer"},
                            "children": {"type": "integer"}}}},
                    },
                }}}}},
            }},
            "/api/queue": {"get": {
                "operationId": "getIdentificationQueue",
                "summary": "Photos and items still awaiting identification.",
                "description": "Recently photographed items that have not been identified yet: each "
                               "queued capture lists its photos, the container it was shot in "
                               "(id + location path) and the user's optional note. Items being "
                               "re-identified show up in /api/items with an empty or old name, so "
                               "check here before concluding something is absent.",
                "responses": {"200": {"description": "Queued uploads and items being identified.",
                                      "content": {"application/json": {"schema": {"type": "object", "properties": {
                                          "count": {"type": "integer"},
                                          "queue": {"type": "array", "items": {"type": "object"}}}}}}}},
            }},
            "/api/wishlist": {
                "get": {
                    "operationId": "listPurchaseRequests",
                    "summary": "Parts requested for purchase.",
                    "description": "The 'to buy' list. status is requested, ordered or bought.",
                    "responses": {"200": {"description": "Purchase requests.", "content": {"application/json": {"schema": {
                        "type": "object", "properties": {"wishlist": {"type": "array", "items": {"type": "object"}}}}}}}},
                },
                "post": {
                    "operationId": "requestPurchase",
                    "summary": "Ask for a part to be bought.",
                    "description": "Adds a purchase request. Use this when a project needs a part that "
                                   "/api/items shows is not in stock. Append-only and unauthenticated: "
                                   "it never changes the inventory itself, the owner reviews the list. "
                                   "Read /api/items first so you do not request something already on a shelf.",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {
                        "type": "object",
                        "required": ["part"],
                        "properties": {
                            "part": {"type": "string", "description": "What to buy. Be specific: '0.1 ohm 3W shunt resistor', not 'resistor'."},
                            "quantity": {"type": "integer", "description": "How many. Default 1."},
                            "project": {"type": "string", "description": "What it is for."},
                            "type": {"type": "string", "description": "Component category."},
                            "reason": {"type": "string", "description": "Why it is needed."},
                            "requested_by": {"type": "string", "description": "Who is asking, e.g. 'ChatGPT'."},
                            "notes": {"type": "string", "description": "Links, part numbers, alternatives."},
                        },
                    }}}},
                    "responses": {"200": {"description": "Request created.", "content": {"application/json": {"schema": {
                        "type": "object", "properties": {"ok": {"type": "boolean"}, "id": {"type": "string"},
                                                         "request": {"type": "object"}}}}}}},
                },
            },
        },
    }


def write_review(item_id, images, reason):
    """Drop a review marker so Claude re-examines newly added photos for an
    existing item and enriches its info. Claude deletes the marker when done."""
    name = "review-%d.json" % int(time.time() * 1000)
    with open(os.path.join(CAPTURES, name), "w") as f:
        json.dump({"id": item_id, "images": images, "reason": reason}, f)
    return name

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".json": "application/json",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".wasm": "application/wasm",
    ".css": "text/css; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path, head_only=False):
        if not os.path.exists(path) or not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        ext = os.path.splitext(path)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        # Last-Modified lets pages self-reload after a deploy (HEAD poll).
        self.send_header(
            "Last-Modified",
            email.utils.formatdate(os.path.getmtime(path), usegmt=True),
        )
        if ext == ".html":
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _require(self, need):
        """Gate a request. need='viewer' allows viewer+admin; 'admin' admin only.
        Returns the role if allowed, else writes 401/403 and returns None."""
        role = check_credentials(self.headers.get("Authorization"))
        if role == "admin" or (need == "viewer" and role in ("viewer", "admin")):
            return role
        if role is None:
            body = b'{"error":"authentication required"}'
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Inventory"')
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send(403, {"error": "admin access required"})
        return None

    def _places(self, obj):
        """resolve_places() for a write payload. Sends the 400 and returns False
        on an unknown container id; saves containers made from legacy names."""
        doc = load_containers()
        err, created = resolve_places(obj, doc)
        if err:
            self._send(400, {"error": err})
            return False
        if created:
            save_containers(doc)
        return True

    def do_HEAD(self):
        # HEAD on the HTML pages only — used by the pages' self-reload poll.
        path = self.path.split("?")[0]
        pages = {
            "/": "dashboard.html", "/dashboard": "dashboard.html",
            "/capture": "capture.html", "/buy": "wishlist.html", "/wishlist": "wishlist.html",
            "/containers": "containers.html",
        }
        name = pages.get(path)
        if not name and item_id_from_path(path):
            name = "dashboard.html"
        if not name and container_id_from_path(path):
            name = "capture.html"
        if not name:
            self.send_response(404)
            self.end_headers()
            return
        self._serve_file(os.path.join(PUBLIC, name), head_only=True)

    def do_GET(self):
        path = self.path.split("?")[0]
        # Public viewing: all GET endpoints (dashboard, APIs, photos, capture page)
        # are open. Editing requires logging in — every POST is gated to admin.
        if path == "/" or path == "/dashboard":
            return self._serve_file(os.path.join(PUBLIC, "dashboard.html"))
        if path.startswith("/i/"):
            # One stable URL per item. The dashboard opens that item's detail
            # modal; a well-formed unknown id still serves the page (JS toasts).
            if item_id_from_path(path):
                return self._serve_file(os.path.join(PUBLIC, "dashboard.html"))
            return self._send(404, {"error": "not found"})
        if path == "/capture":
            return self._serve_file(os.path.join(PUBLIC, "capture.html"))
        if path.startswith("/c/"):
            # The link a container's QR label encodes: the capture page with that
            # container preselected (the page reads the id from the path).
            if container_id_from_path(path):
                return self._serve_file(os.path.join(PUBLIC, "capture.html"))
            return self._send(404, {"error": "not found"})
        if path == "/buy" or path == "/wishlist":
            return self._serve_file(os.path.join(PUBLIC, "wishlist.html"))
        if path == "/containers":
            return self._serve_file(os.path.join(PUBLIC, "containers.html"))
        if path.startswith("/vendor/"):
            # Vendored static assets (the 3D viewer library + its wasm). No path
            # traversal: collapse the request and refuse anything outside public/vendor.
            rel = os.path.normpath(path.lstrip("/"))
            if rel.startswith("vendor" + os.sep) or rel == "vendor":
                return self._serve_file(os.path.join(PUBLIC, rel))
            return self._send(404, {"error": "not found"})
        if path == "/openapi.json":
            # Machine-readable description of the public API, so an LLM tool layer
            # (ChatGPT Actions, LangChain, n8n) can call it without hand-written glue.
            proto = self.headers.get("X-Forwarded-Proto") or "http"
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost:8770"
            return self._send(200, openapi_spec("%s://%s" % (proto, host)))
        if path == "/api/inventory":
            inv = load_inv()
            by_id = container_index(load_containers())
            inv["items"] = [with_places(it, by_id) for it in inv.get("items", [])]
            return self._send(200, inv)
        if path == "/api/containers":
            # Public: zones + containers, each with its path and item count.
            return self._send(200, containers_view(load_containers(),
                                                   load_inv().get("items", [])))
        if path == "/api/identify/status":
            # Identification runs through the photo watcher/bridge (an agent
            # polling for review-*.json markers), not an API key on this server.
            return self._send(200, {"available": True, "mode": "watcher"})
        if path == "/api/pending":
            p = load_pending()
            by_id = container_index(load_containers())
            for rec in p.get("pending", []):
                if isinstance(rec.get("candidate"), dict):
                    rec["candidate"] = with_places(rec["candidate"], by_id)
            return self._send(200, p)
        if path == "/api/wishlist":
            # Public: purchase requests ("to buy"). Anyone (incl. an MCP agent)
            # can read the list; creating is open, managing is admin-only.
            return self._send(200, load_wishlist(), "application/json")
        if path == "/api/users":
            # Admin-only: list accounts (usernames + roles, never hashes).
            if self._require("admin") is None:
                return
            users = load_users()
            return self._send(200, {"users": [
                {"username": u, "role": d.get("role", "viewer")}
                for u, d in sorted(users.items())]})
        if path == "/api/summary":
            # Public overview, handy for an LLM/agent to grok what's in stock.
            items = load_inv().get("items", [])
            cdoc = load_containers()
            by_id = container_index(cdoc)
            zone_names = {z.get("id"): z.get("name") for z in cdoc["zones"]}
            cats, locs, zones, units = {}, {}, {}, 0
            for it in items:
                units += int(it.get("quantity") or 0)
                c = it.get("category") or "Uncategorized"
                cats[c] = cats.get(c, 0) + 1
                # A split item counts once in every place it is stored.
                for row in stock_rows(it):
                    l = container_path(row["container"], by_id) or "Unfiled"
                    locs[l] = locs.get(l, 0) + 1
                for z in {zone_names.get(container_zone(r["container"], by_id)) or "No zone"
                          for r in stock_rows(it)}:
                    zones[z] = zones.get(z, 0) + 1
            wl = load_wishlist().get("wishlist", [])
            return self._send(200, {
                "total_items": len(items),
                "total_units": units,
                "categories": [{"name": k, "items": v} for k, v in sorted(cats.items(), key=lambda x: -x[1])],
                "locations": [{"name": k, "items": v} for k, v in sorted(locs.items(), key=lambda x: -x[1])],
                "zones": [{"name": k, "items": v} for k, v in sorted(zones.items(), key=lambda x: -x[1])],
                "wishlist_total": len(wl),
                "wishlist_open": sum(1 for w in wl if w.get("status") != "bought"),
            })
        if path == "/api/queue":
            # Public: uploads awaiting identification (so the UI can show a queue).
            inv = load_inv()
            by_id = container_index(load_containers())
            try:
                pend = load_pending()
            except (OSError, ValueError):
                pend = {}
            q = []
            # capture-page confirms not yet turned into items. Photos already on a
            # PENDING duplicate decision await the user, not identification, so
            # queued_sidecars() skips them (mirrors deploy/find_unprocessed.py).
            for f, d in queued_sidecars(inv, pend):
                cid = d.get("container")
                q.append({"sidecar": os.path.basename(f),
                          "images": d.get("files") or [d["file"]],
                          "container": cid, "location": container_path(cid, by_id),
                          "note": d.get("note") or "", "quantity": d.get("quantity"),
                          "created": d.get("created"), "status": "queued"})
            # items created but not yet named (dashboard adds being identified),
            # plus items with an outstanding review marker (enrich / re-identify)
            reviewing = pending_review_ids()
            for it in inv.get("items", []):
                if not (it.get("name") or "").strip() or it.get("id") in reviewing:
                    q.append({"id": it.get("id"), "name": it.get("name") or "",
                              "images": it.get("images") or [],
                              "quantity": it.get("quantity"), "container": it.get("container"),
                              "location": container_path(it.get("container"), by_id),
                              "status": "identifying"})
            return self._send(200, {"count": len(q), "queue": q})
        if path == "/api/items":
            # Public. No args: one compact summary row per item (the whole
            # inventory in one small response). ?ids=a,b,c: the full records for
            # those ids. Everything in full is /api/inventory.
            qs = parse_qs(urlparse(self.path).query)
            ids = [x.strip() for x in (qs.get("ids", [""])[0] or "").split(",") if x.strip()]
            by_id = container_index(load_containers())
            items = [with_places(it, by_id) for it in load_inv().get("items", [])]
            if ids:
                found, missing = items_by_ids(items, ids)
                return self._send(200, {"count": len(found), "items": found, "missing": missing})
            return self._send(200, {"count": len(items),
                                    "items": [item_summary(it) for it in items]})
        if path.startswith("/captures/"):
            name = os.path.basename(path)
            return self._serve_file(os.path.join(CAPTURES, name))
        if path.startswith("/models/"):
            # Public download of an item's 3D model file.
            name = os.path.basename(path)
            fp = os.path.join(MODELS, name)
            if not os.path.isfile(fp):
                return self._send(404, {"error": "not found"})
            ctype = MODEL_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        with _WRITE_LOCK:        # one writer at a time — no read-modify-write races
            self._do_post()

    def _do_post(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})

        # Login check: report the role + whether any admin account exists yet.
        if path == "/api/login":
            role = check_credentials(self.headers.get("Authorization"))
            return self._send(200 if role == "admin" else 401,
                              {"ok": role == "admin", "role": role,
                               "authConfigured": bool(load_users())})

        # First-run setup: create the FIRST admin account from the browser.
        # Only works while no users exist (so it can't be used to hijack later).
        if path == "/api/setup":
            if load_users():
                return self._send(403, {"error": "already configured"})
            user = (payload.get("username") or "").strip()
            pw = payload.get("password") or ""
            if not user or not pw:
                return self._send(400, {"error": "username and password required"})
            add_user(user, pw, "admin")
            return self._send(200, {"ok": True})

        # Create a purchase request ("to buy"). PUBLIC on purpose: an MCP agent
        # with no credentials can ask for a part to be bought. It's append-only and
        # low-stakes — the admin reviews, marks bought, or deletes. Managing the
        # list (status/edit/delete) below is admin-only.
        if path == "/api/wishlist":
            part = (payload.get("part") or "").strip()
            if not part:
                return self._send(400, {"error": "part is required (what to buy)"})
            status = (payload.get("status") or "requested").strip().lower()
            if status not in WISH_STATUSES:
                status = "requested"
            rec = {
                "id": "wish-%d" % int(time.time() * 1000),
                "project": (payload.get("project") or "").strip(),
                "part": part,
                "type": (payload.get("type") or "").strip(),
                "reason": (payload.get("reason") or "").strip(),
                "quantity": int(payload.get("quantity") or 1),
                "requested_by": (payload.get("requested_by") or "").strip(),
                "status": status,
                "notes": (payload.get("notes") or "").strip(),
                "created": int(time.time() * 1000),
            }
            w = load_wishlist()
            w.setdefault("wishlist", []).append(rec)
            save_wishlist(w)
            return self._send(200, {"ok": True, "id": rec["id"], "request": rec})

        if self._require("admin") is None:  # all other writes are admin-only
            return

        if path == "/api/identify":
            # Queue the item for the photo watcher/bridge: drop a review-*.json
            # marker (the same trigger as the enrich flow). The watching agent
            # pulls the photos, identifies the part, and writes back via
            # /api/update, then deletes the marker.
            item_id = payload.get("id")
            item = next((it for it in load_inv().get("items", [])
                         if it.get("id") == item_id), None)
            if not item:
                return self._send(404, {"error": "item not found"})
            images = []
            for image in item.get("images") or []:
                name = os.path.basename(str(image))
                if os.path.splitext(name)[1].lower() not in IMAGE_TYPES:
                    continue
                if os.path.isfile(os.path.join(CAPTURES, name)):
                    images.append(name)
            if not images:
                return self._send(400, {"error": "no usable images"})
            if item_id in pending_review_ids():
                return self._send(200, {"ok": True, "queued": True, "already": True})
            write_review(item_id, images, "identify requested from dashboard")
            return self._send(200, {"ok": True, "queued": True})

        if path == "/api/users":
            # Admin-only user management (create / set role / delete).
            action = (payload.get("action") or "add").strip()
            username = (payload.get("username") or "").strip()
            if not username:
                return self._send(400, {"error": "username required"})
            users = load_users()
            if action == "delete":
                if username not in users:
                    return self._send(404, {"error": "no such user"})
                admins = [u for u, d in users.items() if d.get("role") == "admin"]
                if users[username].get("role") == "admin" and len(admins) <= 1:
                    return self._send(400, {"error": "can't delete the last admin"})
                delete_user(username)
                return self._send(200, {"ok": True})
            # add (new user) or update (existing: change password and/or role)
            role = (payload.get("role") or "viewer").strip()
            if role not in ("admin", "viewer"):
                return self._send(400, {"error": "role must be admin or viewer"})
            pw = payload.get("password") or ""
            if username not in users and not pw:
                return self._send(400, {"error": "password required for a new user"})
            # Don't let the last admin be demoted to viewer and lock everyone out.
            if username in users and users[username].get("role") == "admin" and role != "admin":
                admins = [u for u, d in users.items() if d.get("role") == "admin"]
                if len(admins) <= 1:
                    return self._send(400, {"error": "can't demote the last admin"})
            if pw:
                add_user(username, pw, role)
            else:
                set_role(username, role)
            return self._send(200, {"ok": True})

        if path == "/api/containers":
            # Create / update / delete a container. Body:
            #   {action:"create", name, type?, parent?, zone?}
            #   {action:"update", id, name?, type?, parent?, zone?}
            #   {action:"delete", id}   (refused while anything is stored in it)
            action = (payload.get("action") or "create").strip()
            doc = load_containers()
            by_id = container_index(doc)
            zone_ids = {z.get("id") for z in doc["zones"]}
            if action == "create":
                rec = {"id": new_id("c-", by_id), "name": "", "type": "other",
                       "parent": None, "zone": None}
            else:
                rec = by_id.get(payload.get("id"))
                if not rec:
                    return self._send(404, {"error": "container not found"})
            if action == "delete":
                cid = rec["id"]
                kids = [c for c in doc["containers"] if c.get("parent") == cid]
                held = [it for it in load_inv().get("items", [])
                        if any(r["container"] == cid for r in stock_rows(it))]
                queued = [d for _, d in queued_sidecars(load_inv(), load_pending())
                          if d.get("container") == cid]
                if kids or held or queued:
                    return self._send(409, {
                        "error": "container is not empty: %d item(s), %d container(s), "
                                 "%d queued capture(s)" % (len(held), len(kids), len(queued))})
                doc["containers"] = [c for c in doc["containers"] if c.get("id") != cid]
                save_containers(doc)
                return self._send(200, {"ok": True, "removed": cid})
            if action not in ("create", "update"):
                return self._send(400, {"error": "unknown action"})
            new = dict(rec)
            if "name" in payload or action == "create":
                new["name"] = str(payload.get("name") or "").strip()
                if not new["name"]:
                    return self._send(400, {"error": "name required"})
            if "type" in payload:
                new["type"] = str(payload.get("type") or "other").strip().lower()
                if new["type"] not in CONTAINER_TYPES:
                    return self._send(400, {"error": "type must be one of: %s"
                                            % ", ".join(CONTAINER_TYPES)})
            if "parent" in payload:
                parent = payload.get("parent") or None
                if parent and parent not in by_id:
                    return self._send(400, {"error": "unknown parent container"})
                if parent and any(c.get("id") == new["id"] for c in _ancestry(parent, by_id)):
                    return self._send(400, {"error": "a container cannot sit inside itself"})
                new["parent"] = parent
            if "zone" in payload:
                zone = payload.get("zone") or None
                if zone and zone not in zone_ids:
                    return self._send(400, {"error": "unknown zone"})
                if zone and new.get("parent"):
                    return self._send(400, {"error": "a nested container inherits its "
                                            "zone from its top-level container"})
                new["zone"] = zone
            if new.get("parent"):
                new["zone"] = None      # nested: the zone comes from the root
            if action == "create":
                doc["containers"].append(new)
            else:
                rec.clear()
                rec.update(new)
            save_containers(doc)
            view = containers_view(doc, load_inv().get("items", []))
            return self._send(200, {"ok": True, "container": next(
                c for c in view["containers"] if c["id"] == new["id"])})

        if path == "/api/zones":
            # {action:"create", name} / {action:"rename", id, name} / {action:"delete", id}.
            # Deleting a zone leaves its containers zone-less, nothing else.
            action = (payload.get("action") or "create").strip()
            doc = load_containers()
            name = str(payload.get("name") or "").strip()
            zone = next((z for z in doc["zones"] if z.get("id") == payload.get("id")), None)
            if action in ("rename", "delete") and not zone:
                return self._send(404, {"error": "zone not found"})
            if action in ("create", "rename"):
                if not name:
                    return self._send(400, {"error": "name required"})
                if any(z.get("name", "").lower() == name.lower() and z is not zone
                       for z in doc["zones"]):
                    return self._send(400, {"error": "a zone with that name exists"})
            if action == "create":
                zone = {"id": new_id("z-", {z.get("id") for z in doc["zones"]}), "name": name}
                doc["zones"].append(zone)
            elif action == "rename":
                zone["name"] = name
            elif action == "delete":
                doc["zones"] = [z for z in doc["zones"] if z is not zone]
                for c in doc["containers"]:
                    if c.get("zone") == zone["id"]:
                        c["zone"] = None
            else:
                return self._send(400, {"error": "unknown action"})
            save_containers(doc)
            return self._send(200, {"ok": True, "zone": zone})

        if path == "/api/capture":
            data_url = payload.get("image", "")
            if "," in data_url:
                data_url = data_url.split(",", 1)[1]
            try:
                img = base64.b64decode(data_url)
            except Exception:
                return self._send(400, {"error": "bad image"})
            ext = image_ext(img)
            if not ext:
                return self._send(400, {"error": "not a PNG, JPEG or WebP image"})
            name = "cap-%d%s" % (int(time.time() * 1000), ext)
            with open(os.path.join(CAPTURES, name), "wb") as f:
                f.write(img)
            return self._send(200, {"ok": True, "file": name})

        if path == "/api/model":
            # Attach a 3D model file to an existing item. Body:
            #   {id, filename, data}  — data is base64 (a data: URL prefix is ok).
            # The file lands in models/ as model-<ms>-<safe original name>.<ext>
            # and its name is appended to the item's "models" array.
            item_id = (payload.get("id") or "").strip()
            filename = os.path.basename((payload.get("filename") or "").strip())
            ext = os.path.splitext(filename)[1].lower()
            if not item_id or not filename:
                return self._send(400, {"error": "id and filename required"})
            if ext not in MODEL_EXTS:
                return self._send(400, {"error": "unsupported model type %r (allowed: %s)"
                                        % (ext, ", ".join(MODEL_EXTS))})
            data = payload.get("data", "")
            if "," in data[:100]:            # tolerate a data: URL
                data = data.split(",", 1)[1]
            try:
                blob = base64.b64decode(data)
            except Exception:
                return self._send(400, {"error": "bad base64 data"})
            if not blob:
                return self._send(400, {"error": "empty model data"})
            inv = load_inv()
            item = next((it for it in inv.get("items", []) if it.get("id") == item_id), None)
            if not item:
                return self._send(404, {"error": "item not found"})
            stem = os.path.splitext(filename)[0]
            safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in stem)[:80] or "model"
            name = "model-%d-%s%s" % (int(time.time() * 1000), safe, ext)
            with open(os.path.join(MODELS, name), "wb") as f:
                f.write(blob)
            models = item.get("models") or []
            models.append(name)
            item["models"] = models
            save_inv(inv)
            return self._send(200, {"ok": True, "file": name, "id": item_id,
                                    "models": models})

        if path == "/api/model/delete":
            # Detach a model from an item and delete the file from models/.
            item_id = (payload.get("id") or "").strip()
            fname = os.path.basename((payload.get("file") or "").strip())
            if not item_id or not fname:
                return self._send(400, {"error": "id and file required"})
            inv = load_inv()
            item = next((it for it in inv.get("items", []) if it.get("id") == item_id), None)
            if not item:
                return self._send(404, {"error": "item not found"})
            models = item.get("models") or []
            if fname not in models:
                return self._send(404, {"error": "model not on this item"})
            item["models"] = [m for m in models if m != fname]
            save_inv(inv)
            try:
                os.remove(os.path.join(MODELS, fname))
            except OSError:
                pass
            return self._send(200, {"ok": True, "models": item["models"]})

        if path == "/api/confirm":
            # User confirmed an item (one OR MORE captured frames) with
            # quantity/notes. Write a single sidecar JSON listing all the
            # photos for this item; this is what Claude watches for.
            files = payload.get("files")
            if not files:
                single = payload.get("file")
                files = [single] if single else []
            files = [os.path.basename(x) for x in files if x]
            files = [x for x in files if os.path.exists(os.path.join(CAPTURES, x))]
            if not files:
                return self._send(400, {"error": "no valid captures"})
            meta = {
                "files": files,
                "note": str(payload.get("note") or "").strip(),
                "container": payload.get("container"),
                "created": int(time.time() * 1000),
            }
            if "location" in payload and not payload.get("container"):
                meta.pop("container")          # an older capture client
                meta["location"] = payload["location"]
            if payload.get("quantity") is not None:
                meta["quantity"] = payload["quantity"]
            if not self._places(meta):
                return
            sidecar = os.path.splitext(files[0])[0] + ".json"
            with open(os.path.join(CAPTURES, sidecar), "w") as f:
                json.dump(meta, f)
            return self._send(200, {"ok": True, "sidecar": sidecar})

        if path == "/api/ingest":
            # Upsert an identified item by id (used by Claude's bridge). Replaces
            # an existing item with the same id, else appends. No review marker.
            item = dict(payload)
            if not item.get("id"):
                return self._send(400, {"error": "id required"})
            if not self._places(item):
                return
            inv = load_inv()
            items = inv.setdefault("items", [])
            for idx, it in enumerate(items):
                if it.get("id") == item["id"]:
                    merged = {**it, **item}
                    apply_scalar_stock_edit(merged, item)
                    items[idx] = normalize_stock(merged)
                    break
            else:
                item.setdefault("quantity", 1)
                items.append(normalize_stock(item))
            save_inv(inv)
            return self._send(200, {"ok": True, "id": item["id"]})

        if path == "/api/add":
            # Manually add a new item from the dashboard.
            item = dict(payload)
            if not self._places(item):
                return
            inv = load_inv()
            item.setdefault("id", "item-%d" % int(time.time() * 1000))
            item.setdefault("quantity", 1)
            normalize_stock(item)
            inv.setdefault("items", []).append(item)
            save_inv(inv)
            if item.get("images"):
                write_review(item["id"], item["images"], "manual add with photos")
            return self._send(200, {"ok": True, "item": item})

        if path == "/api/update":
            # Merge edited fields into an existing item by id. If the edit adds
            # new photos, drop a review marker so Claude re-examines them.
            item_id = payload.get("id")
            inv = load_inv()
            for it in inv.get("items", []):
                if it.get("id") == item_id:
                    payload = dict(payload)
                    if not self._places(payload):
                        return
                    old_imgs = list(it.get("images", []))
                    had_loc = bool(it.get("container"))
                    for k, v in payload.items():
                        if k != "id":
                            it[k] = v
                    # A bare quantity/location edit (the card's +/- buttons, an
                    # agent) lands on the primary stash of a split item.
                    apply_scalar_stock_edit(it, payload)
                    normalize_stock(it)
                    # An undelivered (ordered) item that gets a container has
                    # arrived: clear the flag unless the edit sets it explicitly.
                    if (it.get("undelivered") and "undelivered" not in payload
                            and not had_loc and it.get("container")):
                        it["undelivered"] = False
                    save_inv(inv)
                    new_imgs = [x for x in it.get("images", []) if x not in old_imgs]
                    if new_imgs:
                        write_review(item_id, new_imgs, "photos added via edit")
                    return self._send(200, {"ok": True, "newImages": new_imgs})
            return self._send(404, {"error": "item not found"})

        if path == "/api/delete":
            item_id = payload.get("id")
            inv = load_inv()
            before = len(inv.get("items", []))
            inv["items"] = [it for it in inv.get("items", []) if it.get("id") != item_id]
            save_inv(inv)
            return self._send(200, {"ok": True, "removed": before - len(inv["items"])})

        if path == "/api/pending":
            # Claude posts a duplicate decision for the user to resolve in the UI.
            # matches = every similar existing stash (the user picks which bag).
            matches = payload.get("matches")
            if not matches and payload.get("match_id"):
                matches = [payload["match_id"]]
            candidate = dict(payload.get("candidate") or {})
            if not self._places(candidate):
                return
            p = load_pending()
            rec = {
                "pending_id": "pend-%d" % int(time.time() * 1000),
                "matches": matches or [],
                "candidate": candidate,
            }
            p.setdefault("pending", []).append(rec)
            save_pending(p)
            return self._send(200, {"ok": True, "pending_id": rec["pending_id"]})

        if path == "/api/resolve":
            # User acted on a pending-duplicate card. They can edit the candidate
            # (name/qty/location/...) and choose which bag (target_id) to merge into,
            # or create a separate entry.
            pid = payload.get("pending_id")
            action = payload.get("action")
            target_id = payload.get("target_id")
            ov = payload.get("candidate") or {}
            p = load_pending()
            rec = next((x for x in p.get("pending", []) if x["pending_id"] == pid), None)
            if not rec:
                return self._send(404, {"error": "pending not found"})
            inv = load_inv()
            cand = dict(rec.get("candidate", {}))
            for k in ("name", "category", "quantity", "container", "location", "condition", "notes"):
                if k in ov:
                    cand[k] = ov[k]
            if "location" in ov and "container" not in ov:
                cand.pop("container", None)     # a legacy location override wins
            if not self._places(cand):
                return
            if action == "separate":
                item = dict(cand)
                item.setdefault("id", "item-%d" % int(time.time() * 1000))
                item.setdefault("quantity", 1)
                inv.setdefault("items", []).append(normalize_stock(item))
            elif action in ("add", "remove", "set"):
                tid = target_id or rec.get("match_id") or (rec.get("matches") or [None])[0]
                target = next((it for it in inv.get("items", []) if it.get("id") == tid), None)
                if not target:
                    return self._send(404, {"error": "target item not found"})
                cq = int(cand.get("quantity") or 0)
                tq = int(target.get("quantity") or 0)
                if action == "add":
                    target["quantity"] = tq + cq
                elif action == "remove":
                    target["quantity"] = max(0, tq - cq)
                else:  # set
                    target["quantity"] = cq
                # A split stash absorbs the change in its primary location.
                apply_scalar_stock_edit(target, {"quantity": target["quantity"]})
                normalize_stock(target)
                imgs = target.get("images") or ([target["image"]] if target.get("image") else [])
                for im in cand.get("images", []):
                    if im not in imgs:
                        imgs.append(im)
                target["images"] = imgs
            elif action != "dismiss":
                return self._send(400, {"error": "unknown action"})
            p["pending"] = [x for x in p.get("pending", []) if x["pending_id"] != pid]
            save_pending(p)
            save_inv(inv)
            return self._send(200, {"ok": True})

        if path == "/api/wishlist/resolve":
            # Admin manages a purchase request: change status / edit fields / delete.
            #   action="update" -> merge any WISH_FIELDS present into the entry
            #   action="delete" -> remove the entry
            wid = payload.get("id")
            action = payload.get("action") or "update"
            w = load_wishlist()
            wl = w.get("wishlist", [])
            rec = next((x for x in wl if x.get("id") == wid), None)
            if not rec:
                return self._send(404, {"error": "request not found"})
            if action == "delete":
                w["wishlist"] = [x for x in wl if x.get("id") != wid]
                save_wishlist(w)
                return self._send(200, {"ok": True, "removed": 1})
            if action == "update":
                for k in WISH_FIELDS:
                    if k in payload:
                        if k == "quantity":
                            rec[k] = int(payload.get(k) or 1)
                        elif k == "status":
                            s = (payload.get(k) or "").strip().lower()
                            rec[k] = s if s in WISH_STATUSES else rec.get("status", "requested")
                        else:
                            rec[k] = (payload.get(k) or "").strip() if isinstance(payload.get(k), str) else payload.get(k)
                save_wishlist(w)
                return self._send(200, {"ok": True, "request": rec})
            return self._send(400, {"error": "unknown action"})

        return self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass  # quiet


def _usage():
    print("usage:")
    print("  python3 server.py                      run the server")
    print("  python3 server.py adduser NAME PASS ROLE   add/update a user (ROLE: admin|viewer)")
    print("  python3 server.py deluser NAME             remove a user")
    print("  python3 server.py users                     list users + roles")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "adduser" and len(sys.argv) in (4, 5):
            name = sys.argv[2]
            if len(sys.argv) == 5:
                password, role = sys.argv[3], sys.argv[4]
            else:  # adduser NAME ROLE  -> prompt for the password (hidden, not logged)
                import getpass
                role = sys.argv[3]
                password = getpass.getpass("password for '%s': " % name)
            add_user(name, password, role)
        elif cmd == "deluser" and len(sys.argv) == 3:
            users = load_users()
            users.pop(sys.argv[2], None)
            _atomic_write(USERS, json.dumps({"users": users}, indent=2))
            print("removed", sys.argv[2])
        elif cmd == "users":
            us = load_users()
            print("auth: " + ("ENABLED" if us else "DISABLED (open) — add a user to enable"))
            for n, u in us.items():
                print("  %-20s %s" % (n, u.get("role")))
        else:
            _usage()
        sys.exit(0)

    migrated = migrate_to_v2()
    if migrated:
        print("Migrated inventory to schema %d: %d item(s), %d container(s) created from "
              "location strings, %d queued capture(s) updated (backup: data/*.pre-v2-*.bak)"
              % (SCHEMA, migrated["items"], migrated["containers_created"], migrated["sidecars"]))
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    auth = "ENABLED" if load_users() else "DISABLED (open — run 'adduser' to secure)"
    print("Inventory server on %s:%d   [auth: %s]" % (HOST, PORT, auth))
    print("  Capture page : http://localhost:%d/capture" % PORT)
    print("  Dashboard    : http://localhost:%d/dashboard" % PORT)
    srv.serve_forever()
