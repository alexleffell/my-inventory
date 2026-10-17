#!/usr/bin/env python3
"""Unique per-item URLs: /i/<id> serves the dashboard; junk paths 404.

    python3 tests/test_item_links.py
"""
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
sys.path.insert(0, ROOT)

import server  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)


def test_id_parser():
    good = ["cap-1780514673872", "item-1780562653347", "a", "A_b.1-2"]
    for ident in good:
        check(server.item_id_from_path("/i/" + ident) == ident, ident)
    check(server.item_id_from_path("/i/" + urllib_quote("item-1")) == "item-1", "quoted")
    bad = [
        "/", "/dashboard", "/i/", "/i/../server.py", "/i/foo/bar",
        "/i/.", "/i/..", "/i/%2e%2e", "/i/%2e%2e%2fserver.py",
        "/i/has space", "/i/has/slash", "/i/" + ("x" * 81),
        "/i/-leading", "/i/.dot",
    ]
    for path in bad:
        check(server.item_id_from_path(path) is None, "should reject %s" % path)


def urllib_quote(s):
    from urllib.parse import quote
    return quote(s, safe="")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _open(url, method="GET"):
    r = urllib.request.Request(url, method=method)
    return urllib.request.urlopen(r, timeout=5)


def test_http():
    tmp = tempfile.mkdtemp(prefix="inv-item-links-")
    proc = None
    try:
        shutil.copy(os.path.join(ROOT, "server.py"), os.path.join(tmp, "server.py"))
        os.makedirs(os.path.join(tmp, "public"))
        shutil.copy(os.path.join(ROOT, "public", "dashboard.html"),
                    os.path.join(tmp, "public", "dashboard.html"))
        port = _free_port()
        base = "http://127.0.0.1:%d" % port
        env = dict(os.environ, INVENTORY_HOST="127.0.0.1", INVENTORY_PORT=str(port))
        proc = subprocess.Popen([sys.executable, "server.py"], cwd=tmp, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                _open(base + "/")
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("server did not start")

        with _open(base + "/i/cap-1780514673872") as resp:
            body = resp.read().decode("utf-8", "replace")
            check(resp.status == 200, "GET /i/<id> status")
            check("text/html" in resp.headers.get("Content-Type", ""), "html")
            check("openDetail" in body and "itemHref" in body, "dashboard js")
            check(resp.headers.get("Last-Modified"), "Last-Modified")

        with _open(base + "/i/item-1", "HEAD") as resp:
            check(resp.status == 200, "HEAD /i/<id>")
            check(resp.headers.get("Last-Modified"), "HEAD Last-Modified")

        with _open(base + "/?q=cap-1&cat=Audio&loc=Bag%201&sort=name") as resp:
            body = resp.read().decode("utf-8", "replace")
            check(resp.status == 200, "GET / with view query")
            check("exactById" in body and "applyFiltersFromUrl" in body, "url state js")

        with _open(base + "/i/cap-1780514673872?q=cap-1780514673872") as resp:
            check(resp.status == 200, "GET /i/<id> with query")

        with _open(base + "/dashboard?sort=qty", "HEAD") as resp:
            check(resp.status == 200, "HEAD /dashboard with query")

        for path in ("/i/", "/i/../server.py", "/i/foo/bar", "/i/%2e%2e%2fserver.py"):
            try:
                _open(base + path)
            except urllib.error.HTTPError as e:
                check(e.code == 404, "%s -> %s" % (path, e.code))
            else:
                raise SystemExit("FAIL: expected 404 for " + path)
    finally:
        if proc:
            proc.terminate()
            proc.wait(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_id_parser()
    test_http()
    print("PASS tests/test_item_links.py")


if __name__ == "__main__":
    main()
