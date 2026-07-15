"""MCP server for JobAICrawler.

Exposes tools that a Claude Code routine (or the desktop app) can call to:

- Load one or more JSON files of job opportunities into the import staging
  area (``import_file`` / ``import_files`` / ``staging_status``), reusing the
  application's own ``parse_jobs`` / ``stage_jobs`` logic.
- Retrieve job URLs a user queued in the web UI (``find_pending_urls``) so the
  client can validate them and prepare a file to import.
- Retrieve job offers from the database (``find_jobs``) and change their
  state (``update_job_status`` — open/closed, set saved/applied).
- Read and manage the target companies and roles to focus searches on
  (``list_targets`` / ``add_target`` / ``rename_target`` / ``remove_target``).
- Suggest new targets for the user to review (``suggest_targets``).

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

from app import routes_targets
from app.routes_import import STATUS_UNPROCESSED, parse_jobs, stage_jobs
from app.routes_jobs import USER_STATUSES

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

# Shared with the web app so the two agree on what the user can mark a job as.
VALID_USER_STATUS = set(USER_STATUSES)
_USER_STATUS_CHOICES = ", ".join(USER_STATUSES)


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
        "user_status": doc.get("user_status"),
        "status": doc.get("status"),
        "state": doc.get("state") or "open",
    }


def _build_job_filter(
    query: Optional[str],
    company: Optional[str],
    keyword: Optional[str],
    state: Optional[str],
    user_status: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the Mongo filter shared by find/count."""
    filt: Dict[str, Any] = {}
    if state == "open":
        filt["state"] = {"$ne": "closed"}
    elif state == "closed":
        filt["state"] = "closed"
    # Mirrors the web list's filter. An unrecognised value is ignored rather
    # than matched literally, so a typo returns everything, not nothing.
    if user_status in VALID_USER_STATUS:
        filt["user_status"] = user_status
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
    state: Optional[str] = None,
    user_status: Optional[str] = None,
) -> int:
    """Count job offers matching the filters (for pagination)."""
    return db.jobs.count_documents(
        _build_job_filter(query, company, keyword, state, user_status)
    )


