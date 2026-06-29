"""Dashboard view: keyword word cloud and frequency table."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from collections import Counter

from flask import Blueprint, jsonify, render_template

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


def aggregate_keywords(db) -> Tuple[List[Dict[str, Any]], int]:
    """Count active opportunities per keyword (case-insensitive grouping).

    Returns ``(rows, total_jobs)`` where each row has ``keyword`` (the most
    common original spelling), ``count`` (distinct opportunities containing it)
    and ``percent`` (share of active opportunities). Rows are sorted by count
    descending, then keyword.
    """
    counts: Counter = Counter()
    spellings: Dict[str, Counter] = {}
    total_jobs = 0

    for job in db.jobs.find({"archived": {"$ne": True}}):
        total_jobs += 1
        seen: set = set()
        for raw in job.get("keywords", []) or []:
            label = str(raw).strip()
            key = label.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            counts[key] += 1
            spellings.setdefault(key, Counter())[label] += 1

    rows: List[Dict[str, Any]] = []
    for key, count in counts.items():
        label = spellings[key].most_common(1)[0][0]
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
    """Render the keyword dashboard."""
    db, error = _get_db_or_error()
    if error:
        return error

    rows, total_jobs = aggregate_keywords(db)
    cloud = _cloud_items(rows)

    return render_template(
        "dashboard.html",
        rows=rows,
        cloud=cloud,
        total_jobs=total_jobs,
    )
