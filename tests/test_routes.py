"""Route tests for job views."""

from __future__ import annotations

from bson import ObjectId

from tests.conftest import FakeDB


def test_jobs_list_route_filters(app_client, monkeypatch):
    job_id_1 = ObjectId()
    job_id_2 = ObjectId()
    fake_db = FakeDB(
        jobs=[
            {
                "_id": job_id_1,
                "title": "Backend Engineer",
                "company": "Acme",
                "location": "Remote",
            },
            {
                "_id": job_id_2,
                "title": "Frontend Engineer",
                "company": "Globex",
                "location": "Dublin",
            },
        ],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    response = app_client.get("/jobs?company=Acme")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "Backend Engineer" in body
    assert "Frontend Engineer" not in body


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
