"""Routes for keyword group management (merge / unmerge)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, render_template, request

from . import get_db

keywords_bp = Blueprint("keywords", __name__, url_prefix="/keywords")


def _get_db_or_error() -> Tuple[object, Optional[tuple]]:
    db = get_db()
    if db is None:
        return None, (jsonify({"error": "database not configured"}), 503)
    return db, None


def build_variant_map(db) -> Dict[str, str]:
    """Build a lowercased-variant → display-name lookup from all keyword groups."""
    mapping: Dict[str, str] = {}
    for group in db.keyword_groups.find():
        display = group["display"]
        for variant in group.get("variants", []):
            mapping[variant] = display
    return mapping


def expand_keyword(db, keyword: str) -> List[str]:
    """Return all variant spellings for a keyword, including itself."""
    key = keyword.lower()
    group = db.keyword_groups.find_one({"variants": key})
    if group is None:
        return [keyword]
    return group.get("variants", [key])


# ── JSON API ────────────────────────────────────────────────────────


@keywords_bp.route("/groups", methods=["GET"])
def list_groups():
    """Return all keyword groups as JSON."""
    db, error = _get_db_or_error()
    if error:
        return error

    groups = []
    for g in db.keyword_groups.find().sort([("display", 1)]):
        groups.append({
            "id": str(g["_id"]),
            "display": g["display"],
            "variants": g.get("variants", []),
        })
    return jsonify(groups)


@keywords_bp.route("/groups/<group_id>", methods=["DELETE"])
def delete_group(group_id: str):
    """Delete a keyword group, restoring its variants to independent keywords."""
    db, error = _get_db_or_error()
    if error:
        return error

    try:
        oid = ObjectId(group_id)
    except InvalidId:
        return jsonify({"error": "invalid group id"}), 400

    result = db.keyword_groups.delete_one({"_id": oid})
    if result.deleted_count == 0:
        return jsonify({"error": "group not found"}), 404
    return jsonify({"ok": True})


@keywords_bp.route("/merge", methods=["POST"])
def merge_keywords():
    """Merge two keywords (and their existing groups) under one display name.

    Expects JSON body: {display, keyword_a, keyword_b}
    """
    db, error = _get_db_or_error()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    display = (data.get("display") or "").strip()
    kw_a = (data.get("keyword_a") or "").strip()
    kw_b = (data.get("keyword_b") or "").strip()

    if not display or not kw_a or not kw_b:
        return jsonify({"error": "display, keyword_a and keyword_b are required"}), 400

    key_a = kw_a.lower()
    key_b = kw_b.lower()

    group_a = db.keyword_groups.find_one({"variants": key_a})
    group_b = db.keyword_groups.find_one({"variants": key_b})

    combined_variants: set = set()

    if group_a:
        combined_variants.update(group_a.get("variants", []))
    else:
        combined_variants.add(key_a)

    if group_b and (not group_a or group_b["_id"] != group_a["_id"]):
        combined_variants.update(group_b.get("variants", []))
    elif not group_b:
        combined_variants.add(key_b)

    combined_variants.add(display.lower())
    sorted_variants = sorted(combined_variants)

    if group_a and group_b and group_a["_id"] != group_b["_id"]:
        db.keyword_groups.delete_one({"_id": group_b["_id"]})
        db.keyword_groups.update_one(
            {"_id": group_a["_id"]},
            {"$set": {"display": display, "variants": sorted_variants}},
        )
        result_id = str(group_a["_id"])
    elif group_a:
        db.keyword_groups.update_one(
            {"_id": group_a["_id"]},
            {"$set": {"display": display, "variants": sorted_variants}},
        )
        result_id = str(group_a["_id"])
    elif group_b:
        db.keyword_groups.update_one(
            {"_id": group_b["_id"]},
            {"$set": {"display": display, "variants": sorted_variants}},
        )
        result_id = str(group_b["_id"])
    else:
        doc = {"display": display, "variants": sorted_variants}
        db.keyword_groups.insert_one(doc)
        result_id = str(doc["_id"])

    return jsonify({
        "ok": True,
        "id": result_id,
        "display": display,
        "variants": sorted_variants,
    })


# ── HTML page ───────────────────────────────────────────────────────


@keywords_bp.route("/manage", methods=["GET"])
def manage_keywords():
    """Render the keyword groups management page."""
    db, error = _get_db_or_error()
    if error:
        return error

    groups = []
    for g in db.keyword_groups.find().sort([("display", 1)]):
        groups.append({
            "id": str(g["_id"]),
            "display": g["display"],
            "variants": g.get("variants", []),
        })
    return render_template("keywords_manage.html", groups=groups)
