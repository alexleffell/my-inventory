#!/usr/bin/env python3
"""Covers QR labels: GET /api/qr renders <base>/c/<id> as an SVG QR code, the
base URL comes from INVENTORY_BASE_URL, and the label-sheet page is served.

The SVG is parsed back into a module grid and checked against the QR standard
independently of the vendored encoder: finder patterns, timing patterns, and
both copies of the BCH-protected format information. (Whether a phone scans a
printed label is the manual check in CLAUDE.md's walkthrough.)

    python3 tests/test_qr.py    # exits non-zero on any failure
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import ROOT, Server  # noqa: E402

sys.path.insert(0, ROOT)
import qrcodegen  # noqa: E402

BASE = "http://maker-laptop.local:8770"
BORDER = 4


def grid_from_svg(svg):
    """Module grid (list of rows of bools) without the quiet zone."""
    dim = int(re.search(r'viewBox="0 0 (\d+) \1"', svg).group(1))
    size = dim - 2 * BORDER
    grid = [[False] * size for _ in range(size)]
    for x, y in re.findall(r"M(\d+),(\d+)h1v1h-1z", svg):
        x, y = int(x) - BORDER, int(y) - BORDER
        assert 0 <= x < size and 0 <= y < size, "dark module inside the quiet zone"
        grid[y][x] = True
    return grid


def format_bits(grid, copy):
    size = len(grid)
    if copy == 1:
        cells = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)]
    else:
        cells = [(size - 1 - i, 8) for i in range(8)] + [(8, size - 15 + i) for i in range(8, 15)]
    return sum(int(grid[y][x]) << i for i, (x, y) in enumerate(cells))


def check_structure(grid):
    size = len(grid)
    assert size >= 21 and (size - 17) % 4 == 0, size
    # finder patterns: 7x7 rings in three corners (dark unless Chebyshev distance 2 from centre)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                want = max(abs(dx), abs(dy)) != 2
                assert grid[cy + dy][cx + dx] == want, ("finder", cx, cy, dx, dy)
    # timing patterns alternate along row 6 and column 6
    for i in range(8, size - 8):
        assert grid[6][i] == (i % 2 == 0) and grid[i][6] == (i % 2 == 0), ("timing", i)
    # format information: both copies agree and carry a valid BCH(15,5) code
    a, b = format_bits(grid, 1), format_bits(grid, 2)
    assert a == b, (bin(a), bin(b))
    raw = a ^ 0x5412
    data, rem = raw >> 10, raw & 0x3FF
    r = data
    for _ in range(10):
        r = (r << 1) ^ ((r >> 9) * 0x537)
    assert r == rem, "format information fails its BCH check"
    assert grid[size - 8][8], "the always-dark module is missing"


def main():
    with Server(env={"INVENTORY_BASE_URL": BASE + "/"}) as srv:
        code, res = srv.post("/api/containers", {"name": "Drawer 3", "type": "drawer"})
        cid = res["container"]["id"]

        # --- the label base URL is the env override (trailing slash trimmed)
        _, view = srv.get("/api/containers")
        assert view["label_base_url"] == BASE, view

        # --- /api/qr: an SVG of exactly <base>/c/<id>
        code, headers, body = srv.request("/api/qr?container=" + cid)
        assert code == 200 and headers["Content-Type"] == "image/svg+xml", (code, headers)
        grid = grid_from_svg(body.decode())
        check_structure(grid)
        want = qrcodegen.QrCode.encode_text("%s/c/%s" % (BASE, cid), qrcodegen.QrCode.Ecc.MEDIUM)
        assert len(grid) == want.get_size(), (len(grid), want.get_size())
        assert all(grid[y][x] == want.get_module(x, y)
                   for y in range(len(grid)) for x in range(len(grid))), "SVG modules differ from the encoder"
        assert want.get_version() <= 4, "label URL should stay a small QR code"

        # --- unknown or missing container: 404
        assert srv.request("/api/qr?container=c-nope")[0] == 404
        assert srv.request("/api/qr")[0] == 404

        # --- the label sheet page (GET + HEAD)
        code, headers, body = srv.request("/labels?ids=" + cid)
        assert code == 200 and b"size: letter" in body, code
        assert srv.request("/labels", method="HEAD")[0] == 200

    # --- the vendored encoder against a structural check over many payloads
    for text in ("http://a.local:8770/c/c-abcdef", "x" * 120, "HELLO WORLD", "0123456789"):
        qr = qrcodegen.QrCode.encode_text(text, qrcodegen.QrCode.Ecc.MEDIUM)
        check_structure([[qr.get_module(x, y) for x in range(qr.get_size())] for y in range(qr.get_size())])

    print("PASS: QR labels encode <base>/c/<id> as well-formed QR codes")


if __name__ == "__main__":
    main()
