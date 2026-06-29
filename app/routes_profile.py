"""Routes for viewing and editing a single user profile."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from . import get_db

profile_bp = Blueprint("profile", __name__, url_prefix="/profile")
PROFILE_ID = "default"


def _split_list(raw_value: str) -> List[str]:
    """Split a comma-separated string into a clean list."""
    return [item.strip() for item in raw_value.split(",") if item.strip()]


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

    return render_template("profile.html", profile=profile)


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
