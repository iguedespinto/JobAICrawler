"""MCP server for JobAICrawler: import opportunity files into the staging area.

Exposes tools that a Claude Code routine can call to load one or more JSON
files of job opportunities into the same import staging area that the web UI
reviews (the ``import_staging`` MongoDB collection). Parsing, deduping and
matching reuse the application's own logic (``parse_jobs`` / ``stage_jobs``).

Run directly for stdio transport (how Claude Code launches it):

    python mcp_server.py
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from dotenv import load_dotenv
from pymongo import MongoClient

from app.routes_import import parse_jobs, stage_jobs

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - dependency hint
    raise SystemExit(
        "The 'mcp' package is required. Install it with: pip install -r requirements-mcp.txt"
    ) from exc

# Load .env next to this file, regardless of the launcher's working directory.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

mcp = FastMCP("jobaicrawler-import")


def _get_db():
    """Connect to MongoDB using the same env vars as the web app."""
    uri = os.getenv("MONGODB_URI", "") or os.getenv("MONGO_URI", "")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set.")
    db_name = os.getenv("MONGO_DB_NAME", "jobs_db")
    return MongoClient(uri, serverSelectionTimeoutMS=8000)[db_name]


def import_file_to_staging(path: str, db=None) -> Dict[str, Any]:
    """Parse a JSON file and load its opportunities into the staging area.

    Returns a summary: parsed count, added, skipped (already staged), and the
    new total in staging. Raises FileNotFoundError / ValueError on bad input.
    """
    with open(path, "r", encoding="utf-8") as handle:
        raw_text = handle.read()
    jobs = parse_jobs(raw_text)
    db = db if db is not None else _get_db()
    result = stage_jobs(db, jobs, source=os.path.basename(path))
    return {
        "file": path,
        "parsed": len(jobs),
        "added": result["added"],
        "skipped": result["skipped"],
        "staged_total": db.import_staging.count_documents({}),
    }


@mcp.tool()
def import_file(path: str) -> Dict[str, Any]:
    """Import a single JSON file of job opportunities into the staging area.

    Args:
        path: Absolute or relative path to a JSON file (array of objects with
            name/company/url/salary/description/keywords, or a single object).

    Returns a summary with parsed/added/skipped counts and the staging total.
    """
    try:
        return import_file_to_staging(path)
    except Exception as exc:  # surface a clean error to the caller
        return {"file": path, "error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def import_files(paths: List[str]) -> Dict[str, Any]:
    """Import several JSON files into the staging area in one call.

    Args:
        paths: List of file paths to import, in order.

    Returns per-file results plus combined totals.
    """
    db = None
    try:
        db = _get_db()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    results = []
    total_added = 0
    total_skipped = 0
    for path in paths:
        try:
            res = import_file_to_staging(path, db=db)
        except Exception as exc:
            res = {"file": path, "error": f"{type(exc).__name__}: {exc}"}
        results.append(res)
        total_added += res.get("added", 0)
        total_skipped += res.get("skipped", 0)

    return {
        "files": results,
        "total_added": total_added,
        "total_skipped": total_skipped,
        "staged_total": db.import_staging.count_documents({}),
    }


@mcp.tool()
def staging_status() -> Dict[str, Any]:
    """Report how many opportunities are currently in the import staging area."""
    try:
        db = _get_db()
        return {"staged_total": db.import_staging.count_documents({})}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    mcp.run()
