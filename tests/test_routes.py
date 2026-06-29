"""Route tests for job views."""

from __future__ import annotations

from bson import ObjectId

from tests.conftest import FakeDB


def test_jobs_list_search_matches_title_keywords_description(app_client, monkeypatch):
    fake_db = FakeDB(
        jobs=[
            {
                "_id": ObjectId(),
                "title": "Backend Engineer",
                "company": "Acme",
                "keywords": ["Python", "Flask"],
                "description_text": "Build APIs",
            },
            {
                "_id": ObjectId(),
                "title": "Frontend Engineer",
                "company": "Globex",
                "keywords": ["React", "TypeScript"],
                "description_text": "Build UIs",
            },
            {
                "_id": ObjectId(),
                "title": "Data Analyst",
                "company": "Initech",
                "keywords": ["SQL"],
                "description_text": "Loves python scripting",
            },
        ],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    # Case-insensitive; matches the keyword on job 1 and the description on job 3.
    body = app_client.get("/jobs?q=python").data.decode("utf-8")
    assert "Backend Engineer" in body
    assert "Data Analyst" in body
    assert "Frontend Engineer" not in body

    # Matches a title only.
    body = app_client.get("/jobs?q=frontend").data.decode("utf-8")
    assert "Frontend Engineer" in body
    assert "Backend Engineer" not in body


def test_jobs_list_keyword_filter_exact_match(app_client, monkeypatch):
    fake_db = FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "Backend", "keywords": ["Java", "Spring"]},
            {"_id": ObjectId(), "title": "Frontend", "keywords": ["JavaScript"]},
            {"_id": ObjectId(), "title": "Mobile", "keywords": ["java"]},
        ],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    # Exact (case-insensitive) keyword match: "Java" and "java", not "JavaScript".
    body = app_client.get("/jobs?keyword=Java").data.decode("utf-8")
    assert "Backend" in body
    assert "Mobile" in body
    assert "Frontend" not in body


def test_jobs_list_search_escapes_special_characters(app_client, monkeypatch):
    fake_db = FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "C++ Engineer", "keywords": ["C++"]},
            {"_id": ObjectId(), "title": "Python Engineer", "keywords": ["Python"]},
        ],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    # "C++" must be treated literally, not as an invalid regex quantifier.
    response = app_client.get("/jobs?q=C%2B%2B")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "C++ Engineer" in body
    assert "Python Engineer" not in body


def test_jobs_list_excludes_archived_by_default(app_client, monkeypatch):
    fake_db = FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "Active Role", "company": "Acme"},
            {"_id": ObjectId(), "title": "Old Role", "company": "Acme", "archived": True},
        ],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    active = app_client.get("/jobs").data.decode("utf-8")
    assert "Active Role" in active
    assert "Old Role" not in active

    archived = app_client.get("/jobs?archived=1").data.decode("utf-8")
    assert "Old Role" in archived
    assert "Active Role" not in archived


def test_archive_job_route(app_client, monkeypatch):
    job_id = ObjectId()
    fake_db = FakeDB(
        jobs=[{"_id": job_id, "title": "Role", "company": "Acme"}],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    response = app_client.post(f"/jobs/{job_id}/archive", data={"archived": "1"})
    assert response.status_code == 302
    assert fake_db.jobs.find_one({"_id": job_id})["archived"] is True

    response = app_client.post(f"/jobs/{job_id}/archive", data={"archived": "0"})
    assert fake_db.jobs.find_one({"_id": job_id})["archived"] is False


def test_job_detail_route(app_client, monkeypatch):
    job_id = ObjectId()
    fake_db = FakeDB(
        jobs=[
            {
                "_id": job_id,
                "title": "Backend Engineer",
                "company": "Acme",
                "location": "Remote",
                "salary": "€90,000",
                "keywords": ["Python", "Flask"],
                "description_text": "Role details",
                "url": "https://example.com/jobs/1",
            }
        ],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    response = app_client.get(f"/jobs/{job_id}")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "Backend Engineer" in body
    assert "Role details" in body
    assert "Python" in body
