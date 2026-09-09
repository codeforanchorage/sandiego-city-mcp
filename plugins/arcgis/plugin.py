"""ArcGIS Server directory plugin implementation for OpenContext.

This plugin fronts a bare ArcGIS Server REST services directory (e.g. the
City of San Diego's https://webmaps.sandiego.gov/arcgis/rest/services).
There is no ArcGIS Hub / Open Data catalog in front of it, so dataset
discovery comes from a precomputed catalog manifest built by
``scripts/crawl_catalog.py`` and bundled with the deployment. Queries go
directly to MapServer/FeatureServer layer endpoints.

Coordinate contract: the source layers are authored in EPSG:2230 (NAD83
State Plane California Zone VI, US survey feet), but every query sent by
this plugin sets ``inSR=4326`` and ``outSR=4326``, so all tools take and
return WGS84 lon/lat. Without inSR, WGS84 coordinates would be read as
State Plane feet and silently match nothing.
"""

import html
import logging
import re
import unicodedata
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import httpx

from core.interfaces import (
    DataPlugin,
    PluginType,
    ToolDefinition,
    ToolInputError,
    ToolResult,
)
from plugins.arcgis.catalog_index import (
    AGGREGATABLE_FIELDS,
    CatalogIndex,
    friendly_geometry,
    load_manifest,
)
from plugins.arcgis.config_schema import ArcGISPluginConfig
from plugins.arcgis.where_validator import (
    OrderByValidator,
    OutFieldsValidator,
    WhereValidator,
)

logger = logging.getLogger(__name__)

# US Census oneline geocoder: free, no API key, nationwide, returns WGS84
# lon/lat that feed directly into spatial_query_point.
_CENSUS_GEOCODER_URL = (
    "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
)

# HTML-tag stripping and a small unicode->ASCII punctuation map. ArcGIS
# descriptions are often authored as HTML with smart quotes, dashes, and
# non-breaking spaces; cleaning these keeps tool output readable and
# ASCII-safe (e.g. for M365 Copilot).
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_UNICODE_PUNCT = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "--",
    "…": "...",
    " ": " ",
    "·": "-",
    "•": "-",
}

# Path segments allowed inside a dataset_id (folder and service names).
_ID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_\-. ()]+$")

# Hard ceiling on pagination round-trips for a single query_data call.
_MAX_QUERY_PAGES = 20

_EXAMPLE_ID = "Planning/PLN_LongRangePlanning/MapServer/7"

# Every tool is a read-only query against public GIS data (readOnlyHint lets
# clients skip per-call confirmation) that reaches an external service
# (openWorldHint). idempotentHint is deliberately absent: the schema defines
# it as meaningful only when readOnlyHint is false.
_READ_ONLY_TOOL = {"readOnlyHint": True, "openWorldHint": True}

# Display titles (top-level Tool.title, precedence title -> annotations.title
# -> name). The wire name is plugin-prefixed (arcgis__query_data) and reads
# badly in a tool picker. Keep these identical across the GIS forks.
_TOOL_TITLES = {
    "search_datasets": "Search GIS Layers",
    "get_dataset": "Layer Details",
    "get_aggregations": "Catalog Facet Counts",
    "query_data": "Query Layer Records",
    "get_layer_schema": "Layer Field Schema",
    "get_distinct_values": "Distinct Field Values",
    "spatial_query_point": "Features at a Point",
    "geocode_address": "Geocode Address",
}

# Marker note on catalog entries fetched live (not in the bundled manifest).
_LIVE_NOTE = "Not in the bundled catalog (fetched live)."

# Stable caveat codes. A caller branches on these instead of parsing prose
# that may be reworded. The schema enum below is generated from this tuple,
# so an emitted code outside it is impossible without also changing the
# contract.
CAVEAT_CODES = (
    "limit_clamped",
    "results_truncated",
    "page_cap_reached",
    "pagination_unsupported",
    "count_unavailable",
    "live_metadata",
    "geocoded",
    "multiple_geocode_matches",
    "address_snapped",
    "no_results",
)


class _Caveats:
    """Warnings for one tool response, rendered into BOTH output forms.

    Every warning is added here exactly once. The prose lines and the
    ``caveats`` array in structuredContent are both derived from this
    list, which is what stops the human-readable text and the
    machine-readable contract from drifting apart as either is edited.
    """

    def __init__(self) -> None:
        self._items: List[Dict[str, str]] = []

    def add(self, code: str, message: Optional[str]) -> None:
        if code not in CAVEAT_CODES:  # pragma: no cover - programming error
            raise RuntimeError(f"unknown caveat code {code!r}")
        if message:
            self._items.append({"code": code, "message": message})

    @property
    def messages(self) -> List[str]:
        return [item["message"] for item in self._items]

    def as_list(self) -> List[Dict[str, str]]:
        return [dict(item) for item in self._items]

    def __len__(self) -> int:
        return len(self._items)


class _ToolOutput(NamedTuple):
    """What a tool handler returns: prose for the model, data for code."""

    text: str
    structured: Dict[str, Any]


# ── Output schemas ────────────────────────────────────────────────────
#
# A declared outputSchema is BINDING -- the spec says servers MUST return
# conforming structured results. These are deliberately loose where the
# real data is loose: rows carry whatever out_fields the caller asked
# for (raw ArcGIS attributes, dates as epoch milliseconds), distinct
# values can be strings, numbers or null, and TOTAL MATCHING is null when
# the count query fails. A schema written from the happy path would make
# the server violate its own contract on live data.
#
# Shared envelope across all eight tools: {query, summary, caveats} plus
# ONE payload key named for what it carries (layers, layer, buckets, rows,
# fields, values, candidates). A model that learns the shape once can
# read any of them. Keep these identical across the GIS forks.

_CAVEATS_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "description": (
        "Warnings about this result. Branch on `code` rather than parsing "
        "the prose; every entry here also appears verbatim in the text "
        "content."
    ),
    "items": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "enum": list(CAVEAT_CODES)},
            "message": {"type": "string"},
        },
        "required": ["code", "message"],
        "additionalProperties": False,
    },
}

_ROW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "One record: RAW layer attributes keyed by physical field name. "
        "Which fields are present depends on out_fields. Date-typed fields "
        "are epoch milliseconds."
    ),
    "additionalProperties": True,
}

_NULLABLE_STR: Dict[str, Any] = {"type": ["string", "null"]}

_LAYER_ENTRY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "One indexed layer from the services-directory catalog.",
    "properties": {
        "dataset_id": {
            "type": "string",
            "description": "Path id: {folder}/{service}/{MapServer|FeatureServer}/{layerId}",
        },
        "name": {"type": "string"},
        "folder": {"type": "string", "description": "'' for the directory root."},
        "service": {"type": "string"},
        "service_type": {"type": "string", "enum": ["MapServer", "FeatureServer"]},
        "layer_id": {"type": "integer"},
        "geometry_type": {
            "type": "string",
            "description": "esriGeometryPolygon / Point / Polyline / '' for tables.",
        },
        "description": {"type": "string"},
        "featured": {"type": "boolean"},
    },
    "required": ["dataset_id", "name", "service_type", "layer_id"],
    "additionalProperties": True,
}


def _envelope_schema(
    description: str,
    query_props: Dict[str, Any],
    summary_props: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Build one tool's output schema around the shared envelope."""
    properties: Dict[str, Any] = {
        "query": {
            "type": "object",
            "description": "What was asked, as the server resolved it.",
            "properties": query_props,
            "additionalProperties": True,
        },
        "summary": {
            "type": "object",
            "description": "Counts and outcome flags for this result.",
            "properties": summary_props,
            "additionalProperties": True,
        },
        "caveats": _CAVEATS_SCHEMA,
    }
    properties.update(payload)
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        # Every declared key is required on every code path; extra keys
        # stay legal so a later addition is not a contract violation.
        "required": ["query", "summary", "caveats", *payload],
        "additionalProperties": True,
    }


