# OpenContext

<p align="center">
  <img src="docs/opencontext_logo.png" alt="OpenContext Logo" width="400">
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

---

**San Diego City GIS MCP** — a City of San Diego fork of OpenContext (forked from the Worcester GIS fork). It serves the City's ArcGIS Server REST services directory ([webmaps.sandiego.gov/arcgis/rest/services](https://webmaps.sandiego.gov/arcgis/rest/services), ArcGIS Enterprise 11.5) through the built-in `arcgis` plugin.

It is a **sibling** to the San Diego regional (SANDAG/SanGIS) and Worcester servers: identical tool names and signatures, so it composes with them at the MCP client with zero new orchestration. The regional server covers county-wide layers; this one covers City-authored layers (MHPA, Base Zones, Community Plan Land Use, and ~700 more).

---

## How discovery works (no Hub here)

`webmaps.sandiego.gov` is a **bare services directory** — there is no ArcGIS Hub / Open Data catalog in front of it, so the Hub-search discovery used by the Worcester fork does not apply. Instead:

1. `scripts/crawl_catalog.py` walks the directory offline: folders → MapServer/FeatureServer services → each service's `/layers?f=json`, capturing layer id, name, geometry type, description, extent, and `maxRecordCount`.
2. The result is serialized to **`plugins/arcgis/catalog.json`** — a versioned, diffable deploy artifact (708 layers from 327 services at the 2026-09-09 crawl).
3. The running server loads that manifest at startup — instant, no live crawl, no cold-start penalty. `search_datasets` does substring/fuzzy/acronym matching over it.
4. Services that require an ArcGIS account (HTTP 401/403 or ArcGIS error codes 498/499) are skipped during the crawl and recorded in the manifest's `skipped` list — 18 services on this host at last crawl (AMPGIS, GetItDone, TED, the PUD_* services, four legacy aerials, …).

**To refresh the catalog:** `python scripts/crawl_catalog.py`, review the diff, commit, redeploy.

## Dataset IDs

A dataset id is the layer's services-directory path:

```
{folder}/{service}/{MapServer|FeatureServer}/{layerId}
```

Known-good public layers (all in the anonymous `Planning/PLN_LongRangePlanning` MapServer):

| Layer | dataset_id | Notes |
| ----- | ---------- | ----- |
| Multi-Habitat Planning Area (MHPA) | `Planning/PLN_LongRangePlanning/MapServer/7` | Polygon. Key fields: `HABPRES` (int, % targeted preservation), `INHABPRES`, `SUBAREA`, `ACRES` |
| Base Zones (official City zoning) | `Planning/PLN_LongRangePlanning/MapServer/27` | Polygon. Zone codes like `RS-1-7`, `CC-3-5` |
| Community Plan Land Use | `Planning/PLN_LongRangePlanning/MapServer/24` | Polygon |

These are curated as `featured_datasets` in `config.yaml` (aliases + notes boost search; edits take effect on deploy without re-crawling).

## The WGS84 contract

The layers are authored in **EPSG:2230** (NAD83 State Plane California Zone VI, US survey feet). Every query this server sends sets `inSR=4326` and `outSR=4326`, so all tools take and return WGS84 lon/lat — same as the sibling servers. Without `inSR`, WGS84 coordinates would be interpreted as State Plane feet and silently return zero rows.

Pagination is metadata-driven: each layer's `maxRecordCount` comes from the catalog (it varies by layer), and `query_data` pages with `resultOffset`/`resultRecordCount` when needed.

## Connect to the server

Add it as a custom connector in Claude (same steps on Claude.ai and Claude Desktop):

1. **Settings → Connectors** (or **Customize → Connectors** on claude.ai)
2. **Add custom connector**
3. Name it e.g. `San Diego City GIS` and paste the URL:

   ```
   https://sandiego-city-gis.codeforanchorage.org/mcp
   ```

Quick health check from a terminal:

```bash
curl -sS -X POST https://sandiego-city-gis.codeforanchorage.org/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
# → {"jsonrpc":"2.0","id":1,"result":{}}
```

### Tools exposed

| Tool | Purpose |
| ---- | ------- |
| `arcgis__search_datasets` | Discover layers by keyword (e.g. "MHPA", "zoning"). Matches names, service/folder names, descriptions, acronyms. Optional `type` filter: `MapServer`/`FeatureServer` or a geometry (`Polygon`, `Point`, `Polyline`) |
| `arcgis__get_dataset` | Fetch a layer's metadata: geometry type, record cap, extent, layer URL |
| `arcgis__get_layer_schema` | List a layer's fields (name, type, alias, coded values), optionally filtered by `keyword` |
| `arcgis__get_distinct_values` | List the distinct values in a field (with optional `like` / `where`) to confirm exact codes |
| `arcgis__query_data` | Query features (supports `where`, `out_fields`, `order_by`, `limit`). Output leads with a `TOTAL MATCHING` count, so "how many X?" needs no paging. Auto-paginates past per-layer record caps |
| `arcgis__spatial_query_point` | Point-in-polygon: which polygon(s) contain a point — by `lon`/`lat` (WGS84) **or** a street `address` |
| `arcgis__geocode_address` | Convert a street address to `lon`/`lat` (US Census geocoder, biased to San Diego) |
| `arcgis__get_aggregations` | Facet counts of the catalog by `folder`, `service`, `service_type`, or `geometry_type` |

Raw JSON-RPC example:

```bash
curl -sS -X POST https://sandiego-city-gis.codeforanchorage.org/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"arcgis__search_datasets",
                 "arguments":{"q":"MHPA","limit":5}}}'
```

### Verified end-to-end

The definition-of-done check is a WGS84 point-in-polygon on MHPA at the **Tijuana River Valley** (32.5539, -117.0846) — a point that returns null on the regional server's County `MSCP_CN` layer but sits inside a City MHPA preserve:

```jsonc
// arcgis__spatial_query_point
{
  "item_id": "Planning/PLN_LongRangePlanning/MapServer/7",
  "lon": -117.0846,
  "lat": 32.5539,
  "out_fields": "HABPRES,INHABPRES,SUBAREA,ACRES"
}
```

returns the containing preserve polygon:

```
Record 1:
  HABPRES: 100
  INHABPRES: Yes
  SUBAREA: 113
  ACRES: 2734.79
```

`scripts/smoke_prod.py` runs this plus the search ("MHPA", "zoning"), schema, TOTAL MATCHING, geocode → zoning chain at City Hall, structured-output, and MCP conformance checks against any deployment:

```bash
python scripts/smoke_prod.py                             # production
python scripts/smoke_prod.py http://localhost:8000/mcp   # local
```

## Local development

```bash
uv sync                              # or: pip install -r requirements.txt
python scripts/crawl_catalog.py      # (re)build plugins/arcgis/catalog.json
python scripts/local_server.py       # serves http://localhost:8000/mcp
python -m pytest tests/ -q           # tests
```

See `CLAUDE.md` and `docs/` for architecture, deployment, and plugin development.
