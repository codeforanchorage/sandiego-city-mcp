"""Structured output (outputSchema + structuredContent) for the arcgis plugin.

A declared outputSchema is BINDING: the spec says servers MUST return
conforming results. So every code path of every tool -- the empty,
truncated, clamped, count-failed, live-metadata and geocoded branches,
not just the happy path -- is exercised here and validated against the
schema the server itself advertises. The caveat/prose parity rule is
asserted on every one of them.
"""

import json
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from core.plugin_manager import PluginManager
from plugins.arcgis.plugin import CAVEAT_CODES
from tests.test_arcgis_plugin import (
    FEATURED,
    MHPA_ID,
    SERVICES_URL,
    ZONES_ID,
    make_manifest,
    make_plugin,
    mock_response,
)


@pytest.fixture
def arcgis_config(tmp_path):
    """Same wiring as the base plugin tests: real index over a temp catalog."""
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(make_manifest()), encoding="utf-8")
    return {
        "services_url": SERVICES_URL,
        "city_name": "TestCity",
        "timeout": 20,
        "geocoder_region": "San Diego, CA",
        "catalog_path": str(path),
        "featured_datasets": FEATURED,
    }


# ── helpers ────────────────────────────────────────────────────────────


def page(records, exceeded=False):
    payload = {"features": [{"attributes": r} for r in records]}
    if exceeded:
        payload["exceededTransferLimit"] = True
    return mock_response(payload)


def count(n):
    return mock_response({"count": n})


def geocode(*matches):
    return mock_response(
        {
            "result": {
                "addressMatches": [
                    {
                        "matchedAddress": addr,
                        "coordinates": {"x": lon, "y": lat},
                    }
                    for addr, lon, lat in matches
                ]
            }
        }
    )


LAYER_META = {
    "name": "Base Zones",
    "geometryType": "esriGeometryPolygon",
    "maxRecordCount": 1000,
    "advancedQueryCapabilities": {"supportsPagination": True},
    "fields": [
        {"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID"},
        {"name": "ZONE_NAME", "type": "esriFieldTypeString", "alias": "Zone"},
        {
            "name": "STATUS",
            "type": "esriFieldTypeSmallInteger",
            "alias": "Status",
            "domain": {"codedValues": [{"code": 1, "name": "Active"}]},
        },
    ],
}


def schema_for(plugin, tool_name):
    return next(t.output_schema for t in plugin.get_tools() if t.name == tool_name)


def assert_conforms(plugin, tool_name, result):
    """Validate structuredContent against the advertised schema and assert
    that every structured caveat appears verbatim in the prose."""
    assert result.success, result.error_message
    structured = result.structured_content
    assert structured is not None, f"{tool_name} returned no structured_content"
    Draft202012Validator(schema_for(plugin, tool_name)).validate(structured)
    text = result.content[0]["text"]
    for caveat in structured["caveats"]:
        assert caveat["message"] in text, (
            f"{tool_name}: caveat {caveat['code']} is in structuredContent "
            f"but not in the rendered text"
        )
    return structured


def codes(structured):
    return [c["code"] for c in structured["caveats"]]


# ── schema declarations ────────────────────────────────────────────────


class TestOutputSchemaDeclarations:
    PAYLOAD_KEYS = {
        "search_datasets": "layers",
        "get_dataset": "layer",
        "get_aggregations": "buckets",
        "query_data": "rows",
        "get_layer_schema": "fields",
        "get_distinct_values": "values",
        "spatial_query_point": "rows",
        "geocode_address": "candidates",
    }

    def test_every_tool_declares_a_valid_output_schema(self, arcgis_config):
        for t in make_plugin(arcgis_config).get_tools():
            assert t.output_schema, f"{t.name} has no output_schema"
            Draft202012Validator.check_schema(t.output_schema)

    def test_output_schema_is_advertised_on_the_wire(self, arcgis_config):
        manager = PluginManager({})
        manager.plugins = {"arcgis": make_plugin(arcgis_config)}
        for tool in manager.get_all_tools():
            assert "outputSchema" in tool, tool["name"]

    def test_every_schema_uses_the_shared_envelope(self, arcgis_config):
        """One shape across the server, so a model learns it once: the
        envelope keys plus exactly one payload key named for its contents."""
        for t in make_plugin(arcgis_config).get_tools():
            required = set(t.output_schema["required"])
            assert required == {
                "query",
                "summary",
                "caveats",
                self.PAYLOAD_KEYS[t.name],
            }

    def test_caveat_enum_matches_the_code_constant(self, arcgis_config):
        for t in make_plugin(arcgis_config).get_tools():
            enum = t.output_schema["properties"]["caveats"]["items"]["properties"][
                "code"
            ]["enum"]
            assert enum == list(CAVEAT_CODES), t.name

    @pytest.mark.asyncio
    async def test_caller_errors_carry_no_structured_content(self, arcgis_config):
        """An isError result has no structuredContent to conform; the
        contract applies to successful results only."""
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool("get_dataset", {})
        assert result.success is False
        assert result.structured_content is None
        assert "dataset_id is required" in result.error_message


