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


def _filter_db():
    return FakeDB(
        jobs=[
            {
                "_id": ObjectId(), "title": "Backend Engineer", "company": "Acme",
                "description_text": "Build APIs with Python and Flask",
                "keywords": ["Python", "AWS"],
            },
            {
                "_id": ObjectId(), "title": "Frontend Engineer", "company": "Globex",
                "description_text": "React and TypeScript work",
                "keywords": ["React", "AWS"],
            },
            {
                "_id": ObjectId(), "title": "Data Engineer", "company": "Initech",
                "description_text": "Pipelines in Python on AWS",
                "keywords": ["Python", "Spark"],
            },
        ],
        profiles=[],
    )


def test_must_contain_requires_all_terms_across_fields():
    # "python" appears in description/keywords of jobs 1 & 3; "aws" in 1 & 3 too.
    rows, total = routes_dashboard.aggregate_keywords(
        _filter_db(), must=["python", "aws"]
    )
    assert total == 2  # Backend + Data engineer
    by = {r["keyword"].lower(): r for r in rows}
    assert by["python"]["count"] == 2
    assert "react" not in by  # Frontend excluded


def test_cannot_contain_excludes_by_keyword_only():
    # Exclude anything tagged AWS (keywords), leaving only the Data Engineer.
    rows, total = routes_dashboard.aggregate_keywords(
        _filter_db(), cannot=["aws"]
    )
    assert total == 1
    by = {r["keyword"].lower(): r for r in rows}
    assert by["spark"]["count"] == 1
    assert "react" not in by


def test_must_and_cannot_work_together():
    # Must have python; must not be tagged Spark -> only the Backend Engineer.
    rows, total = routes_dashboard.aggregate_keywords(
        _filter_db(), must=["python"], cannot=["spark"]
    )
    assert total == 1
    by = {r["keyword"].lower(): r for r in rows}
    assert by["aws"]["count"] == 1
    assert "spark" not in by


def test_no_filters_returns_all():
    rows, total = routes_dashboard.aggregate_keywords(_filter_db())
    assert total == 3


def test_dashboard_route_renders(app_client, monkeypatch):
    fake_db = _db()
    monkeypatch.setattr(routes_dashboard, "get_db", lambda: fake_db)

    response = app_client.get("/dashboard")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "Keyword Dashboard" in body
    assert "Python" in body
    assert "100.0%" in body
