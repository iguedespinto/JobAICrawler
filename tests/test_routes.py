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


def test_jobs_list_company_filter_exact_match(app_client, monkeypatch):
    fake_db = FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "Backend", "company": "Acme"},
            {"_id": ObjectId(), "title": "Mobile", "company": "acme"},
            {"_id": ObjectId(), "title": "Data", "company": "Acme Corp"},
            {"_id": ObjectId(), "title": "Frontend", "company": "Globex"},
        ],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    # Exact (case-insensitive) company match: "Acme" and "acme", but not the
    # different company "Acme Corp" that merely starts with the same word.
    body = app_client.get("/jobs?company=Acme").data.decode("utf-8")
    assert "Backend" in body
    assert "Mobile" in body
    assert "Acme Corp" not in body
    assert "Frontend" not in body


def test_jobs_list_company_name_links_to_filtered_view(app_client, monkeypatch):
    fake_db = FakeDB(
        jobs=[{"_id": ObjectId(), "title": "Backend", "company": "Acme"}],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    body = app_client.get("/jobs").data.decode("utf-8")
    assert 'href="/jobs?company=Acme"' in body


def test_jobs_list_company_filter_combines_with_state(app_client, monkeypatch):
    fake_db = FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "Acme Open", "company": "Acme", "state": "open"},
            {"_id": ObjectId(), "title": "Acme Closed", "company": "Acme", "state": "closed"},
            {"_id": ObjectId(), "title": "Globex Open", "company": "Globex", "state": "open"},
        ],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    body = app_client.get("/jobs?company=Acme&state=open").data.decode("utf-8")
    assert "Acme Open" in body
    assert "Acme Closed" not in body
    assert "Globex Open" not in body

    # The state links keep the company filter, so switching state stays scoped.
    assert 'href="/jobs?state=closed&amp;company=Acme"' in body
    # The company link keeps the state, so it never widens the current view.
    assert 'href="/jobs?company=Acme&amp;state=open"' in body


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


def test_jobs_list_shows_all_states_and_filters(app_client, monkeypatch):
    fake_db = FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "Open Role", "company": "Acme", "state": "open"},
            {"_id": ObjectId(), "title": "Closed Role", "company": "Acme", "state": "closed"},
        ],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    # Default: all states shown.
    everything = app_client.get("/jobs").data.decode("utf-8")
    assert "Open Role" in everything and "Closed Role" in everything

    # Narrow by state.
    open_only = app_client.get("/jobs?state=open").data.decode("utf-8")
    assert "Open Role" in open_only and "Closed Role" not in open_only

    closed_only = app_client.get("/jobs?state=closed").data.decode("utf-8")
    assert "Closed Role" in closed_only and "Open Role" not in closed_only


def test_set_job_state_route(app_client, monkeypatch):
    job_id = ObjectId()
    fake_db = FakeDB(
        jobs=[{"_id": job_id, "title": "Role", "company": "Acme", "state": "open"}],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    response = app_client.post(f"/jobs/{job_id}/state", data={"state": "closed"})
    assert response.status_code == 302
    assert fake_db.jobs.find_one({"_id": job_id})["state"] == "closed"

    response = app_client.post(f"/jobs/{job_id}/state", data={"state": "open"})
    assert fake_db.jobs.find_one({"_id": job_id})["state"] == "open"


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


def test_edit_job_route_updates_fields(app_client, monkeypatch):
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
                "description_text": "Old details",
                "url": "https://example.com/jobs/1",
                "state": "closed",  # editing must work for closed jobs too
            }
        ],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    response = app_client.post(
        f"/jobs/{job_id}/edit",
        data={
            "title": "Senior Backend Engineer",
            "company": "Globex",
            "location": "Dublin",
            "url": "https://example.com/jobs/2",
            "salary": "€120,000",
            "keywords": "Python, Django , AWS",
            "description_html": (
                "<p>New <b>bold</b> details</p>"
                "<ul><li>first</li><li>second</li></ul>"
                "<script>alert('xss')</script>"
            ),
        },
    )
    assert response.status_code == 302

    job = fake_db.jobs.find_one({"_id": job_id})
    assert job["title"] == "Senior Backend Engineer"
    assert job["company"] == "Globex"
    assert job["location"] == "Dublin"
    assert job["url"] == "https://example.com/jobs/2"
    assert job["salary"] == "€120,000"
    assert job["keywords"] == ["Python", "Django", "AWS"]
    # Rich text: formatting kept, script stripped.
    assert "<b>bold</b>" in job["description_html"]
    assert "<li>first</li>" in job["description_html"]
    assert "script" not in job["description_html"].lower()
    # Plain-text copy has no tags but keeps the words (for search / matching).
    assert "<" not in job["description_text"]
    assert "bold" in job["description_text"] and "second" in job["description_text"]
    # Editing does not change the state.
    assert job["state"] == "closed"


