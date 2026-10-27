#!/usr/bin/env python3
"""Covers the schema 1 -> 2 migration: free-text locations become containers.

Starts a throwaway server on tests/fixtures/v1 (a pre-containers app root) and
checks the startup migration backed up the old files, made one container per
distinct location string, rewrote items/stock/pending/queued sidecars to
container ids, and is a no-op on the next start.

    python3 tests/test_migration.py    # exits non-zero on any failure
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import FIXTURES, Server  # noqa: E402


def main():
    with Server(fixture="v1") as srv:
        assert "Migrated inventory to schema 2" in srv.proc.stdout.readline(), "no summary line"

        # --- backups of the original files, byte for byte
        for name in ("inventory.json", "pending.json"):
            baks = glob.glob(srv.path("data", name + ".pre-v2-*.bak"))
            assert len(baks) == 1, baks
            with open(baks[0], "rb") as a, open(os.path.join(FIXTURES, "v1", "data", name), "rb") as b:
                assert a.read() == b.read(), "backup of %s differs from the original" % name

        # --- one top-level container per distinct location ("bag 1" reuses "Bag 1")
        doc = srv.read_json("data", "containers.json")
        names = sorted(c["name"] for c in doc["containers"])
        assert names == ["Bag 1", "Bin 3", "Shelf A"], names
        by_name = {c["name"]: c for c in doc["containers"]}
        assert by_name["Bag 1"]["type"] == "bag" and by_name["Shelf A"]["type"] == "shelf", doc
        assert all(c["parent"] is None and c["zone"] is None for c in doc["containers"]), doc
        bag1, shelf, bin3 = (by_name[n]["id"] for n in ("Bag 1", "Shelf A", "Bin 3"))

        # --- items: container ids stored, no location strings, schema marked
        raw = srv.read_json("data", "inventory.json")
        assert raw["schema"] == 2, raw
        items = {i["id"]: i for i in raw["items"]}
        assert not any("location" in i for i in raw["items"]), raw
        assert items["cap-100"]["container"] == bag1, items["cap-100"]
        assert items["item-200"]["stock"] == [{"container": bag1, "quantity": 3},
                                              {"container": shelf, "quantity": 2}], items["item-200"]
        assert items["item-200"]["container"] == bag1 and items["item-200"]["quantity"] == 5
        assert "container" not in items["item-300"], items["item-300"]          # was ""
        assert "container" not in items["item-400"] and items["item-400"]["undelivered"] is True

        # --- reads add the derived location path
        code, body = srv.get("/api/items?ids=item-200,item-300")
        assert code == 200, body
        split, unfiled = body["items"]
        assert split["location"] == "Bag 1", split
        assert [r["location"] for r in split["stock"]] == ["Bag 1", "Shelf A"], split
        assert unfiled["location"] == "", unfiled

        # --- pending candidate and the QUEUED sidecar point at containers now
        pend = srv.read_json("data", "pending.json")["pending"][0]["candidate"]
        assert pend["container"] == bag1 and "location" not in pend, pend
        code, body = srv.get("/api/pending")
        assert body["pending"][0]["candidate"]["location"] == "Bag 1", body
        queued = srv.read_json("captures", "cap-600.json")
        assert queued["container"] == bin3 and queued["legacy_location"] == "Bin 3", queued
        # an already-processed sidecar is history: left exactly as it was
        with open(srv.path("captures", "cap-100.json")) as a, \
                open(os.path.join(FIXTURES, "v1", "captures", "cap-100.json")) as b:
            assert a.read() == b.read(), "processed sidecar was rewritten"

        # --- a restart is a no-op: no new backups, no new containers, same items
        before = srv.read_json("data", "inventory.json")
        srv.restart()
        assert len(glob.glob(srv.path("data", "inventory.json.pre-v2-*.bak"))) == 1
        assert len(srv.read_json("data", "containers.json")["containers"]) == 3
        assert srv.read_json("data", "inventory.json") == before

    # --- a fresh install starts at schema 2 with nothing to migrate
    with Server() as srv:
        assert srv.read_json("data", "inventory.json") == {"schema": 2, "items": []}
        assert not glob.glob(srv.path("data", "*.pre-v2-*.bak"))

    print("PASS: v1 locations migrate to containers, with backups, exactly once")


if __name__ == "__main__":
    main()
