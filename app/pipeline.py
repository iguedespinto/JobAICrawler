"""Synchronous pipeline orchestration for crawling and scoring jobs."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from typing import Iterable, List, Tuple

from pymongo import MongoClient
from dotenv import load_dotenv

from .crawlers.base import BaseCrawler
from .crawlers.demo_site import DemoSiteCrawler
from .llm_client import enrich_job, generate_job_leads, score_job

logger = logging.getLogger(__name__)

STATUS_NEW_RAW = "new_raw"
STATUS_ENRICHED = "enriched"
STATUS_SCORED = "scored"
PROFILE_ID = "default"


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


def process_new_jobs(batch_size: int = 50) -> int:
    """Enrich jobs with status new_raw using the LLM API."""
    client, db = _get_db()
    jobs_collection = db.jobs
    processed = 0

    cursor = jobs_collection.find({"status": STATUS_NEW_RAW}).limit(batch_size)
    for job in cursor:
        logger.info("Enriching job %s", job.get("_id"))
        enrichment = enrich_job(job.get("description_text", ""))
        jobs_collection.update_one(
            {"_id": job["_id"]},
            {
                "$set": {
                    "enriched": enrichment,
                    "status": STATUS_ENRICHED,
                    "enriched_at": _utcnow(),
                }
            },
        )
        processed += 1

    client.close()
    logger.info("Enriched %s jobs", processed)
    return processed


def score_jobs(batch_size: int = 50) -> int:
    """Score enriched jobs against the single user profile."""
    client, db = _get_db()
    jobs_collection = db.jobs
    profiles_collection = db.profiles

    profile = profiles_collection.find_one({"_id": PROFILE_ID})
    if not profile:
        logger.warning("No profile found with _id=default. Skipping scoring.")
        client.close()
        return 0

    processed = 0
    cursor = jobs_collection.find(
        {"status": STATUS_ENRICHED, "score": {"$exists": False}}
    ).limit(batch_size)
    for job in cursor:
        logger.info("Scoring job %s", job.get("_id"))
        scoring = score_job(job, profile.get("profile", {}))
        jobs_collection.update_one(
            {"_id": job["_id"]},
            {
                "$set": {
                    "score": scoring,
                    "fit_score": scoring.get("fit_score", 0),
                    "status": STATUS_SCORED,
                    "scored_at": _utcnow(),
                }
            },
        )
        processed += 1

    client.close()
    logger.info("Scored %s jobs", processed)
    return processed


def run_pipeline() -> None:
    """Run the full ingest -> enrich -> score pipeline."""
    ingest_jobs()
    process_new_jobs()
    score_jobs()


def generate_leads() -> dict:
    """Generate job search leads from the stored profile."""
    client, db = _get_db()
    profiles_collection = db.profiles

    profile = profiles_collection.find_one({"_id": PROFILE_ID})
    if not profile:
        logger.warning("No profile found with _id=default. Skipping leads.")
        client.close()
        return {"error": "profile not found"}

    leads = generate_job_leads(profile.get("profile", {}))
    profiles_collection.update_one(
        {"_id": PROFILE_ID},
        {"$set": {"leads": leads, "leads_generated_at": _utcnow()}},
    )
    client.close()
    logger.info("Generated leads for profile %s", PROFILE_ID)
    return leads


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Job pipeline runner.")
    parser.add_argument("command", choices=["ingest", "enrich", "score", "leads", "all"])
    parser.add_argument("--batch-size", type=int, default=50)
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> None:
    """CLI entrypoint for manual pipeline runs."""
    _configure_logging()
    args = _parse_args(argv)
    if args.command == "ingest":
        ingest_jobs()
    elif args.command == "enrich":
        process_new_jobs(batch_size=args.batch_size)
    elif args.command == "score":
        score_jobs(batch_size=args.batch_size)
    elif args.command == "all":
        run_pipeline()
    elif args.command == "leads":
        leads = generate_leads()
        print(json.dumps(leads, indent=2, ensure_ascii=True, default=str))


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
