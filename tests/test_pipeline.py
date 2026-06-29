"""Tests for pipeline orchestration."""

from __future__ import annotations

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
