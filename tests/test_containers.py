#!/usr/bin/env python3
"""Covers containers and zones: /api/containers, /api/zones, and how item
writes reference containers (ids, legacy location strings, unknown ids).

    python3 tests/test_containers.py    # exits non-zero on any failure
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Server  # noqa: E402


def main():
    with Server() as srv:
        def ok(path, body):
            code, res = srv.post(path, body)
            assert code == 200, (path, body, code, res)
            return res

        def view():
            return {c["id"]: c for c in srv.get("/api/containers")[1]["containers"]}

        # --- zones: create, unique names, rename
        bench = ok("/api/zones", {"action": "create", "name": "Electronics bench"})["zone"]["id"]
        deep = ok("/api/zones", {"action": "create", "name": "Deep storage"})["zone"]["id"]
        assert bench.startswith("z-") and len(bench) == 8, bench
        assert srv.post("/api/zones", {"action": "create", "name": "electronics BENCH"})[0] == 400
        ok("/api/zones", {"action": "rename", "id": deep, "name": "Attic"})
        assert [z["name"] for z in srv.get("/api/containers")[1]["zones"]] == ["Electronics bench", "Attic"]

        # --- containers: create with type + zone, nest a drawer inside
        cab = ok("/api/containers", {"name": "Red cabinet", "type": "cabinet", "zone": bench})["container"]
        assert cab["id"].startswith("c-") and cab["path"] == "Red cabinet", cab
        drawer = ok("/api/containers", {"name": "Drawer 3", "type": "drawer",
                                        "parent": cab["id"]})["container"]
        assert drawer["path"] == "Red cabinet › Drawer 3", drawer
        assert drawer["zone"] is None and drawer["effective_zone"] == bench, drawer
        assert drawer["zone_name"] == "Electronics bench", drawer

        # --- validation: name, type, parent, zone, cycles, nested zone
        assert srv.post("/api/containers", {"name": " "})[0] == 400
        assert srv.post("/api/containers", {"name": "X", "type": "spaceship"})[0] == 400
        assert srv.post("/api/containers", {"name": "X", "parent": "c-nope"})[0] == 400
        assert srv.post("/api/containers", {"name": "X", "zone": "z-nope"})[0] == 400
        assert srv.post("/api/containers", {"action": "update", "id": cab["id"],
                                            "parent": drawer["id"]})[0] == 400   # cycle
        assert srv.post("/api/containers", {"action": "update", "id": cab["id"],
                                            "parent": cab["id"]})[0] == 400      # self
        assert srv.post("/api/containers", {"action": "update", "id": drawer["id"],
                                            "zone": deep})[0] == 400             # inherits
        assert srv.post("/api/containers", {"action": "update", "id": "c-nope", "name": "X"})[0] == 404

        # --- rename + move: the path and zone follow
        ok("/api/containers", {"action": "update", "id": cab["id"], "name": "Big cabinet"})
        assert view()[drawer["id"]]["path"] == "Big cabinet › Drawer 3"
        ok("/api/containers", {"action": "update", "id": cab["id"], "zone": deep})
        assert view()[drawer["id"]]["effective_zone"] == deep
        bin1 = ok("/api/containers", {"name": "Bin 1", "type": "bin", "zone": bench})["container"]["id"]
        ok("/api/containers", {"action": "update", "id": bin1, "parent": cab["id"]})
        assert view()[bin1]["zone"] is None, "moving under a parent must drop its own zone"

        # --- items reference containers by id; unknown ids are refused
        item = ok("/api/add", {"name": "Flush cutters", "container": drawer["id"]})["item"]
        assert item["container"] == drawer["id"], item
        code, res = srv.post("/api/add", {"name": "Ghost", "container": "c-nope"})
        assert code == 400 and "unknown container" in res["error"], res
        assert srv.post("/api/update", {"id": item["id"], "container": "c-nope"})[0] == 400
        assert srv.post("/api/ingest", {"id": "x-1", "stock": [{"container": "c-nope", "quantity": 1},
                                                               {"container": bin1, "quantity": 1}]})[0] == 400
        row = next(r for r in srv.get("/api/items")[1]["items"] if r["id"] == item["id"])
        assert row["container"] == drawer["id"] and row["location"] == "Big cabinet › Drawer 3", row
        assert view()[drawer["id"]]["item_count"] == 1 and view()[cab["id"]]["children"] == 2

        # --- legacy location strings: a path match finds the nested drawer...
        legacy = ok("/api/add", {"name": "Old agent", "location": "big cabinet › drawer 3"})["item"]
        assert legacy["container"] == drawer["id"] and "location" not in legacy, legacy
        # ...an unknown name becomes a new top-level container, reused next time
        n_before = len(view())
        a = ok("/api/add", {"name": "A", "location": "Shelf B"})["item"]["container"]
        b = ok("/api/add", {"name": "B", "location": "shelf b"})["item"]["container"]
        assert a == b and len(view()) == n_before + 1 and view()[a]["type"] == "shelf"
        # ...an explicit container wins over a location string
        both = ok("/api/add", {"name": "C", "container": bin1, "location": "Shelf B"})["item"]
        assert both["container"] == bin1, both
        # ...and an empty location unfiles the item
        ok("/api/update", {"id": both["id"], "location": ""})
        it = srv.get("/api/items?ids=" + both["id"])[1]["items"][0]
        assert "container" not in it or not it["container"], it
        assert it["location"] == "", it

        # --- summary breaks down by container path and by zone
        _, summary = srv.get("/api/summary")
        locs = {l["name"]: l["items"] for l in summary["locations"]}
        zones = {z["name"]: z["items"] for z in summary["zones"]}
        assert locs["Big cabinet › Drawer 3"] == 2, locs
        assert zones["Attic"] == 2 and zones["No zone"] == 3, zones

        # --- delete: refused while anything is inside, allowed once empty
        code, res = srv.post("/api/containers", {"action": "delete", "id": drawer["id"]})
        assert code == 409 and "2 item(s)" in res["error"], res
        code, res = srv.post("/api/containers", {"action": "delete", "id": cab["id"]})
        assert code == 409 and "2 container(s)" in res["error"], res
        ok("/api/delete", {"id": item["id"]})
        ok("/api/delete", {"id": legacy["id"]})
        ok("/api/containers", {"action": "delete", "id": drawer["id"]})
        assert drawer["id"] not in view()

        # --- deleting a zone leaves its containers zone-less, nothing else
        ok("/api/zones", {"action": "delete", "id": deep})
        assert view()[cab["id"]]["zone"] is None and cab["id"] in view()
        assert srv.post("/api/zones", {"action": "delete", "id": deep})[0] == 404

        # --- the management page is served (GET + HEAD for the self-reload poll)
        code, headers, body = srv.request("/containers")
        assert code == 200 and b"Containers" in body and "no-store" in headers["Cache-Control"]
        assert srv.request("/containers", method="HEAD")[0] == 200

    print("PASS: containers, zones, nesting and container references on items")


if __name__ == "__main__":
    main()
