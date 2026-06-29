"""Routes for importing externally-prepared job opportunities from JSON files.

The import is a two-step flow:

1. ``GET  /import``         - show the upload form.
2. ``POST /import/preview`` - parse the uploaded JSON, match each opportunity
   against what already exists in the database, and render a review table.
3. ``POST /import/commit``  - create the opportunities the user selected.

No server-side state is held between preview and commit: each reviewed row
carries its own data in a hidden field, so the commit request is self-contained.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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


@import_bp.route("", methods=["GET"])
def import_form():
    """Show the JSON upload form."""
    return render_template("import.html")


@import_bp.route("/preview", methods=["POST"])
def preview():
    """Parse the uploaded file and show a review table with match annotations."""
    db, error = _get_db_or_error()
    if error:
        return error

    upload = request.files.get("import_file")
    if upload is None or not upload.filename:
        flash("Please choose a JSON file to import.")
        return redirect(url_for("import_jobs.import_form"))

    try:
        raw_text = upload.read().decode("utf-8")
        jobs = parse_jobs(raw_text)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        flash(f"Could not read JSON file: {exc}")
        return redirect(url_for("import_jobs.import_form"))

    if not jobs:
        flash("No job opportunities found in the file.")
        return redirect(url_for("import_jobs.import_form"))

    rows = match_jobs(jobs, db)
    # Serialize each job so the row can be resubmitted on commit without state.
    for index, row in enumerate(rows):
        row["index"] = index
        row["payload"] = json.dumps(row["job"], ensure_ascii=True)

    summary = {
        "total": len(rows),
        "new": sum(1 for r in rows if r["status"] == "new"),
        "matched": sum(1 for r in rows if r["status"] == "matched"),
        "duplicate": sum(1 for r in rows if r["status"] == "duplicate"),
        "filename": upload.filename,
        "threshold": SIMILARITY_THRESHOLD,
    }
    return render_template("import_preview.html", rows=rows, summary=summary)


@import_bp.route("/commit", methods=["POST"])
def commit():
    """Create the selected opportunities in the database."""
    db, error = _get_db_or_error()
    if error:
        return error

    selected_indices = request.form.getlist("select")
    now = _utcnow()
    created = 0
    skipped = 0

    for index in selected_indices:
        payload = request.form.get(f"job_{index}")
        if not payload:
            continue
        try:
            job = json.loads(payload)
        except json.JSONDecodeError:
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

    flash(f"Imported {created} new opportunities ({skipped} already existed).")
    return redirect(url_for("jobs.list_jobs"))
