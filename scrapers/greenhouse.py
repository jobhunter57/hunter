"""Greenhouse public job board API.

Endpoint: https://boards-api.greenhouse.io/v1/boards/{board}/jobs
Docs:     https://developers.greenhouse.io/job-board.html

No authentication required for public boards. Stable API; we've relied on
it in production for years without breaking changes.
"""

from __future__ import annotations

import requests

from .base import BaseScraper, JobPosting, ScrapeError


class GreenhouseScraper(BaseScraper):
    ATS_NAME = "greenhouse"
    API_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"

    def fetch(self) -> list[JobPosting]:
        board = self._require_config("board")
        url = self.API_URL.format(board=board)

        try:
            resp = requests.get(url, timeout=self.TIMEOUT_SECONDS)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ScrapeError(f"Greenhouse fetch failed for {board!r}: {exc} ({url})")

        try:
            data = resp.json()
        except ValueError as exc:
            raise ScrapeError(f"Greenhouse returned non-JSON for {board!r}: {exc}")

        postings = []
        for job in data.get("jobs", []):
            title = job.get("title", "").strip()
            absolute_url = job.get("absolute_url", "").strip()
            if not title or not absolute_url:
                continue

            location = ""
            loc_obj = job.get("location")
            if isinstance(loc_obj, dict):
                location = loc_obj.get("name", "") or ""

            department = ""
            depts = job.get("departments") or []
            if depts and isinstance(depts[0], dict):
                department = depts[0].get("name", "") or ""

            postings.append(
                JobPosting(
                    company=self.company_name,
                    title=title,
                    url=absolute_url,
                    location=location,
                    department=department,
                )
            )
        return postings
