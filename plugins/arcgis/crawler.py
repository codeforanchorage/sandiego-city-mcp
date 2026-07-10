"""ArcGIS Server services-directory crawler.

Walks a bare ArcGIS Server REST services directory (folders -> services ->
layers) and builds a catalog manifest of every anonymously queryable
MapServer/FeatureServer feature layer. The manifest is written to JSON by
``scripts/crawl_catalog.py`` and bundled with the deployment as a static
artifact, so the running server never crawls live.

Services/layers that demand authentication (HTTP 401/403 or ArcGIS JSON
error codes 498/499/403) are skipped and recorded in the manifest's
``skipped`` list so gating is visible and diffable across crawls.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1

# Service types whose layers answer /query. Everything else in the
# directory (GPServer, GeocodeServer, PrintServer, ...) is ignored.
CRAWLABLE_SERVICE_TYPES = {"MapServer", "FeatureServer"}

# Layer types that hold queryable features. Group Layers are containers
# with no geometry; Raster/Annotation layers don't answer attribute queries.
QUERYABLE_LAYER_TYPES = {"Feature Layer", "Table"}

_AUTH_ERROR_CODES = {401, 403, 498, 499}


class AuthRequiredError(Exception):
    """The endpoint requires a token / signed-in account."""


def _check_arcgis_error(data: Dict[str, Any]) -> None:
    """Raise on an ArcGIS JSON error body; AuthRequiredError if it's gating."""
    err = data.get("error")
    if not err:
        return
    code = err.get("code")
    message = err.get("message", "") or ""
    if code in _AUTH_ERROR_CODES or "token" in message.lower():
        raise AuthRequiredError(f"code {code}: {message}")
    raise RuntimeError(f"code {code}: {message}")


def _truncate(text: Optional[str], length: int = 400) -> str:
    text = (text or "").strip()
    if len(text) > length:
        return text[: length - 3] + "..."
    return text


def _compact_extent(extent: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Keep only the envelope corners and spatial reference wkid."""
    if not isinstance(extent, dict):
        return None
    keys = ("xmin", "ymin", "xmax", "ymax")
    if not all(k in extent for k in keys):
        return None
    sr = extent.get("spatialReference") or {}
    compact = {k: extent[k] for k in keys}
    wkid = sr.get("latestWkid") or sr.get("wkid")
    if wkid:
        compact["wkid"] = wkid
    return compact


class ServicesDirectoryCrawler:
    """Crawls one ArcGIS Server REST services directory into a manifest."""

    def __init__(
        self,
        services_url: str,
        client: httpx.AsyncClient,
        concurrency: int = 8,
    ) -> None:
        self.services_url = services_url.rstrip("/")
        self.client = client
        self._semaphore = asyncio.Semaphore(concurrency)
        self.skipped: List[Dict[str, str]] = []

    async def _get_json(self, url: str) -> Dict[str, Any]:
        """GET ``url`` with f=json under the concurrency cap.

        Raises AuthRequiredError for auth-gated endpoints, RuntimeError for
        other ArcGIS error bodies or HTTP failures.
        """
        async with self._semaphore:
            response = await self.client.get(url, params={"f": "json"})
        if response.status_code in _AUTH_ERROR_CODES:
            raise AuthRequiredError(f"HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()
        _check_arcgis_error(data)
        return data

    async def crawl(self, generated_at: str = "") -> Dict[str, Any]:
        """Walk the whole directory and return the catalog manifest.

        Args:
            generated_at: ISO timestamp stamped into the manifest by the
                caller (the crawler itself does not read the clock).
        """
        root = await self._get_json(self.services_url)
        folders = root.get("folders", []) or []
        services = list(root.get("services", []) or [])

        folder_listings = await asyncio.gather(
            *(self._list_folder(folder) for folder in folders)
        )
        for listing in folder_listings:
            services.extend(listing)

        crawlable = [s for s in services if s.get("type") in CRAWLABLE_SERVICE_TYPES]
        ignored = len(services) - len(crawlable)

        layer_lists = await asyncio.gather(*(self._crawl_service(s) for s in crawlable))
        layers = [layer for sub in layer_lists for layer in sub]
        layers.sort(key=lambda item: item["dataset_id"])
        self.skipped.sort(key=lambda item: item["service"])

        return {
            "version": MANIFEST_VERSION,
            "generated_at": generated_at,
            "services_url": self.services_url,
            "stats": {
                "folders": len(folders),
                "services_seen": len(services),
                "services_crawled": len(crawlable),
                "services_ignored_type": ignored,
                "services_skipped": len(self.skipped),
                "layers": len(layers),
            },
            "skipped": self.skipped,
            "layers": layers,
        }

    async def _list_folder(self, folder: str) -> List[Dict[str, Any]]:
        try:
            listing = await self._get_json(f"{self.services_url}/{folder}")
        except AuthRequiredError as e:
            self._skip(folder, f"auth required: {e}")
            return []
        except Exception as e:
            self._skip(folder, f"folder listing failed: {e}")
            return []
        return listing.get("services", []) or []

    async def _crawl_service(self, service: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Read one service's metadata + /layers into manifest entries."""
        name = service.get("name", "")
        stype = service.get("type", "")
        service_url = f"{self.services_url}/{name}/{stype}"
        label = f"{name}/{stype}"

        try:
            meta, layers_doc = await asyncio.gather(
                self._get_json(service_url),
                self._get_json(f"{service_url}/layers"),
            )
        except AuthRequiredError as e:
            self._skip(label, f"auth required: {e}")
            return []
        except Exception as e:
            self._skip(label, f"unreadable: {e}")
            return []

        service_description = _truncate(
            meta.get("serviceDescription") or meta.get("description")
        )
        folder, _, service_basename = name.rpartition("/")

        entries = []
        for layer in layers_doc.get("layers", []) or []:
            if layer.get("type") not in QUERYABLE_LAYER_TYPES:
                continue
            layer_id = layer.get("id")
            if layer_id is None:
                continue
            entries.append(
                {
                    "dataset_id": f"{name}/{stype}/{layer_id}",
                    "name": layer.get("name", ""),
                    "folder": folder,
                    "service": service_basename,
                    "service_type": stype,
                    "layer_id": layer_id,
                    "geometry_type": layer.get("geometryType") or "",
                    "description": _truncate(layer.get("description")),
                    "service_description": service_description,
                    "max_record_count": layer.get("maxRecordCount"),
                    "supports_pagination": bool(
                        (layer.get("advancedQueryCapabilities") or {}).get(
                            "supportsPagination"
                        )
                    ),
                    "extent": _compact_extent(layer.get("extent")),
                }
            )
        return entries

    def _skip(self, service: str, reason: str) -> None:
        logger.warning(f"Skipping {service}: {reason}")
        self.skipped.append({"service": service, "reason": _truncate(reason, 200)})


async def crawl_services_directory(
    services_url: str,
    timeout: int = 60,
    concurrency: int = 8,
    generated_at: str = "",
) -> Dict[str, Any]:
    """Convenience wrapper: crawl ``services_url`` with a fresh HTTP client."""
    async with httpx.AsyncClient(
        timeout=timeout, headers={"Accept": "application/json"}
    ) as client:
        crawler = ServicesDirectoryCrawler(
            services_url, client, concurrency=concurrency
        )
        return await crawler.crawl(generated_at=generated_at)
