"""Tests for the profile skill board (kanban)."""

from __future__ import annotations

from bson import ObjectId

from app import routes_profile
from tests.conftest import FakeDB


# ── Helpers ──────────────────────────────────────────────────────────


def _db(profiles=None):
    """Two open jobs (Python x2, Flask x1, React x1) and one closed (Go)."""
    return FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "A", "keywords": ["Python", "Flask"]},
            {"_id": ObjectId(), "title": "B", "keywords": ["Python", "React"]},
            {"_id": ObjectId(), "title": "C", "keywords": ["Go"], "state": "closed"},
        ],
        profiles=profiles if profiles is not None else [],
    )


def _profile(categories):
    return [{"_id": routes_profile.PROFILE_ID, "skill_categories": categories}]


def _board_by_key(db):
    """Flatten build_board() into {column_key: {keyword_lower: card}}."""
    board = routes_profile.build_board(db)
    return {
        col["key"]: {c["keyword"].lower(): c for c in col["cards"]}
        for col in board
    }


# ── build_board (no Flask app needed) ────────────────────────────────


def test_build_board_orders_the_five_columns():
    board = routes_profile.build_board(_db())
    assert [col["key"] for col in board] == [
        "strong",
        "some_experience",
        "would_like_to_learn",
        "no_experience",
        "not_categorised",
    ]


def test_uncategorised_keywords_default_to_last_column():
    by_key = _board_by_key(_db())
    not_cat = by_key["not_categorised"]
    assert set(not_cat) == {"python", "flask", "react"}
    # Job counts come from the open-job aggregation.
    assert not_cat["python"]["count"] == 2
    assert not_cat["flask"]["count"] == 1
    # Every other column is empty when nothing is saved.
    for key in ("strong", "some_experience", "would_like_to_learn", "no_experience"):
        assert by_key[key] == {}


def test_closed_jobs_are_excluded_from_counts():
    by_key = _board_by_key(_db())
    all_keywords = {kw for col in by_key.values() for kw in col}
    assert "go" not in all_keywords


def test_saved_category_places_the_card():
    by_key = _board_by_key(_db(_profile({"python": "strong"})))
    assert "python" in by_key["strong"]
    assert by_key["strong"]["python"]["count"] == 2
    assert "python" not in by_key["not_categorised"]


def test_categorised_keyword_with_no_open_jobs_still_shows():
    # "rust" is in no job, but the user placed it in "would like to learn".
    by_key = _board_by_key(_db(_profile({"rust": "would_like_to_learn"})))
    assert "rust" in by_key["would_like_to_learn"]
    assert by_key["would_like_to_learn"]["rust"]["count"] == 0


def test_unknown_saved_category_falls_back_to_not_categorised():
    by_key = _board_by_key(_db(_profile({"python": "bogus"})))
    assert "python" in by_key["not_categorised"]


# ── GET /profile ─────────────────────────────────────────────────────


def test_board_route_renders(app_client, monkeypatch):
    db = _db()
    monkeypatch.setattr(routes_profile, "get_db", lambda: db)

    resp = app_client.get("/profile")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")

    for label in [
        "Strong",
        "Some Experience",
        "Would like to learn",
        "No experience",
        "Not categorised yet",
    ]:
        assert label in body

    # A draggable card per keyword, carrying its name and job count.
    assert 'data-keyword="Python"' in body
    assert 'data-keyword="Flask"' in body
    assert 'data-count="2"' in body
    # The old form is gone.
    assert 'name="about_you"' not in body


def test_get_profile_requires_db(app_client, monkeypatch):
    monkeypatch.setattr(routes_profile, "get_db", lambda: None)
    resp = app_client.get("/profile")
    assert resp.status_code == 503


# ── POST /profile/skill-category ─────────────────────────────────────


def test_set_category_persists(app_client, monkeypatch):
    db = _db()
    monkeypatch.setattr(routes_profile, "get_db", lambda: db)

    resp = app_client.post(
        "/profile/skill-category",
        json={"keyword": "Python", "category": "strong"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    doc = db.profiles.find_one({"_id": routes_profile.PROFILE_ID})
    assert doc["skill_categories"]["python"] == "strong"


def test_set_category_clear_moves_back_to_default(app_client, monkeypatch):
    db = _db(_profile({"python": "strong"}))
    monkeypatch.setattr(routes_profile, "get_db", lambda: db)

    resp = app_client.post(
        "/profile/skill-category",
        json={"keyword": "Python", "category": "not_categorised"},
    )
    assert resp.status_code == 200

    doc = db.profiles.find_one({"_id": routes_profile.PROFILE_ID})
    assert "python" not in doc.get("skill_categories", {})


def test_set_category_rejects_unknown(app_client, monkeypatch):
    db = _db()
    monkeypatch.setattr(routes_profile, "get_db", lambda: db)

    resp = app_client.post(
        "/profile/skill-category",
        json={"keyword": "Python", "category": "expert"},
    )
    assert resp.status_code == 400
    assert db.profiles.find_one({"_id": routes_profile.PROFILE_ID}) is None


def test_set_category_requires_keyword(app_client, monkeypatch):
    db = _db()
    monkeypatch.setattr(routes_profile, "get_db", lambda: db)

    resp = app_client.post(
        "/profile/skill-category",
        json={"keyword": "", "category": "strong"},
    )
    assert resp.status_code == 400


def test_set_category_requires_db(app_client, monkeypatch):
    monkeypatch.setattr(routes_profile, "get_db", lambda: None)
    resp = app_client.post(
        "/profile/skill-category",
        json={"keyword": "Python", "category": "strong"},
    )
    assert resp.status_code == 503