_OUTPUT_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "search_datasets": _envelope_schema(
        "Catalog search results.",
        {
            "q": {"type": "string"},
            "type": _NULLABLE_STR,
            "limit": {"type": "integer"},
        },
        {
            "returned": {"type": "integer"},
            "catalog_layers": {
                "type": "integer",
                "description": "Total layers in the bundled catalog.",
            },
            "catalog_generated_at": {
                **_NULLABLE_STR,
                "description": "ISO timestamp of the catalog crawl.",
            },
        },
        {"layers": {"type": "array", "items": _LAYER_ENTRY_SCHEMA}},
    ),
    "get_dataset": _envelope_schema(
        "Metadata for one layer.",
        {"dataset_id": {"type": "string"}},
        {
            "in_catalog": {
                "type": "boolean",
                "description": "False when metadata was fetched live.",
            },
            "geometry": {
                "type": "string",
                "description": "Friendly geometry name ('' for tables).",
            },
        },
        {
            "layer": {
                "type": "object",
                "properties": {
                    **_LAYER_ENTRY_SCHEMA["properties"],
                    "layer_url": {"type": "string"},
                    "max_record_count": {"type": ["integer", "null"]},
                    "supports_pagination": {"type": "boolean"},
                    "extent": {
                        "type": ["object", "null"],
                        "description": "Native extent (wkid 2230, State Plane feet).",
                    },
                },
                "required": [
                    "dataset_id",
                    "name",
                    "service_type",
                    "layer_id",
                    "layer_url",
                ],
                "additionalProperties": True,
            }
        },
    ),
    "get_aggregations": _envelope_schema(
        "Facet counts of indexed layers.",
        {
            "field": {"type": "string", "enum": list(AGGREGATABLE_FIELDS)},
            "q": _NULLABLE_STR,
        },
        {
            "bucket_count": {"type": "integer"},
            "layers_counted": {"type": "integer"},
        },
        {
            "buckets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "count": {"type": "integer"},
                    },
                    "required": ["key", "count"],
                    "additionalProperties": False,
                },
            }
        },
    ),
    "query_data": _envelope_schema(
        "Attribute query results.",
        {
            "dataset_id": {"type": "string"},
            "where": {"type": "string"},
            "out_fields": {"type": "string"},
            "order_by": _NULLABLE_STR,
            "limit": {"type": "integer"},
        },
        {
            "returned": {"type": "integer"},
            "total_matching": {
                "type": ["integer", "null"],
                "description": (
                    "Server-side count of ALL records matching `where`. Null "
                    "when the count query failed -- which is NOT zero."
                ),
            },
            "truncated": {
                "type": "boolean",
                "description": "True when more records match than were returned.",
            },
            "pages_fetched": {"type": "integer"},
            "server_page_size": {"type": ["integer", "null"]},
        },
        {"rows": {"type": "array", "items": _ROW_SCHEMA}},
    ),
    "get_layer_schema": _envelope_schema(
        "Field list for one layer.",
        {"item_id": {"type": "string"}, "keyword": _NULLABLE_STR},
        {
            "layer_name": {"type": "string"},
            "geometry_type": {"type": "string"},
            "layer_url": {"type": "string"},
            "field_count": {"type": "integer"},
            "filtered": {
                "type": "boolean",
                "description": "True when `keyword` narrowed the list.",
            },
        },
        {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": (
                        "RAW ArcGIS field descriptor (name, type, alias, "
                        "length, domain with codedValues, ...)."
                    ),
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string"},
                        "alias": {"type": "string"},
                    },
                    "required": ["name"],
                    "additionalProperties": True,
                },
            }
        },
    ),
    "get_distinct_values": _envelope_schema(
        "Distinct values of one field.",
        {
            "item_id": {"type": "string"},
            "field": {"type": "string"},
            "like": _NULLABLE_STR,
            "where": {"type": "string"},
            "limit": {"type": "integer"},
        },
        {
            "returned": {"type": "integer"},
            "truncated": {
                "type": "boolean",
                "description": "True when the list hit `limit`; more may exist.",
            },
        },
        {
            "values": {
                "type": "array",
                "description": "Raw values in server order; may include null.",
                "items": {},
            }
        },
    ),
    "spatial_query_point": _envelope_schema(
        "Polygons containing (or, when snapped, within a few metres of) a point.",
        {
            "item_id": {"type": "string"},
            "lon": {"type": "number"},
            "lat": {"type": "number"},
            "address": _NULLABLE_STR,
            "matched_address": {
                **_NULLABLE_STR,
                "description": "Geocoder's normalised address when `address` was used.",
            },
            "where": {"type": "string"},
            "out_fields": {"type": "string"},
            "limit": {"type": "integer"},
        },
        {
            "returned": {"type": "integer"},
            "geocoded": {"type": "boolean"},
            "snapped_to_meters": {
                "type": ["integer", "null"],
                "description": (
                    "Set when no polygon contained the geocoded point and the "
                    "result is polygons within this many metres instead."
                ),
            },
            "truncated": {
                "type": "boolean",
                "description": "True when the result hit `limit`.",
            },
        },
        {"rows": {"type": "array", "items": _ROW_SCHEMA}},
    ),
    "geocode_address": _envelope_schema(
        "Geocoder candidates.",
        {
            "address": {"type": "string"},
            "geocoder_query": {
                "type": "string",
                "description": "The address as sent, with the region bias appended.",
            },
        },
        {"returned": {"type": "integer"}},
        {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "matched_address": {"type": "string"},
                        "lon": {"type": "number"},
                        "lat": {"type": "number"},
                    },
                    "required": ["matched_address", "lon", "lat"],
                    "additionalProperties": False,
                },
            }
        },
    ),
}


