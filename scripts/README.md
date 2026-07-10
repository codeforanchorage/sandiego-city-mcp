# Scripts Directory

This directory contains utility scripts for OpenContext MCP server.

## Scripts

### `crawl_catalog.py`

Builds the layer catalog manifest for the `arcgis` (ArcGIS Server directory) plugin — the discovery index the running server loads at startup. The manifest is a **deploy artifact**: re-run the crawler, review the diff, commit, redeploy.

**Usage:**
```bash
python3 scripts/crawl_catalog.py                       # uses config.yaml services_url
python3 scripts/crawl_catalog.py --services-url URL --out path.json
```

**What it does:**
- Walks the ArcGIS Server REST services directory (folders → MapServer/FeatureServer services → `/layers`)
- Indexes every anonymously queryable feature layer (id, name, geometry, description, extent, `maxRecordCount`)
- Detects auth-gated services (HTTP 401/403, ArcGIS codes 498/499) and records them in the manifest's `skipped` list
- Writes `plugins/arcgis/catalog.json`

### `smoke_prod.py`

End-to-end smoke test of a deployed (or local) server: JSON-RPC surface, search resolution for "MHPA" and "zoning", schema, `TOTAL MATCHING` counts, the MHPA point-in-polygon verification query, and the geocode → zoning chain.

**Usage:**
```bash
python3 scripts/smoke_prod.py                            # production domain
python3 scripts/smoke_prod.py http://localhost:8000/mcp  # local server
```

### `deploy.sh`

Deployment script that validates configuration and deploys the MCP server to AWS Lambda.

**Usage:**
```bash
./scripts/deploy.sh --environment <staging|prod> [--tfworkspace <name>]
```

**Options:**

| Flag | Short | Required | Description |
|------|-------|----------|-------------|
| `--environment` | `-e` | Yes | `staging` or `prod` |
| `--tfworkspace` | `-w` | No | Terraform workspace name (default: `sandiego-city-staging` or `sandiego-city-prod`) |
| `--help` | `-h` | No | Show help |

**Examples:**
```bash
./scripts/deploy.sh --environment staging
./scripts/deploy.sh -e prod
./scripts/deploy.sh --environment staging --tfworkspace my-workspace
./scripts/deploy.sh -e prod -w sandiego-city-prod-v2
```

**What it does:**
- Validates that exactly ONE plugin is enabled
- Packages the code for Lambda deployment
- Selects (or creates) the specified Terraform workspace
- Deploys to AWS using Terraform
- Outputs the API Gateway URL and Lambda Function URL

**Requirements:**
- Python 3.11+
- AWS CLI configured
- Terraform installed
- Valid `config.yaml` in project root (create from `config-example.yaml`)

### `local_server.py`

Local development server for testing the MCP server without deploying to Lambda.

**Usage:**
```bash
python3 scripts/local_server.py
```

**What it does:**
- Starts a local HTTP server on `http://localhost:8000/mcp`
- Supports Streamable HTTP transport with session management
- Provides detailed logging for debugging
- Uses the same MCP server logic as Lambda deployment

**Requirements:**
- Python 3.11+
- `aiohttp` package (`pip install aiohttp`)
- Valid `config.yaml` in project root (create from `config-example.yaml`)

**Testing:**
```bash
# Test with curl
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'

# Or use the test script
./scripts/test_streamable_http.sh
```

### `test_streamable_http.sh`

Test script for Streamable HTTP transport. Tests the full MCP lifecycle.

**Usage:**
```bash
./scripts/test_streamable_http.sh [BASE_URL]
```

**Default:** `http://localhost:8000/mcp`

**What it tests:**
1. Initialize connection and extract session ID
2. List available tools
3. Call a tool (`ckan__search_datasets`)

**Requirements:**
- `jq` installed (`brew install jq` on macOS)
- MCP server running (local or deployed)

**Example:**
```bash
# Test local server
./scripts/test_streamable_http.sh

# Test deployed Lambda
./scripts/test_streamable_http.sh https://your-lambda-url.lambda-url.us-east-1.on.aws/mcp
```

## Notes

- All scripts should be run from the project root directory
- Scripts automatically handle path resolution relative to their location
- Make sure scripts are executable: `chmod +x scripts/*.sh`
