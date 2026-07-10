"""Production smoke test for the San Diego City GIS MCP server.

Exercises the JSON-RPC surface and the core arcgis tool chain end-to-end
against the deployed Lambda, finishing with the "verification query": a
WGS84 point-in-polygon on the Multi-Habitat Planning Area (MHPA) at the
Tijuana River Valley, which must return the containing preserve polygon
with HABPRES. Read-only; paces calls to stay under the API Gateway rate
limit (5 rps) and WAF per-IP cap (300/5min).

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
import urllib.request

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
    check("ping", r.get("result", {}).get("status") == "ok", str(r.get("result")))
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

print("\n=== SUMMARY ===")
n_pass = sum(results)
print(f"{n_pass}/{len(results)} checks passed")
sys.exit(0 if n_pass == len(results) else 1)
