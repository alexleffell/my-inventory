#!/usr/bin/env python3
"""End-to-end test for watcher-based item identification.

/api/identify no longer calls an LLM API from the server. It queues the item
for the photo watcher/bridge by dropping a captures/review-*.json marker (the
same trigger deploy/find_unprocessed.py already watches). This test verifies
the marker lifecycle: queue, dedupe, queue visibility, and error cases.

Runs a throwaway copy of server.py because it stores data relative to its own
location. The repository data and captures directories are never used.
"""
import base64
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _request(url, body=None, method="GET"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _stop(proc):
    if not proc:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def main():
    tmp = tempfile.mkdtemp(prefix="inv-identify-test-")
    proc = None
    try:
        shutil.copy(os.path.join(ROOT, "server.py"), os.path.join(tmp, "server.py"))
        os.makedirs(os.path.join(tmp, "captures"))
        os.makedirs(os.path.join(tmp, "data"))
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with open(os.path.join(tmp, "captures", "cap-1.png"), "wb") as f:
            f.write(png)
        inventory_path = os.path.join(tmp, "data", "inventory.json")
        with open(inventory_path, "w") as f:
            json.dump({"schema": 2, "items": [
                {"id": "cap-1", "name": "Old Name", "images": ["cap-1.png"]},
                {"id": "item-2", "name": "No Photos", "images": []},
            ]}, f)
        with open(inventory_path, "rb") as f:
            inventory_before = f.read()

        port = _free_port()
        env = dict(os.environ)
        env.update(INVENTORY_HOST="127.0.0.1", INVENTORY_PORT=str(port))
        env.pop("ANTHROPIC_API_KEY", None)  # must not be needed anymore
        proc = subprocess.Popen([sys.executable, "server.py"], cwd=tmp, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = "http://127.0.0.1:%d" % port
        for _ in range(50):
            try:
                if _request(base + "/api/identify/status")[0] == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("inventory server did not start")

        code, status = _request(base + "/api/identify/status")
        assert code == 200 and status["available"] is True, status
        assert status.get("mode") == "watcher", status

        # queue an identify request -> exactly one review marker appears
        code, result = _request(base + "/api/identify", {"id": "cap-1"}, "POST")
        assert code == 200 and result.get("queued") is True, result
        markers = glob.glob(os.path.join(tmp, "captures", "review-*.json"))
        assert len(markers) == 1, markers
        marker = json.load(open(markers[0]))
        assert marker["id"] == "cap-1", marker
        assert marker["images"] == ["cap-1.png"], marker
        assert "identify" in marker.get("reason", ""), marker

        # the queue endpoint lists the item as identifying
        code, queue = _request(base + "/api/queue")
        assert code == 200, queue
        entry = next((q for q in queue["queue"] if q.get("id") == "cap-1"), None)
        assert entry and entry["status"] == "identifying", queue
        assert entry.get("name") == "Old Name", entry

        # a second request dedupes instead of dropping a second marker
        code, result = _request(base + "/api/identify", {"id": "cap-1"}, "POST")
        assert code == 200 and result.get("already") is True, result
        assert len(glob.glob(os.path.join(tmp, "captures", "review-*.json"))) == 1

        # error cases: unknown item, item without photos
        code, result = _request(base + "/api/identify", {"id": "missing"}, "POST")
        assert code == 404, (code, result)
        code, result = _request(base + "/api/identify", {"id": "item-2"}, "POST")
        assert code == 400, (code, result)

        # queueing never mutates the inventory itself
        with open(inventory_path, "rb") as f:
            assert f.read() == inventory_before, "identify changed inventory.json"

        # watcher finishes: marker deleted -> item leaves the queue
        os.remove(markers[0])
        code, queue = _request(base + "/api/queue")
        assert code == 200, queue
        assert not any(q.get("id") == "cap-1" for q in queue["queue"]), queue
        print("PASS: watcher-queue identification flow OK")
    finally:
        _stop(proc)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("FAIL: %s" % exc, file=sys.stderr)
        raise
