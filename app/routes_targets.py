"""Manage target companies and target roles.

Targets are simple named entries in the ``targets`` collection, each tagged with
a ``kind`` (``company`` or ``role``). They are managed from ``/targets`` and read
by the MCP server so a routine can search for the companies and roles you care
about.

``list_targets`` / ``add_target`` / ``remove_target`` are the reusable building
blocks shared with the MCP server (see ``mcp_server.py``).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from . import get_db

targets_bp = Blueprint("targets", __name__, url_prefix="/targets")

# Ordered map of stored kind -> (output list key, display title). Add a new
# kind here and the page, the API and list_targets all pick it up.
TARGET_KINDS: Dict[str, Tuple[str, str]] = {
    "company": ("companies", "Companies"),
    "role": ("roles", "Roles"),
    "search_site": ("search_sites", "Search sites"),
    "factor": ("factors", "Other relevant factors"),
}

# Accepted input spellings (singular/plural/friendly) -> stored kind.
_KIND_ALIASES = {
    "company": "company", "companies": "company",
    "role": "role", "roles": "role",
    "search_site": "search_site", "search_sites": "search_site",
    "search site": "search_site", "search sites": "search_site",
    "site": "search_site", "sites": "search_site",
    "factor": "factor", "factors": "factor",
    "other_factor": "factor", "other relevant factor": "factor",
    "other relevant factors": "factor",
}

_WHITESPACE = re.compile(r"\s+")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_db_or_error() -> Tuple[object, Optional[tuple]]:
    db = get_db()
    if db is None:
        return None, (jsonify({"error": "database not configured"}), 503)
    return db, None


def _normalize_name(value: Any) -> str:
    """Trim and collapse internal whitespace in a target name."""
    return _WHITESPACE.sub(" ", str(value or "").strip())


def _normalize_kind(value: Any) -> str:
    """Coerce a kind value to a known target kind, or '' if unrecognised."""
    return _KIND_ALIASES.get(_WHITESPACE.sub(" ", str(value or "").strip().lower()), "")


def _target_item(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": str(doc.get("_id")), "name": doc.get("name")}


def list_targets(db) -> Dict[str, List[Dict[str, Any]]]:
    """Return the managed targets grouped by kind, each sorted by name.

    Keys are the plural list names for every kind in ``TARGET_KINDS``
    (``companies``, ``roles``, ``search_sites``, ``factors``).
    """
    result: Dict[str, List[Dict[str, Any]]] = {
        key: [] for key, _title in TARGET_KINDS.values()
    }
    for doc in db.targets.find({}).sort([("name", 1)]):
        kind = doc.get("kind")
        if kind in TARGET_KINDS:
            result[TARGET_KINDS[kind][0]].append(_target_item(doc))
    return result


def add_target(db, kind: Any, name: Any) -> Dict[str, Any]:
    """Add a target company/role, skipping case-insensitive duplicates.

    Returns the target (with ``added`` telling you whether it was newly created).
    Raises ValueError on a bad kind or empty name.
    """
    kind_n = _normalize_kind(kind)
    if not kind_n:
        raise ValueError("kind must be one of: " + ", ".join(TARGET_KINDS))
    name_n = _normalize_name(name)
    if not name_n:
        raise ValueError("name must not be empty")

    existing = db.targets.find_one(
        {"kind": kind_n, "name": {"$regex": f"^{re.escape(name_n)}$", "$options": "i"}}
    )
    if existing is not None:
        return {"id": str(existing["_id"]), "kind": kind_n,
                "name": existing.get("name"), "added": False}

    doc = {"kind": kind_n, "name": name_n, "created_at": _utcnow()}
    db.targets.insert_one(doc)
    return {"id": str(doc["_id"]), "kind": kind_n, "name": name_n, "added": True}


def remove_target(db, target_id: str) -> bool:
    """Delete a target by id. Returns True if a target was removed.

    Raises InvalidId for a malformed id.
    """
    oid = ObjectId(target_id)  # raises InvalidId for malformed ids
    return db.targets.delete_one({"_id": oid}).deleted_count > 0


# ── HTML page ───────────────────────────────────────────────────────


@targets_bp.route("", methods=["GET"])
def manage_targets():
    """Render the target companies and roles management page."""
    db, error = _get_db_or_error()
    if error:
        return error

    targets = list_targets(db)
    sections = [
        {"kind": kind, "title": title, "entries": targets[key]}
        for kind, (key, title) in TARGET_KINDS.items()
    ]
    return render_template("targets.html", sections=sections)


@targets_bp.route("/add", methods=["POST"])
def add_target_route():
    """Add a target company or role from the management form."""
    db, error = _get_db_or_error()
    if error:
        return error

    try:
        result = add_target(db, request.form.get("kind"), request.form.get("name"))
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("targets.manage_targets"))

    label = result["kind"].replace("_", " ")
    if result["added"]:
        flash(f"Added target {label}: {result['name']}.")
    else:
        flash(f"Target {label} already exists: {result['name']}.")
    return redirect(url_for("targets.manage_targets"))


@targets_bp.route("/delete", methods=["POST"])
def delete_target_route():
    """Remove a target by id from the management form."""
    db, error = _get_db_or_error()
    if error:
        return error

    try:
        removed = remove_target(db, request.form.get("id", ""))
    except InvalidId:
        flash("Invalid target id.")
        return redirect(url_for("targets.manage_targets"))

    flash("Removed target." if removed else "Target not found.")
    return redirect(url_for("targets.manage_targets"))
