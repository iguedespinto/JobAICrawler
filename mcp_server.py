"""MCP server for JobAICrawler.

Exposes tools that a Claude Code routine (or the desktop app) can call to:

- Load one or more JSON files of job opportunities into the import staging
  area (``import_file`` / ``import_files`` / ``staging_status``), reusing the
  application's own ``parse_jobs`` / ``stage_jobs`` logic.
- Retrieve job offers from the database (``find_jobs``) and change their
  status (``update_job_status`` — archive/unarchive, set saved/applied).

Run directly for stdio transport (how Claude Code launches it):

    python mcp_server.py
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from bson import ObjectId
from bson.errors import InvalidId
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


# ── Retrieve job offers & change their status ────────────────────────

VALID_USER_STATUS = {"saved", "applied"}


def _job_summary(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialise a stored job into a plain, id-addressable summary."""
    return {
        "id": str(doc.get("_id")),
        "title": doc.get("title"),
        "company": doc.get("company"),
        "location": doc.get("location"),
        "url": doc.get("url"),
        "salary": doc.get("salary"),
        "keywords": doc.get("keywords", []),
        "archived": bool(doc.get("archived")),
        "user_status": doc.get("user_status"),
        "status": doc.get("status"),
    }


def find_jobs_in_db(
    db,
    query: Optional[str] = None,
    company: Optional[str] = None,
    keyword: Optional[str] = None,
    include_archived: bool = False,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Retrieve job offers from the database with optional filters.

    - ``query``: case-insensitive substring across title, keywords, description.
    - ``company``: case-insensitive substring on company.
    - ``keyword``: exact (case-insensitive) match against the keywords array.
    - ``include_archived``: include archived jobs (default: active only).
    - ``limit``: max results (1-100), newest first.
    """
    filt: Dict[str, Any] = {}
    if not include_archived:
        filt["archived"] = {"$ne": True}
    if company:
        filt["company"] = {"$regex": re.escape(company), "$options": "i"}
    if keyword:
        filt["keywords"] = {"$regex": f"^{re.escape(keyword)}$", "$options": "i"}
    if query:
        regex = {"$regex": re.escape(query), "$options": "i"}
        filt["$or"] = [
            {"title": regex},
            {"keywords": regex},
            {"description_text": regex},
        ]

    limit = max(1, min(int(limit), 100))
    cursor = db.jobs.find(filt).sort([("created_at", -1)]).limit(limit)
    return [_job_summary(doc) for doc in cursor]


def update_job_status_in_db(
    db,
    job_id: str,
    archived: Optional[bool] = None,
    user_status: Optional[str] = None,
) -> Dict[str, Any]:
    """Change a job's status: archive/unarchive and/or set user_status.

    ``user_status`` accepts 'saved', 'applied', or 'none'/'clear' (to unset).
    Raises ValueError / InvalidId on bad input or missing job.
    """
    oid = ObjectId(job_id)  # raises InvalidId for malformed ids

    update: Dict[str, Any] = {}
    if archived is not None:
        update["archived"] = bool(archived)
    if user_status is not None:
        normalized = str(user_status).strip().lower()
        if normalized in {"none", "", "clear"}:
            update["user_status"] = None
        elif normalized in VALID_USER_STATUS:
            update["user_status"] = normalized
        else:
            raise ValueError("user_status must be one of: saved, applied, none")

    if not update:
        raise ValueError("Nothing to update: provide archived and/or user_status.")

    result = db.jobs.update_one({"_id": oid}, {"$set": update})
    if result.matched_count == 0:
        raise ValueError("Job not found.")
    return _job_summary(db.jobs.find_one({"_id": oid}))


@mcp.tool()
def find_jobs(
    query: str = "",
    company: str = "",
    keyword: str = "",
    include_archived: bool = False,
    limit: int = 20,
) -> Dict[str, Any]:
    """Retrieve job offers from the database.

    Args:
        query: Case-insensitive text to match in title, keywords or description.
        company: Case-insensitive company filter.
        keyword: Exact keyword (from the keywords array) to filter by.
        include_archived: Include archived jobs (default: active only).
        limit: Maximum number of results (1-100), newest first.

    Returns a list of jobs, each with its id, fields, archived flag and
    user_status. Use the id with update_job_status to change a job's status.
    """
    try:
        db = _get_db()
        jobs = find_jobs_in_db(
            db,
            query=query or None,
            company=company or None,
            keyword=keyword or None,
            include_archived=include_archived,
            limit=limit,
        )
        return {"count": len(jobs), "jobs": jobs}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def update_job_status(
    job_id: str,
    archived: Optional[bool] = None,
    user_status: Optional[str] = None,
) -> Dict[str, Any]:
    """Change a job offer's status by id.

    Args:
        job_id: The job's id (from find_jobs).
        archived: True to archive (mark inactive), False to unarchive.
        user_status: 'saved', 'applied', or 'none' to clear. Optional.

    At least one of archived / user_status must be provided. Returns the
    updated job summary.
    """
    try:
        db = _get_db()
        return {"job": update_job_status_in_db(db, job_id, archived, user_status)}
    except InvalidId:
        return {"error": "invalid job_id"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    mcp.run()
