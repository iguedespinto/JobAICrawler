"""Tests for the MCP import server's core logic."""

from __future__ import annotations

import json

import pytest

from tests.conftest import FakeDB

mcp_server = pytest.importorskip("mcp_server")


def test_import_file_to_staging(tmp_path):
    payload = [
        {"name": "Role A", "company": "Acme", "url": "https://example.com/a"},
        {"name": "Role B", "company": "Beta", "url": "https://example.com/b"},
    ]
    file_path = tmp_path / "offers.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")

    db = FakeDB(jobs=[], profiles=[])
    result = mcp_server.import_file_to_staging(str(file_path), db=db)

    assert result["parsed"] == 2
    assert result["added"] == 2
    assert result["skipped"] == 0
    assert result["staged_total"] == 2
    assert db.import_staging.count_documents({}) == 2


def test_import_file_to_staging_dedupes_on_repeat(tmp_path):
    payload = [{"name": "Role A", "company": "Acme", "url": "https://example.com/a"}]
    file_path = tmp_path / "offers.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")

    db = FakeDB(jobs=[], profiles=[])
    mcp_server.import_file_to_staging(str(file_path), db=db)
    second = mcp_server.import_file_to_staging(str(file_path), db=db)

    assert second["added"] == 0
    assert second["skipped"] == 1
    assert db.import_staging.count_documents({}) == 1


# ── Retrieve & status-change ─────────────────────────────────────────

from bson import ObjectId  # noqa: E402


def _jobs_db():
    return FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "Backend Engineer", "company": "Acme",
             "keywords": ["Python", "Flask"], "description_text": "Build APIs"},
            {"_id": ObjectId(), "title": "Frontend Engineer", "company": "Globex",
             "keywords": ["React"], "description_text": "Build UIs"},
            {"_id": ObjectId(), "title": "Old Role", "company": "Acme",
             "keywords": ["Python"], "archived": True},
        ],
        profiles=[],
    )


def test_find_jobs_query_and_active_scope():
    db = _jobs_db()
    # Active only by default; matches keyword/description/title case-insensitively.
    jobs = mcp_server.find_jobs_in_db(db, query="python")
    titles = {j["title"] for j in jobs}
    assert "Backend Engineer" in titles
    assert "Old Role" not in titles  # archived excluded
    assert all(j["archived"] is False for j in jobs)


def test_find_jobs_include_archived_and_company():
    db = _jobs_db()
    jobs = mcp_server.find_jobs_in_db(db, company="acme", include_archived=True)
    titles = {j["title"] for j in jobs}
    assert titles == {"Backend Engineer", "Old Role"}


def test_update_job_status_archive_and_user_status():
    db = _jobs_db()
    job = db.jobs.find_one({"title": "Backend Engineer"})
    jid = str(job["_id"])

    updated = mcp_server.update_job_status_in_db(db, jid, archived=True)
    assert updated["archived"] is True

    updated = mcp_server.update_job_status_in_db(db, jid, user_status="applied")
    assert updated["user_status"] == "applied"

    updated = mcp_server.update_job_status_in_db(db, jid, user_status="none")
    assert updated["user_status"] is None


def test_update_job_status_validates():
    db = _jobs_db()
    job = db.jobs.find_one({"title": "Backend Engineer"})
    jid = str(job["_id"])

    import pytest as _pytest
    with _pytest.raises(ValueError):
        mcp_server.update_job_status_in_db(db, jid, user_status="bogus")
    with _pytest.raises(ValueError):
        mcp_server.update_job_status_in_db(db, jid)  # nothing to update
    with _pytest.raises(ValueError):
        mcp_server.update_job_status_in_db(db, str(ObjectId()), archived=True)  # not found


def test_find_jobs_pagination_covers_all_without_overlap():
    jobs = [
        {"_id": ObjectId(), "title": f"Job {i}", "company": "Acme", "keywords": ["Python"]}
        for i in range(5)
    ]
    db = FakeDB(jobs=jobs, profiles=[])

    assert mcp_server.count_jobs_in_db(db, keyword="python") == 5

    p1 = mcp_server.find_jobs_in_db(db, keyword="python", page=1, limit=2)
    p2 = mcp_server.find_jobs_in_db(db, keyword="python", page=2, limit=2)
    p3 = mcp_server.find_jobs_in_db(db, keyword="python", page=3, limit=2)
    p4 = mcp_server.find_jobs_in_db(db, keyword="python", page=4, limit=2)

    assert [len(p1), len(p2), len(p3), len(p4)] == [2, 2, 1, 0]
    ids = {j["id"] for j in p1 + p2 + p3}
    assert len(ids) == 5  # every record reachable, no page overlap


# ── Pending URLs ─────────────────────────────────────────────────────

from app import routes_import  # noqa: E402


def test_find_pending_urls_returns_only_unprocessed():
    db = FakeDB(jobs=[], profiles=[])
    routes_import.stage_urls(
        db, ["https://example.com/a", "https://example.com/b"], source="manual"
    )
    # A fully-staged opportunity must not surface as a pending URL.
    routes_import.stage_jobs(
        db, [{"title": "Full", "company": "Acme", "url": "https://example.com/c"}]
    )

    assert mcp_server.count_pending_urls_in_db(db) == 2

    urls = mcp_server.find_pending_urls_in_db(db, page=1, limit=10)
    assert {u["url"] for u in urls} == {
        "https://example.com/a",
        "https://example.com/b",
    }
    # Each item is a plain, serialisable summary with an id and ISO timestamp.
    assert all(isinstance(u["id"], str) for u in urls)
    assert all(isinstance(u["staged_at"], str) for u in urls)


def test_find_pending_urls_pagination():
    db = FakeDB(jobs=[], profiles=[])
    routes_import.stage_urls(
        db, [f"https://example.com/{i}" for i in range(5)], source="manual"
    )

    p1 = mcp_server.find_pending_urls_in_db(db, page=1, limit=2)
    p2 = mcp_server.find_pending_urls_in_db(db, page=2, limit=2)
    p3 = mcp_server.find_pending_urls_in_db(db, page=3, limit=2)

    assert [len(p1), len(p2), len(p3)] == [2, 2, 1]
    assert len({u["id"] for u in p1 + p2 + p3}) == 5
