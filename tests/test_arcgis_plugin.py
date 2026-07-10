"""Comprehensive tests for the ArcGIS Server directory plugin.

These tests verify catalog loading, search/aggregation over the bundled
manifest, dataset-id validation, WGS84 query parameters, pagination, error
handling, and data formatting. No network access: the catalog is a temp
file and HTTP is mocked.
"""

import json

import pytest
from unittest.mock import AsyncMock, Mock

import httpx
from pydantic import ValidationError

from core.interfaces import PluginType
from plugins.arcgis.catalog_index import CatalogIndex, load_manifest
from plugins.arcgis.config_schema import ArcGISPluginConfig
from plugins.arcgis.plugin import ArcGISPlugin
from plugins.arcgis.where_validator import (
    OrderByValidator,
    OutFieldsValidator,
    WhereValidator,
)


SERVICES_URL = "https://gis.example.gov/arcgis/rest/services"

MHPA_ID = "Planning/PLN_LongRangePlanning/MapServer/7"
ZONES_ID = "Planning/PLN_LongRangePlanning/MapServer/27"


def make_manifest():
    return {
        "version": 1,
        "generated_at": "2026-07-10T00:00:00+00:00",
        "services_url": SERVICES_URL,
        "stats": {"layers": 5, "services_skipped": 1},
        "skipped": [{"service": "AMPGIS", "reason": "auth required: code 499"}],
        "layers": [
            {
                "dataset_id": MHPA_ID,
                "name": "Multi-Habitat Planning Area",
                "folder": "Planning",
                "service": "PLN_LongRangePlanning",
                "service_type": "MapServer",
                "layer_id": 7,
                "geometry_type": "esriGeometryPolygon",
                "description": "",
                "service_description": "Long range planning layers",
                "max_record_count": 2,
                "supports_pagination": True,
                "extent": {
                    "xmin": 6150000,
                    "ymin": 1770000,
                    "xmax": 6390000,
                    "ymax": 2090000,
                    "wkid": 2230,
                },
            },
            {
                "dataset_id": ZONES_ID,
                "name": "Base Zones",
                "folder": "Planning",
                "service": "PLN_LongRangePlanning",
                "service_type": "MapServer",
                "layer_id": 27,
                "geometry_type": "esriGeometryPolygon",
                "description": "Adopted zoning",
                "service_description": "",
                "max_record_count": 1000,
                "supports_pagination": True,
                "extent": None,
            },
            {
                "dataset_id": "DSD/Zoning_Base/MapServer/0",
                "name": "Official Zoning Map",
                "folder": "DSD",
                "service": "Zoning_Base",
                "service_type": "MapServer",
                "layer_id": 0,
                "geometry_type": "esriGeometryPolygon",
                "description": "Base zoning map service",
                "service_description": "",
                "max_record_count": 1000,
                "supports_pagination": True,
                "extent": None,
            },
            {
                "dataset_id": "Hosted/Street_Trees/FeatureServer/0",
                "name": "Street Trees",
                "folder": "Hosted",
                "service": "Street_Trees",
                "service_type": "FeatureServer",
                "layer_id": 0,
                "geometry_type": "esriGeometryPoint",
                "description": "Tree inventory",
                "service_description": "",
                "max_record_count": 2000,
                "supports_pagination": True,
                "extent": None,
            },
            {
                "dataset_id": "TSD/Records/MapServer/3",
                "name": "Permit Records",
                "folder": "TSD",
                "service": "Records",
                "service_type": "MapServer",
                "layer_id": 3,
                "geometry_type": "",
                "description": "Tabular permit records",
                "service_description": "",
                "max_record_count": 1000,
                "supports_pagination": False,
                "extent": None,
            },
        ],
    }


FEATURED = [
    {
        "dataset_id": MHPA_ID,
        "aliases": ["MHPA", "habitat preserve"],
        "note": "Multi-Habitat Planning Area (MHPA). Key fields: HABPRES, ACRES.",
    },
    {
        "dataset_id": ZONES_ID,
        "aliases": ["zoning"],
        "note": "Base Zones -- the official City zoning layer.",
    },
]


@pytest.fixture
def catalog_file(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(make_manifest()), encoding="utf-8")
    return path


@pytest.fixture
def arcgis_config(catalog_file):
    """Standard plugin configuration pointing at the temp catalog."""
    return {
        "services_url": SERVICES_URL,
        "city_name": "TestCity",
        "timeout": 120,
        "catalog_path": str(catalog_file),
        "featured_datasets": FEATURED,
    }


