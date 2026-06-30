"""Tests for the JSON import flow."""

from __future__ import annotations

import io
import json

from app import routes_import
from tests.conftest import FakeDB


def test_parse_jobs_maps_name_to_title():
    raw = json.dumps(
        [
            {
                "name": "Senior Backend Engineer",
                "company": "Acme",
                "url": "https://example.com/jobs/1",
                "salary": "€90,000",
                "description": "Build APIs.",
                "keywords": ["Python", "Flask"],
            }
        ]
    )

    jobs = routes_import.parse_jobs(raw)

    assert len(jobs) == 1
    assert jobs[0]["title"] == "Senior Backend Engineer"
    assert jobs[0]["company"] == "Acme"
    assert jobs[0]["keywords"] == ["Python", "Flask"]
    assert jobs[0]["description_text"] == "Build APIs."


def test_parse_jobs_accepts_single_object_and_skips_empty():
    raw = json.dumps({"name": "Solo Role", "url": "https://example.com/x"})
    assert len(routes_import.parse_jobs(raw)) == 1

    raw_empty = json.dumps([{"company": "No title no url"}, "not-an-object"])
    assert routes_import.parse_jobs(raw_empty) == []


def test_match_jobs_detects_existing_and_in_file_duplicates():
    fake_db = FakeDB(
        jobs=[
            {
                "title": "Existing Role",
                "company": "Acme",
                "url": "https://example.com/jobs/existing",
            },
            {
                "title": "Lead Engineer",
                "company": "Globex",
                "url": "https://example.com/jobs/other",
            },
        ],
        profiles=[],
    )

    jobs = [
        # Matches by URL.
        {"title": "Different name", "company": "Whatever",
         "url": "https://example.com/jobs/existing/"},
        # Matches by title + company even though URL differs.
        {"title": "lead engineer", "company": "globex",
         "url": "https://example.com/jobs/new-url"},
        # Brand new.
        {"title": "Brand New", "company": "Startup",
         "url": "https://example.com/jobs/brand-new"},
        # Duplicate of the brand-new row within this same file.
        {"title": "Brand New", "company": "Startup",
         "url": "https://example.com/jobs/brand-new"},
    ]

    rows = routes_import.match_jobs(jobs, fake_db)

    assert [r["status"] for r in rows] == ["matched", "matched", "new", "duplicate"]
    assert rows[0]["reason"] == "url"
    assert rows[1]["reason"] == "title_company"
    assert rows[3]["reason"] == "duplicate_in_file"


def test_search_urls_do_not_count_as_identity():
    fake_db = FakeDB(jobs=[], profiles=[])

    search_url = "https://ie.indeed.com/q-engineering-manager-l-county-dublin-jobs.html"
    jobs = [
        {"title": "EM, Google", "company": "Google", "url": search_url},
        {"title": "EM, Apple", "company": "Apple", "url": search_url},
        # A genuine same-title+company repeat still collapses.
        {"title": "EM, Apple", "company": "Apple", "url": search_url},
    ]

    rows = routes_import.match_jobs(jobs, fake_db)

    # Distinct jobs sharing the search URL are NOT duplicates of each other...
    assert [r["status"] for r in rows] == ["new", "new", "duplicate"]
    # ...and the only duplicate is by title + company, not URL.
    assert rows[2]["reason"] == "duplicate_in_file"


def test_is_search_url_classification():
    assert routes_import._is_search_url(
        "https://ie.indeed.com/q-engineering-manager-l-county-dublin-jobs.html"
    )
    assert routes_import._is_search_url("https://example.com/jobs/search?keyword=x")
    # A real posting URL must remain identifying.
    assert not routes_import._is_search_url("https://ie.indeed.com/viewjob?jk=abc123")
    assert not routes_import._is_search_url(
        "https://ie.linkedin.com/jobs/view/role-at-acme-4402503248"
    )


_DESCRIPTION = (
    "Senior backend engineer building scalable payment APIs with Python, "
    "Flask and MongoDB. Design distributed microservices, mentor developers, "
    "own delivery from design through production support in a fintech platform."
)


