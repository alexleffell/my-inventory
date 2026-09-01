#!/usr/bin/env python3
"""MCP server exposing the inventory to an LLM agent.

The tool set is deliberately search-free: `list_all_items` returns EVERY item as
a compact row in one call, and `get_items` returns the full records for the ids
the agent picked out of that list. Keyword search used to make agents declare a
part missing when it was in stock under a different name, so it was removed —
see "Agent access contract" in CLAUDE.md before adding one back.

Pure standard library — runs on the system python3 (no uv, no pip). Mostly
read-only: it calls the inventory's public HTTP API. Speaks the MCP stdio
transport (newline-delimited JSON-RPC 2.0).

Point it at a server with INVENTORY_URL (default: a local server):
    INVENTORY_URL=https://inventory.example.com  python3 mcp_server.py

Write tools (attach_item_model) need admin credentials, taken from
INVENTORY_ADMIN_USER / INVENTORY_ADMIN_PASS in the environment. Without them
the read tools and the public request_purchase still work.
"""
import sys
import os
import io
import json
import base64
import urllib.request
from urllib.parse import urlencode

BASE = os.environ.get("INVENTORY_URL", "http://localhost:8770").rstrip("/")

# Pillow is optional: when present we downscale + re-encode images so the base64
# payload stays small. Without it we fall back to sending the raw bytes.
try:
    from PIL import Image
    _HAVE_PIL = True
except Exception:
    _HAVE_PIL = False


