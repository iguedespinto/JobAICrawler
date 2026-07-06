"""Routes for importing externally-prepared job opportunities from JSON files.

Loaded records persist in a staging area (the ``import_staging`` collection)
until they are imported or cleared, and accumulate across uploads:

1. ``GET  /import``        - show the upload form and the current staging area,
   each staged record matched against active jobs and other staged records.
2. ``POST /import/upload`` - parse an uploaded JSON file and add its records to
   the staging area (skipping ones already staged).
3. ``POST /import/commit`` - create the selected staged opportunities and remove
   them from staging.
4. ``POST /import/clear``  - empty the staging area.

``parse_jobs`` and ``stage_jobs`` are the reusable building blocks for loading
files into staging, intended to be shared with a future MCP server that loads
one or more files.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from bson import ObjectId
from bson.errors import InvalidId
from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import get_db
from .crawlers.base import BaseCrawler

import_bp = Blueprint("import_jobs", __name__, url_prefix="/import")

IMPORT_SITE = "import"
STATUS_IMPORTED = "imported"

# Staging-record lifecycle. A record enters as a bare URL (``unprocessed``) that
# an MCP client retrieves, enriches and imports; importing a matching file then
# fills in the remaining fields and promotes it to ``staged`` (viewable and
# committable). Records loaded straight from a file skip ``unprocessed`` and are
# ``staged`` from the start. Existing records predate this field and are treated
# as ``staged`` (the view filter only ever excludes ``unprocessed``).
STATUS_UNPROCESSED = "unprocessed"
STATUS_STAGED = "staged"

# A row whose description is at least this similar (percent) to an active job is
# treated as a likely duplicate/match rather than a new opportunity.
SIMILARITY_THRESHOLD = 85

# Common words carry little signal and only inflate description similarity.
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is",
    "it", "of", "on", "or", "our", "the", "to", "with", "you", "your", "we",
    "will", "this", "that", "have", "has", "they", "their", "but", "not", "all",
    "can", "who", "what", "which", "into", "across", "within", "including",
    "role", "team", "work", "working", "experience", "years", "skills",
    "responsibilities", "requirements", "qualifications", "join", "seeks",
}
_TOKEN = re.compile(r"[a-z0-9]+")

# Mapping of incoming JSON keys -> stored field names. The external files use
# ``name`` for the job title; everything else lines up with our schema.
_WHITESPACE = re.compile(r"\s+")

# Some sources can't resolve a per-posting link and fall back to a search/results
# page URL (e.g. Indeed's ``/q-...-jobs.html``). Such a URL is shared by many
# distinct jobs, so it is NOT a reliable identity and must not drive dedupe.
_SEARCH_URL_PATTERNS = [
    re.compile(r"/q-[^/]*-jobs\.html"),  # Indeed query-results page
    re.compile(r"-jobs\.html(?:$|[?#])"),  # other "...-jobs.html" listings
    re.compile(r"[?&]q="),  # generic search query parameter
    re.compile(r"/jobs/search"),  # LinkedIn / Indeed search listing
    re.compile(r"/search(?:[/?]|$)"),  # generic search path
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_db_or_error() -> Tuple[object, Optional[tuple]]:
    """Return the database or a 503 response."""
    db = get_db()
    if db is None:
        return None, (jsonify({"error": "database not configured"}), 503)
    return db, None


def _normalize_url(value: Any) -> str:
    """Normalize a URL for comparison: trimmed, lowercased, no trailing slash."""
    if not value:
        return ""
    text = str(value).strip().lower()
    return text.rstrip("/")


def _is_search_url(value: Any) -> bool:
    """Return True if the URL is a search/results page rather than a posting."""
    text = _normalize_url(value)
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SEARCH_URL_PATTERNS)


def _identifying_url(value: Any) -> str:
    """Normalized URL to use as an identity, or "" if it does not identify a job.

    Search/results-page URLs are shared across many postings, so they are
    treated as non-identifying: matching then falls back to title + company.
    """
    if _is_search_url(value):
        return ""
    return _normalize_url(value)


def _is_valid_url(value: Any) -> bool:
    """Return True if the value is a well-formed http(s) URL with a host."""
    text = str(value or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _description_vector(text: Any) -> Tuple[Counter, float]:
    """Build a term-frequency vector and its norm for cosine similarity."""
    tokens = [
        tok
        for tok in _TOKEN.findall(str(text or "").lower())
        if len(tok) > 1 and tok not in _STOPWORDS
    ]
    counts = Counter(tokens)
    norm = math.sqrt(sum(count * count for count in counts.values()))
    return counts, norm


def _cosine_percent(
    vec_a: Counter, norm_a: float, vec_b: Counter, norm_b: float
) -> int:
    """Cosine similarity of two TF vectors, as an integer percentage (0-100)."""
    if norm_a == 0 or norm_b == 0:
        return 0
    # Iterate the smaller vector for the dot product.
    if len(vec_a) > len(vec_b):
        vec_a, vec_b = vec_b, vec_a
    dot = sum(count * vec_b.get(term, 0) for term, count in vec_a.items())
    return round(100 * dot / (norm_a * norm_b))


def _normalize_text(value: Any) -> str:
    """Normalize free text for comparison: trimmed, lowercased, single spaces."""
    if not value:
        return ""
    return _WHITESPACE.sub(" ", str(value).strip().lower())


def _title_company_key(title: Any, company: Any) -> str:
    """Build a comparison key from a job title and company."""
    return f"{_normalize_text(title)}|{_normalize_text(company)}"


def _as_keywords(value: Any) -> List[str]:
    """Coerce a keywords value into a clean list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def parse_jobs(raw_text: str) -> List[Dict[str, Any]]:
    """Parse uploaded JSON text into normalized opportunity dictionaries.

    Accepts either a JSON array of objects or a single object. Each object may
    use ``name`` (preferred) or ``title`` for the job title. Entries that are
    not objects are skipped.
    """
    data = json.loads(raw_text)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array of job objects.")

    jobs: List[Dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        title = (entry.get("name") or entry.get("title") or "").strip()
        url = (entry.get("url") or "").strip()
        if not title and not url:
            continue
        jobs.append(
            {
                "title": title,
                "company": (entry.get("company") or "").strip(),
                "location": (entry.get("location") or "").strip(),
                "url": url,
                "salary": (entry.get("salary") or "").strip(),
                "keywords": _as_keywords(entry.get("keywords")),
                "description_text": (entry.get("description") or "").strip(),
            }
        )
    return jobs


def _job_label(title: Any, company: Any) -> str:
    """Human-readable label for a candidate job."""
    title = (str(title or "").strip()) or "(untitled)"
    company = str(company or "").strip()
    return f"{title} @ {company}" if company else title


def _make_candidate(
    title: Any, company: Any, url: Any, description: Any
) -> Dict[str, Any]:
    """Build a comparison candidate (keys + description vector + display fields)."""
    vec, norm = _description_vector(description)
    return {
        "url": _identifying_url(url),
        "raw_url": (str(url).strip() if url else ""),
        "tc": _title_company_key(title, company),
        "label": _job_label(title, company),
        "vec": vec,
        "norm": norm,
    }


def _active_candidates(db) -> List[Dict[str, Any]]:
    """Comparison candidates from active (non-archived) jobs in the database."""
    return [
        _make_candidate(
            doc.get("title"), doc.get("company"), doc.get("url"),
            doc.get("description_text"),
        )
        for doc in db.jobs.find({"archived": {"$ne": True}})
    ]


def _best_similarity(
    vec: Counter, norm: float, candidates: List[Dict[str, Any]]
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Best description-similarity percent (and candidate) over a list."""
    best_pct = 0
    best_cand: Optional[Dict[str, Any]] = None
    for cand in candidates:
        pct = _cosine_percent(vec, norm, cand["vec"], cand["norm"])
        if pct > best_pct:
            best_pct, best_cand = pct, cand
    return best_pct, best_cand


def match_jobs(jobs: List[Dict[str, Any]], db) -> List[Dict[str, Any]]:
    """Annotate each parsed job with how it matches active/earlier opportunities.

    Each incoming job is compared against active (non-archived) jobs already in
    the database AND against earlier jobs in the same upload. Precedence:
      1. ``url``                - same identifying URL.
      2. ``title_company``      - same normalized title + company.
      3. ``similar_description``- description >= SIMILARITY_THRESHOLD vs a DB job.
      4. ``duplicate_in_file`` / ``similar_in_file`` - matches an earlier row.
      5. ``new``                - no match; a brand-new opportunity.

    Every row carries the best similarity percent found, so even ``new`` rows
    show how close their nearest active match is.
    """
    db_candidates = _active_candidates(db)
    db_urls = {c["url"]: c for c in db_candidates if c["url"]}
    db_tc = {c["tc"]: c for c in db_candidates}
    seen: List[Dict[str, Any]] = []
    seen_urls: Dict[str, Dict[str, Any]] = {}
    seen_tc: Dict[str, Dict[str, Any]] = {}

    rows: List[Dict[str, Any]] = []
    for job in jobs:
        url_key = _identifying_url(job.get("url"))
        tc_key = _title_company_key(job.get("title"), job.get("company"))
        vec, norm = _description_vector(job.get("description_text"))

        status = "new"
        reason: Optional[str] = None
        similarity = 0
        match: Optional[Dict[str, Any]] = None

        if url_key and url_key in db_urls:
            status, reason, similarity, match = (
                "matched", "url", 100, db_urls[url_key],
            )
        elif tc_key in db_tc:
            status, reason, similarity, match = (
                "matched", "title_company", 100, db_tc[tc_key],
            )
        elif url_key and url_key in seen_urls:
            status, reason, similarity, match = (
                "duplicate", "duplicate_in_file", 100, seen_urls[url_key],
            )
        elif tc_key in seen_tc:
            status, reason, similarity, match = (
                "duplicate", "duplicate_in_file", 100, seen_tc[tc_key],
            )
        else:
            db_pct, db_cand = _best_similarity(vec, norm, db_candidates)
            file_pct, file_cand = _best_similarity(vec, norm, seen)
            if db_pct >= file_pct:
                similarity, match = db_pct, db_cand
                if db_pct >= SIMILARITY_THRESHOLD:
                    status, reason = "matched", "similar_description"
            else:
                similarity, match = file_pct, file_cand
                if file_pct >= SIMILARITY_THRESHOLD:
                    status, reason = "duplicate", "similar_in_file"

        candidate = _make_candidate(
            job.get("title"), job.get("company"), job.get("url"),
            job.get("description_text"),
        )
        seen.append(candidate)
        if url_key:
            seen_urls.setdefault(url_key, candidate)
        seen_tc.setdefault(tc_key, candidate)

        rows.append(
            {
                "job": job,
                "status": status,
                "reason": reason,
                "similarity": similarity,
                "match_label": match["label"] if match else None,
                "match_url": match["raw_url"] if match else None,
            }
        )
    return rows


def _staging_identity(job: Dict[str, Any]) -> str:
    """Identity used to avoid staging the same opportunity twice."""
    return (
        _identifying_url(job.get("url"))
        or _title_company_key(job.get("title"), job.get("company"))
    )


def _staged_fields(job: Dict[str, Any], source: Optional[str], now: datetime) -> Dict[str, Any]:
    """The stored representation of a fully-described staged opportunity."""
    return {
        "title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "url": job.get("url"),
        "salary": job.get("salary"),
        "keywords": job.get("keywords", []),
        "description_text": job.get("description_text"),
        "source_file": source,
        "status": STATUS_STAGED,
        "staged_at": now,
    }


def stage_jobs(db, jobs: List[Dict[str, Any]], source: Optional[str] = None) -> Dict[str, int]:
    """Append parsed opportunities to the staging area, skipping ones already staged.

    This is the reusable entry point for loading files into the import staging
    area. It is called by the HTTP upload route and is reused by the MCP server
    that loads one or more files. Returns ``{"added", "skipped"}``.

    A job whose URL matches a pending ``unprocessed`` URL record (pre-staged via
    :func:`stage_urls`) fills in that same record's fields and promotes it to
    ``staged`` instead of inserting a new row — that is how a retrieved URL turns
    into a viewable opportunity. Such promotions count towards ``added``.
    """
    existing: set = set()
    pending_by_url: Dict[str, Dict[str, Any]] = {}
    for doc in db.import_staging.find({}):
        if doc.get("status") == STATUS_UNPROCESSED:
            url_key = _identifying_url(doc.get("url"))
            if url_key:
                pending_by_url[url_key] = doc
        else:
            existing.add(_staging_identity(doc))

    now = _utcnow()
    to_insert: List[Dict[str, Any]] = []
    added = 0
    skipped = 0

    for job in jobs:
        identity = _staging_identity(job)
        if not identity:
            skipped += 1
            continue

        url_key = _identifying_url(job.get("url"))
        pending = pending_by_url.pop(url_key, None) if url_key else None
        if pending is not None:
            # Promote the pre-staged URL in place: fill in its fields and mark it
            # viewable. The now-complete record dedupes future uploads.
            db.import_staging.update_one(
                {"_id": pending["_id"]},
                {"$set": _staged_fields(job, source, now)},
            )
            existing.add(identity)
            added += 1
            continue

        if identity in existing:
            skipped += 1
            continue
        existing.add(identity)
        to_insert.append(_staged_fields(job, source, now))

    if to_insert:
        db.import_staging.insert_many(to_insert)
    added += len(to_insert)
    return {"added": added, "skipped": skipped}


def parse_urls(raw_text: str) -> List[str]:
    """Split pasted text into individual URL tokens (whitespace-separated)."""
    return [token for token in str(raw_text or "").split() if token]


def stage_urls(db, urls: List[str], source: Optional[str] = None) -> Dict[str, int]:
    """Pre-stage bare job URLs as ``unprocessed`` records for later enrichment.

    Each URL is checked against active (non-archived) imported jobs and against
    every staging record (pending or staged); only genuinely new, identifying
    URLs are inserted. An MCP client retrieves these via ``find_pending_urls``,
    enriches them and imports a file, which promotes them to ``staged``.

    Returns ``{"added", "skipped", "invalid"}`` — skipped: already known;
    invalid: malformed or a non-identifying search/results URL.
    """
    job_urls = {
        key
        for doc in db.jobs.find({"archived": {"$ne": True}})
        if (key := _identifying_url(doc.get("url")))
    }
    staged_urls = {
        key
        for doc in db.import_staging.find({})
        if (key := _identifying_url(doc.get("url")))
    }
    now = _utcnow()
    to_insert: List[Dict[str, Any]] = []
    seen: set = set()
    added = skipped = invalid = 0

    for raw in urls:
        text = str(raw or "").strip()
        if not _is_valid_url(text):
            invalid += 1
            continue
        key = _identifying_url(text)
        if not key:
            # A search/results-page URL is not a single posting to process.
            invalid += 1
            continue
        if key in job_urls or key in staged_urls or key in seen:
            skipped += 1
            continue
        seen.add(key)
        to_insert.append(
            {
                "url": text,
                "status": STATUS_UNPROCESSED,
                "source_file": source,
                "staged_at": now,
            }
        )
        added += 1

    if to_insert:
        db.import_staging.insert_many(to_insert)
    return {"added": added, "skipped": skipped, "invalid": invalid}


def _staged_rows(db) -> List[Dict[str, Any]]:
    """Load viewable staged opportunities and annotate each with its match.

    Pending ``unprocessed`` URL records are pre-staging only: they carry no
    fields yet and are not offered for import, so they are excluded here.
    """
    staged = list(
        db.import_staging.find({"status": {"$ne": STATUS_UNPROCESSED}}).sort(
            [("staged_at", 1)]
        )
    )
    rows = match_jobs(staged, db)
    for row in rows:
        row["id"] = str(row["job"]["_id"])
    return rows


def _pending_urls(db) -> List[Dict[str, Any]]:
    """List the pre-staged ``unprocessed`` URLs awaiting enrichment."""
    docs = db.import_staging.find({"status": STATUS_UNPROCESSED}).sort(
        [("staged_at", 1)]
    )
    return [
        {
            "id": str(doc["_id"]),
            "url": doc.get("url"),
            "source": doc.get("source_file"),
            "staged_at": doc.get("staged_at"),
        }
        for doc in docs
    ]


@import_bp.route("", methods=["GET"])
def import_form():
    """Show the upload form and the current staging area."""
    db, error = _get_db_or_error()
    if error:
        return error

    rows = _staged_rows(db)
    pending = _pending_urls(db)
    summary = {
        "total": len(rows),
        "new": sum(1 for r in rows if r["status"] == "new"),
        "matched": sum(1 for r in rows if r["status"] == "matched"),
        "duplicate": sum(1 for r in rows if r["status"] == "duplicate"),
        "pending": len(pending),
        "threshold": SIMILARITY_THRESHOLD,
    }
    return render_template(
        "import.html", rows=rows, summary=summary, pending=pending
    )


@import_bp.route("/upload", methods=["POST"])
def upload():
    """Parse an uploaded JSON file and add its opportunities to the staging area."""
    db, error = _get_db_or_error()
    if error:
        return error

    upload_file = request.files.get("import_file")
    if upload_file is None or not upload_file.filename:
        flash("Please choose a JSON file to import.")
        return redirect(url_for("import_jobs.import_form"))

    try:
        raw_text = upload_file.read().decode("utf-8")
        jobs = parse_jobs(raw_text)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        flash(f"Could not read JSON file: {exc}")
        return redirect(url_for("import_jobs.import_form"))

    if not jobs:
        flash("No job opportunities found in the file.")
        return redirect(url_for("import_jobs.import_form"))

    result = stage_jobs(db, jobs, source=upload_file.filename)
    flash(
        f"Loaded {result['added']} opportunities from {upload_file.filename}"
        f" ({result['skipped']} already staged)."
    )
    return redirect(url_for("import_jobs.import_form"))


@import_bp.route("/urls", methods=["POST"])
def add_urls():
    """Pre-stage pasted job URLs as ``unprocessed`` records for MCP processing."""
    db, error = _get_db_or_error()
    if error:
        return error

    urls = parse_urls(request.form.get("urls", ""))
    if not urls:
        flash("Please paste at least one URL.")
        return redirect(url_for("import_jobs.import_form"))

    result = stage_urls(db, urls, source="manual")
    flash(
        f"Added {result['added']} URL(s) to process "
        f"({result['skipped']} already known, {result['invalid']} invalid)."
    )
    return redirect(url_for("import_jobs.import_form"))


@import_bp.route("/urls/clear", methods=["POST"])
def clear_urls():
    """Remove all pending (unprocessed) URLs from the staging area."""
    db, error = _get_db_or_error()
    if error:
        return error

    result = db.import_staging.delete_many({"status": STATUS_UNPROCESSED})
    flash(f"Cleared {result.deleted_count} pending URL(s).")
    return redirect(url_for("import_jobs.import_form"))


@import_bp.route("/commit", methods=["POST"])
def commit():
    """Create the selected staged opportunities, removing them from staging."""
    db, error = _get_db_or_error()
    if error:
        return error

    now = _utcnow()
    created = 0
    skipped = 0

    for raw_id in request.form.getlist("select"):
        try:
            oid = ObjectId(raw_id)
        except (InvalidId, TypeError):
            continue
        job = db.import_staging.find_one({"_id": oid})
        if job is None:
            continue

        # Use the URL as the dedup key only when it identifies a single posting.
        # Search/results-page URLs are shared, so fall back to title + company.
        external_id_source = (
            _identifying_url(job.get("url"))
            or _title_company_key(job.get("title"), job.get("company"))
            or "unknown"
        )
        external_id = BaseCrawler.hash_external_id(IMPORT_SITE, external_id_source)

        result = db.jobs.update_one(
            {"site": IMPORT_SITE, "external_id": external_id},
            {
                "$set": {
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "location": job.get("location"),
                    "url": job.get("url"),
                    "salary": job.get("salary"),
                    "keywords": job.get("keywords", []),
                    "description_text": job.get("description_text"),
                    "site": IMPORT_SITE,
                    "external_id": external_id,
                    "updated_at": now,
                },
                "$setOnInsert": {"status": STATUS_IMPORTED, "created_at": now},
            },
            upsert=True,
        )
        if result.upserted_id is not None:
            created += 1
        else:
            skipped += 1
        # Imported records leave the staging area.
        db.import_staging.delete_one({"_id": oid})

    processed = created + skipped
    if processed == 0:
        flash("No opportunities were selected to import.")
    else:
        # How many viewable opportunities are still staged (pending URLs excluded).
        remaining = db.import_staging.count_documents(
            {"status": {"$ne": STATUS_UNPROCESSED}}
        )
        flash(
            f"Staging processed: imported {created} new "
            f"opportunit{'y' if created == 1 else 'ies'} "
            f"({skipped} already existed). {remaining} still staged."
        )
    return redirect(url_for("import_jobs.import_form"))


@import_bp.route("/clear", methods=["POST"])
def clear():
    """Remove viewable staged opportunities, leaving pending URLs untouched."""
    db, error = _get_db_or_error()
    if error:
        return error

    # Pending (unprocessed) URLs are cleared separately via ``clear_urls`` so a
    # staging cleanup doesn't discard URLs an MCP client hasn't processed yet.
    result = db.import_staging.delete_many({"status": {"$ne": STATUS_UNPROCESSED}})
    flash(f"Cleared {result.deleted_count} staged opportunities.")
    return redirect(url_for("import_jobs.import_form"))