def test_similar_description_marks_match_above_threshold():
    fake_db = FakeDB(
        jobs=[
            {
                "title": "Backend Engineer",
                "company": "Acme",
                "url": "https://example.com/jobs/acme-1",
                "description_text": _DESCRIPTION,
            }
        ],
        profiles=[],
    )

    # Same description, but different title/company/url so only similarity can hit.
    jobs = [
        {
            "title": "Backend Developer",
            "company": "Globex",
            "url": "https://example.com/jobs/globex-9",
            "description_text": _DESCRIPTION,
        }
    ]

    rows = routes_import.match_jobs(jobs, fake_db)

    assert rows[0]["status"] == "matched"
    assert rows[0]["reason"] == "similar_description"
    assert rows[0]["similarity"] >= routes_import.SIMILARITY_THRESHOLD
    assert rows[0]["match_label"] == "Backend Engineer @ Acme"


def test_archived_jobs_are_excluded_from_matching():
    fake_db = FakeDB(
        jobs=[
            {
                "title": "Backend Engineer",
                "company": "Acme",
                "url": "https://example.com/jobs/acme-1",
                "description_text": _DESCRIPTION,
                "archived": True,
            }
        ],
        profiles=[],
    )

    # Identical URL and description, but the only DB job is archived.
    jobs = [
        {
            "title": "Backend Engineer",
            "company": "Acme",
            "url": "https://example.com/jobs/acme-1",
            "description_text": _DESCRIPTION,
        }
    ]

    rows = routes_import.match_jobs(jobs, fake_db)

    assert rows[0]["status"] == "new"
    assert rows[0]["similarity"] == 0


def _upload(app_client, payload, filename="offers.json"):
    data = {"import_file": (io.BytesIO(payload.encode("utf-8")), filename)}
    return app_client.post(
        "/import/upload", data=data, content_type="multipart/form-data"
    )


def test_stage_jobs_appends_and_dedupes():
    fake_db = FakeDB(jobs=[], profiles=[])
    jobs = [
        {"title": "Role A", "company": "Acme", "url": "https://example.com/a"},
        {"title": "Role B", "company": "Beta", "url": "https://example.com/b"},
    ]

    first = routes_import.stage_jobs(fake_db, jobs, source="f1.json")
    assert first == {"added": 2, "skipped": 0}
    assert fake_db.import_staging.count_documents({}) == 2

    # Re-staging the same records (e.g. another run) skips the duplicates.
    second = routes_import.stage_jobs(fake_db, jobs, source="f2.json")
    assert second == {"added": 0, "skipped": 2}
    assert fake_db.import_staging.count_documents({}) == 2


def test_upload_persists_records_in_staging(app_client, monkeypatch):
    fake_db = FakeDB(jobs=[], profiles=[])
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)

    payload = json.dumps(
        [
            {"name": "Role A", "company": "Acme", "url": "https://example.com/a"},
            {"name": "Role B", "company": "Beta", "url": "https://example.com/b"},
        ]
    )
    assert _upload(app_client, payload).status_code == 302
    assert fake_db.import_staging.count_documents({}) == 2

    # Records persist and render on the import page.
    body = app_client.get("/import").data.decode("utf-8")
    assert "Role A" in body and "Role B" in body


def test_commit_imports_selected_and_removes_from_staging(app_client, monkeypatch):
    fake_db = FakeDB(jobs=[], profiles=[])
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)

    routes_import.stage_jobs(
        fake_db,
        [
            {"title": "Role A", "company": "Acme", "url": "https://example.com/a"},
            {"title": "Role B", "company": "Beta", "url": "https://example.com/b"},
        ],
    )
    role_a = fake_db.import_staging.find_one({"title": "Role A"})

    response = app_client.post("/import/commit", data={"select": str(role_a["_id"])})
    assert response.status_code == 302

    # Imported into jobs and removed from staging; the unselected one remains.
    assert fake_db.jobs.find_one({"title": "Role A"}) is not None
    assert fake_db.jobs.find_one({"title": "Role B"}) is None
    assert fake_db.import_staging.find_one({"title": "Role A"}) is None
    assert fake_db.import_staging.find_one({"title": "Role B"}) is not None


def test_clear_empties_staging(app_client, monkeypatch):
    fake_db = FakeDB(jobs=[], profiles=[])
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)

    routes_import.stage_jobs(
        fake_db,
        [{"title": "Role A", "company": "Acme", "url": "https://example.com/a"}],
    )
    assert fake_db.import_staging.count_documents({}) == 1

    response = app_client.post("/import/clear")
    assert response.status_code == 302
    assert fake_db.import_staging.count_documents({}) == 0
