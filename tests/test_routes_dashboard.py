"""Tests for the keyword dashboard."""

from __future__ import annotations

from bson import ObjectId

from app import routes_dashboard
from tests.conftest import FakeDB


def _db():
    return FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "A", "keywords": ["Python", "Flask", "apex"]},
            {"_id": ObjectId(), "title": "B", "keywords": ["Python", "APEX", "apex"]},
            {"_id": ObjectId(), "title": "C", "keywords": ["python"]},
            # Archived job must be ignored.
            {"_id": ObjectId(), "title": "D", "keywords": ["Go"], "archived": True},
        ],
        profiles=[],
    )


def test_aggregate_keywords_counts_percent_and_sort():
    rows, total = routes_dashboard.aggregate_keywords(_db())

    assert total == 3  # archived job excluded

    by_keyword = {r["keyword"].lower(): r for r in rows}
    # Case-insensitive grouping; counted once per opportunity.
    assert by_keyword["python"]["count"] == 3
    assert by_keyword["python"]["percent"] == 100.0
    assert by_keyword["apex"]["count"] == 2  # "apex" and "APEX" merged
    assert by_keyword["flask"]["count"] == 1
    assert "go" not in by_keyword  # archived keyword excluded

    # Sorted by count descending.
    counts = [r["count"] for r in rows]
    assert counts == sorted(counts, reverse=True)
    assert rows[0]["keyword"].lower() == "python"


def test_dashboard_route_renders(app_client, monkeypatch):
    fake_db = _db()
    monkeypatch.setattr(routes_dashboard, "get_db", lambda: fake_db)

    response = app_client.get("/dashboard")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "Keyword Dashboard" in body
    assert "Python" in body
    assert "100.0%" in body
