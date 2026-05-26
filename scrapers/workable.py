"""Workable public job board API.

Endpoint: POST https://apply.workable.com/api/v3/accounts/{slug}/jobs
Body:     {"query": ""}

Returns paginated results in `results` key with continuation tokens. We
follow the cursor until exhausted. For an active hiring company this is
typically 1-3 pages.

There's also a simpler GET endpoint at apply.workable.com/api/v1/widget/
but it's been deprecated multiple times. The v3 POST API is current as of
scaffolding.
"""

from __future__ import annotations

import requests

from .base import BaseScraper, JobPosting, ScrapeError

# Hard cap on pagination to avoid runaway loops if the API misbehaves.
# 50 pages * ~10 jobs/page = 500 jobs, plenty of headroom.
_MAX_PAGES = 50


def _to_str(value) -> str:
    """Coerce a Workable field that may be string/list/dict to a clean string.

    Workable's API inconsistently returns some fields (notably `department`)
    as lists or objects depending on account settings — Hugging Face's
    account, for example, returns `department` as a list. Always returning
    a string keeps the rest of the scraper simple and avoids AttributeError
    when downstream code calls .strip() on the value.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ", ".join(_to_str(v) for v in value if v)
    if isinstance(value, dict):
        return _to_str(value.get("name") or value.get("title") or "")
    return str(value).strip()


class WorkableScraper(BaseScraper):
    ATS_NAME = "workable"
    API_URL = "https://apply.workable.com/api/v3/accounts/{slug}/jobs"

    def fetch(self) -> list[JobPosting]:
        slug = self._require_config("slug")
        url = self.API_URL.format(slug=slug)

        postings: list[JobPosting] = []
        token: str | None = None
        for page in range(_MAX_PAGES):
            body: dict = {"query": ""}
            if token:
                body["token"] = token

            try:
                resp = requests.post(url, json=body, timeout=self.TIMEOUT_SECONDS)
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise ScrapeError(
                    f"Workable fetch failed for {slug!r} on page {page}: {exc} ({url})"
                )

            try:
                data = resp.json()
            except ValueError as exc:
                raise ScrapeError(f"Workable returned non-JSON for {slug!r}: {exc}")

            for job in data.get("results", []):
                title = _to_str(job.get("title"))
                shortcode = _to_str(job.get("shortcode"))
                if not title or not shortcode:
                    continue

                # Workable doesn't return an absolute URL in this API; reconstruct it.
                job_url = f"https://apply.workable.com/{slug}/j/{shortcode}/"

                location = ""
                loc = job.get("location") or {}
                if isinstance(loc, dict):
                    parts = [loc.get("city"), loc.get("region"), loc.get("country")]
                    location = ", ".join(p for p in parts if p)

                postings.append(
                    JobPosting(
                        company=self.company_name,
                        title=title,
                        url=job_url,
                        location=location,
                        department=_to_str(job.get("department")),
                    )
                )

            token = data.get("nextPage")
            if not token:
                return postings

        # If we hit the page cap, something is wrong — surface it loudly.
        raise ScrapeError(
            f"Workable pagination for {slug!r} exceeded {_MAX_PAGES} pages — likely a broken cursor"
        )
