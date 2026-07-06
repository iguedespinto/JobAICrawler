"""MCP server for JobAICrawler.

Exposes tools that a Claude Code routine (or the desktop app) can call to:

- Load one or more JSON files of job opportunities into the import staging
  area (``import_file`` / ``import_files`` / ``staging_status``), reusing the
  application's own ``parse_jobs`` / ``stage_jobs`` logic.
- Retrieve job URLs a user queued in the web UI (``find_pending_urls``) so the
  client can validate them and prepare a file to import.
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

from app.routes_import import STATUS_UNPROCESSED, parse_jobs, stage_jobs

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


# ── Retrieve pending URLs to process ─────────────────────────────────

# Bare URLs queued in the web UI are stored in the staging area as
# ``unprocessed`` records (see app/routes_import.py). This server hands them to
# a client that validates each URL, confirms the job is still open and writes a
# JSON file with the full fields. Importing that file (import_file) fills in the
# matching record and promotes it out of ``unprocessed`` — so a processed URL
# naturally stops appearing here; no explicit "mark done" call is required.


def _pending_url_summary(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialise a pending URL staging record into a plain summary."""
    staged_at = doc.get("staged_at")
    return {
        "id": str(doc.get("_id")),
        "url": doc.get("url"),
        "source": doc.get("source_file"),
        "staged_at": staged_at.isoformat() if staged_at is not None else None,
    }


def count_pending_urls_in_db(db) -> int:
    """Count URLs queued for processing (for pagination)."""
    return db.import_staging.count_documents({"status": STATUS_UNPROCESSED})


def find_pending_urls_in_db(
    db, page: int = 1, limit: int = 50
) -> List[Dict[str, Any]]:
    """Retrieve one page of pending (unprocessed) URLs, oldest first."""
    limit = max(1, min(int(limit), 200))
    page = max(1, int(page))
    skip = (page - 1) * limit
    # Tie-break on _id so paging is a stable total order even when several URLs
    # share a staged_at timestamp (they are inserted in a single batch).
    cursor = (
        db.import_staging.find({"status": STATUS_UNPROCESSED})
        .sort([("staged_at", 1), ("_id", 1)])
        .skip(skip)
        .limit(limit)
    )
    return [_pending_url_summary(doc) for doc in cursor]


@mcp.tool()
def find_pending_urls(page: int = 1, limit: int = 50) -> Dict[str, Any]:
    """Retrieve a page of job URLs queued in the web UI for processing.

    These are bare URLs a user pre-staged: validate each one, confirm the job is
    still open, and write a JSON import file with the full fields. Importing that
    file (import_file) promotes each matching URL into a viewable opportunity and
    removes it from this queue.

    Args:
        page: 1-based page number (use with total_pages / has_more to iterate).
        limit: Page size (1-200), oldest first.

    Returns the page of URLs plus pagination metadata (total, page, limit,
    total_pages, has_more). Each item has id, url, source and staged_at.
    """
    try:
        db = _get_db()
        total = count_pending_urls_in_db(db)
        urls = find_pending_urls_in_db(db, page=page, limit=limit)

        limit_c = max(1, min(int(limit), 200))
        page_c = max(1, int(page))
        total_pages = (total + limit_c - 1) // limit_c if total else 0
        return {
            "urls": urls,
            "count": len(urls),
            "total": total,
            "page": page_c,
            "limit": limit_c,
            "total_pages": total_pages,
            "has_more": page_c < total_pages,
        }
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


def _build_job_filter(
    query: Optional[str],
    company: Optional[str],
    keyword: Optional[str],
    include_archived: bool,
) -> Dict[str, Any]:
    """Build the Mongo filter shared by find/count."""
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
    return filt


def count_jobs_in_db(
    db,
    query: Optional[str] = None,
    company: Optional[str] = None,
    keyword: Optional[str] = None,
    include_archived: bool = False,
) -> int:
    """Count job offers matching the filters (for pagination)."""
    return db.jobs.count_documents(
        _build_job_filter(query, company, keyword, include_archived)
    )


def find_jobs_in_db(
    db,
    query: Optional[str] = None,
    company: Optional[str] = None,
    keyword: Optional[str] = None,
    include_archived: bool = False,
    page: int = 1,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Retrieve one page of job offers from the database with optional filters.

    - ``query``: case-insensitive substring across title, keywords, description.
    - ``company``: case-insensitive substring on company.
    - ``keyword``: exact (case-insensitive) match against the keywords array.
    - ``include_archived``: include archived jobs (default: active only).
    - ``page``: 1-based page number.
    - ``limit``: page size (1-100), newest first.
    """
    filt = _build_job_filter(query, company, keyword, include_archived)
    limit = max(1, min(int(limit), 100))
    page = max(1, int(page))
    skip = (page - 1) * limit
    # Tie-break on _id so the ordering is a stable total order across pages;
    # sorting by created_at alone (non-unique / sometimes missing) would let
    # skip/limit return overlapping or missing rows between pages.
    cursor = (
        db.jobs.find(filt)
        .sort([("created_at", -1), ("_id", -1)])
        .skip(skip)
        .limit(limit)
    )
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
    page: int = 1,
    limit: int = 20,
) -> Dict[str, Any]:
    """Retrieve a page of job offers from the database.

    Args:
        query: Case-insensitive text to match in title, keywords or description.
        company: Case-insensitive company filter.
        keyword: Exact keyword (from the keywords array) to filter by.
        include_archived: Include archived jobs (default: active only).
        page: 1-based page number (use with total_pages / has_more to iterate).
        limit: Page size (1-100), newest first.

    Returns the page of jobs plus pagination metadata (total, page, limit,
    total_pages, has_more). Each job has an id to use with update_job_status.
    """
    try:
        db = _get_db()
        opts = dict(
            query=query or None,
            company=company or None,
            keyword=keyword or None,
            include_archived=include_archived,
        )
        total = count_jobs_in_db(db, **opts)
        jobs = find_jobs_in_db(db, page=page, limit=limit, **opts)

        limit_c = max(1, min(int(limit), 100))
        page_c = max(1, int(page))
        total_pages = (total + limit_c - 1) // limit_c if total else 0
        return {
            "jobs": jobs,
            "count": len(jobs),
            "total": total,
            "page": page_c,
            "limit": limit_c,
            "total_pages": total_pages,
            "has_more": page_c < total_pages,
        }
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
