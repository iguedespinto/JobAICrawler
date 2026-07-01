"""Dashboard view: keyword word cloud and frequency table."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from collections import Counter

from flask import Blueprint, jsonify, render_template, request

from . import get_db

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

# Word-cloud rendering bounds.
_CLOUD_LIMIT = 80          # most frequent keywords shown in the cloud
_CLOUD_MIN_PX = 14
_CLOUD_MAX_PX = 46


def _get_db_or_error() -> Tuple[object, Optional[tuple]]:
    """Return the database or a 503 response."""
    db = get_db()
    if db is None:
        return None, (jsonify({"error": "database not configured"}), 503)
    return db, None


def split_terms(raw: Any) -> List[str]:
    """Split a comma-separated string into lowercased, trimmed terms."""
    return [part.strip().lower() for part in str(raw or "").split(",") if part.strip()]


def _job_passes(job: Dict[str, Any], must: List[str], cannot: List[str]) -> bool:
    """Apply the must-contain / cannot-contain filters to a single job.

    ``must``   - every term must appear in the title, description, or keywords.
    ``cannot`` - no term may appear in the keywords.
    Both are case-insensitive substring checks; empty lists pass everything.
    """
    keywords_text = " ".join(str(k) for k in (job.get("keywords") or [])).lower()
    haystack = " ".join(
        [
            str(job.get("title") or ""),
            str(job.get("description_text") or ""),
            keywords_text,
        ]
    ).lower()

    if must and not all(term in haystack for term in must):
        return False
    if cannot and any(term in keywords_text for term in cannot):
        return False
    return True


def _proper_case(label: str) -> str:
    """Title-case words that are entirely lowercase; leave acronyms and
    mixed-case words (AWS, CI/CD, TypeScript, Node.js) untouched."""
    def _cap(word: str) -> str:
        if not word.islower():
            return word
        if "-" in word:
            return "-".join(p.capitalize() for p in word.split("-"))
        return word.capitalize()
    return " ".join(_cap(w) for w in label.split())


def aggregate_keywords(
    db,
    must: Optional[List[str]] = None,
    cannot: Optional[List[str]] = None,
    include_archived: bool = False,
) -> Tuple[List[Dict[str, Any]], int]:
    """Count opportunities per keyword (case-insensitive grouping).

    By default only active (non-archived) opportunities are counted; pass
    ``include_archived=True`` to count all of them.

    Keyword groups (from the ``keyword_groups`` collection) are respected:
    variants that belong to the same group are counted under the group's
    display name.  Ungrouped keywords still fall back to case-insensitive
    grouping by their most common spelling.

    Returns ``(rows, total_jobs)`` where each row has ``keyword``, ``count``
    and ``percent``.  Rows are sorted by count descending, then keyword.
    """
    from .routes_keywords import build_variant_map

    must = must or []
    cannot = cannot or []
    variant_map = build_variant_map(db)
    counts: Counter = Counter()
    spellings: Dict[str, Counter] = {}
    total_jobs = 0

    job_filter: Dict[str, Any] = {} if include_archived else {"archived": {"$ne": True}}
    for job in db.jobs.find(job_filter):
        if not _job_passes(job, must, cannot):
            continue
        total_jobs += 1
        seen: set = set()
        for raw in job.get("keywords", []) or []:
            label = str(raw).strip()
            key = label.lower()
            if not key:
                continue
            grouped = key in variant_map
            display = variant_map[key] if grouped else key
            display_key = display.lower()
            if display_key in seen:
                continue
            seen.add(display_key)
            counts[display_key] += 1
            display_label = display if grouped else label
            spellings.setdefault(display_key, Counter())[display_label] += 1

    rows: List[Dict[str, Any]] = []
    for key, count in counts.items():
        label = _proper_case(spellings[key].most_common(1)[0][0])
        percent = round(100 * count / total_jobs, 1) if total_jobs else 0.0
        rows.append({"keyword": label, "count": count, "percent": percent})

    rows.sort(key=lambda r: (-r["count"], r["keyword"].lower()))
    return rows, total_jobs


def _cloud_items(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build font-sized, alphabetically-ordered items for the word cloud."""
    top = rows[:_CLOUD_LIMIT]
    if not top:
        return []

    max_count = top[0]["count"]
    min_count = top[-1]["count"]
    span = math.sqrt(max_count) - math.sqrt(min_count)

    items: List[Dict[str, Any]] = []
    for row in top:
        if span <= 0:
            frac = 1.0
        else:
            frac = (math.sqrt(row["count"]) - math.sqrt(min_count)) / span
        size = round(_CLOUD_MIN_PX + frac * (_CLOUD_MAX_PX - _CLOUD_MIN_PX))
        items.append(
            {
                "keyword": row["keyword"],
                "count": row["count"],
                "percent": row["percent"],
                "size": size,
                # Heavier weight and stronger colour for more frequent terms.
                "weight": 700 if frac > 0.66 else (500 if frac > 0.33 else 400),
                "opacity": round(0.55 + 0.45 * frac, 2),
            }
        )

    items.sort(key=lambda item: item["keyword"].lower())
    return items


@dashboard_bp.route("", methods=["GET"])
def view_dashboard():
    """Render the keyword dashboard, optionally filtered by must/cannot terms."""
    db, error = _get_db_or_error()
    if error:
        return error

    must_raw = request.args.get("must", "").strip()
    cannot_raw = request.args.get("cannot", "").strip()
    include_archived = request.args.get("scope") == "all"
    rows, total_jobs = aggregate_keywords(
        db,
        must=split_terms(must_raw),
        cannot=split_terms(cannot_raw),
        include_archived=include_archived,
    )
    cloud = _cloud_items(rows)

    return render_template(
        "dashboard.html",
        rows=rows,
        cloud=cloud,
        total_jobs=total_jobs,
        must=must_raw,
        cannot=cannot_raw,
        scope="all" if include_archived else "active",
    )
