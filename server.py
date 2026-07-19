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
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
DATA = os.path.join(ROOT, "data")
CAPTURES = os.path.join(ROOT, "captures")
MODELS = os.path.join(ROOT, "models")
INVENTORY = os.path.join(DATA, "inventory.json")
PENDING = os.path.join(DATA, "pending.json")
WISHLIST = os.path.join(DATA, "wishlist.json")

for d in (DATA, CAPTURES, MODELS):
    os.makedirs(d, exist_ok=True)
if not os.path.exists(INVENTORY):
    with open(INVENTORY, "w") as f:
        json.dump({"items": []}, f)
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


def query_items(items, search, category, location, has_model, limit):
    fields = ("id", "name", "category", "quantity", "location", "condition", "notes", "images", "models")
    terms = (search or "").lower().split()
    category, location = (category or "").lower(), (location or "").lower()
    matches = []
    for it in items:
        models = it.get("models") or []
        if category and category != (it.get("category", "") or "").lower():
            continue
        if location and location != (it.get("location", "") or "").lower():
            continue
        if has_model is not None and bool(models) != has_model:
            continue
        name = str(it.get("name", "") or "").lower()
        catloc = " ".join(str(it.get(k, "") or "") for k in ("category", "location")).lower()
        detail = " ".join([str(it.get("notes", "") or "")] + [str(model) for model in models]).lower()
        if terms and not all(term in " ".join((name, catloc, detail)) for term in terms):
            continue
        score = sum(3 * (term in name) + 2 * (term in catloc) + (term in detail) for term in terms)
        matches.append((score, it))
    if terms:
        matches = sorted(matches, key=lambda match: -match[0])
    res = [{k: it.get(k) for k in fields} for score, it in matches]
    return res[:limit] if limit > 0 else res


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

    def _serve_file(self, path):
        if not os.path.exists(path) or not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        ext = os.path.splitext(path)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

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

    def do_GET(self):
        path = self.path.split("?")[0]
        # Public viewing: all GET endpoints (dashboard, APIs, photos, capture page)
        # are open. Editing requires logging in — every POST is gated to admin.
        if path == "/" or path == "/dashboard":
            return self._serve_file(os.path.join(PUBLIC, "dashboard.html"))
        if path == "/capture":
            return self._serve_file(os.path.join(PUBLIC, "capture.html"))
        if path == "/buy" or path == "/wishlist":
            return self._serve_file(os.path.join(PUBLIC, "wishlist.html"))
        if path.startswith("/vendor/"):
            # Vendored static assets (the 3D viewer library + its wasm). No path
            # traversal: collapse the request and refuse anything outside public/vendor.
            rel = os.path.normpath(path.lstrip("/"))
            if rel.startswith("vendor" + os.sep) or rel == "vendor":
                return self._serve_file(os.path.join(PUBLIC, rel))
            return self._send(404, {"error": "not found"})
        if path == "/api/inventory":
            with open(INVENTORY) as f:
                return self._send(200, f.read(), "application/json")
        if path == "/api/pending":
            with open(PENDING) as f:
                return self._send(200, f.read(), "application/json")
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
            cats, locs, units = {}, {}, 0
            for it in items:
                q = int(it.get("quantity") or 0)
                units += q
                c = it.get("category") or "Uncategorized"
                l = it.get("location") or "Unfiled"
                cats[c] = cats.get(c, 0) + 1
                locs[l] = locs.get(l, 0) + 1
            wl = load_wishlist().get("wishlist", [])
            return self._send(200, {
                "total_items": len(items),
                "total_units": units,
                "categories": [{"name": k, "items": v} for k, v in sorted(cats.items(), key=lambda x: -x[1])],
                "locations": [{"name": k, "items": v} for k, v in sorted(locs.items(), key=lambda x: -x[1])],
                "wishlist_total": len(wl),
                "wishlist_open": sum(1 for w in wl if w.get("status") != "bought"),
            })
        if path == "/api/queue":
            # Public: uploads awaiting identification (so the UI can show a queue).
            inv = load_inv()
            logged = set(img for i in inv.get("items", []) for img in (i.get("images") or []))
            # Photos already attached to a PENDING duplicate decision are NOT awaiting
            # identification — they await the user's resolution. Exclude them so the
            # queue doesn't double-count them (mirrors deploy/find_unprocessed.py).
            try:
                with open(PENDING) as pf:
                    pend = json.load(pf)
                logged |= set(img for p in pend.get("pending", [])
                              for img in (p.get("candidate", {}).get("images") or []))
            except Exception:
                pass
            q = []
            # capture-page confirms not yet turned into items
            for f in sorted(glob.glob(os.path.join(CAPTURES, "cap-*.json"))):
                try:
                    d = json.load(open(f))
                except Exception:
                    continue
                files = d.get("files") or ([d["file"]] if d.get("file") else [])
                if files and not any(x in logged for x in files):
                    q.append({"images": files, "quantity": d.get("quantity"),
                              "location": d.get("location"), "status": "queued"})
            # items created but not yet named (dashboard adds being identified)
            for it in inv.get("items", []):
                if not (it.get("name") or "").strip():
                    q.append({"id": it.get("id"), "images": it.get("images") or [],
                              "quantity": it.get("quantity"), "location": it.get("location"),
                              "status": "identifying"})
            return self._send(200, {"count": len(q), "queue": q})
        if path == "/api/query":
            # Public search/filter. Params: search|q, category, location, has_model, limit.
            qs = parse_qs(urlparse(self.path).query)
            g = lambda k: (qs.get(k, [""])[0] or "").strip()
            search = g("search") or g("q")
            cat, loc, model = g("category"), g("location"), g("has_model").lower()
            has_model = True if model in ("1", "true", "yes") else False if model in ("0", "false", "no") else None
            try:
                limit = int(g("limit") or 0)
            except ValueError:
                limit = 0
            res = query_items(load_inv().get("items", []), search, cat, loc, has_model, limit)
            return self._send(200, {"count": len(res), "items": res})
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

        if path == "/api/capture":
            data_url = payload.get("image", "")
            if "," in data_url:
                data_url = data_url.split(",", 1)[1]
            try:
                img = base64.b64decode(data_url)
            except Exception:
                return self._send(400, {"error": "bad image"})
            name = "cap-%d.png" % int(time.time() * 1000)
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
                "quantity": payload.get("quantity", 1),
                "note": payload.get("note", ""),
                "location": payload.get("location", ""),
            }
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
            inv = load_inv()
            items = inv.setdefault("items", [])
            for idx, it in enumerate(items):
                if it.get("id") == item["id"]:
                    items[idx] = {**it, **item}
                    break
            else:
                item.setdefault("quantity", 1)
                items.append(item)
            save_inv(inv)
            return self._send(200, {"ok": True, "id": item["id"]})

        if path == "/api/add":
            # Manually add a new item from the dashboard.
            inv = load_inv()
            item = dict(payload)
            item.setdefault("id", "item-%d" % int(time.time() * 1000))
            item.setdefault("quantity", 1)
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
                    old_imgs = list(it.get("images", []))
                    for k, v in payload.items():
                        if k != "id":
                            it[k] = v
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
            p = load_pending()
            rec = {
                "pending_id": "pend-%d" % int(time.time() * 1000),
                "matches": matches or [],
                "candidate": payload.get("candidate", {}),
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
            for k in ("name", "category", "quantity", "location", "condition", "notes"):
                if k in ov:
                    cand[k] = ov[k]
            if action == "separate":
                item = dict(cand)
                item.setdefault("id", "item-%d" % int(time.time() * 1000))
                item.setdefault("quantity", 1)
                inv.setdefault("items", []).append(item)
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

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    auth = "ENABLED" if load_users() else "DISABLED (open — run 'adduser' to secure)"
    print("Inventory server on %s:%d   [auth: %s]" % (HOST, PORT, auth))
    print("  Capture page : http://localhost:%d/capture" % PORT)
    print("  Dashboard    : http://localhost:%d/dashboard" % PORT)
    srv.serve_forever()
