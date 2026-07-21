"""Routes for the profile skill board.

The profile is a kanban of skills (keywords drawn from the job corpus). Each
skill sits in one of five columns describing your relationship to it; anything
you have not placed defaults to "Not categorised yet". A card shows the skill's
name and the number of open jobs that mention it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, render_template, request

from . import get_db
from .routes_dashboard import _proper_case, aggregate_keywords

profile_bp = Blueprint("profile", __name__, url_prefix="/profile")
PROFILE_ID = "default"

# Column order is the board's order, left to right. The last column is the
# default home for any skill you have not placed; the four before it are the
# ones you can drop a card into.
SKILL_CATEGORIES: List[Dict[str, str]] = [
    {"key": "strong", "label": "Strong"},
    {"key": "some_experience", "label": "Some Experience"},
    {"key": "would_like_to_learn", "label": "Would like to learn"},
    {"key": "no_experience", "label": "No experience"},
    {"key": "not_categorised", "label": "Not categorised yet"},
]
DEFAULT_CATEGORY = "not_categorised"
# The categories a card can be moved into (every column except the default).
SETTABLE_CATEGORIES = {c["key"] for c in SKILL_CATEGORIES} - {DEFAULT_CATEGORY}


def _get_db_or_error() -> Tuple[object, Optional[tuple]]:
    """Return the database or a 503 response."""
    db = get_db()
    if db is None:
        return None, (jsonify({"error": "database not configured"}), 503)
    return db, None


def build_board(db) -> List[Dict[str, Any]]:
    """Group every skill into its column, in board order.

    Skills are the keywords across open jobs (grouped and counted exactly like
    the dashboard), unioned with any keyword you have already placed so a saved
    category never vanishes just because no open job currently mentions it. Each
    returned column is ``{key, label, cards}`` where a card is
    ``{keyword, count, category}``; cards are sorted by count then name.
    """
    profile_doc = db.profiles.find_one({"_id": PROFILE_ID}) or {}
    saved: Dict[str, str] = profile_doc.get("skill_categories", {}) or {}

    rows, _total = aggregate_keywords(db, state="open")

    cards: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        cards[row["keyword"].lower()] = {"keyword": row["keyword"], "count": row["count"]}
    # Placed keywords with no current open job still deserve their card.
    for key in saved:
        cards.setdefault(key, {"keyword": _proper_case(key), "count": 0})

    columns: Dict[str, List[Dict[str, Any]]] = {c["key"]: [] for c in SKILL_CATEGORIES}
    for key, card in cards.items():
        category = saved.get(key, DEFAULT_CATEGORY)
        if category not in columns:
            category = DEFAULT_CATEGORY
        columns[category].append({**card, "category": category})

    board: List[Dict[str, Any]] = []
    for category in SKILL_CATEGORIES:
        column_cards = columns[category["key"]]
        column_cards.sort(key=lambda c: (-c["count"], c["keyword"].lower()))
        board.append({**category, "cards": column_cards})
    return board


@profile_bp.route("", methods=["GET"])
def get_profile():
    """Display the skill board."""
    db, error = _get_db_or_error()
    if error:
        return error

    return render_template("profile.html", board=build_board(db))


@profile_bp.route("/skill-category", methods=["POST"])
def set_skill_category():
    """Place one skill into a column (or clear it back to the default).

    Expects JSON ``{keyword, category}``. Persists the whole category map so the
    write works the same against Mongo and the in-memory test double.
    """
    db, error = _get_db_or_error()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or "").strip()
    category = (data.get("category") or "").strip()

    if not keyword:
        return jsonify({"error": "keyword is required"}), 400
    if category not in SETTABLE_CATEGORIES and category != DEFAULT_CATEGORY:
        return jsonify({"error": "unknown category"}), 400

    key = keyword.lower()
    profile_doc = db.profiles.find_one({"_id": PROFILE_ID}) or {}
    categories: Dict[str, str] = dict(profile_doc.get("skill_categories", {}) or {})
    if category == DEFAULT_CATEGORY:
        categories.pop(key, None)
    else:
        categories[key] = category

    db.profiles.update_one(
        {"_id": PROFILE_ID},
        {"$set": {"skill_categories": categories}},
        upsert=True,
    )

    return jsonify({"ok": True, "keyword": keyword, "category": category})