def find_jobs_in_db(
    db,
    query: Optional[str] = None,
    company: Optional[str] = None,
    keyword: Optional[str] = None,
    state: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    user_status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Retrieve one page of job offers from the database with optional filters.

    - ``query``: case-insensitive substring across title, keywords, description.
    - ``company``: case-insensitive substring on company.
    - ``keyword``: exact (case-insensitive) match against the keywords array.
    - ``state``: 'open' or 'closed' to narrow by state (default: all).
    - ``user_status``: 'radar', 'saved' or 'applied' to narrow by how the user
      marked the job (default: all).
    - ``page``: 1-based page number.
    - ``limit``: page size (1-100), newest first.
    """
    filt = _build_job_filter(query, company, keyword, state, user_status)
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
    state: Optional[str] = None,
    user_status: Optional[str] = None,
) -> Dict[str, Any]:
    """Change a job's state (open/closed) and/or set user_status.

    ``state`` accepts 'open' or 'closed'. ``user_status`` accepts 'radar',
    'saved', 'applied', or 'none'/'clear' (to unset); the values are mutually
    exclusive, so setting one replaces the last. Raises ValueError / InvalidId
    on bad input or missing job.
    """
    oid = ObjectId(job_id)  # raises InvalidId for malformed ids

    update: Dict[str, Any] = {}
    if state is not None:
        normalized_state = str(state).strip().lower()
        if normalized_state not in {"open", "closed"}:
            raise ValueError("state must be one of: open, closed")
        update["state"] = normalized_state
    if user_status is not None:
        normalized = str(user_status).strip().lower()
        if normalized in {"none", "", "clear"}:
            update["user_status"] = None
        elif normalized in VALID_USER_STATUS:
            update["user_status"] = normalized
        else:
            raise ValueError(
                f"user_status must be one of: {_USER_STATUS_CHOICES}, none"
            )

    if not update:
        raise ValueError("Nothing to update: provide state and/or user_status.")

    result = db.jobs.update_one({"_id": oid}, {"$set": update})
    if result.matched_count == 0:
        raise ValueError("Job not found.")
    return _job_summary(db.jobs.find_one({"_id": oid}))


@mcp.tool()
def find_jobs(
    query: str = "",
    company: str = "",
    keyword: str = "",
    state: str = "",
    page: int = 1,
    limit: int = 20,
    user_status: str = "",
) -> Dict[str, Any]:
    """Retrieve a page of job offers from the database.

    Args:
        query: Case-insensitive text to match in title, keywords or description.
        company: Case-insensitive company filter.
        keyword: Exact keyword (from the keywords array) to filter by.
        state: 'open' or 'closed' to narrow by state (default: all states).
        page: 1-based page number (use with total_pages / has_more to iterate).
        limit: Page size (1-100), newest first.
        user_status: How the user marked the job — 'radar' (on my radar),
            'saved' or 'applied' (default: all).

    Returns the page of jobs plus pagination metadata (total, page, limit,
    total_pages, has_more). Each job has an id to use with update_job_status.
    """
    try:
        db = _get_db()
        opts = dict(
            query=query or None,
            company=company or None,
            keyword=keyword or None,
            state=state or None,
            user_status=user_status or None,
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
    state: Optional[str] = None,
    user_status: Optional[str] = None,
) -> Dict[str, Any]:
    """Change a job offer's state and/or user_status by id.

    Args:
        job_id: The job's id (from find_jobs).
        state: 'closed' to close (mark no longer open), 'open' to reopen.
        user_status: 'radar' (on my radar), 'saved', 'applied', or 'none' to
            clear. Mutually exclusive — setting one replaces the last. Optional.

    At least one of state / user_status must be provided. Returns the
    updated job summary.
    """
    try:
        db = _get_db()
        return {"job": update_job_status_in_db(db, job_id, state, user_status)}
    except InvalidId:
        return {"error": "invalid job_id"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# ── Target companies & roles ─────────────────────────────────────────


@mcp.tool()
def list_targets() -> Dict[str, Any]:
    """List the targets to focus searches on.

    Returns ``{"companies", "roles", "search_sites", "factors"}`` — each a list
    of entries with an ``id`` and ``name``. Use these to drive which companies,
    roles and sites to search, and which other factors to weigh.
    """
    try:
        db = _get_db()
        return routes_targets.list_targets(db)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def add_target(kind: str, name: str) -> Dict[str, Any]:
    """Add a target company, role, search site or other relevant factor.

    Args:
        kind: 'company', 'role', 'search_site' or 'factor'.
        name: The value to add (case-insensitive duplicates are skipped).

    Returns the target with an ``added`` flag (False if it already existed).
    """
    try:
        db = _get_db()
        return {"target": routes_targets.add_target(db, kind, name)}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def rename_target(target_id: str, name: str) -> Dict[str, Any]:
    """Rename a target by its id (from list_targets).

    Skips the rename if another target of the same kind already has that name.
    """
    try:
        db = _get_db()
        return {"target": routes_targets.rename_target(db, target_id, name)}
    except InvalidId:
        return {"error": "invalid target_id"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def remove_target(target_id: str) -> Dict[str, Any]:
    """Remove a target company or role by its id (from list_targets)."""
    try:
        db = _get_db()
        return {"removed": routes_targets.remove_target(db, target_id)}
    except InvalidId:
        return {"error": "invalid target_id"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@mcp.tool()
def suggest_targets(suggestions: List[Dict[str, str]]) -> Dict[str, Any]:
    """Suggest new targets for the user to review on the Targets page.

    Use this to propose companies, roles, search sites or factors similar to the
    ones already targeted. The user then adds or discards each on the page.

    Args:
        suggestions: list of {"kind": ..., "name": ...} where kind is 'company',
            'role', 'search_site' or 'factor'. Suggestions duplicating an
            existing target or suggestion are skipped.

    Returns {"added", "skipped", "invalid"}.
    """
    try:
        db = _get_db()
        return routes_targets.add_suggestions(db, suggestions)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    mcp.run()
