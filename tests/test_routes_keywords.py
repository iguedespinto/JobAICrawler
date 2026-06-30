"""Tests for keyword group management and merged aggregation."""

from __future__ import annotations

from bson import ObjectId

from app import routes_dashboard, routes_keywords
from tests.conftest import FakeDB


# ── Helpers ──────────────────────────────────────────────────────────


def _db_with_groups():
    group_id = ObjectId()
    return FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "A", "keywords": ["JavaScript", "React"]},
            {"_id": ObjectId(), "title": "B", "keywords": ["JS", "Node.js"]},
            {"_id": ObjectId(), "title": "C", "keywords": ["Python", "Flask"]},
        ],
        keyword_groups=[
            {"_id": group_id, "display": "JavaScript", "variants": ["javascript", "js"]},
        ],
    )


# ── Unit tests (no Flask app needed) ────────────────────────────────


def test_aggregate_merges_grouped_keywords():
    db = _db_with_groups()
    rows, total = routes_dashboard.aggregate_keywords(db)

    assert total == 3
    by_kw = {r["keyword"].lower(): r for r in rows}
    assert by_kw["javascript"]["count"] == 2
    assert by_kw["react"]["count"] == 1
    assert by_kw["node.js"]["count"] == 1
    assert by_kw["python"]["count"] == 1


def test_aggregate_without_groups_unchanged():
    db = FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "A", "keywords": ["Python", "Flask"]},
            {"_id": ObjectId(), "title": "B", "keywords": ["python", "Go"]},
        ],
    )
    rows, total = routes_dashboard.aggregate_keywords(db)

    assert total == 2
    by_kw = {r["keyword"].lower(): r for r in rows}
    assert by_kw["python"]["count"] == 2
    assert by_kw["go"]["count"] == 1


def test_aggregate_job_with_two_variants_counts_once():
    """A job with both 'JS' and 'JavaScript' should only count once for the group."""
    db = FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "A", "keywords": ["JS", "JavaScript"]},
        ],
        keyword_groups=[
            {"_id": ObjectId(), "display": "JavaScript", "variants": ["javascript", "js"]},
        ],
    )
    rows, total = routes_dashboard.aggregate_keywords(db)
    by_kw = {r["keyword"].lower(): r for r in rows}
    assert by_kw["javascript"]["count"] == 1


def test_build_variant_map():
    db = _db_with_groups()
    mapping = routes_keywords.build_variant_map(db)
    assert mapping["javascript"] == "JavaScript"
    assert mapping["js"] == "JavaScript"
    assert "python" not in mapping


def test_expand_keyword_grouped():
    db = _db_with_groups()
    variants = routes_keywords.expand_keyword(db, "JS")
    assert "javascript" in variants
    assert "js" in variants


def test_expand_keyword_ungrouped():
    db = _db_with_groups()
    variants = routes_keywords.expand_keyword(db, "Python")
    assert variants == ["Python"]


# ── Route tests (need Flask test client) ─────────────────────────────


def test_merge_creates_new_group(app_client, monkeypatch):
    db = FakeDB(jobs=[], keyword_groups=[])
    monkeypatch.setattr(routes_keywords, "get_db", lambda: db)

    resp = app_client.post(
        "/keywords/merge",
        json={"display": "JavaScript", "keyword_a": "JS", "keyword_b": "JavaScript"},
    )
    data = resp.get_json()
    assert data["ok"]
    assert "javascript" in data["variants"]
    assert "js" in data["variants"]
    assert data["display"] == "JavaScript"
    assert db.keyword_groups.count_documents({}) == 1


def test_merge_extends_existing_group(app_client, monkeypatch):
    group_id = ObjectId()
    db = FakeDB(
        jobs=[],
        keyword_groups=[
            {"_id": group_id, "display": "JavaScript", "variants": ["javascript", "js"]},
        ],
    )
    monkeypatch.setattr(routes_keywords, "get_db", lambda: db)

    resp = app_client.post(
        "/keywords/merge",
        json={"display": "JavaScript", "keyword_a": "JS", "keyword_b": "ECMAScript"},
    )
    data = resp.get_json()
    assert data["ok"]
    assert "ecmascript" in data["variants"]
    assert "js" in data["variants"]
    assert "javascript" in data["variants"]
    assert db.keyword_groups.count_documents({}) == 1


def test_merge_combines_two_groups(app_client, monkeypatch):
    id_a = ObjectId()
    id_b = ObjectId()
    db = FakeDB(
        jobs=[],
        keyword_groups=[
            {"_id": id_a, "display": "JS", "variants": ["js", "javascript"]},
            {"_id": id_b, "display": "TS", "variants": ["ts", "typescript"]},
        ],
    )
    monkeypatch.setattr(routes_keywords, "get_db", lambda: db)

    resp = app_client.post(
        "/keywords/merge",
        json={"display": "JS/TS", "keyword_a": "JavaScript", "keyword_b": "TypeScript"},
    )
    data = resp.get_json()
    assert data["ok"]
    for v in ["js", "javascript", "ts", "typescript", "js/ts"]:
        assert v in data["variants"]
    assert db.keyword_groups.count_documents({}) == 1


def test_delete_group(app_client, monkeypatch):
    group_id = ObjectId()
    db = FakeDB(
        jobs=[],
        keyword_groups=[
            {"_id": group_id, "display": "JavaScript", "variants": ["javascript", "js"]},
        ],
    )
    monkeypatch.setattr(routes_keywords, "get_db", lambda: db)

    resp = app_client.delete(f"/keywords/groups/{group_id}")
    data = resp.get_json()
    assert data["ok"]
    assert db.keyword_groups.count_documents({}) == 0


def test_delete_nonexistent_group(app_client, monkeypatch):
    db = FakeDB(jobs=[], keyword_groups=[])
    monkeypatch.setattr(routes_keywords, "get_db", lambda: db)

    resp = app_client.delete(f"/keywords/groups/{ObjectId()}")
    assert resp.status_code == 404


def test_list_groups(app_client, monkeypatch):
    db = FakeDB(
        jobs=[],
        keyword_groups=[
            {"_id": ObjectId(), "display": "JavaScript", "variants": ["javascript", "js"]},
            {"_id": ObjectId(), "display": "Python", "variants": ["python", "py"]},
        ],
    )
    monkeypatch.setattr(routes_keywords, "get_db", lambda: db)

    resp = app_client.get("/keywords/groups")
    data = resp.get_json()
    assert len(data) == 2
    displays = {g["display"] for g in data}
    assert "JavaScript" in displays
    assert "Python" in displays


def test_manage_page_renders(app_client, monkeypatch):
    db = FakeDB(
        jobs=[],
        keyword_groups=[
            {"_id": ObjectId(), "display": "JavaScript", "variants": ["javascript", "js"]},
        ],
    )
    monkeypatch.setattr(routes_keywords, "get_db", lambda: db)

    resp = app_client.get("/keywords/manage")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Keyword Groups" in body
    assert "JavaScript" in body
