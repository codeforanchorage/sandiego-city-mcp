"""Tests for the ArcGIS services-directory crawler.

The crawler is exercised against a fake HTTP client that serves a small
canned directory: a root with one service, one crawlable folder, one
auth-gated folder, and one service whose /layers mixes feature, group,
and raster layers.
"""

import pytest
from unittest.mock import Mock

from plugins.arcgis.crawler import (
    AuthRequiredError,
    ServicesDirectoryCrawler,
    _check_arcgis_error,
)

SERVICES_URL = "https://gis.example.gov/arcgis/rest/services"


def _response(payload, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.raise_for_status = Mock()
    response.json = Mock(return_value=payload)
    return response


class FakeClient:
    """Maps URLs to canned JSON payloads (or HTTP status codes)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append(url)
        payload = self.routes.get(url)
        if payload is None:
            return _response({"error": {"code": 500, "message": "no route"}})
        if isinstance(payload, int):
            return _response({}, status_code=payload)
        return _response(payload)


ROUTES = {
    SERVICES_URL: {
        "currentVersion": 11.5,
        "folders": ["Planning", "AMPGIS"],
        "services": [
            {"name": "GeocoderMerged", "type": "MapServer"},
            {"name": "RootLocator", "type": "GeocodeServer"},
        ],
    },
    f"{SERVICES_URL}/Planning": {
        "services": [
            {"name": "Planning/PLN_LongRangePlanning", "type": "MapServer"},
            {"name": "Planning/PLN_Tools", "type": "GPServer"},
        ]
    },
    # Whole folder gated, as on the real San Diego host.
    f"{SERVICES_URL}/AMPGIS": {"error": {"code": 499, "message": "Token Required"}},
    f"{SERVICES_URL}/GeocoderMerged/MapServer": {
        "serviceDescription": "Citywide composite geocoder"
    },
    f"{SERVICES_URL}/GeocoderMerged/MapServer/layers": {
        "layers": [
            {
                "id": 0,
                "name": "Address Points",
                "type": "Feature Layer",
                "geometryType": "esriGeometryPoint",
                "maxRecordCount": 1000,
                "advancedQueryCapabilities": {"supportsPagination": True},
            }
        ]
    },
    f"{SERVICES_URL}/Planning/PLN_LongRangePlanning/MapServer": {
        "serviceDescription": "Long range planning layers"
    },
    f"{SERVICES_URL}/Planning/PLN_LongRangePlanning/MapServer/layers": {
        "layers": [
            {
                "id": 6,
                "name": "Environmental Features",
                "type": "Group Layer",
            },
            {
                "id": 7,
                "name": "Multi-Habitat Planning Area",
                "type": "Feature Layer",
                "geometryType": "esriGeometryPolygon",
                "description": "MHPA preserve boundaries",
                "maxRecordCount": 2000,
                "advancedQueryCapabilities": {"supportsPagination": True},
                "extent": {
                    "xmin": 1,
                    "ymin": 2,
                    "xmax": 3,
                    "ymax": 4,
                    "spatialReference": {"wkid": 102646, "latestWkid": 2230},
                },
            },
            {
                "id": 8,
                "name": "Aerial Imagery",
                "type": "Raster Layer",
            },
        ]
    },
}


@pytest.fixture
def manifest_result():
    import asyncio

    client = FakeClient(ROUTES)
    crawler = ServicesDirectoryCrawler(SERVICES_URL, client, concurrency=2)
    return asyncio.run(crawler.crawl(generated_at="2026-07-10T00:00:00+00:00"))


class TestCrawl:
    def test_manifest_shape(self, manifest_result):
        assert manifest_result["version"] == 1
        assert manifest_result["generated_at"] == "2026-07-10T00:00:00+00:00"
        assert manifest_result["services_url"] == SERVICES_URL

    def test_indexes_feature_layers_only(self, manifest_result):
        ids = [layer["dataset_id"] for layer in manifest_result["layers"]]
        assert ids == [
            "GeocoderMerged/MapServer/0",
            "Planning/PLN_LongRangePlanning/MapServer/7",
        ]

    def test_layer_entry_fields(self, manifest_result):
        mhpa = next(
            layer for layer in manifest_result["layers"] if layer["layer_id"] == 7
        )
        assert mhpa["name"] == "Multi-Habitat Planning Area"
        assert mhpa["folder"] == "Planning"
        assert mhpa["service"] == "PLN_LongRangePlanning"
        assert mhpa["service_type"] == "MapServer"
        assert mhpa["geometry_type"] == "esriGeometryPolygon"
        assert mhpa["max_record_count"] == 2000
        assert mhpa["supports_pagination"] is True
        assert mhpa["service_description"] == "Long range planning layers"
        assert mhpa["extent"] == {
            "xmin": 1,
            "ymin": 2,
            "xmax": 3,
            "ymax": 4,
            "wkid": 2230,
        }

    def test_root_service_has_empty_folder(self, manifest_result):
        root_layer = manifest_result["layers"][0]
        assert root_layer["folder"] == ""
        assert root_layer["service"] == "GeocoderMerged"

    def test_auth_gated_folder_skipped_and_recorded(self, manifest_result):
        skipped = {
            item["service"]: item["reason"] for item in manifest_result["skipped"]
        }
        assert "AMPGIS" in skipped
        assert "auth required" in skipped["AMPGIS"]

    def test_non_query_service_types_ignored(self, manifest_result):
        stats = manifest_result["stats"]
        # GeocodeServer + GPServer ignored by type, not counted as skipped.
        assert stats["services_ignored_type"] == 2
        assert stats["services_crawled"] == 2
        assert stats["layers"] == 2


class TestAuthDetection:
    def test_error_code_499_is_auth(self):
        with pytest.raises(AuthRequiredError):
            _check_arcgis_error({"error": {"code": 499, "message": "Token Required"}})

    def test_error_message_token_is_auth(self):
        with pytest.raises(AuthRequiredError):
            _check_arcgis_error(
                {"error": {"code": 400, "message": "A valid token is required"}}
            )

    def test_other_errors_are_runtime(self):
        with pytest.raises(RuntimeError):
            _check_arcgis_error({"error": {"code": 500, "message": "boom"}})

    def test_no_error_passes(self):
        _check_arcgis_error({"layers": []})

    @pytest.mark.asyncio
    async def test_http_403_is_auth(self):
        client = FakeClient({f"{SERVICES_URL}/Private": 403})
        crawler = ServicesDirectoryCrawler(SERVICES_URL, client)
        with pytest.raises(AuthRequiredError):
            await crawler._get_json(f"{SERVICES_URL}/Private")

    @pytest.mark.asyncio
    async def test_gated_service_recorded_not_fatal(self):
        routes = dict(ROUTES)
        routes[f"{SERVICES_URL}/GeocoderMerged/MapServer"] = {
            "error": {"code": 499, "message": "Token Required"}
        }
        routes[f"{SERVICES_URL}/GeocoderMerged/MapServer/layers"] = {
            "error": {"code": 499, "message": "Token Required"}
        }
        client = FakeClient(routes)
        crawler = ServicesDirectoryCrawler(SERVICES_URL, client)
        manifest = await crawler.crawl()
        ids = [layer["dataset_id"] for layer in manifest["layers"]]
        assert ids == ["Planning/PLN_LongRangePlanning/MapServer/7"]
        skipped_names = {item["service"] for item in manifest["skipped"]}
        assert "GeocoderMerged/MapServer" in skipped_names
