"""Routes for listing and retrieving jobs."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from . import get_db

jobs_bp = Blueprint("jobs", __name__, url_prefix="/jobs")


def _parse_pagination() -> Tuple[int, int]:
    """Parse pagination params from the query string."""
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", 20))
    except ValueError:
        per_page = 20
    page = max(page, 1)
    per_page = max(1, min(per_page, 100))
    return page, per_page


def _build_filters() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build Mongo filters and echo-able filter values."""
    filters: Dict[str, Any] = {}
    echo: Dict[str, Any] = {}

    normalized_role = request.args.get("role")
    if normalized_role:
        filters["enriched.normalized_role"] = normalized_role
        echo["role"] = normalized_role

    location = request.args.get("location")
    if location:
        filters["enriched.location"] = location
        echo["location"] = location

    remote_only = request.args.get("remote_only")
    if remote_only in {"1", "true", "yes", "on"}:
        filters["enriched.remote_level"] = "remote"
        echo["remote_only"] = True

    min_score_raw = request.args.get("min_score")
    if min_score_raw:
        try:
            min_score = int(min_score_raw)
            filters["fit_score"] = {"$gte": min_score}
            echo["min_score"] = min_score
        except ValueError:
            echo["min_score"] = ""

    return filters, echo


def _get_db_or_error():
    """Return the database or a 503 response."""
    db = get_db()
    if db is None:
        return None, (jsonify({"error": "database not configured"}), 503)
    return db, None


def _parse_job_id(job_id: str):
    """Parse a job ObjectId or return an error response."""
    try:
        return ObjectId(job_id), None
    except InvalidId:
        return None, (jsonify({"error": "invalid job id"}), 400)


def _normalize_user_status(raw_value: str) -> Optional[str]:
    """Normalize user_status input into stored values."""
    normalized = raw_value.strip().lower()
    if normalized not in {"saved", "applied", "none"}:
        normalized = "saved"
    if normalized == "none":
        return None
    return normalized


@jobs_bp.route("", methods=["GET"])
def list_jobs():
    """Paginated list of jobs ordered by fit score."""
    db, error = _get_db_or_error()
    if error:
        return error

    page, per_page = _parse_pagination()
    filters, echo = _build_filters()

    sort = [("fit_score", -1), ("posted_at", -1)]
    cursor = db.jobs.find(filters).sort(sort).skip((page - 1) * per_page).limit(per_page)
    total = db.jobs.count_documents(filters)
    total_pages = max(1, (total + per_page - 1) // per_page)

    jobs = []
    for job in cursor:
        jobs.append(
            {
                "id": str(job["_id"]),
                "title": job.get("title"),
                "company": job.get("company"),
                "location": job.get("location"),
                "fit_score": job.get("fit_score"),
                "status": job.get("status"),
                "user_status": job.get("user_status"),
            }
        )

    return render_template(
        "jobs_list.html",
        jobs=jobs,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        filters=echo,
    )


@jobs_bp.route("/<job_id>", methods=["GET"])
def get_job(job_id: str):
    """Show a single job by MongoDB ObjectId."""
    db, error = _get_db_or_error()
    if error:
        return error

    oid, error = _parse_job_id(job_id)
    if error:
        return error

    job = db.jobs.find_one({"_id": oid})
    if job is None:
        return jsonify({"error": "job not found"}), 404

    job["id"] = str(job["_id"])
    job.pop("_id", None)
    enrichment = job.get("enriched", {}) or {}
    score = job.get("score", {}) or {}

    return render_template(
        "job_detail.html",
        job=job,
        enrichment=enrichment,
        score=score,
    )


@jobs_bp.route("/<job_id>/save", methods=["POST"])
def save_job(job_id: str):
    """Mark a job as saved or applied."""
    db, error = _get_db_or_error()
    if error:
        return error

    oid, error = _parse_job_id(job_id)
    if error:
        return error

    update_value = _normalize_user_status(request.form.get("user_status", ""))

    result = db.jobs.update_one({"_id": oid}, {"$set": {"user_status": update_value}})
    if result.matched_count == 0:
        return jsonify({"error": "job not found"}), 404

    return redirect(url_for("jobs.get_job", job_id=job_id))
