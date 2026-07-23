"""Tests for the JSON import flow."""

from __future__ import annotations

import io
import json

from bson import ObjectId

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


def test_matching_considers_every_job_regardless_of_state():
    # There is no longer an "archived" exclusion: every job (open or closed) is
    # a match candidate, so an identical closed job is still matched by URL.
    fake_db = FakeDB(
        jobs=[
            {
                "title": "Backend Engineer",
                "company": "Acme",
                "url": "https://example.com/jobs/acme-1",
                "description_text": _DESCRIPTION,
                "state": "closed",
            }
        ],
        profiles=[],
    )

    jobs = [
        {
            "title": "Backend Engineer",
            "company": "Acme",
            "url": "https://example.com/jobs/acme-1",
            "description_text": _DESCRIPTION,
        }
    ]

    rows = routes_import.match_jobs(jobs, fake_db)

    assert rows[0]["status"] == "matched"
    assert rows[0]["reason"] == "url"


# ── The closest-match hover card ─────────────────────────────────────
#
# Every row's "closest match" link carries a card describing the matched
# opportunity, revealed on hover. The card data rides on each row as
# ``match_card`` and is rendered (hidden) into the page.


def test_match_card_carries_matched_db_job_details():
    existing_id = ObjectId()
    fake_db = FakeDB(
        jobs=[
            {
                "_id": existing_id,
                "title": "Data Architect",
                "company": "Kennedy & Partners Recruitment",
                "location": "Dublin",
                "url": "https://example.com/jobs/da",
                "salary": "70,000-75,000/year",
                "keywords": ["Data Architect", "Analytics"],
                "description_text": "Lead the data platform strategy. " * 30,
                "state": "open",
            }
        ],
        profiles=[],
    )

    jobs = [
        {"title": "Data Architect", "company": "Kennedy & Partners Recruitment",
         "url": "https://example.com/jobs/da"}
    ]

    rows = routes_import.match_jobs(jobs, fake_db)
    card = rows[0]["match_card"]

    # A database match links through to its detail page and mirrors the job card.
    assert card["job_id"] == str(existing_id)
    assert card["title"] == "Data Architect"
    assert card["company"] == "Kennedy & Partners Recruitment"
    assert card["location"] == "Dublin"
    assert card["salary"] == "70,000-75,000/year"
    assert card["state"] == "open"
    assert card["keywords"] == ["Data Architect", "Analytics"]
    # A trimmed description snippet, not the whole body.
    assert card["description"]
    assert len(card["description"]) <= 221


def test_match_card_for_in_file_duplicate_has_no_detail_link():
    # A row that matches an earlier row in the same upload points at that staged
    # row, which has no imported detail page — so no job_id to link through.
    fake_db = FakeDB(jobs=[], profiles=[])
    jobs = [
        {"title": "Brand New", "company": "Startup",
         "url": "https://example.com/bn", "salary": "€100k",
         "keywords": ["Go"], "description_text": "Build things."},
        {"title": "Brand New", "company": "Startup",
         "url": "https://example.com/bn"},
    ]

    rows = routes_import.match_jobs(jobs, fake_db)
    card = rows[1]["match_card"]

    assert rows[1]["status"] == "duplicate"
    assert card is not None
    assert card["job_id"] is None
    assert card["title"] == "Brand New"
    assert card["company"] == "Startup"


def test_match_card_absent_when_there_is_no_match():
    # An empty database gives a lone new row nothing to match against.
    fake_db = FakeDB(jobs=[], profiles=[])

    rows = routes_import.match_jobs(
        [{"title": "Solo", "company": "X", "url": "https://example.com/s"}],
        fake_db,
    )

    assert rows[0]["match_label"] is None
    assert rows[0]["match_card"] is None


