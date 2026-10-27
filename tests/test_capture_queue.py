#!/usr/bin/env python3
"""Covers the capture queue with containers: photo upload (type detected from
the bytes), /api/confirm writing a sidecar with the container id, /api/queue
listing it, an agent's /api/ingest taking it off the queue, and /c/<id>.

    python3 tests/test_capture_queue.py    # exits non-zero on any failure
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Server  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
# Enough of a JPEG for the magic-byte check (SOI + APP0 marker).
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32 + b"\xff\xd9"


def data_url(blob, mime):
    return "data:%s;base64,%s" % (mime, base64.b64encode(blob).decode())


def main():
    with Server() as srv:
        def ok(path, body):
            code, res = srv.post(path, body)
            assert code == 200, (path, code, res)
            return res

        zone = ok("/api/zones", {"name": "Bench"})["zone"]["id"]
        cab = ok("/api/containers", {"name": "Cabinet", "type": "cabinet", "zone": zone})["container"]["id"]
        drawer = ok("/api/containers", {"name": "Drawer 2", "type": "drawer", "parent": cab})["container"]["id"]

        # --- uploads keep their real type; non-images are refused
        jpg = ok("/api/capture", {"image": data_url(JPEG, "image/jpeg")})["file"]
        png = ok("/api/capture", {"image": data_url(PNG, "image/png")})["file"]
        assert jpg.startswith("cap-") and jpg.endswith(".jpg"), jpg
        assert png.endswith(".png"), png
        assert srv.post("/api/capture", {"image": data_url(b"hello", "text/plain")})[0] == 400
        code, headers, body = srv.request("/captures/" + jpg)
        assert code == 200 and headers["Content-Type"] == "image/jpeg" and body == JPEG

        # --- confirm writes one sidecar carrying the container id and the note
        res = ok("/api/confirm", {"files": [jpg, png], "container": drawer, "note": " blue box "})
        side = srv.read_json("captures", res["sidecar"])
        assert res["sidecar"] == jpg.rsplit(".", 1)[0] + ".json", res
        assert side["files"] == [jpg, png] and side["container"] == drawer, side
        assert side["note"] == "blue box" and isinstance(side["created"], int), side
        assert "location" not in side and "quantity" not in side, side

        # --- bad confirms: unknown container, no real files
        assert srv.post("/api/confirm", {"files": [png], "container": "c-nope"})[0] == 400
        assert srv.post("/api/confirm", {"files": ["cap-404.png"], "container": drawer})[0] == 400
        # an older client sending a location string still lands in a container
        old = ok("/api/capture", {"image": data_url(PNG, "image/png")})["file"]
        side_old = srv.read_json("captures", ok("/api/confirm", {"files": [old], "location": "Bag 9"})["sidecar"])
        assert side_old["container"].startswith("c-") and "location" not in side_old, side_old

        # --- the queue lists both captures with container, path and note
        _, queue = srv.get("/api/queue")
        queued = [q for q in queue["queue"] if q["status"] == "queued"]
        assert len(queued) == 2, queue
        entry = next(q for q in queued if q["images"][0] == jpg)
        assert entry["container"] == drawer and entry["location"] == "Cabinet › Drawer 2", entry
        assert entry["note"] == "blue box" and entry["sidecar"] == res["sidecar"], entry

        # --- a container with a queued capture cannot be deleted out from under it
        code, err = srv.post("/api/containers", {"action": "delete", "id": drawer})
        assert code == 409 and "1 queued capture" in err["error"], err

        # --- the agent writes the identified item back: it leaves the queue
        ok("/api/ingest", {"id": jpg.rsplit(".", 1)[0], "name": "Hex keys", "category": "Tools",
                           "quantity": 1, "container": entry["container"], "images": [png, jpg]})
        _, queue = srv.get("/api/queue")
        assert [q["images"][0] for q in queue["queue"] if q["status"] == "queued"] == [old], queue
        _, items = srv.get("/api/items")
        row = items["items"][0]
        assert row["container"] == drawer and row["location"] == "Cabinet › Drawer 2", row
        _, summary = srv.get("/api/summary")
        assert summary["zones"] == [{"name": "Bench", "items": 1}], summary

        # --- /c/<id> serves the capture page (GET and HEAD); bad ids are 404
        code, headers, body = srv.request("/c/" + drawer)
        assert code == 200 and b"Capture" in body and "text/html" in headers["Content-Type"]
        assert srv.request("/c/" + drawer, method="HEAD")[0] == 200
        for bad in ("/c/", "/c/../server.py", "/c/a/b", "/c/%2e%2e"):
            assert srv.request(bad)[0] == 404, bad

    print("PASS: captures queue with their container and leave the queue once ingested")


if __name__ == "__main__":
    main()