# A real User-Agent is required — Cloudflare 403s the default "Python-urllib".
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _get(path):
    req = urllib.request.Request(BASE + path, headers={
        "Accept": "application/json",
        "User-Agent": _UA,
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST", headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _UA,
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _get_bytes(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(), (r.headers.get("Content-Type") or "")


def _post_admin(path, body):
    """POST with admin Basic auth from INVENTORY_ADMIN_USER/INVENTORY_ADMIN_PASS."""
    user = os.environ.get("INVENTORY_ADMIN_USER", "")
    pw = os.environ.get("INVENTORY_ADMIN_PASS", "")
    if not user or not pw:
        raise ValueError("admin credentials missing: set INVENTORY_ADMIN_USER and "
                         "INVENTORY_ADMIN_PASS in the MCP server's environment")
    token = base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST", headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _UA,
        "Authorization": "Basic " + token,
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".gif": "image/gif", ".webp": "image/webp"}


def _image_block(filename, max_dim=1024):
    """Fetch /captures/<filename> and return an MCP image content block.

    With Pillow we downscale to max_dim on the long edge and re-encode as JPEG
    (q80) to keep the base64 small; otherwise we pass the raw bytes through.
    """
    raw, ctype = _get_bytes("/captures/" + filename)
    mime = ctype.split(";")[0].strip() or _MIME.get(
        os.path.splitext(filename)[1].lower(), "application/octet-stream")
    if _HAVE_PIL:
        try:
            im = Image.open(io.BytesIO(raw))
            im.thumbnail((max_dim, max_dim), Image.LANCZOS)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80, optimize=True)
            raw, mime = buf.getvalue(), "image/jpeg"
        except Exception:
            pass  # fall back to the raw bytes we already have
    return {"type": "image", "data": base64.b64encode(raw).decode(), "mimeType": mime}


TOOLS = [
    {
        "name": "inventory_summary",
        "description": "Overview of Moshe's electronics inventory: total items, total units, and a breakdown by category and storage location (bag/bin).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_all_items",
        "description": (
            "THE first call for any question about what is in stock. Returns EVERY item in the "
            "inventory as a compact row (id, name, category, quantity, location, photo count, "
            "attached 3D model files) in one response — a few hundred rows. There is no search "
            "tool on purpose: read the whole list and pick what you need yourself, rather than "
            "guessing keywords. Then call get_items with the ids you care about for full details "
            "(notes, SKUs, specs). Pass full=true to get every field of every item at once."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "full": {"type": "boolean", "description": "true = every field of every item (large); default false = compact rows"},
            },
        },
    },
    {
        "name": "get_items",
        "description": (
            "Full records (notes, SKUs, specs, condition, photo filenames, 3D models) for specific "
            "items, by id. Get the ids from list_all_items. Ask for several at once."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"},
                        "description": "item ids, e.g. ['cap-1717000000000', 'item-1718000000000']"},
            },
            "required": ["ids"],
        },
    },
    {
        "name": "list_wishlist",
        "description": "List purchase requests — parts that have been asked to be bought and added to the inventory (the 'to buy' list). Each entry has project, part, type, reason, quantity and status (requested/ordered/bought). Check this before requesting to avoid duplicate asks.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_item_images",
        "description": (
            "Return the actual photos of inventory items so you can SEE the parts, not just their "
            "names. Give the item ids (from list_all_items). The first photo of an item shows the "
            "physical part; later ones are packaging/labels. Use this when a name alone is not "
            "enough to tell whether an item is the part you need."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"},
                        "description": "item ids whose photos to return"},
                "id": {"type": "string", "description": "a single item id (shorthand for ids)"},
                "max_images": {"type": "integer", "description": "max images returned total (default 20)"},
                "max_dimension": {"type": "integer", "description": "long-edge pixel cap per image (default 1024)"},
            },
        },
    },
    {
        "name": "attach_item_model",
        "description": "Attach a 3D model file (STL/3MF/OBJ/GLB/GLTF/STEP/SCAD) from the local filesystem to an inventory item, e.g. a printable holder, case or the part's own CAD model. The file is uploaded to the inventory server and listed in the item's 'models' field. Requires admin credentials in the MCP server environment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "exact item id, e.g. cap-1717000000000"},
                "file_path": {"type": "string", "description": "absolute path of the local model file to upload"},
                "filename": {"type": "string", "description": "override the stored filename (defaults to the file's basename)"},
            },
            "required": ["id", "file_path"],
        },
    },
    {
        "name": "get_item_model",
        "description": "Download a 3D model attached to an inventory item to a local file so it can be opened, edited, or printed. Give an item 'id' (downloads its models; lists them if it has several and no 'file' is given) or a specific model 'file' name from the item's 'models' field.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "item id whose model(s) to fetch"},
                "file": {"type": "string", "description": "specific model filename, e.g. model-1717000000000-holder.stl"},
                "save_dir": {"type": "string", "description": "local directory to save into (default: a temp dir)"},
            },
        },
    },
    {
        "name": "request_purchase",
        "description": "Request that a part be bought and added to Moshe's inventory. Use when a project needs a component that isn't in stock. Be specific: the exact part, what kind of product it is, which project it's for, and why it's needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "part": {"type": "string", "description": "The exact part to buy, e.g. '10kΩ 1/4W resistor' or 'ESP32-WROOM-32 dev board'"},
                "project": {"type": "string", "description": "The project this is for, e.g. 'soil-moisture logger'"},
                "type": {"type": "string", "description": "Type/category of product, e.g. 'Resistor', 'Microcontroller', 'Sensor'"},
                "reason": {"type": "string", "description": "Why it's needed — what it's used for in the project"},
                "quantity": {"type": "integer", "description": "How many to buy (default 1)"},
                "requested_by": {"type": "string", "description": "Who/what is asking, e.g. the agent or task name (optional)"},
            },
            "required": ["part"],
        },
    },
]


def _item_images(args):
    """Resolve item(s) by id and return interleaved text + image content blocks."""
    ids = [str(x).strip() for x in (args.get("ids") or []) if str(x).strip()]
    single = (args.get("id") or "").strip()
    if single and single not in ids:
        ids.append(single)
    if not ids:
        raise ValueError("provide 'ids' (item ids from list_all_items)")
    max_images = int(args.get("max_images") or 20)
    max_dim = int(args.get("max_dimension") or 1024)

    found, missing = _items_by_ids(ids)
    if not found:
        raise ValueError("no item with id %s" % ", ".join(missing))

    content, sent, skipped = [], 0, 0
    for it in found:
        imgs = it.get("images") or []
        label = "%s — %s (id %s, %s, qty %s) — %d image(s)" % (
            it.get("name") or "⏳ unidentified", it.get("category") or "?",
            it.get("id"), it.get("location") or "?", it.get("quantity", "?"), len(imgs))
        content.append({"type": "text", "text": label})
        for fn in imgs:
            if sent >= max_images:
                skipped += 1
                continue
            try:
                content.append(_image_block(fn, max_dim))
                sent += 1
            except Exception as e:
                content.append({"type": "text", "text": "  (failed to load %s: %s)" % (fn, e)})
    if missing:
        content.append({"type": "text", "text": "no such item id: %s" % ", ".join(missing)})
    if skipped:
        content.append({"type": "text",
                        "text": "… %d more image(s) omitted (max_images=%d). Raise max_images to see them."
                        % (skipped, max_images)})
    if sent == 0:
        content.append({"type": "text", "text": "(these items have no photos)"})
    return content