class ArcGISPlugin(DataPlugin):
    """Plugin for a bare ArcGIS Server REST services directory.

    Implements the DataPlugin interface with the same tool names and
    signatures as the sibling Hub-based forks, so it composes with them
    at the MCP client with zero new orchestration.
    """

    plugin_name = "arcgis"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "2.0.0"

    # Retry radius for address-form spatial_query_point when the geocoded
    # point hits nothing. Geocoders place addresses on the street
    # centerline, and City polygon layers (zoning, plan areas) can leave
    # the right-of-way unclassified. Same value as the SANDAG fork, where
    # 10 m recovered the named parcel at City Hall and 20 m already pulled
    # in unrelated lots across the block.
    _ADDRESS_SNAP_METERS = 10

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[ArcGISPluginConfig] = None
        self.feature_client: Optional[httpx.AsyncClient] = None
        self.index: Optional[CatalogIndex] = None
        # Layers queried by id but absent from the bundled catalog (e.g.
        # published after the last crawl) get their metadata fetched live
        # once and cached here for the life of the instance.
        self._live_meta_cache: Dict[str, Dict[str, Any]] = {}
        # Field names per layer, fetched once per instance so the WHERE
        # field-name check costs one extra round-trip per layer, not per
        # query (warm Lambdas keep it across invocations).
        self._fields_cache: Dict[str, List[str]] = {}
        self._catalog_generated_at: Optional[str] = None

    async def initialize(self) -> bool:
        try:
            self.plugin_config = ArcGISPluginConfig(**self.config)

            manifest = load_manifest(self.plugin_config.catalog_path)
            featured = [f.model_dump() for f in self.plugin_config.featured_datasets]
            self.index = CatalogIndex(manifest, featured=featured)
            self._catalog_generated_at = manifest.get("generated_at")

            feature_headers = {"Accept": "application/json"}
            params = {}
            if self.plugin_config.token:
                params["token"] = self.plugin_config.token
            self.feature_client = httpx.AsyncClient(
                headers=feature_headers,
                params=params,
                timeout=self.plugin_config.timeout,
            )

            stats = manifest.get("stats", {})
            self._initialized = True
            logger.info(
                f"ArcGIS directory plugin initialized for "
                f"{self.plugin_config.city_name}: "
                f"{len(self.index.entries)} layers indexed "
                f"(catalog generated {manifest.get('generated_at', 'unknown')}, "
                f"{stats.get('services_skipped', 0)} services skipped)"
            )
            return True

        except Exception as e:
            logger.error(
                f"Failed to initialize ArcGIS directory plugin: {e}", exc_info=True
            )
            return False

    async def shutdown(self) -> None:
        if self.feature_client:
            await self.feature_client.aclose()
            self.feature_client = None
        self._initialized = False
        logger.info("ArcGIS directory plugin shut down")

    def get_tools(self) -> List[ToolDefinition]:
        city = self.plugin_config.city_name if self.plugin_config else "Unknown"
        return [
            ToolDefinition(
                name="search_datasets",
                title=_TOOL_TITLES["search_datasets"],
                annotations=_READ_ONLY_TOOL,
                output_schema=_OUTPUT_SCHEMAS["search_datasets"],
                description=(
                    f"Search {city}'s GIS layer catalog (an indexed crawl of "
                    "the city's ArcGIS Server services directory). Matches "
                    "layer names, service/folder names, descriptions, and "
                    "common acronyms (e.g. 'MHPA'). Every result is a "
                    "queryable map layer; pass its dataset_id (a path like "
                    f"'{_EXAMPLE_ID}') to get_dataset, get_layer_schema, "
                    "query_data, or spatial_query_point."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "q": {
                            "type": "string",
                            "description": "Full-text search query",
                        },
                        "type": {
                            "type": "string",
                            "description": (
                                "Optional filter: 'MapServer' or "
                                "'FeatureServer' (service type), or a "
                                "geometry type -- 'Polygon', 'Point', "
                                "'Polyline'."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 10)",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                    "required": ["q"],
                },
            ),
            ToolDefinition(
                name="get_dataset",
                title=_TOOL_TITLES["get_dataset"],
                annotations=_READ_ONLY_TOOL,
                output_schema=_OUTPUT_SCHEMAS["get_dataset"],
                description=(
                    "Get metadata for a specific layer by dataset_id (a path "
                    f"like '{_EXAMPLE_ID}'): geometry type, description, "
                    "record cap, extent, and the layer URL."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "dataset_id": {
                            "type": "string",
                            "description": (
                                "Layer path id from search_datasets, e.g. "
                                f"'{_EXAMPLE_ID}'"
                            ),
                        },
                    },
                    "required": ["dataset_id"],
                },
            ),
            ToolDefinition(
                name="get_aggregations",
                title=_TOOL_TITLES["get_aggregations"],
                annotations=_READ_ONLY_TOOL,
                output_schema=_OUTPUT_SCHEMAS["get_aggregations"],
                description=(
                    "Get facet counts of the indexed layers by a catalog "
                    "field -- explore what the directory holds by 'folder', "
                    "'service', 'service_type', or 'geometry_type'."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "field": {
                            "type": "string",
                            "description": (
                                "Field to aggregate. Available fields: "
                                '"folder", "service", "service_type", '
                                '"geometry_type"'
                            ),
                        },
                        "q": {
                            "type": "string",
                            "description": "Optional search query to scope the aggregation",
                        },
                    },
                    "required": ["field"],
                },
            ),
            ToolDefinition(
                name="query_data",
                title=_TOOL_TITLES["query_data"],
                annotations=_READ_ONLY_TOOL,
                output_schema=_OUTPUT_SCHEMAS["query_data"],
                description=(
                    "Query records from a layer by dataset_id. The output "
                    "leads with TOTAL MATCHING, the full count of records "
                    "matching `where` -- so for 'how many X?' you do not need "
                    "to page through results. Results paginate automatically "
                    "past the layer's server-side record cap. Use `order_by` "
                    "(e.g. 'ACRES DESC') for top-N questions, and "
                    "get_layer_schema first for CASE-SENSITIVE field names."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "dataset_id": {
                            "type": "string",
                            "description": (
                                "Layer path id (same as get_dataset), e.g. "
                                f"'{_EXAMPLE_ID}'"
                            ),
                        },
                        "where": {
                            "type": "string",
                            "description": "SQL WHERE clause for filtering",
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": "Comma-separated field names to return",
                            "default": "*",
                        },
                        "order_by": {
                            "type": "string",
                            "description": (
                                "Optional ORDER BY, e.g. 'ACRES DESC' for "
                                "largest-first. Field names are "
                                "CASE-SENSITIVE."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of records (default: 100)",
                            "default": 100,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                    },
                    "required": ["dataset_id"],
                },
            ),
            ToolDefinition(
                name="get_layer_schema",
                title=_TOOL_TITLES["get_layer_schema"],
                annotations=_READ_ONLY_TOOL,
                output_schema=_OUTPUT_SCHEMAS["get_layer_schema"],
                description=(
                    "List a layer's fields (name, type, alias, coded values) "
                    "so you can write a correct query_data WHERE clause "
                    "without guessing. Field names are CASE-SENSITIVE. Pass a "
                    "dataset_id; optional `keyword` shows only matching "
                    "fields. Typical chain: search_datasets -> "
                    "get_layer_schema -> query_data."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": (
                                "Layer path id (same as get_dataset), e.g. "
                                f"'{_EXAMPLE_ID}'"
                            ),
                        },
                        "keyword": {
                            "type": "string",
                            "description": (
                                "Optional: only show fields whose name or alias "
                                "contains this term."
                            ),
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="get_distinct_values",
                title=_TOOL_TITLES["get_distinct_values"],
                annotations=_READ_ONLY_TOOL,
                output_schema=_OUTPUT_SCHEMAS["get_distinct_values"],
                description=(
                    "List the distinct values in one field of a layer -- to "
                    "confirm the exact spelling/format of codes before "
                    "filtering (e.g. 'RS-1-7' vs 'RS-1-07' zone codes). Field "
                    "names are CASE-SENSITIVE (use get_layer_schema first). "
                    "Optional `like` substring-narrows the values."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "Layer path id (same as get_dataset).",
                        },
                        "field": {
                            "type": "string",
                            "description": (
                                "Field name (CASE-SENSITIVE) to list values for."
                            ),
                        },
                        "like": {
                            "type": "string",
                            "description": (
                                "Optional substring; only values containing it "
                                "are returned."
                            ),
                        },
                        "where": {
                            "type": "string",
                            "description": (
                                "Optional WHERE clause to narrow contributing records."
                            ),
                            "default": "1=1",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max distinct values (default 200).",
                            "default": 200,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                    },
                    "required": ["item_id", "field"],
                },
            ),
            ToolDefinition(
                name="spatial_query_point",
                title=_TOOL_TITLES["spatial_query_point"],
                annotations=_READ_ONLY_TOOL,
                output_schema=_OUTPUT_SCHEMAS["spatial_query_point"],
                description=(
                    "Point-in-polygon lookup: return the attributes of every "
                    "polygon in a layer that contains a point -- 'which zone / "
                    "community plan area / preserve is at this location?'. "
                    "Provide EITHER a street `address` (geocoded "
                    "automatically) OR both `lon` and `lat` (WGS84 decimal "
                    "degrees -- the plugin handles the State Plane "
                    "conversion server-side). Use on polygon layers (check "
                    "geometry with get_layer_schema). If a geocoded address "
                    "falls in the street and hits no polygon, the lookup "
                    "retries once within 10 m and flags it (address_snapped). "
                    "Returns attributes only, no geometry."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": (
                                "Layer path id of a polygon layer, e.g. "
                                f"'{_EXAMPLE_ID}' (MHPA preserves)."
                            ),
                        },
                        "address": {
                            "type": "string",
                            "description": (
                                "Street address to geocode (alternative to "
                                "lon/lat), e.g. '202 C St' (City Hall). Biased "
                                "to the configured region."
                            ),
                        },
                        "lon": {
                            "type": "number",
                            "description": (
                                "Longitude, WGS84 decimal degrees (-180 to 180). "
                                "Note: lon first. Omit if `address` is given."
                            ),
                        },
                        "lat": {
                            "type": "number",
                            "description": (
                                "Latitude, WGS84 decimal degrees (-90 to 90). "
                                "Omit if `address` is given."
                            ),
                        },
                        "where": {
                            "type": "string",
                            "description": "Optional WHERE clause to further filter.",
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": "Comma-separated field names to return.",
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max features (default 10, max 50).",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="geocode_address",
                title=_TOOL_TITLES["geocode_address"],
                annotations=_READ_ONLY_TOOL,
                output_schema=_OUTPUT_SCHEMAS["geocode_address"],
                description=(
                    "Convert a street address to coordinates (lon/lat, WGS84) via "
                    "the US Census geocoder. Use the result with "
                    "spatial_query_point, or call spatial_query_point with "
                    "`address` directly. Biased to the configured region; include "
                    "city/state for addresses elsewhere."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "address": {
                            "type": "string",
                            "description": "Street address, e.g. '202 C St' (City Hall).",
                        },
                    },
                    "required": ["address"],
                },
            ),
        ]

    @staticmethod
    def _int_arg(arguments: Dict[str, Any], name: str, default: int) -> int:
        """Read an integer argument, rejecting garbage as a caller error.

        A bare ``int()`` over caller input raises a stdlib ValueError that
        logs as a server fault and tells the caller nothing useful.
        """
        raw = arguments.get(name, default)
        if raw is None:
            return default
        if isinstance(raw, bool):
            raise ToolInputError(f"{name} must be an integer (got {raw!r})")
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ToolInputError(f"{name} must be an integer (got {raw!r})") from None

    @staticmethod
    def _float_arg(arguments: Dict[str, Any], name: str) -> Optional[float]:
        """Read an optional float argument, rejecting garbage as a caller error."""
        raw = arguments.get(name)
        if raw is None:
            return None
        if isinstance(raw, bool):
            raise ToolInputError(f"{name} must be a number (got {raw!r})")
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise ToolInputError(f"{name} must be a number (got {raw!r})") from None

    @staticmethod
    def _require_str(arguments: Dict[str, Any], name: str) -> str:
        value = arguments.get(name)
        if not value or not isinstance(value, str):
            raise ToolInputError(f"{name} is required")
        return value

    @staticmethod
    def _clamp_limit(limit: int, ceiling: int, caveats: "_Caveats") -> int:
        """Enforce a tool's server-side ceiling, recording it as a caveat
        rather than silently returning fewer rows than asked for."""
        if limit < 1:
            raise ToolInputError(f"limit must be at least 1 (got {limit})")
        if limit > ceiling:
            caveats.add(
                "limit_clamped",
                f"limit {limit} was clamped to this tool's maximum of {ceiling}.",
            )
            return ceiling
        return limit

    @staticmethod
    def _envelope(
        query: Dict[str, Any],
        summary: Dict[str, Any],
        caveats: "_Caveats",
        **payload: Any,
    ) -> Dict[str, Any]:
        """Assemble the {query, summary, caveats, <payload>} envelope."""
        envelope: Dict[str, Any] = {
            "query": query,
            "summary": summary,
            "caveats": caveats.as_list(),
        }
        envelope.update(payload)
        return envelope

    @staticmethod
    def _with_caveats(text: str, caveats: "_Caveats") -> str:
        """Render every caveat into the prose, on EVERY return path, so
        anything in structured `caveats` is also visible to a model that
        only reads the text."""
        if not len(caveats):
            return text
        return text.rstrip("\n") + "\n\n" + "\n".join(caveats.messages)

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        handler = getattr(self, f"_tool_{tool_name}", None)
        if tool_name not in _TOOL_TITLES or handler is None:
            return ToolResult(
                content=[],
                success=False,
                error_message=f"Unknown tool: {tool_name}",
            )
        try:
            output: _ToolOutput = await handler(arguments)
            return ToolResult(
                content=[{"type": "text", "text": output.text}],
                structured_content=output.structured,
                success=True,
            )
        except ToolInputError as e:
            # The caller passed something invalid. WARNING, no traceback:
            # a stack trace here is noise that buries real faults, and the
            # message alone already tells the caller how to fix the call.
            logger.warning(f"Invalid arguments for tool {tool_name}: {e}")
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Invalid tool arguments",
            )
        except Exception as e:
            # Everything else IS a server or upstream fault -- keep the
            # traceback, that is what these logs are for.
            logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Tool execution failed",
            )

    # ── Tool handlers: each returns prose + structured content ──────────

    async def _tool_search_datasets(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        q = a.get("q") or ""
        item_type = a.get("type") or None
        limit = self._clamp_limit(self._int_arg(a, "limit", 10), 100, caveats)
        datasets = await self.search_datasets(q, limit, item_type)
        if not datasets:
            caveats.add(
                "no_results",
                "No layers found. Try a broader keyword, or explore with "
                "get_aggregations (field='folder' or 'service').",
            )
        layers = [
            {
                "dataset_id": d.get("dataset_id", ""),
                "name": d.get("name", ""),
                "folder": d.get("folder", "") or "",
                "service": d.get("service", "") or "",
                "service_type": d.get("service_type", ""),
                "layer_id": int(d.get("layer_id", 0)),
                "geometry_type": d.get("geometry_type", "") or "",
                "description": self._describe_entry(d),
                "featured": bool(d.get("featured")),
            }
            for d in datasets
        ]
        structured = self._envelope(
            {"q": q, "type": item_type, "limit": limit},
            {
                "returned": len(layers),
                "catalog_layers": len(self.index.entries) if self.index else 0,
                "catalog_generated_at": self._catalog_generated_at,
            },
            caveats,
            layers=layers,
        )
        text = self._with_caveats(self._format_search_results(datasets), caveats)
        return _ToolOutput(text, structured)

    async def _tool_get_dataset(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        dataset_id = self._validate_dataset_id(self._require_str(a, "dataset_id"))
        dataset = await self.get_dataset(dataset_id)
        in_catalog = dataset.get("note") != _LIVE_NOTE
        if not in_catalog:
            caveats.add("live_metadata", _LIVE_NOTE)
        layer = dict(dataset)
        layer["layer_id"] = int(layer.get("layer_id", 0))
        layer["supports_pagination"] = bool(layer.get("supports_pagination"))
        layer["featured"] = bool(layer.get("featured"))
        structured = self._envelope(
            {"dataset_id": dataset_id},
            {
                "in_catalog": in_catalog,
                "geometry": friendly_geometry(dataset.get("geometry_type", "")),
            },
            caveats,
            layer=layer,
        )
        text = self._with_caveats(self._format_dataset(dataset), caveats)
        return _ToolOutput(text, structured)

    async def _tool_get_aggregations(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        field = self._require_str(a, "field")
        q = a.get("q") or None
        buckets = await self.get_aggregations(field, q)
        if not buckets:
            caveats.add(
                "no_results",
                f"No aggregation results for '{field}'"
                + (f" with query '{q}'" if q else "")
                + ".",
            )
        out = [
            {"key": str(b.get("key", "")), "count": int(b.get("doc_count", 0))}
            for b in buckets
        ]
        structured = self._envelope(
            {"field": field, "q": q},
            {"bucket_count": len(out), "layers_counted": sum(b["count"] for b in out)},
            caveats,
            buckets=out,
        )
        text = self._with_caveats(self._format_aggregations(field, buckets), caveats)
        return _ToolOutput(text, structured)

    async def _tool_query_data(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        dataset_id = self._validate_dataset_id(self._require_str(a, "dataset_id"))
        where = a.get("where") or "1=1"
        out_fields = a.get("out_fields") or "*"
        order_by = a.get("order_by") or None
        limit = self._clamp_limit(self._int_arg(a, "limit", 100), 1000, caveats)
        filters: Dict[str, Any] = {"where": where, "out_fields": out_fields}
        if order_by:
            filters["order_by"] = order_by
        records, meta = await self._fetch_records(dataset_id, filters, limit)
        # Total match count is best-effort: a count failure must not hide
        # the records we already fetched.
        try:
            total: Optional[int] = await self.get_record_count(dataset_id, where)
        except Exception as count_err:
            logger.warning(f"Could not get record count: {count_err}")
            total = None
            caveats.add(
                "count_unavailable",
                "The total match count is unavailable: the count query "
                "failed, so the total is unknown (not zero).",
            )
        if meta["live_metadata"]:
            caveats.add("live_metadata", _LIVE_NOTE)
        truncated = (total is not None and total > len(records)) or (
            total is None and meta["exceeded_transfer_limit"]
        )
        if truncated:
            caveats.add(
                "results_truncated",
                f"Only the first {len(records)} matching record(s) are shown"
                + (f" of {total}" if total is not None else "")
                + f" (limit {limit}); raise limit or narrow `where`.",
            )
        if meta["page_cap_reached"]:
            caveats.add(
                "page_cap_reached",
                f"Stopped after {meta['pages']} server pages; narrow `where` "
                "or lower limit to see everything.",
            )
        if meta["pagination_unsupported"]:
            caveats.add(
                "pagination_unsupported",
                "This layer does not support paging; only the first server "
                f"page ({meta['server_page_size']} records max) is available.",
            )
        if not records:
            caveats.add(
                "no_results",
                "No records matched. Check field names with get_layer_schema "
                "and exact values with get_distinct_values.",
            )
        structured = self._envelope(
            {
                "dataset_id": dataset_id,
                "where": where,
                "out_fields": out_fields,
                "order_by": order_by,
                "limit": limit,
            },
            {
                "returned": len(records),
                "total_matching": total,
                "truncated": bool(truncated),
                "pages_fetched": meta["pages"],
                "server_page_size": meta["server_page_size"],
            },
            caveats,
            rows=records,
        )
        text = self._with_caveats(
            self._format_query_results(records, limit, total=total), caveats
        )
        return _ToolOutput(text, structured)

    async def _tool_get_layer_schema(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        item_id = self._validate_dataset_id(self._require_str(a, "item_id"))
        keyword = a.get("keyword") or None
        schema = await self.get_layer_schema(item_id, keyword)
        fields = schema.get("fields", []) or []
        if not fields:
            caveats.add(
                "no_results",
                "No fields found for this layer"
                + (f" matching '{keyword}'" if keyword else "")
                + ".",
            )
        structured = self._envelope(
            {"item_id": item_id, "keyword": keyword},
            {
                "layer_name": schema.get("layer_name", "") or "",
                "geometry_type": schema.get("geometry_type", "") or "",
                "layer_url": schema.get("layer_url", ""),
                "field_count": len(fields),
                "filtered": bool(keyword),
            },
            caveats,
            fields=[f for f in fields if isinstance(f, dict) and f.get("name")],
        )
        text = self._with_caveats(self._format_layer_schema(schema), caveats)
        return _ToolOutput(text, structured)

    async def _tool_get_distinct_values(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        item_id = self._validate_dataset_id(self._require_str(a, "item_id"))
        field = self._require_str(a, "field")
        like = a.get("like") or None
        where = a.get("where") or "1=1"
        limit = self._clamp_limit(self._int_arg(a, "limit", 200), 1000, caveats)
        values = await self.get_distinct_values(item_id, field, like, where, limit)
        truncated = len(values) >= limit
        if truncated:
            caveats.add(
                "results_truncated",
                f"Distinct values were capped at {limit}; more may exist. Pass "
                "a `like` filter or raise limit.",
            )
        if not values:
            caveats.add("no_results", f"No distinct values found for '{field}'.")
        structured = self._envelope(
            {
                "item_id": item_id,
                "field": field,
                "like": like,
                "where": where,
                "limit": limit,
            },
            {"returned": len(values), "truncated": truncated},
            caveats,
            values=list(values),
        )
        text = self._with_caveats(self._format_distinct_values(field, values), caveats)
        return _ToolOutput(text, structured)

    async def _tool_spatial_query_point(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        item_id = self._validate_dataset_id(self._require_str(a, "item_id"))
        lon = self._float_arg(a, "lon")
        lat = self._float_arg(a, "lat")
        address = a.get("address") or None
        matched_address: Optional[str] = None
        if (lon is None or lat is None) and address:
            candidates = await self.geocode_address(address)
            if not candidates:
                raise ToolInputError(
                    f"Could not geocode address: {address}. Try including the "
                    f"city and state, e.g. '{address}, San Diego, CA'."
                )
            lon = candidates[0]["lon"]
            lat = candidates[0]["lat"]
            matched_address = candidates[0]["matched_address"]
            caveats.add(
                "geocoded",
                f"Geocoded '{address}' -> {matched_address} ({lat}, {lon})",
            )
            if len(candidates) > 1:
                caveats.add(
                    "multiple_geocode_matches",
                    f"{len(candidates)} geocode matches for '{address}'; the "
                    "first was used. Call geocode_address to see them all.",
                )
        if lon is None or lat is None:
            raise ToolInputError("Provide either `address` or both `lon` and `lat`.")
        where = a.get("where") or "1=1"
        out_fields = a.get("out_fields") or "*"
        limit = self._clamp_limit(self._int_arg(a, "limit", 10), 50, caveats)
        records = await self.spatial_query_point(
            item_id, lon, lat, where, out_fields, limit
        )
        snapped: Optional[int] = None
        if not records and matched_address is not None:
            # Geocoders place addresses on the street centerline, so the
            # point can fall in the right-of-way just outside the polygon
            # it names. Retry once within a few metres; the caveat keeps
            # the caller honest about what was matched.
            records = await self.spatial_query_point(
                item_id,
                lon,
                lat,
                where,
                out_fields,
                limit,
                distance_m=self._ADDRESS_SNAP_METERS,
            )
            if records:
                snapped = self._ADDRESS_SNAP_METERS
                caveats.add(
                    "address_snapped",
                    f"No polygon contains the geocoded point exactly; showing "
                    f"polygons within {snapped} m of it (geocoders place "
                    f"addresses on the street centerline).",
                )
        truncated = len(records) >= limit
        if truncated:
            caveats.add(
                "results_truncated",
                f"Result hit the limit of {limit}; more polygons may contain "
                "this point.",
            )
        if not records:
            caveats.add(
                "no_results",
                "No polygon in this layer contains the point. Check that "
                "item_id is a polygon layer (get_dataset) and that lon/lat are "
                "WGS84 with lon first.",
            )
        structured = self._envelope(
            {
                "item_id": item_id,
                "lon": lon,
                "lat": lat,
                "address": address,
                "matched_address": matched_address,
                "where": where,
                "out_fields": out_fields,
                "limit": limit,
            },
            {
                "returned": len(records),
                "geocoded": matched_address is not None,
                "snapped_to_meters": snapped,
                "truncated": truncated,
            },
            caveats,
            rows=records,
        )
        text = self._with_caveats(self._format_query_results(records, limit), caveats)
        return _ToolOutput(text, structured)

    async def _tool_geocode_address(self, a: Dict[str, Any]) -> _ToolOutput:
        caveats = _Caveats()
        address = self._require_str(a, "address")
        candidates = await self.geocode_address(address)
        if not candidates:
            caveats.add(
                "no_results",
                f"No geocode match for '{address}'. Try including the city and "
                f"state, e.g. '{address}, San Diego, CA'.",
            )
        structured = self._envelope(
            {"address": address, "geocoder_query": self._geocoder_query(address)},
            {"returned": len(candidates)},
            caveats,
            candidates=[
                {
                    "matched_address": str(c.get("matched_address", "")),
                    "lon": float(c["lon"]),
                    "lat": float(c["lat"]),
                }
                for c in candidates
            ],
        )
        text = self._with_caveats(self._format_geocode(address, candidates), caveats)
        return _ToolOutput(text, structured)

    # ── Dataset id / URL resolution ──────────────────────────────────────

    @staticmethod
    def _validate_dataset_id(dataset_id: str) -> str:
        """Validate the path-style dataset id and return its normal form.

        The id is interpolated into the request URL, so this is a security
        boundary: only ``folder(s)/service/(MapServer|FeatureServer)/<int>``
        shapes survive — no absolute URLs, no traversal.
        """
        if not dataset_id or not isinstance(dataset_id, str):
            raise ToolInputError("dataset_id is required")
        parts = [p for p in dataset_id.strip().strip("/").split("/") if p]
        if (
            len(parts) < 3
            or parts[-2] not in ("MapServer", "FeatureServer")
            or not parts[-1].isdigit()
        ):
            raise ToolInputError(
                f"Invalid dataset_id {dataset_id!r}. Expected a path like "
                f"'{_EXAMPLE_ID}' (see search_datasets)."
            )
        for segment in parts[:-2]:
            # Dots are legal inside names but a dot-only segment is traversal.
            if not _ID_SEGMENT_RE.match(segment) or segment.strip(".") == "":
                raise ToolInputError(
                    f"Invalid dataset_id segment {segment!r} in {dataset_id!r}."
                )
        return "/".join(parts)

    def _layer_url_for_item(self, item_id: str) -> str:
        """Resolve a dataset id to its full layer URL."""
        dataset_id = self._validate_dataset_id(item_id)
        return f"{self.plugin_config.services_url}/{dataset_id}"

    async def _entry_for(self, dataset_id: str) -> Dict[str, Any]:
        """Catalog entry for an id; falls back to a live metadata fetch.

        The live path covers layers published after the bundled catalog was
        crawled; results are cached per instance.
        """
        dataset_id = self._validate_dataset_id(dataset_id)
        entry = self.index.by_id.get(dataset_id) if self.index else None
        if entry is not None:
            return entry
        if dataset_id in self._live_meta_cache:
            return self._live_meta_cache[dataset_id]

        layer_url = self._layer_url_for_item(dataset_id)
        try:
            response = await self.feature_client.get(layer_url, params={"f": "json"})
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Layer metadata error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e
        meta = response.json()
        err = meta.get("error")
        if err:
            raise ToolInputError(
                f"Dataset {dataset_id!r} is not in the catalog and its layer "
                f"endpoint returned an error (code {err.get('code', 'unknown')}): "
                f"{err.get('message', 'Unknown error')}"
            )

        parts = dataset_id.split("/")
        service_path = "/".join(parts[:-2])
        folder, _, service = service_path.rpartition("/")
        entry = {
            "dataset_id": dataset_id,
            "name": meta.get("name", ""),
            "folder": folder,
            "service": service,
            "service_type": parts[-2],
            "layer_id": int(parts[-1]),
            "geometry_type": meta.get("geometryType") or "",
            "description": self._clean_text(meta.get("description", ""))[:400],
            "service_description": "",
            "max_record_count": meta.get("maxRecordCount"),
            "supports_pagination": bool(
                (meta.get("advancedQueryCapabilities") or {}).get("supportsPagination")
            ),
            "extent": None,
            "note": _LIVE_NOTE,
        }
        self._live_meta_cache[dataset_id] = entry
        return entry

    # ── DataPlugin abstract method implementations ──────────────────────

    async def search_datasets(
        self, query: str, limit: int = 10, item_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        return self.index.search(query, limit, item_type)

    async def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        entry = await self._entry_for(dataset_id)
        result = dict(entry)
        result["layer_url"] = self._layer_url_for_item(dataset_id)
        return result

    async def query_data(
        self,
        resource_id: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        records, _ = await self._fetch_records(resource_id, filters, limit)
        return records

    async def _fetch_records(
        self,
        resource_id: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 100,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """query_data plus the paging facts the structured output reports:
        pages fetched, the layer's server page size, whether the page cap
        or a non-paging layer stopped us, and whether the server flagged
        exceededTransferLimit on the last page."""
        if limit < 1:
            raise ToolInputError(f"limit must be at least 1 (got {limit})")
        layer_url = self._layer_url_for_item(resource_id)
        meta: Dict[str, Any] = {
            "pages": 0,
            "server_page_size": None,
            "page_cap_reached": False,
            "pagination_unsupported": False,
            "exceeded_transfer_limit": False,
            "live_metadata": False,
        }

        where_clause = filters.get("where", "1=1") if filters else "1=1"
        where_clause = WhereValidator.validate(where_clause)
        if where_clause != "1=1":
            # A misspelled field is a hallucination magnet for LLM callers
            # and ArcGIS answers it with an opaque 400. Check the identifiers
            # against the real schema first and answer with a did-you-mean.
            WhereValidator.validate_against_schema(
                where_clause, await self._field_names_for(resource_id)
            )
        out_fields = OutFieldsValidator.validate(
            filters.get("out_fields", "*") if filters else "*"
        )
        order_by = OrderByValidator.validate(
            (filters.get("order_by") if filters else None) or ""
        )

        # Per-layer server cap read from catalog metadata -- it varies by
        # layer, so never hardcode one page size.
        max_record_count = 1000
        supports_pagination = True
        try:
            entry = await self._entry_for(resource_id)
            max_record_count = entry.get("max_record_count") or max_record_count
            supports_pagination = entry.get("supports_pagination", True)
            meta["live_metadata"] = entry.get("note") == _LIVE_NOTE
        except Exception as meta_err:
            logger.warning(
                f"No catalog/live metadata for {resource_id}; using default "
                f"page size {max_record_count}: {meta_err}"
            )

        meta["server_page_size"] = max_record_count
        records: List[Dict[str, Any]] = []
        offset = 0
        stopped = False
        for _ in range(_MAX_QUERY_PAGES):
            want = min(max_record_count, limit - len(records))
            params = {
                "where": where_clause,
                "outFields": out_fields,
                "resultRecordCount": want,
                "inSR": 4326,
                "outSR": 4326,
                "returnGeometry": "false",
                "f": "json",
            }
            if order_by:
                params["orderByFields"] = order_by
            if offset:
                params["resultOffset"] = offset

            data = await self._query_layer(layer_url, params)
            meta["pages"] += 1
            meta["exceeded_transfer_limit"] = bool(data.get("exceededTransferLimit"))
            features = data.get("features", [])
            records.extend(f.get("attributes", {}) for f in features)

            if len(records) >= limit or len(features) < want:
                stopped = True
                break
            if not supports_pagination:
                logger.warning(
                    f"Layer {resource_id} does not support pagination; "
                    f"returning first {len(records)} records"
                )
                meta["pagination_unsupported"] = True
                stopped = True
                break
            offset += len(features)
        if not stopped:
            meta["page_cap_reached"] = True

        return records[:limit], meta

    async def _field_names_for(self, dataset_id: str) -> Optional[List[str]]:
        """Field names of a layer, cached per instance.

        Returns None when the schema cannot be read, which makes
        ``validate_against_schema`` skip the check: a metadata hiccup must
        never block a query that ArcGIS itself would have accepted.
        """
        cached = self._fields_cache.get(dataset_id)
        if cached is not None:
            return cached
        try:
            schema = await self.get_layer_schema(dataset_id)
        except Exception as schema_err:
            logger.warning(
                f"Could not read the schema for {dataset_id}; skipping the "
                f"WHERE field-name check: {schema_err}"
            )
            return None
        names = [
            f["name"]
            for f in schema.get("fields", []) or []
            if isinstance(f, dict) and f.get("name")
        ]
        self._fields_cache[dataset_id] = names
        return names

    # ── Aggregations (catalog facets, not a DataPlugin method) ──────────

    async def get_aggregations(
        self, field: str, q: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        return self.index.aggregate(field, q)

    # ── Schema / distinct values / spatial point ────────────────────────

    async def _query_layer(
        self, layer_url: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Run an ArcGIS layer /query and return parsed JSON, raising on
        HTTP errors or error objects embedded in the response body."""
        query_url = f"{layer_url}/query"
        try:
            response = await self.feature_client.get(query_url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"ArcGIS query error (HTTP {e.response.status_code}): {e.response.text}"
            ) from e
        try:
            data = response.json()
        except Exception as json_err:
            content_type = response.headers.get("content-type", "")
            raise ValueError(
                f"ArcGIS returned a non-JSON response (content-type: "
                f"{content_type}). The dataset id may not point to a "
                f"queryable layer."
            ) from json_err
        err = data.get("error")
        if err:
            code = err.get("code", "unknown")
            msg = err.get("message", "Unknown error")
            details = "; ".join(err.get("details", []) or [])
            hint = ""
            if code in (401, 403, 498, 499) or "token" in str(msg).lower():
                hint = (
                    " -- this layer requires an ArcGIS account and is not "
                    "anonymously queryable."
                )
            raise RuntimeError(
                f"ArcGIS query failed (code {code}): {msg}"
                + (f" -- {details}" if details else "")
                + hint
            )
        return data

    async def get_record_count(self, item_id: str, where: str = "1=1") -> int:
        """Total number of records matching `where` (returnCountOnly)."""
        layer_url = self._layer_url_for_item(item_id)
        where_clause = WhereValidator.validate(where)
        data = await self._query_layer(
            layer_url,
            {"where": where_clause, "returnCountOnly": "true", "f": "json"},
        )
        return int(data.get("count", 0))

    async def get_layer_schema(
        self, item_id: str, keyword: Optional[str] = None
    ) -> Dict[str, Any]:
        layer_url = self._layer_url_for_item(item_id)
        try:
            response = await self.feature_client.get(layer_url, params={"f": "json"})
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Layer metadata error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e
        meta = response.json()
        err = meta.get("error")
        if err:
            raise RuntimeError(
                f"Could not read layer schema (code {err.get('code', 'unknown')}): "
                f"{err.get('message', 'Unknown error')}"
            )
        fields = meta.get("fields", []) or []
        if keyword:
            kw = keyword.lower()
            fields = [
                f
                for f in fields
                if kw in (f.get("name", "") or "").lower()
                or kw in (f.get("alias", "") or "").lower()
            ]
        return {
            "layer_name": meta.get("name", ""),
            "geometry_type": meta.get("geometryType", ""),
            "layer_url": layer_url,
            "fields": fields,
        }

    async def get_distinct_values(
        self,
        item_id: str,
        field: str,
        like: Optional[str] = None,
        where: str = "1=1",
        limit: int = 200,
    ) -> List[Any]:
        layer_url = self._layer_url_for_item(item_id)
        # `field` lands in outFields, orderByFields AND the WHERE clause,
        # after the WHERE has already been validated -- so it must be a
        # single bare identifier, not a list and not an expression.
        if not OutFieldsValidator.is_identifier(field):
            raise ToolInputError(
                f"field must be a single field name (got {field!r}); "
                "see get_layer_schema."
            )
        field = field.strip()
        where_clause = WhereValidator.validate(where)
        if like:
            safe_like = like.replace("'", "''")
            like_clause = f"{field} LIKE '%{safe_like}%'"
            where_clause = (
                like_clause
                if where_clause in ("", "1=1")
                else f"({where_clause}) AND {like_clause}"
            )
        params = {
            "where": where_clause,
            "outFields": field,
            "returnDistinctValues": "true",
            "returnGeometry": "false",
            "orderByFields": field,
            "resultRecordCount": min(max(limit, 1), 1000),
            "inSR": 4326,
            "outSR": 4326,
            "f": "json",
        }
        data = await self._query_layer(layer_url, params)
        values = []
        for feat in data.get("features", []):
            attrs = feat.get("attributes", {})
            if field in attrs:
                values.append(attrs[field])
        return values

    async def spatial_query_point(
        self,
        item_id: str,
        lon: float,
        lat: float,
        where: str = "1=1",
        out_fields: str = "*",
        limit: int = 10,
        distance_m: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Polygons intersecting a WGS84 point. With `distance_m`, polygons
        within that many metres of the point instead (server-side buffer)."""
        if not -180 <= lon <= 180:
            raise ToolInputError(f"lon must be between -180 and 180 (got {lon})")
        if not -90 <= lat <= 90:
            raise ToolInputError(f"lat must be between -90 and 90 (got {lat})")
        layer_url = self._layer_url_for_item(item_id)
        where_clause = WhereValidator.validate(where)
        # inSR/outSR 4326 is the WGS84 contract. The layers are authored in
        # EPSG:2230 (State Plane feet); without inSR the point would be read
        # as State Plane coordinates and silently match nothing.
        params = {
            "where": where_clause,
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "outSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": OutFieldsValidator.validate(out_fields),
            "returnGeometry": "false",
            "resultRecordCount": min(max(limit, 1), 50),
            "f": "json",
        }
        if distance_m:
            params["distance"] = distance_m
            params["units"] = "esriSRUnit_Meter"
        data = await self._query_layer(layer_url, params)
        return [f.get("attributes", {}) for f in data.get("features", [])]

    async def geocode_address(self, address: str) -> List[Dict[str, Any]]:
        """Geocode a street address to WGS84 lon/lat via the US Census geocoder.

        Free and key-less. If `geocoder_region` is configured (e.g.
        'San Diego, CA') it is appended to bias results to this jurisdiction.
        Returns candidates with matched_address, lon, and lat.
        """
        if not address or not address.strip():
            raise ToolInputError("address is required")
        full = self._geocoder_query(address)

        params = {
            "address": full,
            "benchmark": "Public_AR_Current",
            "format": "json",
        }
        try:
            response = await self.feature_client.get(
                _CENSUS_GEOCODER_URL, params=params
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Geocoder error (HTTP {e.response.status_code}): {e.response.text}"
            ) from e

        matches = response.json().get("result", {}).get("addressMatches", [])
        results = []
        for m in matches:
            coords = m.get("coordinates", {})
            if coords.get("x") is not None and coords.get("y") is not None:
                results.append(
                    {
                        "matched_address": m.get("matchedAddress", ""),
                        "lon": coords["x"],
                        "lat": coords["y"],
                    }
                )
        return results

    def _geocoder_query(self, address: str) -> str:
        """The address as sent to the geocoder: region bias appended unless
        the caller already included it."""
        region = (
            self.plugin_config.geocoder_region if self.plugin_config else ""
        ) or ""
        if region and region.lower() not in address.lower():
            return f"{address}, {region}"
        return address

    # ── Health check ────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            response = await self.feature_client.get(
                self.plugin_config.services_url, params={"f": "json"}
            )
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    # ── Private helpers ─────────────────────────────────────────────────

    @staticmethod
    def _clean_text(value: Any) -> str:
        """Strip HTML and normalize to readable ASCII.

        ArcGIS descriptions can be HTML with smart quotes, em-dashes, and
        non-breaking spaces. Unescape entities, drop tags, map common unicode
        punctuation to ASCII, then transliterate/drop anything still non-ASCII
        and collapse whitespace.
        """
        if value is None:
            return ""
        text = html.unescape(str(value))
        text = _HTML_TAG_RE.sub(" ", text)
        for uni, ascii_ in _UNICODE_PUNCT.items():
            text = text.replace(uni, ascii_)
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _describe_entry(entry: Dict[str, Any]) -> str:
        """Best available one-line description for a catalog entry."""
        for key in ("note", "description", "service_description"):
            text = ArcGISPlugin._clean_text(entry.get(key, ""))
            if text:
                return text[:300] + ("..." if len(text) > 300 else "")
        return "No description"

    def _format_search_results(self, datasets: List[Dict[str, Any]]) -> str:
        if not datasets:
            return "Found 0 layer(s)."

        lines = [f"Found {len(datasets)} layer(s):\n"]
        for i, ds in enumerate(datasets, 1):
            geometry = friendly_geometry(ds.get("geometry_type", ""))
            lines.append(f"{i}. {ds.get('name', 'Untitled')}")
            lines.append(f"   ID: {ds.get('dataset_id', 'unknown')}")
            lines.append(
                f"   Type: {ds.get('service_type', '?')} layer"
                + (f" -- {geometry}" if geometry else "")
            )
            if ds.get("featured"):
                lines.append("   Featured: yes")
            lines.append(f"   Description: {self._describe_entry(ds)}")
            lines.append("")
        return "\n".join(lines)

    def _format_dataset(self, dataset: Dict[str, Any]) -> str:
        geometry = friendly_geometry(dataset.get("geometry_type", ""))
        lines = [
            f"Layer: {dataset.get('name', 'Untitled')}",
            f"ID: {dataset.get('dataset_id', 'unknown')}",
            f"Folder: {dataset.get('folder') or '(root)'}",
            f"Service: {dataset.get('service', '')} ({dataset.get('service_type', '')})",
            f"Geometry Type: {geometry or 'none (table)'}",
            f"Max Record Count (per request): {dataset.get('max_record_count', 'N/A')}",
            f"Supports Pagination: {dataset.get('supports_pagination', 'unknown')}",
            f"Description: {self._describe_entry(dataset)}",
        ]
        if dataset.get("note") and dataset.get("description"):
            lines.append(
                f"Layer Description: {self._clean_text(dataset['description'])}"
            )
        extent = dataset.get("extent")
        if extent:
            lines.append(
                f"Extent (wkid {extent.get('wkid', '?')}): "
                f"[{extent.get('xmin')}, {extent.get('ymin')}] - "
                f"[{extent.get('xmax')}, {extent.get('ymax')}]"
            )
        lines.append(f"Layer URL: {dataset.get('layer_url', '')}")
        lines.append(
            "All queries take and return WGS84 lon/lat "
            "(inSR/outSR=4326 is applied automatically)."
        )
        return "\n".join(lines)

    def _format_query_results(
        self, records: List[Dict[str, Any]], limit: int, total: Optional[int] = None
    ) -> str:
        if not records:
            if total is not None:
                return (
                    f"TOTAL MATCHING: {total}\nReturned 0 record(s) (limit: {limit})."
                )
            return f"Returned 0 record(s) (limit: {limit})."

        lines = []
        if total is not None:
            lines.append(f"TOTAL MATCHING: {total}")
        lines.append(f"Returned {len(records)} record(s) (limit: {limit}):")
        lines.append("")

        for i, record in enumerate(records, 1):
            lines.append(f"Record {i}:")
            for key, value in record.items():
                clean = self._clean_text(value) if isinstance(value, str) else value
                lines.append(f"  {key}: {clean}")
            lines.append("")

        return "\n".join(lines)

    def _format_aggregations(self, field: str, buckets: List[Dict[str, Any]]) -> str:
        if not buckets:
            return f"Aggregations for '{field}': 0 bucket(s)."

        lines = [f"Aggregations for '{field}':\n"]
        for bucket in buckets:
            lines.append(
                f"  {bucket.get('key', 'unknown')}: "
                f"{bucket.get('doc_count', bucket.get('count', 0))} layer(s)"
            )
        return "\n".join(lines)

    def _format_layer_schema(self, schema: Dict[str, Any]) -> str:
        fields = schema.get("fields", [])
        if not fields:
            return f"Layer: {schema.get('layer_name', '')}\nFields (0)."

        lines = [
            f"Layer: {schema.get('layer_name', '')}",
            f"Geometry: {schema.get('geometry_type', '') or 'none (table)'}",
            f"Fields ({len(fields)}):",
            "",
        ]
        for f in fields:
            name = f.get("name", "")
            ftype = (f.get("type", "") or "").replace("esriFieldType", "")
            alias = f.get("alias", "")
            line = f"  {name} ({ftype})"
            if alias and alias != name:
                line += f" -- {alias}"
            lines.append(line)
            domain = f.get("domain") or {}
            coded = domain.get("codedValues") if isinstance(domain, dict) else None
            if coded:
                sample = ", ".join(
                    f"{c.get('code')}={c.get('name')}" for c in coded[:8]
                )
                more = " ..." if len(coded) > 8 else ""
                lines.append(f"      coded values: {sample}{more}")
        return "\n".join(lines)

    def _format_distinct_values(self, field: str, values: List[Any]) -> str:
        if not values:
            return f"0 distinct value(s) for '{field}'."

        lines = [f"{len(values)} distinct value(s) for '{field}':", ""]
        for v in values:
            lines.append(f"  {v}")
        return "\n".join(lines)

    def _format_geocode(self, address: str, candidates: List[Dict[str, Any]]) -> str:
        if not candidates:
            return f"0 match(es) for '{address}'."
        lines = [f"{len(candidates)} match(es) for '{address}':", ""]
        for c in candidates:
            lines.append(f"  {c.get('matched_address', '')}")
            lines.append(f"    lon: {c.get('lon')}, lat: {c.get('lat')}")
        return "\n".join(lines)
