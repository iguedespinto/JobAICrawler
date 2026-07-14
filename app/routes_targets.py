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


def rename_target(db, target_id: str, name: Any) -> Dict[str, Any]:
    """Rename a target, skipping if another of the same kind already has the name.

    Returns ``{"found", "renamed", "duplicate", "kind", "name"}``. Raises
    ValueError on an empty name and InvalidId on a malformed id.
    """
    oid = ObjectId(target_id)  # raises InvalidId for malformed ids
    name_n = _normalize_name(name)
    if not name_n:
        raise ValueError("name must not be empty")

    doc = db.targets.find_one({"_id": oid})
    if doc is None:
        return {"found": False, "renamed": False, "duplicate": False}

    kind = doc.get("kind")
    clash = db.targets.find_one(
        {
            "kind": kind,
            "name": {"$regex": f"^{re.escape(name_n)}$", "$options": "i"},
            "_id": {"$ne": oid},
        }
    )
    if clash is not None:
        return {"found": True, "renamed": False, "duplicate": True,
                "kind": kind, "name": doc.get("name")}

    db.targets.update_one({"_id": oid}, {"$set": {"name": name_n}})
    return {"found": True, "renamed": True, "duplicate": False,
            "kind": kind, "name": name_n}


# ── Suggestions (populated by the MCP client, reviewed on the page) ──


def list_suggestions(db) -> List[Dict[str, Any]]:
    """List pending target suggestions, sorted by type then name."""
    out: List[Dict[str, Any]] = []
    for doc in db.target_suggestions.find({}).sort([("kind", 1), ("name", 1)]):
        kind = doc.get("kind")
        if kind in TARGET_KINDS:
            out.append(
                {
                    "id": str(doc["_id"]),
                    "name": doc.get("name"),
                    "kind": kind,
                    "type_label": TARGET_KINDS[kind][1],
                }
            )
    return out


def add_suggestions(db, items: Any) -> Dict[str, int]:
    """Add suggested targets for review, skipping ones already targeted/suggested.

    ``items`` is an iterable of mappings with ``kind`` and ``name``. Returns
    ``{"added", "skipped", "invalid"}``.
    """
    added = skipped = invalid = 0
    seen: set = set()
    now = _utcnow()
    to_insert: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            invalid += 1
            continue
        kind = _normalize_kind(item.get("kind"))
        name = _normalize_name(item.get("name"))
        if not kind or not name:
            invalid += 1
            continue
        key = (kind, name.lower())
        if key in seen:
            skipped += 1
            continue
        name_rx = {"$regex": f"^{re.escape(name)}$", "$options": "i"}
        if db.targets.find_one({"kind": kind, "name": name_rx}) is not None:
            skipped += 1  # already a target
            continue
        if db.target_suggestions.find_one({"kind": kind, "name": name_rx}) is not None:
            skipped += 1  # already suggested
            continue
        seen.add(key)
        to_insert.append({"kind": kind, "name": name, "created_at": now})
        added += 1
    if to_insert:
        db.target_suggestions.insert_many(to_insert)
    return {"added": added, "skipped": skipped, "invalid": invalid}


def accept_suggestion(db, suggestion_id: str) -> Dict[str, Any]:
    """Promote a suggestion into its target list and drop it from suggestions.

    Raises InvalidId for a malformed id.
    """
    oid = ObjectId(suggestion_id)  # raises InvalidId for malformed ids
    doc = db.target_suggestions.find_one({"_id": oid})
    if doc is None:
        return {"found": False}
    result = add_target(db, doc.get("kind"), doc.get("name"))
    db.target_suggestions.delete_one({"_id": oid})
    return {"found": True, "kind": result["kind"],
            "name": result["name"], "added": result["added"]}


def discard_suggestion(db, suggestion_id: str) -> bool:
    """Remove a suggestion without adding it. Returns True if one was removed.

    Raises InvalidId for a malformed id.
    """
    oid = ObjectId(suggestion_id)  # raises InvalidId for malformed ids
    return db.target_suggestions.delete_one({"_id": oid}).deleted_count > 0


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
    return render_template(
        "targets.html", sections=sections, suggestions=list_suggestions(db)
    )


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


@targets_bp.route("/edit", methods=["POST"])
def edit_target_route():
    """Rename a target inline from the management page."""
    db, error = _get_db_or_error()
    if error:
        return error

    try:
        result = rename_target(db, request.form.get("id", ""), request.form.get("name"))
    except InvalidId:
        flash("Invalid target id.")
        return redirect(url_for("targets.manage_targets"))
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("targets.manage_targets"))

    if not result["found"]:
        flash("Target not found.")
    elif result["duplicate"]:
        flash(f"A {result['kind'].replace('_', ' ')} with that name already exists.")
    else:
        flash(f"Renamed target to {result['name']}.")
    return redirect(url_for("targets.manage_targets"))


@targets_bp.route("/suggestions/accept", methods=["POST"])
def accept_suggestion_route():
    """Promote a suggestion into its target list."""
    db, error = _get_db_or_error()
    if error:
        return error

    try:
        result = accept_suggestion(db, request.form.get("id", ""))
    except InvalidId:
        flash("Invalid suggestion id.")
        return redirect(url_for("targets.manage_targets"))

    if not result["found"]:
        flash("Suggestion not found.")
    else:
        label = result["kind"].replace("_", " ")
        if result["added"]:
            flash(f"Added {label}: {result['name']}.")
        else:
            flash(f"{result['name']} was already a target {label}.")
    return redirect(url_for("targets.manage_targets"))


@targets_bp.route("/suggestions/discard", methods=["POST"])
def discard_suggestion_route():
    """Dismiss a suggestion without adding it."""
    db, error = _get_db_or_error()
    if error:
        return error

    try:
        removed = discard_suggestion(db, request.form.get("id", ""))
    except InvalidId:
        flash("Invalid suggestion id.")
        return redirect(url_for("targets.manage_targets"))

    flash("Discarded suggestion." if removed else "Suggestion not found.")
    return redirect(url_for("targets.manage_targets"))