def test_import_page_renders_match_card_with_detail_link(app_client, monkeypatch):
    existing_id = ObjectId()
    fake_db = FakeDB(
        jobs=[
            {
                "_id": existing_id,
                "title": "Data Architect",
                "company": "Kennedy & Partners Recruitment",
                "location": "Dublin",
                "url": "https://example.com/jobs/da",
                "salary": "70,000-75,000/year",
                "keywords": ["Analytics"],
                "description_text": "Own the enterprise data warehouse roadmap.",
                "state": "open",
            }
        ],
        profiles=[],
    )
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)

    # The staged row carries no salary of its own, so the salary below can only be
    # the matched job's, shown by the card.
    routes_import.stage_jobs(
        fake_db,
        [{"title": "Data Architect",
          "company": "Kennedy & Partners Recruitment",
          "url": "https://example.com/jobs/da"}],
    )

    body = app_client.get("/import").data.decode("utf-8")

    assert "match-card" in body
    assert f"/jobs/{existing_id}" in body          # links through to the detail page
    assert "70,000-75,000/year" in body            # the matched job's salary
    assert "Own the enterprise data warehouse roadmap." in body  # its description


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


def test_import_company_name_links_to_filtered_open_view(app_client, monkeypatch):
    fake_db = FakeDB(jobs=[], profiles=[])
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)

    payload = json.dumps(
        [{"name": "Data Architect", "company": "Kennedy & Partners",
          "url": "https://example.com/a"}]
    )
    assert _upload(app_client, payload).status_code == 302

    body = app_client.get("/import").data.decode("utf-8")
    # The company name is a link to that company's opportunities. No state is
    # passed, so the jobs list falls back to its open-by-default view, and it
    # opens in a new tab so the staging review is not lost.
    assert 'href="/jobs?company=Kennedy+%26+Partners"' in body
    assert 'target="_blank"' in body


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

    # Following the redirect surfaces a confirmation with counts and remainder.
    body = app_client.post(
        "/import/commit",
        data={"select": str(role_a["_id"])},  # already imported -> 0 processed
        follow_redirects=True,
    ).data.decode("utf-8")
    assert "No opportunities were selected to import." in body


def test_commit_flash_reports_processed_and_remaining(app_client, monkeypatch):
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

    body = app_client.post(
        "/import/commit",
        data={"select": str(role_a["_id"])},
        follow_redirects=True,
    ).data.decode("utf-8")

    # Confirms processing, the imported count, and how many remain staged.
    assert "Staging processed: imported 1 new opportunity" in body
    assert "1 still staged." in body


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


# ── Pre-staging bare URLs (unprocessed records) ──────────────────────

_UNPROCESSED = routes_import.STATUS_UNPROCESSED


def _pending_count(db):
    return db.import_staging.count_documents({"status": _UNPROCESSED})


def test_stage_urls_adds_dedupes_and_validates():
    fake_db = FakeDB(
        jobs=[{"title": "Known", "company": "Acme", "url": "https://example.com/known"}],
        profiles=[],
    )
    fake_db.import_staging.insert_one(
        {"url": "https://example.com/staged", "status": _UNPROCESSED}
    )

    result = routes_import.stage_urls(
        fake_db,
        [
            "https://example.com/new-1",       # added
            "https://example.com/new-1",       # duplicate within the batch
            "https://example.com/known",       # already an imported job
            "https://example.com/staged/",     # already staged (trailing slash)
            "not-a-url",                        # invalid
            "https://ie.indeed.com/q-dev-jobs.html",  # non-identifying search URL
        ],
    )

    assert result == {"added": 1, "skipped": 3, "invalid": 2}
    assert _pending_count(fake_db) == 2  # the pre-existing one plus the new one


def test_strip_url_params():
    s = routes_import._strip_url_params
    # All query params + fragment dropped -> clean base URL.
    assert s(
        "https://app.welcometothejungle.com/jobs/cPEm4jDG"
        "?position=3&count=3&utm_campaign=x&utm_medium=email&utm_source=email#top"
    ) == "https://app.welcometothejungle.com/jobs/cPEm4jDG"
    # LinkedIn navigation param is dropped (id is in the path).
    assert s("https://www.linkedin.com/jobs/view/4372131673/?alternateChannel=search") \
        == "https://www.linkedin.com/jobs/view/4372131673/"
    # Identifying params (Indeed's jk) are kept; everything else dropped.
    assert s("https://ie.indeed.com/viewjob?jk=abc123&utm_source=email&from=alert") \
        == "https://ie.indeed.com/viewjob?jk=abc123"
    # Non-URLs pass through untouched.
    assert s("not a url") == "not a url"


