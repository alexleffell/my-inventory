#!/usr/bin/env python3
"""Keeps /openapi.json honest.

The OpenAPI document is written by hand (it carries prose an LLM reads), but it
must not drift from the actual routes. This test derives the route table from
server.py's source, works out which routes are public, and fails loudly when a
public route is neither documented nor explicitly listed as internal.

Run: python3 tests/test_openapi.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server  # noqa: E402

SRC = open(os.path.join(ROOT, "server.py")).read()
ROUTE_RE = re.compile(r'path == "(/api/[^"]*)"')

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def _slice(start_marker, end_marker=None):
    i = SRC.index(start_marker)
    j = SRC.index(end_marker, i) if end_marker else len(SRC)
    return SRC[i:j]


get_src = _slice("    def do_GET(self):", "    def do_POST(self):")
post_src = _slice("    def _do_post(self):")

# Everything after the blanket admin gate in _do_post is admin-only.
gate = '        if self._require("admin") is None:'
public_post_src = post_src[:post_src.index(gate)] if gate in post_src else post_src

get_routes = set(ROUTE_RE.findall(get_src))
public_post_routes = set(ROUTE_RE.findall(public_post_src))

# A GET route that calls _require inside its own block is not public. Split the
# do_GET body on the route conditions and inspect each block.
public_get_routes = set()
for m in re.finditer(r'if path == "(/api/[^"]*)":', get_src):
    nxt = get_src.find('if path == "', m.end())
    block = get_src[m.end():nxt if nxt != -1 else len(get_src)]
    if "_require(" not in block:
        public_get_routes.add(m.group(1))

spec = server.openapi_spec("https://example.test")
documented_get = {p for p, ops in spec["paths"].items() if "get" in ops}
documented_post = {p for p, ops in spec["paths"].items() if "post" in ops}

# 1. Every public route is documented or consciously marked internal.
for r in sorted(public_get_routes - server.OPENAPI_INTERNAL):
    check(r in documented_get,
          "GET %s is public but missing from openapi_spec() (add it, or to OPENAPI_INTERNAL)" % r)
for r in sorted(public_post_routes - server.OPENAPI_INTERNAL):
    check(r in documented_post,
          "POST %s is public but missing from openapi_spec() (add it, or to OPENAPI_INTERNAL)" % r)

# 2. Nothing documented that no longer exists, or that is actually admin-only.
for r in sorted(documented_get):
    check(r in get_routes, "openapi_spec() documents GET %s but no such route exists" % r)
    check(r in public_get_routes, "openapi_spec() documents GET %s but it is admin-gated" % r)
for r in sorted(documented_post):
    check(r in public_post_routes,
          "openapi_spec() documents POST %s but it is not a public POST route" % r)

# 3. OPENAPI_INTERNAL has no dead entries.
for r in sorted(server.OPENAPI_INTERNAL):
    check(r in get_routes or r in public_post_routes,
          "OPENAPI_INTERNAL lists %s but no such route exists" % r)

# 4. Shape checks the tool layer depends on.
check(spec.get("openapi", "").startswith("3."), "missing/!= OpenAPI 3.x version")
check(spec["servers"][0]["url"] == "https://example.test", "servers[0].url not taken from base_url")
op_ids = [op["operationId"] for ops in spec["paths"].values() for op in ops.values()]
check(len(op_ids) == len(set(op_ids)), "duplicate operationId: %s" % op_ids)
for ops in spec["paths"].values():
    for verb, op in ops.items():
        check(bool(op.get("summary")) and bool(op.get("description")),
              "%s %s needs both summary and description" % (verb, op.get("operationId")))
        check(bool(op.get("responses")), "%s has no responses" % op.get("operationId"))
json.dumps(spec)  # must be serialisable

print("checked %d public GET, %d public POST routes against %d documented paths"
      % (len(public_get_routes), len(public_post_routes), len(spec["paths"])))
if failures:
    print("\nFAIL:")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("PASS")
