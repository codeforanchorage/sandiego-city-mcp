"""In-memory index over a precomputed services-directory catalog manifest.

The manifest is produced offline by ``scripts/crawl_catalog.py`` (see
``plugins/arcgis/crawler.py``) and bundled with the deployment, so lookups
here are pure dictionary/string work — no network.
"""

import difflib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.interfaces import ToolInputError

logger = logging.getLogger(__name__)

_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")

# Friendly names for esriGeometry* constants, used in output and in
# search/aggregation matching ("polygon" reads better than the constant).
_GEOMETRY_FRIENDLY = {
    "esriGeometryPolygon": "Polygon",
    "esriGeometryPolyline": "Polyline",
    "esriGeometryPoint": "Point",
    "esriGeometryMultipoint": "Multipoint",
    "esriGeometryEnvelope": "Envelope",
}

AGGREGATABLE_FIELDS = ("folder", "service", "service_type", "geometry_type")


def friendly_geometry(geometry_type: str) -> str:
    return _GEOMETRY_FRIENDLY.get(geometry_type, geometry_type or "")


def load_manifest(catalog_path: str) -> Dict[str, Any]:
    """Load the catalog manifest, resolving relative paths robustly.

    Tries, in order: the path as given (absolute or cwd-relative), then
    relative to the repo root (two levels above this file) — the latter is
    what makes the bundled manifest load inside Lambda regardless of cwd.
    """
    candidates = [Path(catalog_path)]
    if not Path(catalog_path).is_absolute():
        repo_root = Path(__file__).resolve().parents[2]
        candidates.append(repo_root / catalog_path)

    for candidate in candidates:
        if candidate.is_file():
            with open(candidate, encoding="utf-8") as f:
                manifest = json.load(f)
            if not isinstance(manifest.get("layers"), list):
                raise ValueError(f"Catalog manifest {candidate} has no 'layers' list")
            return manifest

    tried = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"Catalog manifest not found (tried: {tried}). "
        "Generate it with: python scripts/crawl_catalog.py"
    )


def _acronym(name: str) -> str:
    """First letters of a multi-word name: 'Multi-Habitat Planning Area' -> MHPA.

    Lets terse local shorthand (MHPA, TPA, ...) resolve without curation.
    Only names of 3+ words get one; two-letter 'acronyms' are noise.
    """
    words = [w for w in _WORD_SPLIT_RE.split(name) if w]
    if len(words) < 3:
        return ""
    return "".join(w[0] for w in words).upper()


