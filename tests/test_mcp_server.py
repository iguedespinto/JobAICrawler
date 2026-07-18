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
             "keywords": ["Python"], "state": "closed"},
        ],
        profiles=[],
    )


def test_find_jobs_query_spans_all_states():
    db = _jobs_db()
    # No state filter by default: open and closed both returned.
    jobs = mcp_server.find_jobs_in_db(db, query="python")
    titles = {j["title"] for j in jobs}
    assert "Backend Engineer" in titles
    assert "Old Role" in titles  # closed job still returned
    assert all("state" in j for j in jobs)


def test_find_jobs_state_filter_and_company():
    db = _jobs_db()
    # Only open Acme jobs (Old Role is closed).
    jobs = mcp_server.find_jobs_in_db(db, company="acme", state="open")
    assert {j["title"] for j in jobs} == {"Backend Engineer"}
    # Only closed Acme jobs.
    closed = mcp_server.find_jobs_in_db(db, company="acme", state="closed")
    assert {j["title"] for j in closed} == {"Old Role"}


def _exclude_jobs_db():
    return FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "Alpha", "description_text": "reports to a manager"},
            {"_id": ObjectId(), "title": "Beta", "keywords": ["Manager"]},
            {"_id": ObjectId(), "title": "Gamma Manager", "keywords": ["Ops"]},
            {"_id": ObjectId(), "title": "Delta", "keywords": ["Python"], "description_text": "APIs"},
            {"_id": ObjectId(), "title": "Epsilon", "keywords": ["Go"], "description_text": "cloud"},
        ],
        profiles=[],
    )


def test_find_jobs_exclude_spans_title_keywords_description():
    db = _exclude_jobs_db()
    # Dropped: Alpha (description), Beta (keyword), Gamma (title).
    titles = {j["title"] for j in mcp_server.find_jobs_in_db(db, exclude="manager")}
    assert titles == {"Delta", "Epsilon"}


def test_find_jobs_exclude_takes_several_comma_separated_terms():
    db = _exclude_jobs_db()
    titles = {j["title"] for j in mcp_server.find_jobs_in_db(db, exclude="manager, python")}
    assert titles == {"Epsilon"}
    # Count is filtered the same way, so pagination stays correct.
    assert mcp_server.count_jobs_in_db(db, exclude="manager, python") == 1


def test_find_jobs_exclude_combines_with_query():
    db = FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "Backend Engineer", "keywords": ["Python"]},
            {"_id": ObjectId(), "title": "Engineering Manager", "keywords": ["Leadership"]},
        ],
        profiles=[],
    )
    titles = {
        j["title"]
        for j in mcp_server.find_jobs_in_db(db, query="engineer", exclude="manager")
    }
    assert titles == {"Backend Engineer"}


def test_update_job_status_state_and_user_status():
    db = _jobs_db()
    job = db.jobs.find_one({"title": "Backend Engineer"})
    jid = str(job["_id"])

    updated = mcp_server.update_job_status_in_db(db, jid, state="closed")
    assert updated["state"] == "closed"

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
        mcp_server.update_job_status_in_db(db, jid, state="bogus")  # bad state
    with _pytest.raises(ValueError):
        mcp_server.update_job_status_in_db(db, jid)  # nothing to update
    with _pytest.raises(ValueError):
        mcp_server.update_job_status_in_db(db, str(ObjectId()), state="closed")  # not found


def test_update_job_status_rejects_radar_as_a_status():
    """Radar is a filter, not something a job can be set to."""
    db = _jobs_db()
    job = db.jobs.find_one({"title": "Backend Engineer"})

    with pytest.raises(ValueError):
        mcp_server.update_job_status_in_db(db, str(job["_id"]), user_status="radar")


def test_find_jobs_radar_filter_means_saved_or_applied():
    db = _jobs_db()
    backend = db.jobs.find_one({"title": "Backend Engineer"})
    mcp_server.update_job_status_in_db(db, str(backend["_id"]), user_status="saved")
    frontend = db.jobs.find_one({"title": "Frontend Engineer"})
    mcp_server.update_job_status_in_db(db, str(frontend["_id"]), user_status="applied")

    # Radar is the umbrella over both marks; the untouched closed job is out.
    on_radar = mcp_server.find_jobs_in_db(db, user_status="radar")
    assert {j["title"] for j in on_radar} == {"Backend Engineer", "Frontend Engineer"}
    assert mcp_server.count_jobs_in_db(db, user_status="radar") == 2

    # The individual marks still narrow to one.
    assert {j["title"] for j in mcp_server.find_jobs_in_db(db, user_status="saved")} == {
        "Backend Engineer"
    }

    # Combines with the other filters rather than replacing them.
    assert mcp_server.find_jobs_in_db(db, user_status="radar", state="closed") == []
    assert mcp_server.count_jobs_in_db(db, user_status="radar", company="acme") == 1


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
