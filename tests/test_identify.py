#!/usr/bin/env python3
"""End-to-end test for in-app AI item identification.

Runs a throwaway copy of server.py because it stores data relative to its own
location. The repository data and captures directories are never used.
"""
import base64
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_KEY = "test-key"
SUGGESTION = {
    "name": "Test Buzzer",
    "category": "Audio",
    "quantity": None,
    "condition": "New",
    "notes": "stub",
    "confidence": "high",
}


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


def _start_inventory(tmp, port, api_url, with_key):
    env = dict(os.environ)
    env.update(INVENTORY_HOST="127.0.0.1", INVENTORY_PORT=str(port),
               ANTHROPIC_BASE_URL=api_url, INVENTORY_IDENTIFY_MODEL="claude-opus-5")
    if with_key:
        env["ANTHROPIC_API_KEY"] = TEST_KEY
    else:
        env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=tmp, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    for _ in range(50):
        try:
            if _request(base + "/api/identify/status")[0] == 200:
                return proc, base
        except Exception:
            pass
        time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("inventory server did not start")


def _stop(proc):
    if not proc:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


class AnthropicStub(http.server.BaseHTTPRequestHandler):
    error = None

    def do_POST(self):
        try:
            assert self.path == "/v1/messages", self.path
            assert self.headers.get("x-api-key") == TEST_KEY
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            assert body["model"] == "claude-opus-5"
            assert body["max_tokens"] == 8000
            assert "output_config" in body and "output_format" not in body
            assert body["messages"][0]["content"][0]["type"] == "image"
            response = {
                "content": [{"type": "text", "text": json.dumps(SUGGESTION)}],
                "stop_reason": "end_turn",
            }
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except Exception as exc:
            AnthropicStub.error = exc
            self.send_error(500)

    def log_message(self, *args):
        pass


def main():
    tmp = tempfile.mkdtemp(prefix="inv-identify-test-")
    proc = None
    stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), AnthropicStub)
    stub_thread = threading.Thread(target=stub.serve_forever, daemon=True)
    stub_thread.start()
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
            json.dump({"items": [{"id": "cap-1", "name": "", "images": ["cap-1.png"]}]}, f)
        with open(inventory_path, "rb") as f:
            inventory_before = f.read()

        api_url = "http://127.0.0.1:%d" % stub.server_address[1]
        proc, base = _start_inventory(tmp, _free_port(), api_url, True)
        code, status = _request(base + "/api/identify/status")
        assert code == 200 and status["available"] is True, status
        code, result = _request(base + "/api/identify", {"id": "cap-1"}, "POST")
        assert code == 200, result
        assert result["suggestion"]["name"] == "Test Buzzer", result
        assert result["suggestion"]["quantity"] is None, result
        assert AnthropicStub.error is None, AnthropicStub.error
        with open(inventory_path, "rb") as f:
            assert f.read() == inventory_before, "identify changed inventory.json"
        code, _ = _request(base + "/api/identify", {"id": "missing"}, "POST")
        assert code != 200, "unknown item unexpectedly succeeded"
        _stop(proc)
        proc = None

        proc, base = _start_inventory(tmp, _free_port(), api_url, False)
        code, status = _request(base + "/api/identify/status")
        assert code == 200 and status["available"] is False, status
        code, result = _request(base + "/api/identify", {"id": "cap-1"}, "POST")
        assert code == 503, (code, result)
        print("PASS: configured and unavailable identification flows OK")
    finally:
        _stop(proc)
        stub.shutdown()
        stub.server_close()
        stub_thread.join(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("FAIL: %s" % exc, file=sys.stderr)
        raise