# ── conformance per tool, per branch ───────────────────────────────────


class TestSearchDatasets:
    @pytest.mark.asyncio
    async def test_hit(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool("search_datasets", {"q": "MHPA"})
        s = assert_conforms(plugin, "search_datasets", result)
        assert s["summary"]["returned"] == len(s["layers"]) >= 1
        assert s["layers"][0]["dataset_id"] == MHPA_ID
        assert s["layers"][0]["featured"] is True
        assert s["summary"]["catalog_layers"] == len(plugin.index.entries)
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_empty(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool("search_datasets", {"q": "qwzxjv"})
        s = assert_conforms(plugin, "search_datasets", result)
        assert s["layers"] == []
        assert codes(s) == ["no_results"]

    @pytest.mark.asyncio
    async def test_limit_clamped(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool(
            "search_datasets", {"q": "zon", "limit": 500}
        )
        s = assert_conforms(plugin, "search_datasets", result)
        assert s["query"]["limit"] == 100
        assert "limit_clamped" in codes(s)


class TestGetDataset:
    @pytest.mark.asyncio
    async def test_in_catalog(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool("get_dataset", {"dataset_id": MHPA_ID})
        s = assert_conforms(plugin, "get_dataset", result)
        assert s["summary"]["in_catalog"] is True
        assert s["layer"]["layer_url"].endswith(MHPA_ID)
        assert s["layer"]["extent"]["wkid"] == 2230
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_live_metadata(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(return_value=mock_response(LAYER_META))
        result = await plugin.execute_tool(
            "get_dataset", {"dataset_id": "New/Service/FeatureServer/3"}
        )
        s = assert_conforms(plugin, "get_dataset", result)
        assert s["summary"]["in_catalog"] is False
        assert s["layer"]["extent"] is None
        assert codes(s) == ["live_metadata"]


class TestGetAggregations:
    @pytest.mark.asyncio
    async def test_folder(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool("get_aggregations", {"field": "folder"})
        s = assert_conforms(plugin, "get_aggregations", result)
        assert s["summary"]["bucket_count"] == len(s["buckets"]) >= 1
        assert s["summary"]["layers_counted"] == len(plugin.index.entries)

    @pytest.mark.asyncio
    async def test_empty_scope(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool(
            "get_aggregations", {"field": "service", "q": "qwzxjv"}
        )
        s = assert_conforms(plugin, "get_aggregations", result)
        assert s["buckets"] == []
        assert codes(s) == ["no_results"]

    @pytest.mark.asyncio
    async def test_unknown_field_is_a_caller_error(self, arcgis_config, caplog):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool("get_aggregations", {"field": "colour"})
        assert result.success is False
        assert result.structured_content is None
        assert "not an aggregatable field" in result.error_message
        assert not [r for r in caplog.records if r.levelno >= 40]


class TestQueryData:
    @pytest.mark.asyncio
    async def test_happy_path_with_count(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            side_effect=[page([{"ZONE_NAME": "RS-1-7"}]), count(1)]
        )
        result = await plugin.execute_tool("query_data", {"dataset_id": ZONES_ID})
        s = assert_conforms(plugin, "query_data", result)
        assert s["rows"] == [{"ZONE_NAME": "RS-1-7"}]
        assert s["summary"]["total_matching"] == 1
        assert s["summary"]["truncated"] is False
        assert s["summary"]["pages_fetched"] == 1
        assert s["summary"]["server_page_size"] == 1000
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_truncated_by_limit(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            side_effect=[page([{"A": 1}, {"A": 2}]), count(626)]
        )
        result = await plugin.execute_tool(
            "query_data", {"dataset_id": ZONES_ID, "limit": 2}
        )
        s = assert_conforms(plugin, "query_data", result)
        assert s["summary"]["returned"] == 2
        assert s["summary"]["total_matching"] == 626
        assert s["summary"]["truncated"] is True
        assert "results_truncated" in codes(s)
        assert "of 626" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_count_failure_is_null_not_zero(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            side_effect=[page([{"A": 1}]), RuntimeError("count exploded")]
        )
        result = await plugin.execute_tool("query_data", {"dataset_id": ZONES_ID})
        s = assert_conforms(plugin, "query_data", result)
        assert s["summary"]["total_matching"] is None
        assert s["summary"]["truncated"] is False
        assert codes(s) == ["count_unavailable"]

    @pytest.mark.asyncio
    async def test_exceeded_transfer_limit_without_count(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            side_effect=[
                page([{"A": 1}], exceeded=True),
                RuntimeError("count exploded"),
            ]
        )
        result = await plugin.execute_tool(
            "query_data", {"dataset_id": ZONES_ID, "limit": 1}
        )
        s = assert_conforms(plugin, "query_data", result)
        assert s["summary"]["total_matching"] is None
        assert s["summary"]["truncated"] is True
        assert set(codes(s)) == {"count_unavailable", "results_truncated"}

    @pytest.mark.asyncio
    async def test_empty(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(side_effect=[page([]), count(0)])
        result = await plugin.execute_tool(
            "query_data", {"dataset_id": ZONES_ID, "where": "ZONE_NAME = 'nope'"}
        )
        s = assert_conforms(plugin, "query_data", result)
        assert s["rows"] == []
        assert s["summary"]["total_matching"] == 0
        assert codes(s) == ["no_results"]
        assert "TOTAL MATCHING: 0" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_page_cap_reached(self, arcgis_config):
        """MHPA's catalog page size is 2; 20 full pages hit the hard cap."""
        plugin = make_plugin(arcgis_config)
        pages = [page([{"A": i}, {"A": i + 1}]) for i in range(0, 40, 2)]
        plugin.feature_client.get = AsyncMock(side_effect=pages + [count(1000)])
        result = await plugin.execute_tool(
            "query_data", {"dataset_id": MHPA_ID, "limit": 100}
        )
        s = assert_conforms(plugin, "query_data", result)
        assert s["summary"]["returned"] == 40
        assert s["summary"]["pages_fetched"] == 20
        assert s["summary"]["server_page_size"] == 2
        assert {"page_cap_reached", "results_truncated"} <= set(codes(s))

    @pytest.mark.asyncio
    async def test_pagination_unsupported(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.index.by_id[MHPA_ID]["supports_pagination"] = False
        plugin.feature_client.get = AsyncMock(
            side_effect=[page([{"A": 1}, {"A": 2}]), count(50)]
        )
        result = await plugin.execute_tool(
            "query_data", {"dataset_id": MHPA_ID, "limit": 10}
        )
        s = assert_conforms(plugin, "query_data", result)
        assert s["summary"]["returned"] == 2
        assert "pagination_unsupported" in codes(s)

    @pytest.mark.asyncio
    async def test_limit_clamped(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(side_effect=[page([{"A": 1}]), count(1)])
        result = await plugin.execute_tool(
            "query_data", {"dataset_id": ZONES_ID, "limit": 5000}
        )
        s = assert_conforms(plugin, "query_data", result)
        assert s["query"]["limit"] == 1000
        assert "limit_clamped" in codes(s)

    @pytest.mark.asyncio
    async def test_live_metadata_layer(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            side_effect=[mock_response(LAYER_META), page([{"A": 1}]), count(1)]
        )
        result = await plugin.execute_tool(
            "query_data", {"dataset_id": "New/Service/FeatureServer/3"}
        )
        s = assert_conforms(plugin, "query_data", result)
        assert codes(s) == ["live_metadata"]


class TestGetLayerSchema:
    @pytest.mark.asyncio
    async def test_fields(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(return_value=mock_response(LAYER_META))
        result = await plugin.execute_tool("get_layer_schema", {"item_id": ZONES_ID})
        s = assert_conforms(plugin, "get_layer_schema", result)
        assert s["summary"]["field_count"] == 3
        assert s["summary"]["filtered"] is False
        assert s["fields"][2]["domain"]["codedValues"][0]["name"] == "Active"
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_keyword_no_match(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(return_value=mock_response(LAYER_META))
        result = await plugin.execute_tool(
            "get_layer_schema", {"item_id": ZONES_ID, "keyword": "acreage"}
        )
        s = assert_conforms(plugin, "get_layer_schema", result)
        assert s["fields"] == []
        assert s["summary"]["filtered"] is True
        assert codes(s) == ["no_results"]


class TestGetDistinctValues:
    @pytest.mark.asyncio
    async def test_values(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=page([{"INHABPRES": "Yes"}, {"INHABPRES": None}])
        )
        result = await plugin.execute_tool(
            "get_distinct_values", {"item_id": MHPA_ID, "field": "INHABPRES"}
        )
        s = assert_conforms(plugin, "get_distinct_values", result)
        assert s["values"] == ["Yes", None]
        assert s["summary"]["truncated"] is False
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_truncated_at_limit(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=page([{"Z": "A"}, {"Z": "B"}])
        )
        result = await plugin.execute_tool(
            "get_distinct_values", {"item_id": MHPA_ID, "field": "Z", "limit": 2}
        )
        s = assert_conforms(plugin, "get_distinct_values", result)
        assert s["summary"]["truncated"] is True
        assert codes(s) == ["results_truncated"]

    @pytest.mark.asyncio
    async def test_empty(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(return_value=page([]))
        result = await plugin.execute_tool(
            "get_distinct_values", {"item_id": MHPA_ID, "field": "Z", "like": "q"}
        )
        s = assert_conforms(plugin, "get_distinct_values", result)
        assert s["values"] == []
        assert s["query"]["like"] == "q"
        assert codes(s) == ["no_results"]


class TestSpatialQueryPoint:
    @pytest.mark.asyncio
    async def test_by_coordinates(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=page([{"HABPRES": 100, "SUBAREA": 113}])
        )
        result = await plugin.execute_tool(
            "spatial_query_point",
            {"item_id": MHPA_ID, "lon": -117.0846, "lat": 32.5539},
        )
        s = assert_conforms(plugin, "spatial_query_point", result)
        assert s["rows"][0]["HABPRES"] == 100
        assert s["summary"]["geocoded"] is False
        assert s["query"]["matched_address"] is None
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_by_address_with_multiple_matches(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            side_effect=[
                geocode(
                    ("202 C ST, SAN DIEGO, CA, 92101", -117.163, 32.717),
                    ("202 C ST, CHULA VISTA, CA, 91910", -117.08, 32.64),
                ),
                page([{"ZONE_NAME": "CCPD-CORE"}]),
            ]
        )
        result = await plugin.execute_tool(
            "spatial_query_point", {"item_id": ZONES_ID, "address": "202 C St"}
        )
        s = assert_conforms(plugin, "spatial_query_point", result)
        assert s["summary"]["geocoded"] is True
        assert s["query"]["matched_address"].startswith("202 C ST, SAN DIEGO")
        assert s["query"]["lon"] == -117.163
        assert codes(s) == ["geocoded", "multiple_geocode_matches"]
        assert "Geocoded '202 C St'" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_address_not_found_is_a_caller_error(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(return_value=geocode())
        result = await plugin.execute_tool(
            "spatial_query_point", {"item_id": ZONES_ID, "address": "nowhere"}
        )
        assert result.success is False
        assert result.structured_content is None
        assert "Could not geocode" in result.error_message

    @pytest.mark.asyncio
    async def test_outside_all_polygons(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(return_value=page([]))
        result = await plugin.execute_tool(
            "spatial_query_point", {"item_id": MHPA_ID, "lon": -117.0, "lat": 33.0}
        )
        s = assert_conforms(plugin, "spatial_query_point", result)
        assert s["rows"] == []
        assert codes(s) == ["no_results"]

    @pytest.mark.asyncio
    async def test_limit_clamped_and_truncated(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=page([{"A": i} for i in range(50)])
        )
        result = await plugin.execute_tool(
            "spatial_query_point",
            {"item_id": MHPA_ID, "lon": -117.0, "lat": 33.0, "limit": 500},
        )
        s = assert_conforms(plugin, "spatial_query_point", result)
        assert s["query"]["limit"] == 50
        assert set(codes(s)) == {"limit_clamped", "results_truncated"}

    @pytest.mark.asyncio
    async def test_neither_address_nor_coords(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool("spatial_query_point", {"item_id": MHPA_ID})
        assert result.success is False
        assert "either `address` or both" in result.error_message


class TestGeocodeAddress:
    @pytest.mark.asyncio
    async def test_candidates(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=geocode(("202 C ST, SAN DIEGO, CA, 92101", -117.163, 32.717))
        )
        result = await plugin.execute_tool("geocode_address", {"address": "202 C St"})
        s = assert_conforms(plugin, "geocode_address", result)
        assert s["candidates"][0]["lon"] == -117.163
        assert s["query"]["geocoder_query"] == "202 C St, San Diego, CA"
        assert codes(s) == []

    @pytest.mark.asyncio
    async def test_no_match(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(return_value=geocode())
        result = await plugin.execute_tool("geocode_address", {"address": "nowhere"})
        s = assert_conforms(plugin, "geocode_address", result)
        assert s["candidates"] == []
        assert codes(s) == ["no_results"]


class TestWire:
    @pytest.mark.asyncio
    async def test_structured_content_reaches_tools_call(self, arcgis_config):
        """The MCP layer must surface structured_content as
        structuredContent alongside the prose block."""
        from core.mcp_server import MCPServer

        plugin = make_plugin(arcgis_config)
        manager = PluginManager({})
        manager.plugins = {"arcgis": plugin}
        manager.tools = {"arcgis__search_datasets": ("arcgis", "search_datasets")}
        manager._initialized = True
        server = MCPServer(manager)
        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "arcgis__search_datasets",
                    "arguments": {"q": "MHPA"},
                },
            }
        )
        result = response["result"]
        assert result["content"][0]["type"] == "text"
        Draft202012Validator(schema_for(plugin, "search_datasets")).validate(
            result["structuredContent"]
        )
