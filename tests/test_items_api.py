#!/usr/bin/env python3
"""Covers the summaries-then-details contract: GET /api/items and the MCP tools.

Runs a THROWAWAY copy of server.py in a temp dir (server.py hardcodes DATA
relative to its own file, so running the real one would write into the repo's
data/). The MCP layer is driven in-process against that server.

    python3 tests/test_items_api.py    # exits non-zero on any failure
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _json(url, body=None, method="GET"):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        return json.loads(resp.read().decode())


def main():
    tmp = tempfile.mkdtemp(prefix="inv-items-test-")
    proc = None
    try:
        shutil.copy(os.path.join(ROOT, "server.py"), os.path.join(tmp, "server.py"))
        port = _free_port()
        base = "http://127.0.0.1:%d" % port
        env = dict(os.environ, INVENTORY_HOST="127.0.0.1", INVENTORY_PORT=str(port))
        proc = subprocess.Popen([sys.executable, "server.py"], cwd=tmp, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                _json(base + "/api/inventory")
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("server did not start")

        # seed two items, one of them with the long fields a summary must drop
        a = _json(base + "/api/add", {"name": "Widget A", "category": "Misc",
                                      "quantity": 3, "location": "Bag 1",
                                      "notes": "SKU #123 long spec text",
                                      "condition": "New"}, "POST")["item"]["id"]
        b = _json(base + "/api/add", {"name": "Widget B", "category": "Power",
                                      "undelivered": True}, "POST")["item"]["id"]

        # --- HTTP: no args = one compact row per item, whole inventory ---
        res = _json(base + "/api/items")
        assert res["count"] == 2, res
        rows = {r["id"]: r for r in res["items"]}
        ra = rows[a]
        assert ra["name"] == "Widget A" and ra["quantity"] == 3 and ra["location"] == "Bag 1", ra
        assert ra["images"] == 0, "images must be a count in a summary row: %r" % ra
        assert "notes" not in ra and "condition" not in ra, "summary row must stay compact: %r" % ra
        assert rows[b].get("undelivered") is True, rows[b]

        # --- HTTP: ids= returns full records, in order, with a missing list ---
        res = _json(base + "/api/items?ids=%s,nope,%s" % (b, a))
        assert [i["id"] for i in res["items"]] == [b, a], res
        assert res["missing"] == ["nope"], res
        assert res["items"][1]["notes"] == "SKU #123 long spec text", res["items"][1]

        # --- MCP tool layer against the same server ---
        os.environ["INVENTORY_URL"] = base
        sys.path.insert(0, ROOT)
        import mcp_server  # imported here so BASE picks up INVENTORY_URL

        names = {t["name"] for t in mcp_server.TOOLS}
        assert "search_inventory" not in names, "search tool is back: %s" % names
        assert {"list_all_items", "get_items", "inventory_summary",
                "get_item_images"} <= names, names

        compact = mcp_server.call_tool("list_all_items", {})
        assert compact["count"] == 2 and "notes" not in compact["items"][0], compact
        full = mcp_server.call_tool("list_all_items", {"full": True})
        assert full["count"] == 2 and any(i.get("notes") for i in full["items"]), full

        det = mcp_server.call_tool("get_items", {"ids": [a]})
        assert det["items"][0]["notes"] == "SKU #123 long spec text", det
        assert "missing" not in det, det
        det = mcp_server.call_tool("get_items", {"ids": [a, "nope"]})
        assert det["missing"] == ["nope"], det
        for bad in ({}, {"ids": []}):
            try:
                mcp_server.call_tool("get_items", bad)
                raise AssertionError("get_items accepted %r" % bad)
            except ValueError:
                pass

        print("PASS: /api/items summaries + details and the MCP tool layer agree")
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
