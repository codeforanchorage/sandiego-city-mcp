# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**San Diego City fork.** This is a City of San Diego fork of the OpenContext MCP server framework (forked from the Worcester GIS fork). It serves the City's bare ArcGIS Server REST services directory (`webmaps.sandiego.gov/arcgis/rest/services`, ArcGIS Enterprise 11.5) via the built-in `arcgis` plugin (see `config.yaml`). There is NO ArcGIS Hub catalog in front of this host, so discovery comes from a **precomputed catalog manifest** (`plugins/arcgis/catalog.json`) built offline by `scripts/crawl_catalog.py` and bundled with the deployment — the running server never crawls live. To refresh the catalog, re-run the crawler and redeploy.

Key invariants of the `arcgis` plugin in this fork:
- **Same tool surface as sibling servers** — `search_datasets`, `get_dataset`, `get_layer_schema`, `get_distinct_values`, `get_aggregations`, `query_data`, `spatial_query_point`, `geocode_address` under the `arcgis__` prefix, identical names/shapes to the Worcester and SANDAG forks.
- **dataset_id is a services-directory path** — `{folder}/{service}/{MapServer|FeatureServer}/{layerId}`, e.g. `Planning/PLN_LongRangePlanning/MapServer/7` (MHPA). Validated before URL interpolation (security boundary).
- **WGS84 hard contract** — layers are authored in EPSG:2230 (State Plane CA Zone VI, US survey feet); every query sets `inSR=4326` and `outSR=4326`. Without `inSR`, WGS84 coords are read as State Plane feet and silently match nothing.
- **Per-layer pagination** — `maxRecordCount` varies by layer and is read from the catalog; `query_data` pages with `resultOffset`/`resultRecordCount`.
- **Auth-gated services** are detected during the crawl (HTTP 401/403, ArcGIS JSON codes 498/499/403) and recorded in the manifest's `skipped` list; only anonymously queryable layers are indexed.

## Build & Development Commands

```bash
# Install dependencies (uv preferred, pip fallback)
uv sync                              # or: pip install -r requirements.txt

# Run local MCP server (no Lambda needed)
python3 scripts/local_server.py      # Serves on http://localhost:8000/mcp
# Or: python3 local_server.py        # Alternate entry point, serves on / and /mcp

# Validate config
python3 -c "from core.validators import load_and_validate_config; load_and_validate_config('config.yaml')"

# Rebuild the layer catalog (deploy artifact; commit the result and redeploy)
python3 scripts/crawl_catalog.py

# Smoke test a deployment (or a local server)
python3 scripts/smoke_prod.py http://localhost:8000/mcp

# Tests
uv run pytest tests/ -n auto                                    # All tests, parallel
uv run pytest tests/test_ckan_plugin.py -v                      # Single file
uv run pytest tests/test_ckan_plugin.py::TestClass::test_name -v  # Single test
uv run pytest tests/ --cov=core --cov=plugins --cov-report=term-missing  # With coverage (80% minimum)

# Linting (ruff)
uv run ruff check core/ plugins/ server/ tests/      # Check
uv run ruff check core/ plugins/ server/ tests/ --fix # Auto-fix
uv run ruff format core/ plugins/ server/ tests/      # Format

# Pre-commit hooks
pre-commit run --all-files

# Go client (requires Go 1.21+)
cd client && make build

# Deploy to AWS
./scripts/deploy.sh --environment staging
```

## Architecture

**Core rule: One Fork = One MCP Server.** Each deployment runs exactly ONE plugin. This is enforced at config validation time (`core/validators.py`) and at runtime (`PluginManager.load_plugins()`). To deploy multiple MCP servers, fork the repo per plugin.

**Request flow:**
```
Claude (stdio) → Go client (client/) or stdio_bridge.py → HTTP POST /mcp
  → Lambda (server/adapters/aws_lambda.py) or local_server.py
  → server/http_handler.py → core/mcp_server.py (JSON-RPC 2.0)
  → core/plugin_manager.py → Plugin → External API
```

**Key modules:**
- `core/interfaces.py` — Abstract bases: `MCPPlugin`, `DataPlugin`, plus `ToolDefinition`, `ToolResult`, `PluginType` enum
- `core/plugin_manager.py` — Discovers plugins by scanning `plugins/` and `custom_plugins/` for `plugin.py` files. Registers tools with `pluginname__toolname` prefix. Routes `tools/call` to the correct plugin.
- `core/mcp_server.py` — Handles MCP JSON-RPC methods: `initialize`, `tools/list`, `tools/call`, `ping`
- `core/validators.py` — Loads config from `config.yaml` (local) or `OPENCONTEXT_CONFIG` env var (Lambda). Enforces single-plugin rule.
- `server/adapters/aws_lambda.py` — AWS Lambda entry point (handler: `server.adapters.aws_lambda.lambda_handler`). Also `server/lambda_handler.py` as legacy entry point.
- `server/http_handler.py` — Cloud-agnostic HTTP handler shared by Lambda and local server
- `stdio_bridge.py` — Python stdio-to-HTTP bridge for connecting Claude Desktop/Code to the local server (alternative to Go client)

**Built-in plugins** (`plugins/`): `ckan`, `arcgis`, `socrata` — each implements `DataPlugin` with `search_datasets`, `get_dataset`, `query_data`. Custom plugins go in `custom_plugins/` and are auto-discovered.

## Plugin Development

New plugins must implement `MCPPlugin` (or `DataPlugin` for data sources). Place in `custom_plugins/<name>/plugin.py`. The class must define `plugin_name`, `plugin_type`, `plugin_version` and implement `initialize()`, `shutdown()`, `get_tools()`, `execute_tool()`, `health_check()`. Tool names are auto-prefixed — return bare names from `get_tools()`.

## Configuration

Copy `config-example.yaml` to `config.yaml`. Enable exactly one plugin. Config supports `${ENV_VAR}` substitution. For Lambda, config is serialized to the `OPENCONTEXT_CONFIG` env var by Terraform.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs ruff lint/format, pip-audit, pytest with coverage, and Go tests on push to main/develop and on PRs.
