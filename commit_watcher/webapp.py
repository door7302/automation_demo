#!/usr/bin/env python3
"""Web UI for browsing Junos commit diffs archived by the syslog watcher.

Serves a small single-page frontend plus a JSON API on top of the same MongoDB
collection the watcher writes to. Two views are provided:

* **Changes** - every commit within a time frame, as clickable abstracts
  (router name, date, model, version); click to reveal the full diff.
* **Timeline** - a per-router timeline; hover a change to see its date, model,
  version and diff.

Configuration is read from the same ``config.yaml`` as the watcher (the
``mongodb`` section), plus an optional ``web`` section for the HTTP server and
TLS. Every value can also be set via environment variables.

Run ``python webapp.py --help`` for usage.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from motor.motor_asyncio import AsyncIOMotorClient

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class WebConfig:
    """Runtime configuration for the web UI (MongoDB + HTTP server + TLS)."""

    def __init__(self, data: Dict[str, Any]) -> None:
        mongo = data.get("mongodb") or {}
        self.mongo_uri: str = os.environ.get(
            "MONGODB_URI", mongo.get("uri", "mongodb://localhost:27017")
        )
        self.mongo_db: str = os.environ.get(
            "MONGODB_DB", mongo.get("database", "junos_commits")
        )
        self.mongo_collection: str = os.environ.get(
            "MONGODB_COLLECTION", mongo.get("collection", "commit_diffs")
        )

        web = data.get("web") or {}
        self.host: str = os.environ.get("WEB_HOST", web.get("host", "0.0.0.0"))
        self.port: int = int(os.environ.get("WEB_PORT", web.get("port", 8080)))

        ssl = web.get("ssl") or {}
        self.ssl_enabled: bool = _env_bool(
            "WEB_SSL_ENABLED", bool(ssl.get("enabled", False))
        )
        self.ssl_certfile: Optional[str] = os.environ.get(
            "WEB_SSL_CERTFILE", ssl.get("certfile")
        )
        self.ssl_keyfile: Optional[str] = os.environ.get(
            "WEB_SSL_KEYFILE", ssl.get("keyfile")
        )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_config(path: Optional[str]) -> WebConfig:
    data: Dict[str, Any] = {}
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    return WebConfig(data)


# --------------------------------------------------------------------------- #
# Application factory
# --------------------------------------------------------------------------- #
def create_app(config: WebConfig) -> FastAPI:
    app = FastAPI(title="Junos Commit Watcher", docs_url=None, redoc_url=None)
    client = AsyncIOMotorClient(config.mongo_uri)
    collection = client[config.mongo_db][config.mongo_collection]

    def _serialise(doc: Dict[str, Any]) -> Dict[str, Any]:
        date = doc.get("date")
        received = doc.get("received_at")
        return {
            "id": str(doc.get("_id")),
            "source": doc.get("source", ""),
            "date": date.isoformat() if date is not None else None,
            "received_at": received.isoformat() if received is not None else None,
            "model": doc.get("model", ""),
            "version": doc.get("version", ""),
            "diff": doc.get("diff", ""),
        }

    @app.get("/api/sources")
    async def sources() -> List[str]:
        """Distinct router names, sorted, for the filter controls."""
        values = await collection.distinct("source")
        return sorted(v for v in values if v)

    @app.get("/api/commits")
    async def commits(
        start: Optional[str] = Query(None, description="ISO start of time frame"),
        end: Optional[str] = Query(None, description="ISO end of time frame"),
        sources: Optional[str] = Query(
            None, description="Comma-separated router names to include"
        ),
        limit: int = Query(1000, ge=1, le=10000),
    ) -> JSONResponse:
        """Return commits within a time frame, newest first."""
        query: Dict[str, Any] = {}

        date_range: Dict[str, Any] = {}
        if start:
            date_range["$gte"] = _parse_dt(start)
        if end:
            date_range["$lte"] = _parse_dt(end)
        if date_range:
            query["date"] = date_range

        if sources:
            names = [s.strip() for s in sources.split(",") if s.strip()]
            if names:
                query["source"] = {"$in": names}

        cursor = collection.find(query).sort("date", -1).limit(limit)
        docs = [_serialise(doc) async for doc in cursor]
        return JSONResponse(docs)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        client.close()

    # Static SPA (mounted last so /api/* takes precedence).
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def _parse_dt(value: str):
    import datetime as dt

    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid datetime: %s" % value) from err


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-c", "--config",
        default=os.environ.get("WATCHER_CONFIG", "config.yaml"),
        help="Path to the YAML configuration file (default: config.yaml).",
    )
    return parser.parse_args()


def main() -> None:
    import uvicorn

    args = parse_args()
    config = load_config(args.config)
    app = create_app(config)

    ssl_kwargs: Dict[str, Any] = {}
    if config.ssl_enabled:
        if not (config.ssl_certfile and config.ssl_keyfile):
            raise SystemExit(
                "web.ssl.enabled is true but certfile/keyfile are not set"
            )
        ssl_kwargs = {
            "ssl_certfile": config.ssl_certfile,
            "ssl_keyfile": config.ssl_keyfile,
        }

    scheme = "https" if config.ssl_enabled else "http"
    print("Junos Commit Watcher UI on %s://%s:%d" % (scheme, config.host, config.port))
    uvicorn.run(app, host=config.host, port=config.port, **ssl_kwargs)


if __name__ == "__main__":
    main()