def _attach_model(args):
    iid = (args.get("id") or "").strip()
    fp = (args.get("file_path") or "").strip()
    if not iid or not fp:
        raise ValueError("id and file_path are required")
    if not os.path.isfile(fp):
        raise ValueError("no such file: %s" % fp)
    with open(fp, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    filename = (args.get("filename") or "").strip() or os.path.basename(fp)
    return _post_admin("/api/model", {"id": iid, "filename": filename, "data": data})


def _get_model(args):
    iid = (args.get("id") or "").strip()
    fname = (args.get("file") or "").strip()
    if not iid and not fname:
        raise ValueError("provide an item 'id' or a model 'file' name")
    files = [fname] if fname else []
    if not files:
        items = [it for it in _get("/api/inventory").get("items", [])
                 if it.get("id") == iid]
        if not items:
            raise ValueError("no item with id %r" % iid)
        files = items[0].get("models") or []
        if not files:
            raise ValueError("item %s has no 3D models attached" % iid)
    save_dir = (args.get("save_dir") or "").strip()
    if not save_dir:
        import tempfile
        save_dir = tempfile.mkdtemp(prefix="inventory-models-")
    os.makedirs(save_dir, exist_ok=True)
    saved = []
    for fn in files:
        raw, _ctype = _get_bytes("/models/" + fn)
        out = os.path.join(save_dir, os.path.basename(fn))
        with open(out, "wb") as f:
            f.write(raw)
        saved.append({
            "file": fn,
            "saved_to": out,
            "bytes": len(raw),
            "url": BASE + "/models/" + fn,
        })
    return {"ok": True, "models": saved}


def _items_by_ids(ids):
    """Full item records for ids, in the order asked for, plus the ids that missed."""
    data = _get("/api/items?" + urlencode({"ids": ",".join(ids)}))
    return data.get("items", []), data.get("missing", [])


def call_tool(name, args):
    if name == "inventory_summary":
        return _get("/api/summary")
    if name == "list_all_items":
        if args.get("full"):
            items = _get("/api/inventory").get("items", [])
            return {"count": len(items), "items": items}
        return _get("/api/items")
    if name == "get_items":
        ids = [str(x).strip() for x in (args.get("ids") or []) if str(x).strip()]
        single = (args.get("id") or "").strip() if args.get("id") else ""
        if single and single not in ids:
            ids.append(single)
        if not ids:
            raise ValueError("ids is required — get them from list_all_items")
        found, missing = _items_by_ids(ids)
        out = {"count": len(found), "items": found}
        if missing:
            out["missing"] = missing
        return out
    if name == "get_item_images":
        return _item_images(args)
    if name == "list_wishlist":
        return _get("/api/wishlist")
    if name == "attach_item_model":
        return _attach_model(args)
    if name == "get_item_model":
        return _get_model(args)
    if name == "request_purchase":
        part = (args.get("part") or "").strip()
        if not part:
            raise ValueError("part is required (what to buy)")
        return _post("/api/wishlist", {
            "part": part,
            "project": args.get("project", "") or "",
            "type": args.get("type", "") or "",
            "reason": args.get("reason", "") or "",
            "quantity": args.get("quantity", 1) or 1,
            "requested_by": args.get("requested_by", "") or "",
        })
    raise ValueError("unknown tool: %s" % name)


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "moshes-inventory", "version": "1.0.0"},
            }})
        elif method == "notifications/initialized":
            pass  # notification, no reply
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {}) or {}
            try:
                data = call_tool(params.get("name"), params.get("arguments") or {})
                # A tool may return ready-made MCP content blocks (e.g. images);
                # anything else is JSON we wrap as a single text block.
                content = data if isinstance(data, list) else \
                    [{"type": "text", "text": json.dumps(data, indent=2)}]
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": content}})
            except Exception as e:  # report tool errors back to the model
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "error: %s" % e}], "isError": True}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()
