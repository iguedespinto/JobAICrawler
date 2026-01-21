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
                "fit_score": 90,
                "posted_at": 2,
                "enriched": {"normalized_role": "Backend Engineer", "remote_level": "remote"},
            },
            {
                "_id": job_id_2,
                "title": "Frontend Engineer",
                "company": "Globex",
                "location": "Dublin",
                "fit_score": 70,
                "posted_at": 1,
                "enriched": {"normalized_role": "Frontend Engineer", "remote_level": "onsite"},
            },
        ],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    response = app_client.get("/jobs?role=Backend%20Engineer&remote_only=1")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "Backend Engineer" in body
    assert "Frontend Engineer" not in body


def test_job_detail_route(app_client, monkeypatch):
    job_id = ObjectId()
    fake_db = FakeDB(
        jobs=[
            {
                "_id": job_id,
                "title": "Backend Engineer",
                "company": "Acme",
                "location": "Remote",
                "description_text": "Role details",
                "url": "https://example.com/jobs/1",
                "fit_score": 88,
                "enriched": {"normalized_role": "Backend Engineer"},
                "score": {"summary": "Strong fit"},
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
    assert "Strong fit" in body
