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
from typing import Any, Dict, List, Optional

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


class ArcGISPlugin(DataPlugin):
    """Plugin for a bare ArcGIS Server REST services directory.

    Implements the DataPlugin interface with the same tool names and
    signatures as the sibling Hub-based forks, so it composes with them
    at the MCP client with zero new orchestration.
    """

    plugin_name = "arcgis"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "2.0.0"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[ArcGISPluginConfig] = None
        self.feature_client: Optional[httpx.AsyncClient] = None
        self.index: Optional[CatalogIndex] = None
        # Layers queried by id but absent from the bundled catalog (e.g.
        # published after the last crawl) get their metadata fetched live
        # once and cached here for the life of the instance.
        self._live_meta_cache: Dict[str, Dict[str, Any]] = {}

    async def initialize(self) -> bool:
        try:
            self.plugin_config = ArcGISPluginConfig(**self.config)

            manifest = load_manifest(self.plugin_config.catalog_path)
            featured = [f.model_dump() for f in self.plugin_config.featured_datasets]
            self.index = CatalogIndex(manifest, featured=featured)

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
                description=(
                    "Point-in-polygon lookup: return the attributes of every "
                    "polygon in a layer that contains a point -- 'which zone / "
                    "community plan area / preserve is at this location?'. "
                    "Provide EITHER a street `address` (geocoded "
                    "automatically) OR both `lon` and `lat` (WGS84 decimal "
                    "degrees -- the plugin handles the State Plane "
                    "conversion server-side). Use on polygon layers (check "
                    "geometry with get_layer_schema). Returns attributes "
                    "only, no geometry."
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

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        try:
            if tool_name == "search_datasets":
                q = arguments.get("q", "")
                limit = self._int_arg(arguments, "limit", 10)
                item_type = arguments.get("type")
                datasets = await self.search_datasets(q, limit, item_type)
                return ToolResult(
                    content=[
                        {"type": "text", "text": self._format_search_results(datasets)}
                    ],
                    success=True,
                )

            elif tool_name == "get_dataset":
                dataset_id = arguments.get("dataset_id")
                if not dataset_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="dataset_id is required",
                    )
                dataset = await self.get_dataset(dataset_id)
                return ToolResult(
                    content=[{"type": "text", "text": self._format_dataset(dataset)}],
                    success=True,
                )

            elif tool_name == "get_aggregations":
                field = arguments.get("field")
                if not field:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="field is required",
                    )
                q = arguments.get("q")
                buckets = await self.get_aggregations(field, q)
                return ToolResult(
                    content=[
                        {
                            "type": "text",
                            "text": self._format_aggregations(field, buckets),
                        }
                    ],
                    success=True,
                )

            elif tool_name == "query_data":
                dataset_id = arguments.get("dataset_id")
                if not dataset_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="dataset_id is required",
                    )
                where = arguments.get("where", "1=1")
                out_fields = arguments.get("out_fields", "*")
                limit = self._int_arg(arguments, "limit", 100)
                filters = {"where": where, "out_fields": out_fields}
                if arguments.get("order_by"):
                    filters["order_by"] = arguments["order_by"]
                records = await self.query_data(dataset_id, filters, limit)
                # Total match count is best-effort: a count failure must not
                # hide the records we already fetched.
                try:
                    total = await self.get_record_count(dataset_id, where)
                except Exception as count_err:
                    logger.warning(f"Could not get record count: {count_err}")
                    total = None
                return ToolResult(
                    content=[
                        {
                            "type": "text",
                            "text": self._format_query_results(
                                records, limit, total=total
                            ),
                        }
                    ],
                    success=True,
                )

            elif tool_name == "get_layer_schema":
                item_id = arguments.get("item_id")
                if not item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id is required",
                    )
                schema = await self.get_layer_schema(item_id, arguments.get("keyword"))
                return ToolResult(
                    content=[
                        {"type": "text", "text": self._format_layer_schema(schema)}
                    ],
                    success=True,
                )

            elif tool_name == "get_distinct_values":
                item_id = arguments.get("item_id")
                field = arguments.get("field")
                if not item_id or not field:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id and field are required",
                    )
                values = await self.get_distinct_values(
                    item_id,
                    field,
                    arguments.get("like"),
                    arguments.get("where", "1=1"),
                    self._int_arg(arguments, "limit", 200),
                )
                return ToolResult(
                    content=[
                        {
                            "type": "text",
                            "text": self._format_distinct_values(field, values),
                        }
                    ],
                    success=True,
                )

            elif tool_name == "spatial_query_point":
                item_id = arguments.get("item_id")
                lon = self._float_arg(arguments, "lon")
                lat = self._float_arg(arguments, "lat")
                address = arguments.get("address")
                if not item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id is required",
                    )
                geocoded_note = ""
                if (lon is None or lat is None) and address:
                    candidates = await self.geocode_address(address)
                    if not candidates:
                        return ToolResult(
                            content=[],
                            success=False,
                            error_message=f"Could not geocode address: {address}",
                        )
                    lon = candidates[0]["lon"]
                    lat = candidates[0]["lat"]
                    geocoded_note = (
                        f"Geocoded '{address}' -> {candidates[0]['matched_address']} "
                        f"({lat}, {lon})\n\n"
                    )
                if lon is None or lat is None:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="Provide either `address` or both `lon` and `lat`.",
                    )
                limit = self._int_arg(arguments, "limit", 10)
                records = await self.spatial_query_point(
                    item_id,
                    lon,
                    lat,
                    arguments.get("where", "1=1"),
                    arguments.get("out_fields", "*"),
                    limit,
                )
                return ToolResult(
                    content=[
                        {
                            "type": "text",
                            "text": geocoded_note
                            + self._format_query_results(records, limit),
                        }
                    ],
                    success=True,
                )

            elif tool_name == "geocode_address":
                address = arguments.get("address")
                if not address:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="address is required",
                    )
                candidates = await self.geocode_address(address)
                return ToolResult(
                    content=[
                        {
                            "type": "text",
                            "text": self._format_geocode(address, candidates),
                        }
                    ],
                    success=True,
                )

            else:
                return ToolResult(
                    content=[],
                    success=False,
                    error_message=f"Unknown tool: {tool_name}",
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
            "note": "Not in the bundled catalog (fetched live).",
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
        if limit < 1:
            raise ToolInputError(f"limit must be at least 1 (got {limit})")
        layer_url = self._layer_url_for_item(resource_id)

        where_clause = filters.get("where", "1=1") if filters else "1=1"
        where_clause = WhereValidator.validate(where_clause)
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
        except Exception as meta_err:
            logger.warning(
                f"No catalog/live metadata for {resource_id}; using default "
                f"page size {max_record_count}: {meta_err}"
            )

        records: List[Dict[str, Any]] = []
        offset = 0
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
            features = data.get("features", [])
            records.extend(f.get("attributes", {}) for f in features)

            if len(records) >= limit or len(features) < want:
                break
            if not supports_pagination:
                logger.warning(
                    f"Layer {resource_id} does not support pagination; "
                    f"returning first {len(records)} records"
                )
                break
            offset += len(features)

        return records[:limit]

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
    ) -> List[Dict[str, Any]]:
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
        region = (
            self.plugin_config.geocoder_region if self.plugin_config else ""
        ) or ""
        full = address
        if region and region.lower() not in address.lower():
            full = f"{address}, {region}"

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
            return (
                "No layers found. Try a broader keyword, or explore with "
                "get_aggregations (field='folder' or 'service')."
            )

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
                return f"TOTAL MATCHING: {total}\nNo records on this page."
            return "No records returned."

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
            return (
                f"No aggregation results for '{field}'. Available fields: "
                f"{', '.join(AGGREGATABLE_FIELDS)}."
            )

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
            return "No fields found for this layer (or none matched the keyword)."

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
            return f"No distinct values found for '{field}'."

        lines = [f"{len(values)} distinct value(s) for '{field}':", ""]
        for v in values:
            lines.append(f"  {v}")
        return "\n".join(lines)

    def _format_geocode(self, address: str, candidates: List[Dict[str, Any]]) -> str:
        if not candidates:
            return (
                f"No geocode match for '{address}'. Try including the city and "
                f"state, e.g. '{address}, San Diego, CA'."
            )
        lines = [f"{len(candidates)} match(es) for '{address}':", ""]
        for c in candidates:
            lines.append(f"  {c.get('matched_address', '')}")
            lines.append(f"    lon: {c.get('lon')}, lat: {c.get('lat')}")
        return "\n".join(lines)