def test_stage_urls_strips_tracking_and_dedupes():
    fake_db = FakeDB(jobs=[], profiles=[])
    result = routes_import.stage_urls(
        fake_db,
        [
            "https://x.com/jobs/42?utm_source=email&position=1",
            "https://x.com/jobs/42?utm_campaign=other#apply",  # same posting, other tracking
        ],
    )
    assert result == {"added": 1, "skipped": 1, "invalid": 0}
    doc = fake_db.import_staging.find_one({"status": _UNPROCESSED})
    assert doc["url"] == "https://x.com/jobs/42"  # stored clean


def test_stage_urls_skips_url_of_any_existing_job():
    # Every job blocks a URL now (no archived exemption): a closed job's URL is
    # already known, so queuing it again is skipped.
    fake_db = FakeDB(
        jobs=[{"title": "Old", "company": "Acme",
               "url": "https://example.com/old", "state": "closed"}],
        profiles=[],
    )

    result = routes_import.stage_urls(fake_db, ["https://example.com/old"])

    assert result == {"added": 0, "skipped": 1, "invalid": 0}
    assert _pending_count(fake_db) == 0


def test_pending_urls_hidden_from_staging_view():
    fake_db = FakeDB(jobs=[], profiles=[])
    routes_import.stage_urls(fake_db, ["https://example.com/pending"])
    routes_import.stage_jobs(
        fake_db,
        [{"title": "Full", "company": "Acme", "url": "https://example.com/full"}],
    )

    rows = routes_import._staged_rows(fake_db)
    pending = routes_import._pending_urls(fake_db)

    assert [r["job"]["title"] for r in rows] == ["Full"]
    assert [p["url"] for p in pending] == ["https://example.com/pending"]


def test_import_promotes_matching_pending_url_in_place():
    fake_db = FakeDB(jobs=[], profiles=[])
    routes_import.stage_urls(fake_db, ["https://example.com/jobs/1"])
    assert _pending_count(fake_db) == 1

    result = routes_import.stage_jobs(
        fake_db,
        [{"title": "Backend Engineer", "company": "Acme",
          "url": "https://example.com/jobs/1", "description_text": "Build APIs"}],
    )

    # Promotion, not a new row: still a single staging record, now viewable.
    assert result == {"added": 1, "skipped": 0}
    assert fake_db.import_staging.count_documents({}) == 1
    assert _pending_count(fake_db) == 0
    doc = fake_db.import_staging.find_one({"url": "https://example.com/jobs/1"})
    assert doc["title"] == "Backend Engineer"
    assert doc["status"] == routes_import.STATUS_STAGED


def test_add_urls_route_and_clear(app_client, monkeypatch):
    fake_db = FakeDB(jobs=[], profiles=[])
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)

    response = app_client.post(
        "/import/urls",
        data={"urls": "https://example.com/a\nhttps://example.com/b"},
    )
    assert response.status_code == 302
    assert _pending_count(fake_db) == 2

    # Clearing the staging area leaves pending URLs untouched...
    app_client.post("/import/clear")
    assert _pending_count(fake_db) == 2

    # ...but clearing pending URLs empties them.
    app_client.post("/import/urls/clear")
    assert _pending_count(fake_db) == 0


# ── Open / closed opportunity state ──────────────────────────────────


def test_parse_jobs_state_defaults_open_and_reads_closed():
    raw = json.dumps(
        [
            {"name": "A", "url": "https://example.com/a"},
            {"name": "B", "url": "https://example.com/b", "state": "closed"},
            {"name": "C", "url": "https://example.com/c", "status": "expired"},
            {"name": "D", "url": "https://example.com/d", "state": "OPEN"},
        ]
    )

    jobs = routes_import.parse_jobs(raw)

    assert [j["state"] for j in jobs] == ["open", "closed", "closed", "open"]


def test_preselect_reflects_state_and_match():
    fake_db = FakeDB(
        jobs=[{"title": "Existing", "company": "Acme",
               "url": "https://example.com/e"}],
        profiles=[],
    )
    jobs = [
        # open + matched -> not selected
        {"title": "Existing", "company": "Acme", "url": "https://example.com/e"},
        # open + new -> selected
        {"title": "Fresh Open", "company": "Beta", "url": "https://example.com/f"},
        # closed + matched -> selected (will close the match)
        {"title": "Existing", "company": "Acme",
         "url": "https://example.com/e", "state": "closed"},
        # closed + new -> selected (import as closed record)
        {"title": "Fresh Closed", "company": "Gamma",
         "url": "https://example.com/g", "state": "closed"},
    ]

    rows = routes_import.match_jobs(jobs, fake_db)

    assert [r["state"] for r in rows] == ["open", "open", "closed", "closed"]
    assert [r["preselect"] for r in rows] == [False, True, True, True]


