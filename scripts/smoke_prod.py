"""Production smoke test for the San Diego City GIS MCP server.

Exercises the JSON-RPC surface and the core arcgis tool chain end-to-end
against the deployed Lambda, including the "verification query": a WGS84
point-in-polygon on the Multi-Habitat Planning Area (MHPA) at the Tijuana
River Valley, which must return the containing preserve polygon with
HABPRES. It then asserts the MCP conformance surface: protocol version
negotiation, spec error codes for caller mistakes (-32601/-32602), the
Origin allowlist (403), the MCP-Protocol-Version header check (400, and
deliberately NOT -32022), CORS preflight headers, and that a bad tool
argument comes back as a readable tool error. Read-only; paces calls to
stay under the API Gateway rate limit (5 rps) and WAF per-IP cap
(300/5min).

Usage:
    python3 scripts/smoke_prod.py [URL]

URL defaults to the production custom domain; override with an argument or
the OPENCONTEXT_SMOKE_URL env var to point at a different deployment
(e.g. the raw API Gateway URL or a local server).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

try:  # optional: full schema validation when jsonschema is installed
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover
    Draft202012Validator = None

URL = (
    (sys.argv[1] if len(sys.argv) > 1 else None)
    or os.environ.get("OPENCONTEXT_SMOKE_URL")
    or "https://sandiego-city-gis.codeforanchorage.org/mcp"
)

MHPA_ID = "Planning/PLN_LongRangePlanning/MapServer/7"
ZONES_ID = "Planning/PLN_LongRangePlanning/MapServer/27"

_id = 0
results = []


def rpc(method, params=None):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read().decode())
    time.sleep(0.4)  # pace under 5 rps
    return body


def raw(method="POST", payload=None, headers=None):
    """Low-level request that returns (status, headers, body) and never
    raises on 4xx/5xx -- the conformance checks assert on those."""
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Accept": "application/json"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(URL, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, resp_headers, body = r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        status, resp_headers, body = e.code, dict(e.headers), e.read()
    time.sleep(0.4)  # pace under 5 rps
    try:
        parsed = json.loads(body.decode()) if body else None
    except ValueError:
        parsed = body.decode(errors="replace")
    return status, {k.lower(): v for k, v in resp_headers.items()}, parsed


def jsonrpc(method, params=None, id_=None):
    global _id
    _id += 1
    payload = {
        "jsonrpc": "2.0",
        "id": id_ if id_ is not None else _id,
        "method": method,
    }
    if params is not None:
        payload["params"] = params
    return payload


def call_tool(name, args):
    return rpc("tools/call", {"name": f"arcgis__{name}", "arguments": args})


def text_of(resp):
    return resp["result"]["content"][0]["text"]


def check(label, ok, detail=""):
    results.append(ok)
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


print(f"Smoke testing: {URL}\n")

# 1. ping
try:
    r = rpc("ping")
    # Spec: ping MUST return an empty result object; the response itself is the signal.
    check("ping", r.get("result") == {}, str(r.get("result")))
except Exception as e:
    check("ping", False, repr(e))

# 2. initialize
try:
    r = rpc(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "1.0"},
        },
    )
    check("initialize", bool(r["result"]["serverInfo"]["name"]))
    # The server `instructions` block is what steers the model; it ships
    # inside the zip, so a stale package silently drops it.
    instr = r["result"].get("instructions", "")
    check(
        "initialize carries instructions",
        "WGS84" in instr and "dataset_id" in instr,
        f"{len(instr)} chars",
    )
except Exception as e:
    check("initialize", False, repr(e))

# 3. tools/list -- expect the eight arcgis tools (same surface as the
#    sibling Hub-based servers, so cross-server orchestration keeps working)
try:
    r = rpc("tools/list")
    tools = {t["name"]: t for t in r["result"]["tools"]}
    expected = {
        "arcgis__search_datasets",
        "arcgis__get_dataset",
        "arcgis__get_aggregations",
        "arcgis__query_data",
        "arcgis__get_layer_schema",
        "arcgis__get_distinct_values",
        "arcgis__spatial_query_point",
        "arcgis__geocode_address",
    }
    check("tools/list (8 tools)", set(tools) == expected, f"{sorted(tools)}")
    # MCP tier-2 metadata: a top-level display title and read-only
    # annotations on every tool (never idempotentHint on a read-only tool).
    missing = sorted(
        n
        for n, t in tools.items()
        if not t.get("title")
        or t.get("annotations", {}).get("readOnlyHint") is not True
        or "idempotentHint" in t.get("annotations", {})
    )
    check(
        "tools/list metadata (title + readOnlyHint)",
        not missing,
        "all 8 carry title + readOnlyHint" if not missing else f"missing: {missing}",
    )
except Exception as e:
    check("tools/list (8 tools)", False, repr(e))

# 4. discovery -- search_datasets('MHPA') must resolve to the featured layer
try:
    t = text_of(call_tool("search_datasets", {"q": "MHPA", "limit": 5}))
    check(
        "search_datasets('MHPA') resolves",
        MHPA_ID in t and "Multi-Habitat Planning Area" in t,
        t.split("\n")[0][:60],
    )
except Exception as e:
    check("search_datasets('MHPA') resolves", False, repr(e))

# 5. discovery -- search_datasets('zoning') must surface Base Zones
try:
    t = text_of(call_tool("search_datasets", {"q": "zoning", "limit": 5}))
    check(
        "search_datasets('zoning') resolves",
        ZONES_ID in t and "Base Zones" in t,
        t.split("\n")[0][:60],
    )
except Exception as e:
    check("search_datasets('zoning') resolves", False, repr(e))

# 6. get_dataset on the MHPA path id
try:
    t = text_of(call_tool("get_dataset", {"dataset_id": MHPA_ID}))
    ok = "Multi-Habitat Planning Area" in t and "WGS84" in t
    check("get_dataset(MHPA)", ok, f"{len(t)} chars")
except Exception as e:
    check("get_dataset(MHPA)", False, repr(e))

# 7. get_layer_schema -- field list with the HABPRES field
try:
    t = text_of(call_tool("get_layer_schema", {"item_id": MHPA_ID}))
    ok = "Fields (" in t and "HABPRES" in t
    check("get_layer_schema(MHPA)", ok, t.split("\n")[0][:60])
except Exception as e:
    check("get_layer_schema(MHPA)", False, repr(e))

# 8. query_data -- TOTAL MATCHING count with a where clause
try:
    t = text_of(
        call_tool(
            "query_data",
            {
                "dataset_id": MHPA_ID,
                "where": "HABPRES >= 90",
                "out_fields": "SUBAREA,HABPRES,ACRES",
                "order_by": "ACRES DESC",
                "limit": 3,
            },
        )
    )
    ok = "TOTAL MATCHING:" in t and "Record 1:" in t and "HABPRES" in t
    check("query_data where+order_by (TOTAL MATCHING)", ok, t.split("\n")[0][:60])
except Exception as e:
    check("query_data where+order_by (TOTAL MATCHING)", False, repr(e))

# 9. VERIFICATION QUERY -- WGS84 point-in-polygon on MHPA at the Tijuana
#    River Valley (32.5539, -117.0846). This point returns null on the
#    regional (SANDAG) server's County MSCP_CN layer but sits inside a City
#    MHPA preserve, so it proves the inSR=4326 contract end-to-end.
try:
    t = text_of(
        call_tool(
            "spatial_query_point",
            {
                "item_id": MHPA_ID,
                "lon": -117.0846,
                "lat": 32.5539,
                "out_fields": "HABPRES,INHABPRES,SUBAREA,ACRES",
            },
        )
    )
    ok = "Record 1:" in t and "HABPRES:" in t
    check(
        "verification query (MHPA point-in-polygon, WGS84)",
        ok,
        t.split("\n")[0][:60] if ok else "ERROR/empty: " + t[:80],
    )
except Exception as e:
    check("verification query (MHPA point-in-polygon, WGS84)", False, repr(e))

# 10. get_distinct_values -- INHABPRES should include Yes
try:
    t = text_of(
        call_tool(
            "get_distinct_values",
            {"item_id": MHPA_ID, "field": "INHABPRES", "limit": 10},
        )
    )
    ok = "Yes" in t and "distinct value" in t
    check("get_distinct_values(INHABPRES)", ok, t.replace("\n", " ")[:60])
except Exception as e:
    check("get_distinct_values(INHABPRES)", False, repr(e))

# 11. geocode_address -- street address to lon/lat (US Census geocoder).
#     202 C St is San Diego City Hall (a public landmark used as the demo).
try:
    t = text_of(call_tool("geocode_address", {"address": "202 C St"}))
    ok = "match(es)" in t and "lon:" in t and "lat:" in t
    check("geocode_address(City Hall)", ok, t.split("\n")[0][:60])
except Exception as e:
    check("geocode_address(City Hall)", False, repr(e))

# 12. spatial_query_point BY ADDRESS -- geocode + zoning lookup in one call.
#     Downtown City Hall sits in the Centre City Planned District (CCPD-*).
try:
    t = text_of(
        call_tool(
            "spatial_query_point",
            {
                "item_id": ZONES_ID,
                "address": "202 C St",
                "out_fields": "ZONE_NAME",
                "limit": 2,
            },
        )
    )
    ok = "Geocoded" in t and "ZONE_NAME" in t
    check("spatial_query_point(zoning by address)", ok, t.split("\n")[0][:60])
except Exception as e:
    check("spatial_query_point(zoning by address)", False, repr(e))

# 13. get_aggregations sanity -- catalog facets by folder
try:
    t = text_of(call_tool("get_aggregations", {"field": "folder"}))
    check("get_aggregations(folder)", "Planning" in t, t.replace("\n", " ")[:60])
except Exception as e:
    check("get_aggregations(folder)", False, repr(e))

# ── MCP conformance surface ────────────────────────────────────────────
# These mirror the checks the sibling forks run after every deploy. Each
# one caught a real regression somewhere in the fleet at least once.

# 14. protocol version negotiation -- a supported requested version is
#     echoed; an unknown one falls back to the newest supported.
try:
    r = rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        },
    )
    echoed = r["result"]["protocolVersion"] == "2025-06-18"
    r2 = rpc(
        "initialize",
        {
            "protocolVersion": "1999-01-01",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        },
    )
    fallback = r2["result"]["protocolVersion"]
    info = r2["result"]["serverInfo"]
    ok = echoed and fallback >= "2025-11-25" and info["name"] != "opencontext"
    check(
        "initialize negotiates protocolVersion",
        ok,
        f"echo={echoed} fallback={fallback} serverInfo={info}",
    )
except Exception as e:
    check("initialize negotiates protocolVersion", False, repr(e))

# 15. unknown method -> -32601 Method not found (not -32603 Internal error)
try:
    r = rpc("resources/list")
    err = r.get("error", {})
    check("unknown method -> -32601", err.get("code") == -32601, str(err)[:70])
except Exception as e:
    check("unknown method -> -32601", False, repr(e))

# 16. unknown tool -> -32602 with the available tool list in data
try:
    r = rpc("tools/call", {"name": "arcgis__nope", "arguments": {}})
    err = r.get("error", {})
    ok = (
        err.get("code") == -32602
        and err.get("message", "").startswith("Unknown tool")
        and "arcgis__query_data" in (err.get("data") or {}).get("available_tools", [])
    )
    check("unknown tool -> -32602 + available_tools", ok, str(err)[:70])
except Exception as e:
    check("unknown tool -> -32602 + available_tools", False, repr(e))

# 17. non-object arguments -> -32602, never a raw Python error
try:
    r = rpc("tools/call", {"name": "arcgis__get_dataset", "arguments": "x"})
    err = r.get("error", {})
    check("non-object arguments -> -32602", err.get("code") == -32602, str(err)[:70])
except Exception as e:
    check("non-object arguments -> -32602", False, repr(e))

# 18. bad tool argument -> readable tool error (isError), no traceback path
try:
    r = call_tool("query_data", {"dataset_id": MHPA_ID, "limit": "many"})
    res = r.get("result", {})
    ok = res.get("isError") is True and "limit must be an integer" in text_of(r)
    check("bad tool argument -> isError with message", ok, text_of(r)[:60])
except Exception as e:
    check("bad tool argument -> isError with message", False, repr(e))

# 19. disallowed Origin -> 403 before routing (DNS-rebinding defence)
try:
    status, _, body = raw(
        payload=jsonrpc("ping"), headers={"Origin": "https://evil.example"}
    )
    ok = status == 403 and (body or {}).get("error", {}).get("code") == -32600
    check("disallowed Origin -> 403", ok, f"HTTP {status} {str(body)[:50]}")
except Exception as e:
    check("disallowed Origin -> 403", False, repr(e))

# 20. allowlisted Origin -> 200 with the origin reflected
try:
    status, hdrs, _ = raw(
        payload=jsonrpc("ping"), headers={"Origin": "https://claude.ai"}
    )
    ok = (
        status == 200 and hdrs.get("access-control-allow-origin") == "https://claude.ai"
    )
    check("allowlisted Origin -> 200 + reflected", ok, f"HTTP {status}")
except Exception as e:
    check("allowlisted Origin -> 200 + reflected", False, repr(e))

# 21. unsupported MCP-Protocol-Version -> 400 / -32600 with the supported
#     list, and deliberately NOT -32022 (a dual-era client reads a plain
#     4xx as "legacy server" and falls back to initialize, which we want).
try:
    status, _, body = raw(
        payload=jsonrpc("ping"), headers={"MCP-Protocol-Version": "1999-01-01"}
    )
    err = (body or {}).get("error", {})
    ok = (
        status == 400
        and err.get("code") == -32600
        and "2025-11-25" in (err.get("data") or {}).get("supported", [])
    )
    check(
        "bad MCP-Protocol-Version -> 400/-32600", ok, f"HTTP {status} {str(err)[:50]}"
    )
except Exception as e:
    check("bad MCP-Protocol-Version -> 400/-32600", False, repr(e))

# 22. supported MCP-Protocol-Version header -> 200
try:
    status, _, body = raw(
        payload=jsonrpc("ping"), headers={"MCP-Protocol-Version": "2025-06-18"}
    )
    ok = status == 200 and (body or {}).get("result") == {}
    check("good MCP-Protocol-Version -> 200", ok, f"HTTP {status}")
except Exception as e:
    check("good MCP-Protocol-Version -> 200", False, repr(e))

# 23. CORS preflight allows the headers browser MCP clients send
try:
    status, hdrs, _ = raw(method="OPTIONS", headers={"Origin": "https://claude.ai"})
    allowed = hdrs.get("access-control-allow-headers", "").lower()
    ok = (
        status == 200
        and "mcp-protocol-version" in allowed
        and "mcp-session-id" in allowed
    )
    check("OPTIONS preflight allows MCP headers", ok, f"HTTP {status} {allowed[:50]}")
except Exception as e:
    check("OPTIONS preflight allows MCP headers", False, repr(e))

# 24. tools/list is byte-stable between calls (clients cache it; a stable
#     list keeps prompt-cache hits alive)
try:
    a = rpc("tools/list")["result"]["tools"]
    b = rpc("tools/list")["result"]["tools"]
    check("tools/list is deterministic", a == b, f"{len(a)} tools")
except Exception as e:
    check("tools/list is deterministic", False, repr(e))

# ── Structured output (outputSchema is BINDING) ───────────────────────
# Validate LIVE structuredContent against the outputSchema the server
# itself advertises -- across the awkward branches (an empty search, a
# truncated query, a geocoded point lookup), not just the happy path --
# and assert every structured caveat appears verbatim in the prose.


def check_structured(label, tool, args, expect_codes=None):
    try:
        r = call_tool(tool, args)
        res = r["result"]
        sc = res.get("structuredContent")
        schema = tools_by_name[f"arcgis__{tool}"].get("outputSchema")
        problems = []
        if not sc:
            problems.append("no structuredContent")
        if not schema:
            problems.append("no outputSchema advertised")
        if sc and schema:
            if Draft202012Validator is not None:
                errs = list(Draft202012Validator(schema).iter_errors(sc))
                problems += [f"schema: {e.message}" for e in errs[:3]]
            else:
                missing = [k for k in schema.get("required", []) if k not in sc]
                if missing:
                    problems.append(f"missing keys {missing}")
            text = res["content"][0]["text"]
            for c in sc.get("caveats", []):
                if c["message"] not in text:
                    problems.append(f"caveat {c['code']} absent from prose")
            got = [c["code"] for c in sc.get("caveats", [])]
            for code in expect_codes or []:
                if code not in got:
                    problems.append(f"expected caveat {code}, got {got}")
        detail = (
            "; ".join(problems)
            if problems
            else f"caveats={[c['code'] for c in sc.get('caveats', [])]}"
        )
        check(label, not problems, detail)
    except Exception as e:
        check(label, False, repr(e))


try:
    tools_by_name = {t["name"]: t for t in rpc("tools/list")["result"]["tools"]}
except Exception as e:  # pragma: no cover
    tools_by_name = {}
    check("tools/list for structured checks", False, repr(e))

check_structured("structured: search_datasets hit", "search_datasets", {"q": "MHPA"})
check_structured(
    "structured: search_datasets empty",
    "search_datasets",
    {"q": "qwzxjvplk"},
    expect_codes=["no_results"],
)
check_structured("structured: get_dataset", "get_dataset", {"dataset_id": MHPA_ID})
check_structured(
    "structured: get_aggregations", "get_aggregations", {"field": "folder"}
)
check_structured(
    "structured: query_data truncated",
    "query_data",
    {
        "dataset_id": MHPA_ID,
        "where": "HABPRES >= 90",
        "out_fields": "SUBAREA",
        "limit": 2,
    },
    expect_codes=["results_truncated"],
)
check_structured(
    "structured: get_layer_schema",
    "get_layer_schema",
    {"item_id": MHPA_ID, "keyword": "HAB"},
)
check_structured(
    "structured: get_distinct_values",
    "get_distinct_values",
    {"item_id": MHPA_ID, "field": "INHABPRES"},
)
check_structured(
    "structured: spatial_query_point by address",
    "spatial_query_point",
    {"item_id": ZONES_ID, "address": "202 C St", "out_fields": "ZONE_NAME"},
    expect_codes=["geocoded"],
)
check_structured(
    "structured: geocode_address", "geocode_address", {"address": "202 C St"}
)

print("\n=== SUMMARY ===")
n_pass = sum(results)
print(f"{n_pass}/{len(results)} checks passed")
sys.exit(0 if n_pass == len(results) else 1)
