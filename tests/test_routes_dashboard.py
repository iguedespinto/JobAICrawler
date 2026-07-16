"""Tests for the keyword dashboard."""

from __future__ import annotations

import re

from bson import ObjectId

from app import routes_dashboard
from tests.conftest import FakeDB


def _db():
    return FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "A", "keywords": ["Python", "Flask", "apex"]},
            {"_id": ObjectId(), "title": "B", "keywords": ["Python", "APEX", "apex"]},
            {"_id": ObjectId(), "title": "C", "keywords": ["python"]},
            # Closed job — counted by default (kept for keyword analysis).
            {"_id": ObjectId(), "title": "D", "keywords": ["Go"], "state": "closed"},
        ],
        profiles=[],
    )


def test_aggregate_counts_all_states_by_default():
    rows, total = routes_dashboard.aggregate_keywords(_db())
    assert total == 4  # open and closed both counted
    assert {r["keyword"].lower(): r["count"] for r in rows}["go"] == 1


def test_aggregate_open_only_excludes_closed():
    rows, total = routes_dashboard.aggregate_keywords(_db(), state="open")
    assert total == 3  # closed job excluded
    assert "go" not in {r["keyword"].lower() for r in rows}


def test_aggregate_closed_only():
    rows, total = routes_dashboard.aggregate_keywords(_db(), state="closed")
    assert total == 1
    assert {r["keyword"].lower() for r in rows} == {"go"}


def test_aggregate_keywords_counts_percent_and_sort():
    # Scope to open jobs so the three-opportunity percentages are exact.
    rows, total = routes_dashboard.aggregate_keywords(_db(), state="open")

    assert total == 3  # closed job excluded

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
    # The heading itself, not just the word: "Keywords" is also a nav label on
    # every page, so a bare substring check would pass anywhere.
    assert '<h1 class="page-title">Keywords</h1>' in body
    assert "Python" in body
    # Default scope counts all 4 jobs (incl. the closed one): Python is in 3.
    assert "75.0%" in body


def test_dashboard_table_links_both_the_keyword_and_its_count(app_client, monkeypatch):
    """The keyword and the count are two ways to ask the same question.

    Both cells lead to the opportunities behind the row, so the whole row reads
    as one link rather than making you find the number to click.
    """
    fake_db = _db()
    monkeypatch.setattr(routes_dashboard, "get_db", lambda: fake_db)

    body = app_client.get("/dashboard").data.decode("utf-8")

    # Both anchors point at the same filtered list; matched on href and label
    # rather than the exact tag, so adding an attribute doesn't fail the test.
    links = re.findall(r'<a href="([^"]+)"[^>]*>\s*(Python|3)\s*</a>', body)
    assert sorted(links) == [("/jobs?keyword=Python", "3"), ("/jobs?keyword=Python", "Python")]