def test_commit_open_new_sets_state_open(app_client, monkeypatch):
    fake_db = FakeDB(jobs=[], profiles=[])
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)

    routes_import.stage_jobs(
        fake_db,
        [{"title": "Role A", "company": "Acme", "url": "https://example.com/a"}],
    )
    staged = fake_db.import_staging.find_one({"title": "Role A"})

    app_client.post("/import/commit", data={"select": str(staged["_id"])})

    job = fake_db.jobs.find_one({"title": "Role A"})
    assert job is not None
    assert job["state"] == "open"


def test_commit_closed_match_closes_existing_without_new_record(app_client, monkeypatch):
    existing_id = ObjectId()
    fake_db = FakeDB(
        jobs=[{"_id": existing_id, "title": "Backend Engineer", "company": "Acme",
               "url": "https://example.com/jobs/1", "state": "open",
               "site": "import"}],
        profiles=[],
    )
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)

    routes_import.stage_jobs(
        fake_db,
        [{"title": "Backend Engineer", "company": "Acme",
          "url": "https://example.com/jobs/1", "state": "closed"}],
    )
    staged = fake_db.import_staging.find_one({"status": routes_import.STATUS_STAGED})

    body = app_client.post(
        "/import/commit",
        data={"select": str(staged["_id"])},
        follow_redirects=True,
    ).data.decode("utf-8")

    # The existing job is closed in place; no second record is created.
    assert fake_db.jobs.count_documents({}) == 1
    assert fake_db.jobs.find_one({"_id": existing_id})["state"] == "closed"
    assert fake_db.import_staging.find_one({"_id": staged["_id"]}) is None
    assert "closed 1 existing" in body


def test_matching_spans_open_and_closed_existing_jobs():
    # Matching never filters on state, so closed jobs stay candidates: an incoming
    # opportunity (open or closed) matches existing open AND closed jobs.
    fake_db = FakeDB(
        jobs=[
            {"title": "Open Role", "company": "Acme",
             "url": "https://example.com/open", "state": "open"},
            {"title": "Closed Role", "company": "Beta",
             "url": "https://example.com/closed", "state": "closed"},
        ],
        profiles=[],
    )
    incoming = [
        # closed import vs an existing OPEN job
        {"title": "Open Role", "company": "Acme",
         "url": "https://example.com/open", "state": "closed"},
        # closed import vs an existing CLOSED job
        {"title": "Closed Role", "company": "Beta",
         "url": "https://example.com/closed", "state": "closed"},
        # open import vs an existing CLOSED job
        {"title": "Closed Role", "company": "Beta",
         "url": "https://example.com/closed"},
    ]

    rows = routes_import.match_jobs(incoming, fake_db)
    assert [r["status"] for r in rows] == ["matched", "matched", "matched"]

    # And the close-target lookup finds both open and closed matches.
    assert routes_import._find_existing_job(fake_db, incoming[0]) is not None
    assert routes_import._find_existing_job(fake_db, incoming[1]) is not None


def test_commit_closed_match_closes_already_closed_job(app_client, monkeypatch):
    existing_id = ObjectId()
    fake_db = FakeDB(
        jobs=[{"_id": existing_id, "title": "Closed Role", "company": "Beta",
               "url": "https://example.com/closed", "state": "closed",
               "site": "import"}],
        profiles=[],
    )
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)

    routes_import.stage_jobs(
        fake_db,
        [{"title": "Closed Role", "company": "Beta",
          "url": "https://example.com/closed", "state": "closed"}],
    )
    staged = fake_db.import_staging.find_one({"status": routes_import.STATUS_STAGED})

    app_client.post("/import/commit", data={"select": str(staged["_id"])})

    # Matched the already-closed job (idempotent); no duplicate record created.
    assert fake_db.jobs.count_documents({}) == 1
    assert fake_db.jobs.find_one({"_id": existing_id})["state"] == "closed"