def make_plugin(arcgis_config):
    """A plugin wired up without network: real index, mocked HTTP client."""
    plugin = ArcGISPlugin(arcgis_config)
    plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
    plugin.index = CatalogIndex(make_manifest(), featured=FEATURED)
    plugin.feature_client = AsyncMock()
    return plugin


def mock_response(payload, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.raise_for_status = Mock()
    response.json = Mock(return_value=payload)
    response.headers = {"content-type": "application/json"}
    return response


# ── Plugin attributes ──────────────────────────────────────────────────


class TestPluginAttributes:
    def test_plugin_attributes(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        assert plugin.plugin_name == "arcgis"
        assert plugin.plugin_type == PluginType.OPEN_DATA


# ── Config schema ──────────────────────────────────────────────────────


class TestConfigSchema:
    def test_requires_services_url(self):
        with pytest.raises(ValidationError):
            ArcGISPluginConfig(city_name="X")

    def test_rejects_bad_scheme(self):
        with pytest.raises(ValidationError):
            ArcGISPluginConfig(services_url="ftp://example.gov/arcgis", city_name="X")

    def test_strips_trailing_slash(self):
        config = ArcGISPluginConfig(services_url=SERVICES_URL + "/", city_name="X")
        assert config.services_url == SERVICES_URL

    def test_rejects_unknown_keys(self):
        with pytest.raises(ValidationError):
            ArcGISPluginConfig(
                services_url=SERVICES_URL, city_name="X", portal_url="nope"
            )


# ── Initialization ─────────────────────────────────────────────────────


class TestInitialization:
    @pytest.mark.asyncio
    async def test_initialize_success(self, arcgis_config):
        plugin = ArcGISPlugin(arcgis_config)
        result = await plugin.initialize()
        assert result is True
        assert plugin._initialized is True
        assert len(plugin.index.entries) == 5
        await plugin.shutdown()

    @pytest.mark.asyncio
    async def test_initialize_missing_catalog(self, arcgis_config, tmp_path):
        arcgis_config["catalog_path"] = str(tmp_path / "nope.json")
        plugin = ArcGISPlugin(arcgis_config)
        result = await plugin.initialize()
        assert result is False
        assert plugin._initialized is False

    @pytest.mark.asyncio
    async def test_initialize_invalid_config(self, arcgis_config):
        del arcgis_config["services_url"]
        plugin = ArcGISPlugin(arcgis_config)
        assert await plugin.initialize() is False


# ── Catalog loading ────────────────────────────────────────────────────


class TestLoadManifest:
    def test_load_absolute_path(self, catalog_file):
        manifest = load_manifest(str(catalog_file))
        assert len(manifest["layers"]) == 5

    def test_missing_file_raises_with_hint(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="crawl_catalog"):
            load_manifest(str(tmp_path / "absent.json"))

    def test_rejects_manifest_without_layers(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text('{"version": 1}', encoding="utf-8")
        with pytest.raises(ValueError, match="layers"):
            load_manifest(str(path))


# ── get_tools ──────────────────────────────────────────────────────────


class TestGetTools:
    def test_get_tools_returns_expected_tools(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        tools = plugin.get_tools()

        tool_names = {t.name for t in tools}
        assert tool_names == {
            "search_datasets",
            "get_dataset",
            "get_aggregations",
            "query_data",
            "get_layer_schema",
            "get_distinct_values",
            "spatial_query_point",
            "geocode_address",
        }


# ── Search ─────────────────────────────────────────────────────────────


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_by_alias_and_acronym(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        results = await plugin.search_datasets("MHPA")
        assert results
        assert results[0]["dataset_id"] == MHPA_ID

    @pytest.mark.asyncio
    async def test_search_acronym_without_alias(self, arcgis_config):
        # 'Multi-Habitat Planning Area' auto-acronyms to MHPA even with no
        # curated alias.
        plugin = make_plugin(arcgis_config)
        plugin.index = CatalogIndex(make_manifest(), featured=[])
        results = await plugin.search_datasets("mhpa")
        assert results
        assert results[0]["dataset_id"] == MHPA_ID

    @pytest.mark.asyncio
    async def test_search_zoning_prefers_featured(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        results = await plugin.search_datasets("zoning")
        ids = [r["dataset_id"] for r in results]
        assert ids[0] == ZONES_ID
        assert "DSD/Zoning_Base/MapServer/0" in ids

    @pytest.mark.asyncio
    async def test_search_type_filter_feature_server(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        results = await plugin.search_datasets("trees", item_type="FeatureServer")
        assert [r["dataset_id"] for r in results] == [
            "Hosted/Street_Trees/FeatureServer/0"
        ]

    @pytest.mark.asyncio
    async def test_search_type_filter_geometry(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        results = await plugin.search_datasets("zoning", item_type="Polygon")
        assert results
        assert all(r["geometry_type"] == "esriGeometryPolygon" for r in results)

    @pytest.mark.asyncio
    async def test_search_no_results_message(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool(
            "search_datasets", {"q": "xyzzy-nothing-matches"}
        )
        assert result.success is True
        assert "No layers found" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_search_result_formatting(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool("search_datasets", {"q": "MHPA"})
        text = result.content[0]["text"]
        assert MHPA_ID in text
        assert "Polygon" in text
        assert "Featured: yes" in text


# ── Aggregations ───────────────────────────────────────────────────────


class TestAggregations:
    @pytest.mark.asyncio
    async def test_aggregate_by_folder(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        buckets = await plugin.get_aggregations("folder")
        as_dict = {b["key"]: b["doc_count"] for b in buckets}
        assert as_dict["Planning"] == 2
        assert as_dict["DSD"] == 1

    @pytest.mark.asyncio
    async def test_aggregate_by_geometry_type(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        buckets = await plugin.get_aggregations("geometry_type")
        as_dict = {b["key"]: b["doc_count"] for b in buckets}
        assert as_dict["Polygon"] == 3
        assert as_dict["Point"] == 1
        assert as_dict["none"] == 1

    @pytest.mark.asyncio
    async def test_aggregate_scoped_by_query(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        buckets = await plugin.get_aggregations("folder", q="zoning")
        keys = {b["key"] for b in buckets}
        assert "Hosted" not in keys

    @pytest.mark.asyncio
    async def test_aggregate_invalid_field(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool("get_aggregations", {"field": "tags"})
        assert result.success is False
        assert "not an aggregatable field" in result.error_message


# ── Dataset id validation ──────────────────────────────────────────────


class TestDatasetIdValidation:
    def test_valid_ids(self):
        validate = ArcGISPlugin._validate_dataset_id
        assert validate(MHPA_ID) == MHPA_ID
        assert validate("GeocoderMerged/MapServer/0") == "GeocoderMerged/MapServer/0"
        assert (
            validate("/Hosted/Street_Trees/FeatureServer/12/")
            == "Hosted/Street_Trees/FeatureServer/12"
        )

    @pytest.mark.parametrize(
        "bad_id",
        [
            "",
            "just-a-name",
            "Planning/PLN/MapServer/notanumber",
            "Planning/PLN/GPServer/0",
            "https://evil.example.com/MapServer/0",
            "../../../secrets/MapServer/0",
            "Planning/PLN/MapServer",
            "32-char-hex-hub-id-0123456789abcdef",
        ],
    )
    def test_invalid_ids(self, bad_id):
        with pytest.raises(ValueError):
            ArcGISPlugin._validate_dataset_id(bad_id)

    def test_layer_url_resolution(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        assert plugin._layer_url_for_item(MHPA_ID) == f"{SERVICES_URL}/{MHPA_ID}"


# ── get_dataset ────────────────────────────────────────────────────────


class TestGetDataset:
    @pytest.mark.asyncio
    async def test_get_dataset_from_catalog(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool("get_dataset", {"dataset_id": MHPA_ID})
        assert result.success is True
        text = result.content[0]["text"]
        assert "Multi-Habitat Planning Area" in text
        assert f"{SERVICES_URL}/{MHPA_ID}" in text
        assert "Max Record Count (per request): 2" in text
        assert "WGS84" in text
        plugin.feature_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_dataset_live_fallback(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=mock_response(
                {
                    "name": "Brand New Layer",
                    "geometryType": "esriGeometryPolygon",
                    "description": "published after crawl",
                    "maxRecordCount": 500,
                    "advancedQueryCapabilities": {"supportsPagination": True},
                }
            )
        )
        dataset = await plugin.get_dataset("New/Stuff/MapServer/4")
        assert dataset["name"] == "Brand New Layer"
        assert dataset["max_record_count"] == 500
        assert "Not in the bundled catalog" in dataset["note"]
        # Second call hits the instance cache, not the network.
        await plugin.get_dataset("New/Stuff/MapServer/4")
        assert plugin.feature_client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_get_dataset_missing_id(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool("get_dataset", {})
        assert result.success is False
        assert "dataset_id is required" in result.error_message


# ── query_data ─────────────────────────────────────────────────────────


def _page(features):
    return mock_response({"features": [{"attributes": a} for a in features]})


class TestQueryData:
    @pytest.mark.asyncio
    async def test_query_sets_wgs84_contract(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(return_value=_page([{"A": 1}]))
        await plugin.query_data(ZONES_ID, {"where": "1=1"}, limit=10)
        params = plugin.feature_client.get.await_args.kwargs["params"]
        assert params["inSR"] == 4326
        assert params["outSR"] == 4326
        assert params["returnGeometry"] == "false"

    @pytest.mark.asyncio
    async def test_query_paginates_past_layer_cap(self, arcgis_config):
        # MHPA's manifest entry caps at 2 records per request; a limit of 5
        # must page with resultOffset.
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            side_effect=[
                _page([{"OID": 1}, {"OID": 2}]),
                _page([{"OID": 3}, {"OID": 4}]),
                _page([{"OID": 5}]),
            ]
        )
        records = await plugin.query_data(MHPA_ID, None, limit=5)
        assert [r["OID"] for r in records] == [1, 2, 3, 4, 5]
        calls = plugin.feature_client.get.await_args_list
        assert len(calls) == 3
        assert "resultOffset" not in calls[0].kwargs["params"]
        assert calls[1].kwargs["params"]["resultOffset"] == 2
        assert calls[2].kwargs["params"]["resultOffset"] == 4

    @pytest.mark.asyncio
    async def test_query_stops_at_limit(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            side_effect=[
                _page([{"OID": 1}, {"OID": 2}]),
                _page([{"OID": 3}]),
            ]
        )
        records = await plugin.query_data(MHPA_ID, None, limit=3)
        assert len(records) == 3
        assert plugin.feature_client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_query_short_page_ends_pagination(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(return_value=_page([{"OID": 1}]))
        records = await plugin.query_data(MHPA_ID, None, limit=10)
        assert len(records) == 1
        assert plugin.feature_client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_query_no_pagination_support_stops(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=_page([{"OID": i} for i in range(1000)])
        )
        records = await plugin.query_data("TSD/Records/MapServer/3", None, limit=1000)
        # One full page comes back but the layer can't page further.
        assert plugin.feature_client.get.await_count == 1
        assert len(records) == 1000

    @pytest.mark.asyncio
    async def test_query_rejects_bad_limit(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        with pytest.raises(ValueError, match="limit"):
            await plugin.query_data(ZONES_ID, None, limit=0)

    @pytest.mark.asyncio
    async def test_query_rejects_forbidden_where(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        with pytest.raises(ValueError):
            await plugin.query_data(ZONES_ID, {"where": "1=1; DROP TABLE x"}, limit=5)

    @pytest.mark.asyncio
    async def test_query_surfaces_auth_error_hint(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=mock_response(
                {"error": {"code": 499, "message": "Token Required"}}
            )
        )
        with pytest.raises(RuntimeError, match="not\\s+anonymously queryable"):
            await plugin.query_data("AMPGIS/Private/MapServer/0", None, limit=5)

    @pytest.mark.asyncio
    async def test_execute_query_data_includes_total(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            side_effect=[
                _page([{"ZONE": "RS-1-7"}]),
                mock_response({"count": 42}),
            ]
        )
        result = await plugin.execute_tool(
            "query_data", {"dataset_id": ZONES_ID, "limit": 1}
        )
        assert result.success is True
        assert "TOTAL MATCHING: 42" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_get_record_count(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(return_value=mock_response({"count": 7}))
        count = await plugin.get_record_count(ZONES_ID, "HABPRES > 50")
        assert count == 7
        params = plugin.feature_client.get.await_args.kwargs["params"]
        assert params["returnCountOnly"] == "true"


# ── get_layer_schema / get_distinct_values ─────────────────────────────


class TestSchemaAndDistinct:
    @pytest.mark.asyncio
    async def test_get_layer_schema(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=mock_response(
                {
                    "name": "Base Zones",
                    "geometryType": "esriGeometryPolygon",
                    "fields": [
                        {"name": "ZONE_NAME", "type": "esriFieldTypeString"},
                        {"name": "ACRES", "type": "esriFieldTypeDouble"},
                    ],
                }
            )
        )
        schema = await plugin.get_layer_schema(ZONES_ID)
        assert schema["layer_name"] == "Base Zones"
        assert len(schema["fields"]) == 2

    @pytest.mark.asyncio
    async def test_get_layer_schema_keyword_filter(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=mock_response(
                {
                    "name": "Base Zones",
                    "geometryType": "esriGeometryPolygon",
                    "fields": [
                        {"name": "ZONE_NAME", "type": "esriFieldTypeString"},
                        {"name": "ACRES", "type": "esriFieldTypeDouble"},
                    ],
                }
            )
        )
        schema = await plugin.get_layer_schema(ZONES_ID, keyword="zone")
        assert [f["name"] for f in schema["fields"]] == ["ZONE_NAME"]

    @pytest.mark.asyncio
    async def test_get_distinct_values(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=_page([{"ZONE_NAME": "RS-1-7"}, {"ZONE_NAME": "CC-3-5"}])
        )
        values = await plugin.get_distinct_values(ZONES_ID, "ZONE_NAME")
        assert values == ["RS-1-7", "CC-3-5"]
        params = plugin.feature_client.get.await_args.kwargs["params"]
        assert params["returnDistinctValues"] == "true"

    @pytest.mark.asyncio
    async def test_get_distinct_values_like_escapes_quotes(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(return_value=_page([]))
        await plugin.get_distinct_values(ZONES_ID, "ZONE_NAME", like="O'Farrell")
        params = plugin.feature_client.get.await_args.kwargs["params"]
        assert "O''Farrell" in params["where"]


# ── spatial_query_point ────────────────────────────────────────────────


class TestSpatialQueryPoint:
    @pytest.mark.asyncio
    async def test_point_query_wgs84_contract(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=_page([{"HABPRES": 100, "SUBAREA": 113}])
        )
        records = await plugin.spatial_query_point(MHPA_ID, -117.0846, 32.5539)
        assert records[0]["HABPRES"] == 100
        params = plugin.feature_client.get.await_args.kwargs["params"]
        assert params["inSR"] == 4326
        assert params["outSR"] == 4326
        assert params["geometry"] == "-117.0846,32.5539"
        assert params["geometryType"] == "esriGeometryPoint"
        assert params["spatialRel"] == "esriSpatialRelIntersects"

    @pytest.mark.asyncio
    async def test_point_query_validates_range(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        with pytest.raises(ValueError, match="lon"):
            await plugin.spatial_query_point(MHPA_ID, -200, 32.5)
        with pytest.raises(ValueError, match="lat"):
            await plugin.spatial_query_point(MHPA_ID, -117.0, 95)

    @pytest.mark.asyncio
    async def test_execute_spatial_with_address_geocodes(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.geocode_address = AsyncMock(
            return_value=[
                {
                    "matched_address": "202 C ST, SAN DIEGO, CA, 92101",
                    "lon": -117.1625,
                    "lat": 32.7174,
                }
            ]
        )
        plugin.feature_client.get = AsyncMock(
            return_value=_page([{"ZONE_NAME": "CC-5-5"}])
        )
        result = await plugin.execute_tool(
            "spatial_query_point",
            {"item_id": ZONES_ID, "address": "202 C St"},
        )
        assert result.success is True
        text = result.content[0]["text"]
        assert "Geocoded '202 C St'" in text
        assert "CC-5-5" in text

    @pytest.mark.asyncio
    async def test_execute_spatial_requires_point_or_address(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool("spatial_query_point", {"item_id": ZONES_ID})
        assert result.success is False
        assert "address" in result.error_message


# ── geocode_address ────────────────────────────────────────────────────


class TestGeocode:
    @pytest.mark.asyncio
    async def test_geocode_appends_region(self, arcgis_config):
        arcgis_config["geocoder_region"] = "San Diego, CA"
        plugin = make_plugin(arcgis_config)
        plugin.plugin_config = ArcGISPluginConfig(**arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=mock_response(
                {
                    "result": {
                        "addressMatches": [
                            {
                                "matchedAddress": "202 C ST, SAN DIEGO, CA",
                                "coordinates": {"x": -117.1625, "y": 32.7174},
                            }
                        ]
                    }
                }
            )
        )
        candidates = await plugin.geocode_address("202 C St")
        assert candidates[0]["lon"] == -117.1625
        params = plugin.feature_client.get.await_args.kwargs["params"]
        assert params["address"] == "202 C St, San Diego, CA"

    @pytest.mark.asyncio
    async def test_geocode_empty_address_rejected(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        with pytest.raises(ValueError):
            await plugin.geocode_address("   ")


# ── execute_tool plumbing ──────────────────────────────────────────────


class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_execute_tool_unknown(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        result = await plugin.execute_tool("unknown_tool", {})
        assert result.success is False
        assert "Unknown tool" in result.error_message

    @pytest.mark.asyncio
    async def test_execute_tool_catches_exceptions(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        result = await plugin.execute_tool("get_layer_schema", {"item_id": ZONES_ID})
        assert result.success is False
        assert result.error_message


# ── Health check ───────────────────────────────────────────────────────


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_ok(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(
            return_value=mock_response({"currentVersion": 11.5})
        )
        assert await plugin.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self, arcgis_config):
        plugin = make_plugin(arcgis_config)
        plugin.feature_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
        assert await plugin.health_check() is False


# ── WHERE validator (regression) ───────────────────────────────────────


class TestWhereValidator:
    def test_allows_normal_clause(self):
        assert WhereValidator.validate("HABPRES > 50") == "HABPRES > 50"

    def test_empty_becomes_tautology(self):
        assert WhereValidator.validate("") == "1=1"

    @pytest.mark.parametrize(
        "clause",
        [
            "1=1; DROP TABLE parcels",
            "name = 'x' -- comment",
            "1=1 UNION SELECT * FROM users",
        ],
    )
    def test_rejects_injection(self, clause):
        with pytest.raises(ValueError):
            WhereValidator.validate(clause)

    def test_rejects_overlong_clause(self):
        with pytest.raises(ValueError, match="max length"):
            WhereValidator.validate("A=1 OR " * 400)

    def test_schema_check_accepts_known_fields(self):
        WhereValidator.validate_against_schema(
            "HABPRES > 50 AND INHABPRES = 'Yes'", ["HABPRES", "INHABPRES"]
        )

    def test_schema_check_suggests_close_match(self):
        with pytest.raises(ValueError, match="did you\\s+mean 'HABPRES'"):
            WhereValidator.validate_against_schema(
                "HABPRESS > 50", ["HABPRES", "ACRES"]
            )

    def test_schema_check_ignores_literals_and_functions(self):
        WhereValidator.validate_against_schema(
            "UPPER(ZONE_NAME) LIKE 'RS%' AND ACRES BETWEEN 1 AND 5",
            ["ZONE_NAME", "ACRES"],
        )

    def test_schema_check_skips_without_fields(self):
        WhereValidator.validate_against_schema("ANYTHING = 1", None)
        WhereValidator.validate_against_schema("1=1", ["A"])


class TestOutFieldsValidator:
    def test_star_and_empty(self):
        assert OutFieldsValidator.validate("*") == "*"
        assert OutFieldsValidator.validate("") == "*"
        assert OutFieldsValidator.validate(None) == "*"

    def test_valid_list_normalized(self):
        assert (
            OutFieldsValidator.validate("HABPRES, ACRES ,SUBAREA")
            == "HABPRES,ACRES,SUBAREA"
        )

    @pytest.mark.parametrize(
        "bad", ["HABPRES;DROP", "a b", "1FIELD", "f(x)", "*,ACRES"]
    )
    def test_rejects_non_identifiers(self, bad):
        with pytest.raises(ValueError):
            OutFieldsValidator.validate(bad)


class TestOrderByValidator:
    def test_empty_passthrough(self):
        assert OrderByValidator.validate("") == ""
        assert OrderByValidator.validate(None) == ""

    def test_valid_entries(self):
        assert OrderByValidator.validate("ACRES DESC") == "ACRES DESC"
        assert OrderByValidator.validate("ACRES desc, SUBAREA") == "ACRES desc,SUBAREA"

    @pytest.mark.parametrize("bad", ["ACRES DESCENDING", "1; DROP", "a=b"])
    def test_rejects_invalid_entries(self, bad):
        with pytest.raises(ValueError):
            OrderByValidator.validate(bad)
