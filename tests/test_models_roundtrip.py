#!/usr/bin/env python3
"""End-to-end test of the 3D-model attach / list / download / delete cycle.

Runs a THROWAWAY copy of server.py in a temp dir (server.py hardcodes
DATA/MODELS relative to its own file, so we must not run the real one — it
would write into the repo's data/). No admin user exists in the copy, so auth
is disabled and writes are open. Stdlib only, no pytest.

    python3 tests/test_models_roundtrip.py    # exits non-zero on any failure
"""
import base64
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


def _req(url, body=None, method="GET"):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        return resp.read()


def _json(url, body=None, method="GET"):
    return json.loads(_req(url, body, method).decode())


def main():
    tmp = tempfile.mkdtemp(prefix="inv-model-test-")
    proc = None
    try:
        shutil.copy(os.path.join(ROOT, "server.py"), os.path.join(tmp, "server.py"))
        port = _free_port()
        base = "http://127.0.0.1:%d" % port
        env = dict(os.environ, INVENTORY_HOST="127.0.0.1", INVENTORY_PORT=str(port))
        proc = subprocess.Popen([sys.executable, "server.py"], cwd=tmp, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # wait for the server to come up
        for _ in range(50):
            try:
                _req(base + "/api/inventory")
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("server did not start")

        # 1. seed an item
        item = _json(base + "/api/add", {"name": "Test Widget", "category": "Misc"}, "POST")["item"]
        iid = item["id"]

        # 2. attach a tiny fake .stl
        blob = b"solid test\nfacet normal 0 0 0\nendsolid test\n"
        res = _json(base + "/api/model",
                    {"id": iid, "filename": "widget.stl",
                     "data": base64.b64encode(blob).decode()}, "POST")
        assert res.get("models"), "no models returned after attach: %r" % res
        stored = res["models"][0]
        assert stored.endswith(".stl"), "stored name not .stl: %r" % stored

        # 3. the item's models array is updated in the inventory
        inv = _json(base + "/api/inventory")
        it = next(x for x in inv["items"] if x["id"] == iid)
        assert it.get("models") == [stored], "item models mismatch: %r" % it.get("models")

        # 4. the file downloads byte-for-byte
        got = _req(base + "/models/" + stored)
        assert got == blob, "downloaded bytes differ (%d vs %d)" % (len(got), len(blob))

        # 5. detach removes it from the item and deletes the file
        res = _json(base + "/api/model/delete", {"id": iid, "file": stored}, "POST")
        assert res.get("models") == [], "models not empty after delete: %r" % res
        try:
            _req(base + "/models/" + stored)
            raise AssertionError("model file still downloadable after delete")
        except urllib.error.HTTPError as e:
            assert e.code == 404, "expected 404 after delete, got %s" % e.code

        print("PASS: attach -> list -> download -> delete round-trip OK")
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