def test_commit_closed_no_match_imports_closed_record(app_client, monkeypatch):
    fake_db = FakeDB(jobs=[], profiles=[])
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)

    routes_import.stage_jobs(
        fake_db,
        [{"title": "Gone Role", "company": "Acme",
          "url": "https://example.com/gone", "state": "closed"}],
    )
    staged = fake_db.import_staging.find_one({"title": "Gone Role"})

    app_client.post("/import/commit", data={"select": str(staged["_id"])})

    job = fake_db.jobs.find_one({"title": "Gone Role"})
    assert job is not None
    assert job["state"] == "closed"
    assert job["status"] == routes_import.STATUS_IMPORTED


# ── Queued URLs arrive saved (and so, on the radar) ──────────────────


def test_stage_urls_marks_queued_urls_as_saved():
    fake_db = FakeDB(jobs=[], profiles=[])

    routes_import.stage_urls(fake_db, ["https://example.com/a"], source="manual")

    pending = fake_db.import_staging.find_one({"url": "https://example.com/a"})
    assert pending["user_status"] == routes_import.USER_STATUS_SAVED


def test_queued_url_stays_saved_through_enrichment_and_commit(app_client, monkeypatch):
    """A URL queued on /import carries its saved mark all the way to the job."""
    fake_db = FakeDB(jobs=[], profiles=[])
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)

    routes_import.stage_urls(fake_db, ["https://example.com/a"], source="manual")

    # An MCP client enriches the URL and imports a file, promoting the pending
    # record in place. The promotion must not drop the mark.
    routes_import.stage_jobs(
        fake_db,
        [{"title": "Role A", "company": "Acme", "url": "https://example.com/a"}],
        source="enriched.json",
    )
    staged = fake_db.import_staging.find_one({"title": "Role A"})
    assert staged["user_status"] == routes_import.USER_STATUS_SAVED

    app_client.post("/import/commit", data={"select": str(staged["_id"])})

    assert fake_db.jobs.find_one({"title": "Role A"})["user_status"] == (
        routes_import.USER_STATUS_SAVED
    )


def test_file_uploaded_opportunity_is_not_marked(app_client, monkeypatch):
    """Only URLs queued in the import view get the mark -- not plain uploads."""
    fake_db = FakeDB(jobs=[], profiles=[])
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)

    routes_import.stage_jobs(
        fake_db,
        [{"title": "Role B", "company": "Beta", "url": "https://example.com/b"}],
    )
    staged = fake_db.import_staging.find_one({"title": "Role B"})

    app_client.post("/import/commit", data={"select": str(staged["_id"])})

    assert fake_db.jobs.find_one({"title": "Role B"}).get("user_status") is None


def test_commit_leaves_an_existing_jobs_user_status_alone(app_client, monkeypatch):
    """Importing over a known job must not knock it back from applied to saved."""
    external_id = routes_import.BaseCrawler.hash_external_id(
        routes_import.IMPORT_SITE, "https://example.com/a"
    )
    existing_id = ObjectId()
    fake_db = FakeDB(
        jobs=[
            {
                "_id": existing_id,
                "title": "Role A",
                "company": "Acme",
                "url": "https://example.com/a",
                "site": routes_import.IMPORT_SITE,
                "external_id": external_id,
                "user_status": "applied",
            }
        ],
        profiles=[],
    )
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)

    fake_db.import_staging.insert_one(
        {
            "title": "Role A",
            "company": "Acme",
            "url": "https://example.com/a",
            "state": routes_import.STATE_OPEN,
            "status": routes_import.STATUS_STAGED,
            "user_status": routes_import.USER_STATUS_SAVED,
        }
    )
    staged = fake_db.import_staging.find_one({"status": routes_import.STATUS_STAGED})

    app_client.post("/import/commit", data={"select": str(staged["_id"])})

    assert fake_db.jobs.find_one({"_id": existing_id})["user_status"] == "applied"


# ── Hiding 100% matches ──────────────────────────────────────────────
#
# The staging list can hide rows whose Similarity reads exactly 100% — the ones
# needing no decision — so the fuzzy middle stands out. The flag is computed
# server-side; the hiding itself is a client-side, purely visual toggle (see
# tests/browser/test_import_menu.py), so a hidden row still commits as before.


