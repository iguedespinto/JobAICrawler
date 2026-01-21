"""Tests for pipeline orchestration."""

from __future__ import annotations

from bson import ObjectId

from app.crawlers.base import BaseCrawler
from app import pipeline


class StaticCrawler(BaseCrawler):
    def __init__(self) -> None:
        super().__init__(site="static")

    def fetch_jobs(self):
        return [
            {
                "title": "Backend Engineer",
                "company": "Acme",
                "location": "Remote",
                "url": "https://example.com/jobs/1",
                "site": self.site,
                "external_id": "1",
                "description_text": "Python role",
            }
        ]


def test_ingest_jobs_upserts(mocker):
    from tests.conftest import FakeClient, FakeDB

    fake_client = FakeClient()
    fake_db = FakeDB(jobs=[], profiles=[])
    mocker.patch("app.pipeline._get_db", return_value=(fake_client, fake_db))
    mocker.patch("app.pipeline._configured_crawlers", return_value=[StaticCrawler()])

    count = pipeline.ingest_jobs()

    assert count == 1
    assert fake_db.jobs.find_one({"site": "static", "external_id": "1"}) is not None


def test_process_new_jobs_enriches(mocker):
    from tests.conftest import FakeClient, FakeDB

    job_id = ObjectId()
    fake_db = FakeDB(
        jobs=[{"_id": job_id, "status": pipeline.STATUS_NEW_RAW, "description_text": "Text"}],
        profiles=[],
    )
    fake_client = FakeClient()
    mocker.patch("app.pipeline._get_db", return_value=(fake_client, fake_db))
    mocker.patch("app.pipeline.enrich_job", return_value={"normalized_role": "Backend"})

    count = pipeline.process_new_jobs()

    job = fake_db.jobs.find_one({"_id": job_id})
    assert count == 1
    assert job["status"] == pipeline.STATUS_ENRICHED
    assert job["enriched"]["normalized_role"] == "Backend"


def test_score_jobs_updates_scores(mocker):
    from tests.conftest import FakeClient, FakeDB

    job_id = ObjectId()
    fake_db = FakeDB(
        jobs=[{"_id": job_id, "status": pipeline.STATUS_ENRICHED}],
        profiles=[{"_id": pipeline.PROFILE_ID, "profile": {"skills": ["Python"]}}],
    )
    fake_client = FakeClient()
    mocker.patch("app.pipeline._get_db", return_value=(fake_client, fake_db))
    mocker.patch(
        "app.pipeline.score_job",
        return_value={"fit_score": 85, "summary": "Good fit"},
    )

    count = pipeline.score_jobs()

    job = fake_db.jobs.find_one({"_id": job_id})
    assert count == 1
    assert job["status"] == pipeline.STATUS_SCORED
    assert job["fit_score"] == 85
