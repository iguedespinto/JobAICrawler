"""Routes for viewing and editing a single user profile."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import re
from datetime import datetime, timezone

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from . import get_db
from .crawlers.base import BaseCrawler

profile_bp = Blueprint("profile", __name__, url_prefix="/profile")
PROFILE_ID = "default"
MANUAL_SITE = "manual_import"


def _split_list(raw_value: str) -> List[str]:
    """Split a comma-separated string into a clean list."""
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_import_blocks(raw_text: str) -> List[Dict[str, str]]:
    """Parse pasted text into job dictionaries."""
    url_pattern = re.compile(r"https?://\S+")
    blocks: List[List[str]] = []
    current: List[str] = []

    for raw_line in raw_text.splitlines():
        line = raw_line.strip(" -•\t")
        if not line:
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
        if url_pattern.search(line):
            blocks.append(current)
            current = []

    if current:
        blocks.append(current)

    jobs: List[Dict[str, str]] = []
    for lines in blocks:
        url = ""
        for line in lines:
            match = url_pattern.search(line)
            if match:
                url = match.group(0)
                break

        title = lines[0] if lines else ""
        company = lines[1] if len(lines) >= 2 else ""
        location = lines[2] if len(lines) >= 3 else ""
        if not title and url:
            title = url

        jobs.append(
            {
                "title": title,
                "company": company,
                "location": location,
                "url": url,
                "description_text": "\n".join(lines),
            }
        )

    return jobs


def _get_db_or_error() -> Tuple[object, Optional[tuple]]:
    """Return the database or a 503 response."""
    db = get_db()
    if db is None:
        return None, (jsonify({"error": "database not configured"}), 503)
    return db, None


@profile_bp.route("", methods=["GET"])
def get_profile():
    """Display the current user's profile."""
    db, error = _get_db_or_error()
    if error:
        return error

    profile_doc = db.profiles.find_one({"_id": PROFILE_ID}) or {"profile": {}}
    profile = profile_doc.get("profile", {})
    leads = profile_doc.get("leads")

    return render_template("profile.html", profile=profile, leads=leads)


@profile_bp.route("", methods=["POST"])
def update_profile():
    """Create or update the single profile from form data."""
    db, error = _get_db_or_error()
    if error:
        return error

    about_you = request.form.get("about_you", "").strip()
    skills = _split_list(request.form.get("skills", ""))
    preferred_locations = _split_list(request.form.get("preferred_locations", ""))
    remote_preference = request.form.get("remote_preference", "").strip()
    target_roles = _split_list(request.form.get("target_roles", ""))
    target_seniority = request.form.get("target_seniority", "").strip()

    profile_data: Dict[str, object] = {
        "about_you": about_you,
        "skills": skills,
        "preferred_locations": preferred_locations,
        "remote_preference": remote_preference,
        "target_roles": target_roles,
        "target_seniority": target_seniority,
    }

    db.profiles.update_one(
        {"_id": PROFILE_ID},
        {"$set": {"profile": profile_data}},
        upsert=True,
    )

    return redirect(url_for("profile.get_profile"))


@profile_bp.route("/import", methods=["POST"])
def import_jobs():
    """Import job leads pasted from an external source."""
    db, error = _get_db_or_error()
    if error:
        return error

    raw_text = request.form.get("import_text", "").strip()
    if not raw_text:
        return redirect(url_for("profile.get_profile"))

    jobs = _parse_import_blocks(raw_text)
    now = _utcnow()
    for job in jobs:
        external_id_source = job.get("url") or job.get("title") or "unknown"
        external_id = BaseCrawler.hash_external_id(MANUAL_SITE, external_id_source)

        db.jobs.update_one(
            {"site": MANUAL_SITE, "external_id": external_id},
            {
                "$set": {
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "location": job.get("location"),
                    "url": job.get("url"),
                    "site": MANUAL_SITE,
                    "external_id": external_id,
                    "description_text": job.get("description_text"),
                    "updated_at": now,
                },
                "$setOnInsert": {"status": "new_raw", "created_at": now},
            },
            upsert=True,
        )

    return redirect(url_for("profile.get_profile"))
