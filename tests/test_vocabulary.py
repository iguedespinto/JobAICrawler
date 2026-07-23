"""The words the interface uses for its two central things.

An opportunity is a **role**, and a keyword is a **skill**. Only the wording
changed: the routes (``/keywords/...``, ``?keyword=``), the stored fields
(``jobs.keywords``, ``keyword_groups``) and the MCP arguments still use the old
names, so links, saved data and MCP clients all keep working. These tests pin
the visible half of that split — and the tests below them pin the other half, so
nobody "finishes the rename" into the parts that would break.
"""

from __future__ import annotations

import re

from bson import ObjectId

from app import routes_dashboard, routes_jobs, routes_keywords
from tests.conftest import FakeDB


def _nav(body: str) -> str:
    """Just the rail's navigation links, where the section names live."""
    match = re.search(r'<div class="rail__nav">(.*?)</div>', body, re.S)
    assert match, "navigation not found"
    return match.group(1)


def _db():
    return FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "A", "company": "Acme",
             "keywords": ["Python"], "state": "open"},
        ],
        profiles=[],
        keyword_groups=[
            {"_id": ObjectId(), "display": "JavaScript", "variants": ["javascript", "js"]},
        ],
    )


def test_the_navigation_names_roles_and_skills(app_client, monkeypatch):
    monkeypatch.setattr(routes_jobs, "get_db", lambda: _db())

    nav = _nav(app_client.get("/jobs").data.decode("utf-8"))

    assert ">Roles</a>" in nav
    assert ">Skills</a>" in nav
    assert "Opportunities" not in nav
    assert "Keywords" not in nav


def test_the_roles_list_is_titled_roles(app_client, monkeypatch):
    monkeypatch.setattr(routes_jobs, "get_db", lambda: _db())

    body = app_client.get("/jobs").data.decode("utf-8")

    assert '<h1 class="page-title">Roles</h1>' in body
    assert "<title>Roles · Job AI Crawler</title>" in body


def test_the_skills_dashboard_is_titled_skills(app_client, monkeypatch):
    monkeypatch.setattr(routes_dashboard, "get_db", lambda: _db())

    body = app_client.get("/dashboard").data.decode("utf-8")

    assert '<h1 class="page-title">Skills</h1>' in body
    assert "Every skill</h2>" in body
    assert "Merge skills" in body


def test_the_groups_page_is_titled_skill_groups(app_client, monkeypatch):
    monkeypatch.setattr(routes_keywords, "get_db", lambda: _db())

    body = app_client.get("/keywords/manage").data.decode("utf-8")

    assert '<h1 class="page-title">Skill Groups</h1>' in body


# ── The half that must NOT be renamed ────────────────────────────────


def test_the_urls_and_query_parameter_are_unchanged(app_client, monkeypatch):
    """Renaming these would break saved links and the dashboard's own hrefs."""
    monkeypatch.setattr(routes_dashboard, "get_db", lambda: _db())
    monkeypatch.setattr(routes_keywords, "get_db", lambda: _db())

    # The groups page still lives under /keywords/.
    assert app_client.get("/keywords/manage").status_code == 200
    assert app_client.get("/keywords/groups").status_code == 200

    # And the dashboard still scopes the list with ?keyword=.
    body = app_client.get("/dashboard").data.decode("utf-8")
    assert "/jobs?keyword=" in body


def test_the_stored_field_is_still_keywords(app_client, monkeypatch):
    """The database keeps ``keywords``; only the label above it says Skills."""
    db = _db()
    monkeypatch.setattr(routes_jobs, "get_db", lambda: db)

    job = db.jobs.find_one({})
    assert "keywords" in job

    # The edit form posts the same field name back.
    body = app_client.get(f"/jobs/{job['_id']}").data.decode("utf-8")
    assert 'name="keywords"' in body
    assert '<h2 class="section-title">Skills</h2>' in body  # ...but reads "Skills"