class CatalogIndex:
    """Searchable index over manifest layer entries."""

    def __init__(
        self,
        manifest: Dict[str, Any],
        featured: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.manifest = manifest
        self.entries: List[Dict[str, Any]] = manifest.get("layers", [])
        self.by_id: Dict[str, Dict[str, Any]] = {
            e["dataset_id"]: e for e in self.entries
        }
        self._apply_featured(featured or [])

    def _apply_featured(self, featured: List[Dict[str, Any]]) -> None:
        """Overlay curated aliases/notes from config onto crawled entries.

        Config-side overlay (rather than baking into the manifest) means
        alias edits take effect on deploy without a re-crawl.
        """
        for item in featured:
            entry = self.by_id.get(item.get("dataset_id", ""))
            if entry is None:
                logger.warning(
                    f"featured_datasets entry not in catalog: "
                    f"{item.get('dataset_id')!r}"
                )
                continue
            entry["featured"] = True
            entry["aliases"] = [a for a in item.get("aliases", []) if a]
            if item.get("note"):
                entry["note"] = item["note"]

    # ── Search ───────────────────────────────────────────────────────────

    def search(
        self, query: str, limit: int = 10, item_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Rank entries against ``query`` tokens; higher score first.

        Matching is substring + acronym + curated-alias + fuzzy (difflib)
        over layer name, service/folder names, and descriptions.
        """
        tokens = [t for t in query.lower().split() if t]
        entries = self.entries
        if item_type:
            entries = [e for e in entries if self._type_matches(e, item_type)]
        if not tokens:
            return entries[:limit]

        scored = []
        for entry in entries:
            score, matched = self._score(entry, tokens)
            if matched == 0:
                continue
            if entry.get("featured"):
                score += 5
            scored.append((score, matched, entry))

        # AND semantics first: if any entry matched every token, drop the
        # partial matches so multi-word queries stay precise.
        full = [s for s in scored if s[1] == len(tokens)]
        pool = full if full else scored
        pool.sort(key=lambda item: (-item[0], item[2]["dataset_id"]))
        return [entry for _, _, entry in pool[:limit]]

    def _score(self, entry: Dict[str, Any], tokens: List[str]) -> tuple:
        name = (entry.get("name") or "").lower()
        name_words = [w for w in _WORD_SPLIT_RE.split(name) if w]
        acronym = _acronym(entry.get("name") or "")
        aliases = [a.lower() for a in entry.get("aliases", [])]
        service = (entry.get("service") or "").lower()
        folder = (entry.get("folder") or "").lower()
        descriptions = " ".join(
            (
                entry.get("note") or "",
                entry.get("description") or "",
                entry.get("service_description") or "",
            )
        ).lower()

        score = 0
        matched = 0
        for token in tokens:
            token_score = 0
            if acronym and token.upper() == acronym:
                token_score = max(token_score, 40)
            if any(token in alias for alias in aliases):
                token_score = max(token_score, 40)
            if token in name:
                bonus = 10 if token in name_words else 0
                token_score = max(token_score, 25 + bonus)
            if token in service or token in folder:
                token_score = max(token_score, 12)
            if token in descriptions:
                token_score = max(token_score, 6)
            if token_score < 25 and name_words:
                # Fuzzy catch for inflections: 'zoning' ~ 'zones'.
                close = difflib.get_close_matches(token, name_words, n=1, cutoff=0.7)
                if close:
                    token_score = max(token_score, 10)
            if token_score:
                matched += 1
                score += token_score
        return score, matched

    @staticmethod
    def _type_matches(entry: Dict[str, Any], item_type: str) -> bool:
        """Match a type filter against service type or geometry.

        Accepts Hub-style names ('Feature Service', 'Map Service') for
        muscle-memory compatibility with sibling servers, plus raw
        MapServer/FeatureServer and geometry names ('Polygon', 'Point').
        """
        wanted = item_type.lower().replace(" ", "")
        service_type = (entry.get("service_type") or "").lower()
        geometry = friendly_geometry(entry.get("geometry_type") or "").lower()
        if wanted in ("featureservice", "featureserver"):
            return service_type == "featureserver"
        if wanted in ("mapservice", "mapserver"):
            return service_type == "mapserver"
        return bool(geometry) and wanted == geometry

    # ── Aggregations ─────────────────────────────────────────────────────

    def aggregate(
        self, field: str, query: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Facet counts of indexed layers by a catalog field."""
        if field not in AGGREGATABLE_FIELDS:
            raise ToolInputError(
                f"'{field}' is not an aggregatable field. "
                f"Available fields: {', '.join(AGGREGATABLE_FIELDS)}."
            )
        entries = self.search(query, limit=len(self.entries)) if query else self.entries
        counts: Dict[str, int] = {}
        for entry in entries:
            if field == "geometry_type":
                key = friendly_geometry(entry.get("geometry_type") or "") or "none"
            elif field == "folder":
                key = entry.get("folder") or "(root)"
            elif field == "service":
                folder = entry.get("folder") or ""
                service = entry.get("service") or ""
                key = f"{folder}/{service}" if folder else service
            else:
                key = entry.get(field) or "unknown"
            counts[key] = counts.get(key, 0) + 1
        return [
            {"key": key, "doc_count": count}
            for key, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
