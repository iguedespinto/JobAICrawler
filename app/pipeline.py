"""Synchronous pipeline orchestration for crawling jobs into MongoDB.

Enrichment, scoring and lead generation used to run here via an LLM. Those
features have been removed: opportunities are now prepared externally and
brought in through the JSON import flow (see ``app/routes_import.py``).
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone
from typing import Iterable, List, Tuple

from pymongo import MongoClient
from dotenv import load_dotenv

from .crawlers.base import BaseCrawler
from .crawlers.demo_site import DemoSiteCrawler

logger = logging.getLogger(__name__)

STATUS_NEW_RAW = "new_raw"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_db() -> Tuple[MongoClient, object]:
    load_dotenv()
    mongo_uri = os.getenv("MONGODB_URI", "") or os.getenv("MONGO_URI", "")
    db_name = os.getenv("MONGO_DB_NAME", "jobs_db")
    if not mongo_uri:
        raise RuntimeError("MONGODB_URI is not set.")
    client = MongoClient(mongo_uri)
    return client, client[db_name]


def _configured_crawlers() -> List[BaseCrawler]:
    """Return the list of crawler instances to run."""
    return [DemoSiteCrawler()]


def _ensure_indexes(collection) -> None:
    collection.create_index([("site", 1), ("external_id", 1)], unique=True)
    collection.create_index([("status", 1)])


def ingest_jobs() -> int:
    """Fetch jobs from crawlers and upsert into MongoDB."""
    client, db = _get_db()
    jobs_collection = db.jobs
    _ensure_indexes(jobs_collection)

    total_upserts = 0
    for crawler in _configured_crawlers():
        logger.info("Fetching jobs from %s", crawler.site)
        raw_jobs = crawler.normalize_jobs(crawler.fetch_jobs())
        for job in raw_jobs:
            now = _utcnow()
            update = {
                "$set": {
                    "title": job["title"],
                    "company": job["company"],
                    "location": job["location"],
                    "url": job["url"],
                    "site": job["site"],
                    "external_id": job["external_id"],
                    "description_text": job["description_text"],
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "status": STATUS_NEW_RAW,
                    "created_at": now,
                },
            }
            result = jobs_collection.update_one(
                {"site": job["site"], "external_id": job["external_id"]},
                update,
                upsert=True,
            )
            if result.upserted_id is not None:
                total_upserts += 1

    client.close()
    logger.info("Ingested %s new jobs", total_upserts)
    return total_upserts


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Job pipeline runner.")
    parser.add_argument("command", choices=["ingest"])
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> None:
    """CLI entrypoint for manual pipeline runs."""
    _configure_logging()
    args = _parse_args(argv)
    if args.command == "ingest":
        ingest_jobs()


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
