"""Pydantic configuration schema for the ArcGIS Server directory plugin."""

from typing import List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FeaturedDataset(BaseModel):
    """A curated, known-good layer highlighted in search results.

    Featured entries are overlaid onto the crawled catalog at load time, so
    alias or note changes take effect without re-crawling.
    """

    dataset_id: str = Field(
        ...,
        description=(
            "Catalog dataset id, e.g. 'Planning/PLN_LongRangePlanning/MapServer/7'"
        ),
    )
    aliases: List[str] = Field(
        default_factory=list,
        description="Extra search terms that should resolve to this layer",
    )
    note: str = Field(
        default="",
        description="Curated description shown in search/get_dataset output",
    )

    model_config = ConfigDict(extra="forbid")


class ArcGISPluginConfig(BaseModel):
    """Configuration schema for the ArcGIS Server directory plugin.

    This plugin fronts a bare ArcGIS Server REST services directory (no
    ArcGIS Hub / Open Data catalog). Discovery comes from a precomputed
    catalog manifest produced by ``scripts/crawl_catalog.py``.
    """

    enabled: bool = Field(default=False, description="Whether plugin is enabled")
    services_url: str = Field(
        ...,
        description=(
            "ArcGIS Server REST services root, e.g. "
            "https://webmaps.sandiego.gov/arcgis/rest/services"
        ),
    )
    city_name: str = Field(..., description="Name of the city/organization")
    timeout: int = Field(
        default=120, ge=1, le=300, description="HTTP request timeout in seconds"
    )
    token: Optional[str] = Field(
        None, description="Optional token for authenticated requests"
    )
    geocoder_region: str = Field(
        default="",
        description=(
            "Optional region (e.g. 'San Diego, CA') appended to addresses "
            "during geocoding to bias results to this jurisdiction."
        ),
    )
    catalog_path: str = Field(
        default="plugins/arcgis/catalog.json",
        description=(
            "Path to the precomputed catalog manifest (JSON). Relative paths "
            "are resolved against the working directory, then the repo root."
        ),
    )
    featured_datasets: List[FeaturedDataset] = Field(
        default_factory=list,
        description="Curated layers boosted in search, with aliases and notes",
    )

    @field_validator("services_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate that URL is well-formed."""
        if not v:
            raise ValueError("URL cannot be empty")
        try:
            result = urlparse(v)
            if not result.scheme or not result.netloc:
                raise ValueError("URL must include scheme (http/https) and hostname")
            if result.scheme not in ("http", "https"):
                raise ValueError("URL scheme must be http or https")
        except Exception as e:
            raise ValueError(f"Invalid URL format: {e}")
        return v.rstrip("/")

    model_config = ConfigDict(extra="forbid")
