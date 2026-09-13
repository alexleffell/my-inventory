#!/usr/bin/env python3
"""Covers `stock`: an item split across several bags/shelves.

The contract is that `stock` is the source of truth when it holds two or more
rows, while `quantity` (the total) and `location` (the primary stash) keep
meaning exactly what they meant when every item lived in one place.

Runs a THROWAWAY copy of server.py in a temp dir (server.py hardcodes DATA
relative to its own file, so running the real one would write into the repo's
data/).

    python3 tests/test_stock_locations.py    # exits non-zero on any failure
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
    tmp = tempfile.mkdtemp(prefix="inv-stock-test-")
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

        def get(i):
            return _json(base + "/api/items?ids=" + i)["items"][0]

        # --- add split across two places: totals and primary derive from stock
        a = _json(base + "/api/add", {"name": "Resistor 10k", "category": "Passives",
                                      "stock": [{"location": "Bag 1", "quantity": 5},
                                                {"location": "Shelf A", "quantity": 3}]},
                  "POST")["item"]["id"]
        it = get(a)
        assert it["quantity"] == 8, it
        assert it["location"] == "Bag 1", it
        assert it["stock"] == [{"location": "Bag 1", "quantity": 5},
                               {"location": "Shelf A", "quantity": 3}], it

        # --- a summary row carries the split so an agent sees both places
        row = next(r for r in _json(base + "/api/items")["items"] if r["id"] == a)
        assert row["quantity"] == 8 and row["location"] == "Bag 1", row
        assert row["stock"][1]["location"] == "Shelf A", row

        # --- /api/summary counts the item once in every place it is stored
        locs = {l["name"]: l["items"] for l in _json(base + "/api/summary")["locations"]}
        assert locs.get("Bag 1") == 1 and locs.get("Shelf A") == 1, locs

        # --- a bare quantity edit (the card's +/- buttons) lands on the primary
        _json(base + "/api/update", {"id": a, "quantity": 9}, "POST")
        it = get(a)
        assert it["quantity"] == 9 and it["stock"][0]["quantity"] == 6, it
        assert it["stock"][1]["quantity"] == 3, it

        # --- editing the rows replaces the split
        _json(base + "/api/update", {"id": a, "stock": [{"location": "Bin 2", "quantity": 4},
                                                        {"location": "Bag 1", "quantity": 1}]}, "POST")
        it = get(a)
        assert it["quantity"] == 5 and it["location"] == "Bin 2", it

        # --- collapsing to ONE row drops `stock` entirely: a plain item again
        _json(base + "/api/update", {"id": a, "stock": [{"location": "Bin 2", "quantity": 4}]}, "POST")
        it = get(a)
        assert "stock" not in it, it
        assert it["quantity"] == 4 and it["location"] == "Bin 2", it

        # --- an item with no stock at all behaves exactly as before
        b = _json(base + "/api/add", {"name": "Plain", "quantity": 2,
                                      "location": "Bag 7"}, "POST")["item"]["id"]
        it = get(b)
        assert "stock" not in it and it["quantity"] == 2 and it["location"] == "Bag 7", it

        # --- undelivered clears only when a location ARRIVES, incl. via stock
        c = _json(base + "/api/add", {"name": "Ordered", "undelivered": True},
                  "POST")["item"]["id"]
        _json(base + "/api/update", {"id": c, "notes": "still waiting"}, "POST")
        assert get(c)["undelivered"] is True, get(c)
        _json(base + "/api/update", {"id": c, "stock": [{"location": "Bag 3", "quantity": 1},
                                                        {"location": "Bag 4", "quantity": 2}]}, "POST")
        it = get(c)
        assert it["undelivered"] is False and it["quantity"] == 3, it

        # --- ingest takes a split too (agents write through it)
        _json(base + "/api/ingest", {"id": b, "stock": [{"location": "Bag 7", "quantity": 2},
                                                        {"location": "Bag 8", "quantity": 6}]}, "POST")
        it = get(b)
        assert it["quantity"] == 8 and len(it["stock"]) == 2, it

        print("PASS: split stock stays consistent with quantity/location everywhere")
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