# ── "On my radar" (a filter, not a stored status) ────────────────────


def _radar_db():
    return FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "Saved Role", "user_status": "saved"},
            {"_id": ObjectId(), "title": "Applied Role", "user_status": "applied"},
            {"_id": ObjectId(), "title": "Untouched Role"},
        ],
        profiles=[],
    )


def test_save_job_has_no_radar_status(app_client, monkeypatch):
    """Radar is not something a job can be set to -- only saved/applied/none."""
    job_id = ObjectId()
    fake_db = FakeDB(
        jobs=[{"_id": job_id, "title": "Role", "company": "Acme"}],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    assert "radar" not in routes_jobs.USER_STATUSES

    app_client.post(f"/jobs/{job_id}/save", data={"user_status": "saved"})
    assert fake_db.jobs.find_one({"_id": job_id})["user_status"] == "saved"

    app_client.post(f"/jobs/{job_id}/save", data={"user_status": "applied"})
    assert fake_db.jobs.find_one({"_id": job_id})["user_status"] == "applied"

    app_client.post(f"/jobs/{job_id}/save", data={"user_status": "none"})
    assert fake_db.jobs.find_one({"_id": job_id})["user_status"] is None


def test_job_detail_form_offers_no_radar_option(app_client, monkeypatch):
    job_id = ObjectId()
    fake_db = FakeDB(
        jobs=[{"_id": job_id, "title": "Role", "company": "Acme",
               "user_status": "saved"}],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    body = app_client.get(f"/jobs/{job_id}").data.decode("utf-8")
    assert 'value="radar"' not in body
    assert 'value="saved"' in body and 'value="applied"' in body


def test_jobs_list_radar_filter_means_saved_or_applied(app_client, monkeypatch):
    fake_db = _radar_db()

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    # Default: no filter, everything shows.
    body = app_client.get("/jobs").data.decode("utf-8")
    assert "Saved Role" in body and "Applied Role" in body and "Untouched Role" in body

    # Radar is the umbrella: both marks, and nothing untriaged.
    radar = app_client.get("/jobs?user_status=radar").data.decode("utf-8")
    assert "Saved Role" in radar and "Applied Role" in radar
    assert "Untouched Role" not in radar

    # The individual marks still narrow to one.
    saved = app_client.get("/jobs?user_status=saved").data.decode("utf-8")
    assert "Saved Role" in saved
    assert "Applied Role" not in saved and "Untouched Role" not in saved

    applied = app_client.get("/jobs?user_status=applied").data.decode("utf-8")
    assert "Applied Role" in applied and "Saved Role" not in applied

    # An unrecognised value is ignored rather than silently narrowing the list.
    bogus = app_client.get("/jobs?user_status=bogus").data.decode("utf-8")
    assert "Saved Role" in bogus and "Untouched Role" in bogus


def test_jobs_list_radar_filter_combines_with_state(app_client, monkeypatch):
    fake_db = FakeDB(
        jobs=[
            {"_id": ObjectId(), "title": "Open Saved", "user_status": "saved",
             "state": "open"},
            {"_id": ObjectId(), "title": "Closed Applied", "user_status": "applied",
             "state": "closed"},
            {"_id": ObjectId(), "title": "Open Untouched", "state": "open"},
        ],
        profiles=[],
    )

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    # Both filters narrow together: state excludes the closed one, radar
    # excludes the untriaged one.
    body = app_client.get("/jobs?user_status=radar&state=open").data.decode("utf-8")
    assert "Open Saved" in body
    assert "Closed Applied" not in body and "Open Untouched" not in body


def test_jobs_list_offers_the_radar_filter(app_client, monkeypatch):
    fake_db = _radar_db()

    import app.routes_jobs as routes_jobs

    monkeypatch.setattr(routes_jobs, "get_db", lambda: fake_db)

    body = app_client.get("/jobs").data.decode("utf-8")
    assert "On my radar" in body
    assert "user_status=radar" in body
