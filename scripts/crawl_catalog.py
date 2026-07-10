#!/usr/bin/env python3
"""Build the ArcGIS services-directory catalog manifest.

Crawls the ArcGIS Server REST services directory configured in config.yaml
(plugins.arcgis.services_url) and writes the layer catalog to
plugins/arcgis/catalog.json. The manifest is a deploy artifact: the running
MCP server only ever reads this file — it never crawls live. To refresh the
catalog, re-run this script and redeploy.

Usage:
    python scripts/crawl_catalog.py
    python scripts/crawl_catalog.py --services-url https://... --out path.json
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from plugins.arcgis.crawler import crawl_services_directory  # noqa: E402


def _services_url_from_config(config_path: Path) -> str:
    import yaml

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    url = (config.get("plugins", {}).get("arcgis", {}) or {}).get("services_url")
    if not url:
        raise SystemExit(
            f"No plugins.arcgis.services_url in {config_path}; "
            "pass --services-url explicitly."
        )
    return url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--services-url",
        help="ArcGIS Server REST services root (default: from config.yaml)",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "config.yaml"),
        help="Config file to read services_url from",
    )
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "plugins" / "arcgis" / "catalog.json"),
        help="Output manifest path",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    services_url = args.services_url or _services_url_from_config(Path(args.config))
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print(f"Crawling {services_url} ...")
    manifest = asyncio.run(
        crawl_services_directory(
            services_url,
            timeout=args.timeout,
            concurrency=args.concurrency,
            generated_at=generated_at,
        )
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
        f.write("\n")

    stats = manifest["stats"]
    print(f"Wrote {out_path} ({out_path.stat().st_size / 1024:.0f} KiB)")
    print(
        f"  folders: {stats['folders']}  services crawled: "
        f"{stats['services_crawled']}/{stats['services_seen']}  "
        f"layers indexed: {stats['layers']}"
    )
    if manifest["skipped"]:
        print(f"  skipped {len(manifest['skipped'])} service(s):")
        for item in manifest["skipped"]:
            print(f"    - {item['service']}: {item['reason']}")


if __name__ == "__main__":
    main()
