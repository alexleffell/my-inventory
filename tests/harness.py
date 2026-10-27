"""Throwaway-server harness shared by the newer tests.

server.py keeps its data next to itself, so every test runs a COPY of it in a
temp dir and never touches the repo's data/ or captures/. Optionally seeds the
copy from a fixture app root (tests/fixtures/<name>/ with data/ and captures/).

    with Server(fixture="v1") as srv:
        code, body = srv.get("/api/items")
"""
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
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
CODE = ("server.py", "qrcodegen.py")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self, fixture=None, env=None):
        self.fixture = fixture
        self.env = env or {}
        self.proc = None
        self.output = ""

    def __enter__(self):
        self.root = tempfile.mkdtemp(prefix="inv-test-")
        for name in CODE:
            if os.path.exists(os.path.join(ROOT, name)):
                shutil.copy(os.path.join(ROOT, name), os.path.join(self.root, name))
        os.makedirs(os.path.join(self.root, "public"))
        for name in os.listdir(os.path.join(ROOT, "public")):
            if name.endswith(".html"):
                shutil.copy(os.path.join(ROOT, "public", name), os.path.join(self.root, "public", name))
        if self.fixture:
            src = os.path.join(FIXTURES, self.fixture)
            for sub in os.listdir(src):
                if os.path.isdir(os.path.join(src, sub)):
                    shutil.copytree(os.path.join(src, sub), os.path.join(self.root, sub))
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def start(self):
        self.port = _free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        env = dict(os.environ, INVENTORY_HOST="127.0.0.1", INVENTORY_PORT=str(self.port),
                   PYTHONUNBUFFERED="1")
        env.update(self.env)
        self.proc = subprocess.Popen([sys.executable, "server.py"], cwd=self.root, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(100):
            try:
                urllib.request.urlopen(self.base + "/api/summary", timeout=1)
                return
            except Exception:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.05)
        self.stop()
        raise SystemExit("server did not start:\n" + self.output)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                out, _ = self.proc.communicate(timeout=5)
            except Exception:
                self.proc.kill()
                out, _ = self.proc.communicate()
            self.output += out or ""
            self.proc = None

    def restart(self):
        self.stop()
        self.start()

    def request(self, path, body=None, method=None):
        """(status, headers, raw bytes). HTTP errors are returned, not raised."""
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data,
                                   method=method or ("POST" if body is not None else "GET"),
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def get(self, path):
        code, _, raw = self.request(path)
        return code, json.loads(raw.decode() or "null")

    def post(self, path, body):
        code, _, raw = self.request(path, body)
        return code, json.loads(raw.decode() or "null")

    def read_json(self, *parts):
        with open(self.path(*parts)) as f:
            return json.load(f)
