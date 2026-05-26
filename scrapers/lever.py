"""Lever public job board API.

Endpoint: https://api.lever.co/v0/postings/{slug}?mode=json
Docs:     https://github.com/lever/postings-api

No auth for public companies. Returns a flat list of postings.
"""

from __future__ import annotations

import requests

from .base import BaseScraper, JobPosting, ScrapeError


class LeverScraper(BaseScraper):
    ATS_NAME = "lever"
    API_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"

    def fetch(self) -> list[JobPosting]:
        slug = self._require_config("slug")
        url = self.API_URL.format(slug=slug)

        try:
            resp = requests.get(url, timeout=self.TIMEOUT_SECONDS)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ScrapeError(f"Lever fetch failed for {slug!r}: {exc} ({url})")

        try:
            data = resp.json()
        except ValueError as exc:
            raise ScrapeError(f"Lever returned non-JSON for {slug!r}: {exc}")

        if not isinstance(data, list):
            raise ScrapeError(
                f"Lever returned unexpected shape for {slug!r}: expected list, got {type(data).__name__}"
            )

        postings = []
        for job in data:
            title = (job.get("text") or "").strip()
            hosted_url = (job.get("hostedUrl") or "").strip()
            if not title or not hosted_url:
                continue

            categories = job.get("categories") or {}
            postings.append(
                JobPosting(
                    company=self.company_name,
                    title=title,
                    url=hosted_url,
                    location=(categories.get("location") or "").strip(),
                    department=(categories.get("team") or "").strip(),
                )
            )
        return postings