def test_full_match_flags_rows_at_exactly_100_percent():
    """``full_match`` tracks the Similarity column, not the match reason.

    URL, title+company and in-file repeats all score 100. An identical
    description scores 100 as well, so the flag keys off the percentage itself.
    """
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
        # 100% — same URL.
        {"title": "Different name", "company": "Whatever",
         "url": "https://example.com/jobs/existing/"},
        # 100% — same title + company.
        {"title": "lead engineer", "company": "globex",
         "url": "https://example.com/jobs/new-url"},
        # 0% — brand new, nothing to compare against.
        {"title": "Brand New", "company": "Startup",
         "url": "https://example.com/jobs/brand-new"},
        # 100% — repeated within this same upload.
        {"title": "Brand New", "company": "Startup",
         "url": "https://example.com/jobs/brand-new"},
    ]

    rows = routes_import.match_jobs(jobs, fake_db)

    assert [r["full_match"] for r in rows] == [True, True, False, True]
    # The flag is exactly "the Similarity column reads 100%".
    for row in rows:
        assert row["full_match"] is (row["similarity"] == 100)


def test_partial_similarity_is_not_a_full_match():
    """A row that only partly resembles an existing job stays visible.

    These are the judgement calls the hide is meant to leave on screen.
    """
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

    jobs = [
        {
            "title": "Data Analyst",
            "company": "Globex",
            "url": "https://example.com/jobs/globex-9",
            "description_text": (
                "Backend engineer building payment APIs with Python and Flask. "
                "Very different remainder: reporting dashboards, spreadsheets, "
                "stakeholder briefings and quarterly forecasting cycles."
            ),
        }
    ]

    rows = routes_import.match_jobs(jobs, fake_db)

    assert 0 < rows[0]["similarity"] < 100
    assert rows[0]["full_match"] is False


def test_identical_description_counts_as_a_full_match():
    """Same description, different title/company/url — still 100%, so hidable."""
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

    jobs = [
        {
            "title": "Backend Developer",
            "company": "Globex",
            "url": "https://example.com/jobs/globex-9",
            "description_text": _DESCRIPTION,
        }
    ]

    rows = routes_import.match_jobs(jobs, fake_db)

    assert rows[0]["similarity"] == 100
    assert rows[0]["full_match"] is True


def _staging_db_with_a_full_and_a_new_row(monkeypatch):
    fake_db = FakeDB(
        jobs=[
            {
                "title": "Existing Role",
                "company": "Acme",
                "url": "https://example.com/jobs/existing",
            }
        ],
        profiles=[],
    )
    monkeypatch.setattr(routes_import, "get_db", lambda: fake_db)
    routes_import.stage_jobs(
        fake_db,
        [
            # 100% match on URL.
            {"title": "Existing Role", "company": "Acme",
             "url": "https://example.com/jobs/existing"},
            # Brand new.
            {"title": "Fresh Role", "company": "Startup",
             "url": "https://example.com/jobs/fresh"},
        ],
    )
    return fake_db


def test_import_page_marks_full_match_rows_and_counts_them(app_client, monkeypatch):
    _staging_db_with_a_full_and_a_new_row(monkeypatch)

    body = app_client.get("/import").data.decode("utf-8")

    # Exactly one of the two staged rows is flagged in the markup.
    assert body.count('data-full-match="1"') == 1
    # Both rows are still rendered — hiding is the browser's job, not the server's.
    assert "Existing Role" in body
    assert "Fresh Role" in body


def test_import_page_offers_the_hide_toggle_with_its_count(app_client, monkeypatch):
    _staging_db_with_a_full_and_a_new_row(monkeypatch)

    body = app_client.get("/import").data.decode("utf-8")

    # The control and the menu entry that drive the toggle, plus the count of
    # what would be hidden.
    assert 'data-action="toggle-full-matches"' in body
    assert 'data-full-match-count="1"' in body


def test_staging_summary_counts_full_matches(app_client, monkeypatch):
    fake_db = _staging_db_with_a_full_and_a_new_row(monkeypatch)

    rows = routes_import._staged_rows(fake_db)

    assert sum(1 for r in rows if r["full_match"]) == 1
