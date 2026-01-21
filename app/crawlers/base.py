"""Base crawler interfaces and helpers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, Iterable, List


REQUIRED_FIELDS = {
    "title",
    "company",
    "location",
    "url",
    "site",
    "external_id",
    "description_text",
}


@dataclass
class BaseCrawler:
    """Base class for job site crawlers.

    Subclasses should implement fetch_jobs() and return raw job dictionaries
    containing at least the REQUIRED_FIELDS.
    """

    site: str

    def fetch_jobs(self) -> List[Dict]:
        """Return a list of raw job dicts from the site."""
        raise NotImplementedError

    def normalize_jobs(self, jobs: Iterable[Dict]) -> List[Dict]:
        """Validate job payloads and enforce the required fields."""
        normalized = []
        for job in jobs:
            missing = REQUIRED_FIELDS.difference(job.keys())
            if missing:
                raise ValueError(f"Job missing required fields: {sorted(missing)}")
            normalized.append(job)
        return normalized

    @staticmethod
    def hash_external_id(site: str, external_id: str) -> str:
        """Create a stable hash to use as a secondary dedup key."""
        payload = f"{site}:{external_id}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
