"""Demo crawler that returns static job data for testing the pipeline."""

from __future__ import annotations

from typing import Dict, List

from .base import BaseCrawler


class DemoSiteCrawler(BaseCrawler):
    """A simple crawler implementation with static jobs."""

    def __init__(self) -> None:
        super().__init__(site="demo_site")

    def fetch_jobs(self) -> List[Dict]:
        """Return a fixed set of jobs to exercise the pipeline."""
        return [
            {
                "title": "Backend Engineer",
                "company": "Acme Corp",
                "location": "Remote",
                "url": "https://example.com/jobs/1",
                "site": self.site,
                "external_id": "acme-1",
                "description_text": (
                    "We are looking for a backend engineer with Python, "
                    "Flask, and MongoDB experience to build APIs."
                ),
            },
            {
                "title": "Senior Full Stack Engineer",
                "company": "Globex",
                "location": "Dublin, Ireland",
                "url": "https://example.com/jobs/2",
                "site": self.site,
                "external_id": "globex-2",
                "description_text": (
                    "Full stack role working with React, Flask, and AWS. "
                    "Experience with data pipelines is a plus."
                ),
            },
        ]
