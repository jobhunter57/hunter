"""Ashby public job board API.

Endpoint: https://api.ashbyhq.com/posting-api/job-board/{org}
Docs:     https://developers.ashbyhq.com/reference/publicjobpostings

No auth required for public job boards. Newer ATS (popular with 2023+
startups), so URL schema is the most likely to drift — keep an eye on it.
"""

from __future__ import annotations

import requests

from .base import BaseScraper, JobPosting, ScrapeError


class AshbyScraper(BaseScraper):
    ATS_NAME = "ashby"
    API_URL = "https://api.ashbyhq.com/posting-api/job-board/{org}"

    def fetch(self) -> list[JobPosting]:
        org = self._require_config("org")
        url = self.API_URL.format(org=org)

        try:
            resp = requests.get(url, timeout=self.TIMEOUT_SECONDS)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ScrapeError(f"Ashby fetch failed for {org!r}: {exc} ({url})")

        try:
            data = resp.json()
        except ValueError as exc:
            raise ScrapeError(f"Ashby returned non-JSON for {org!r}: {exc}")

        postings = []
        for job in data.get("jobs", []):
            title = (job.get("title") or "").strip()
            job_url = (job.get("jobUrl") or "").strip()
            if not title or not job_url:
                continue

            postings.append(
                JobPosting(
                    company=self.company_name,
                    title=title,
                    url=job_url,
                    location=(job.get("locationName") or job.get("location") or "").strip(),
                    department=(job.get("department") or "").strip(),
                )
            )
        return postings
