"""SmartRecruiters public job board API.

Endpoint: https://api.smartrecruiters.com/v1/companies/{company}/postings
Docs:     https://dev.smartrecruiters.com/customer-api/posting-api/

Public; no auth needed. Paginated via `limit` and `offset`.
"""

from __future__ import annotations

import requests

from .base import BaseScraper, JobPosting, ScrapeError

_PAGE_LIMIT = 100
_MAX_PAGES = 20  # Safety cap; 20 * 100 = 2000 jobs, plenty.


class SmartRecruitersScraper(BaseScraper):
    ATS_NAME = "smartrecruiters"
    API_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings"

    def fetch(self) -> list[JobPosting]:
        company = self._require_config("company")
        url = self.API_URL.format(company=company)

        postings: list[JobPosting] = []
        for page in range(_MAX_PAGES):
            params = {"limit": _PAGE_LIMIT, "offset": page * _PAGE_LIMIT}
            try:
                resp = requests.get(url, params=params, timeout=self.TIMEOUT_SECONDS)
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise ScrapeError(
                    f"SmartRecruiters fetch failed for {company!r} on page {page}: {exc} ({url})"
                )

            try:
                data = resp.json()
            except ValueError as exc:
                raise ScrapeError(
                    f"SmartRecruiters returned non-JSON for {company!r}: {exc}"
                )

            content = data.get("content", [])
            for job in content:
                title = (job.get("name") or "").strip()
                job_id = (job.get("id") or "").strip()
                if not title or not job_id:
                    continue

                ref_url = (job.get("ref") or "").strip()
                # ref is the API URL; we want the public application URL.
                public_url = f"https://jobs.smartrecruiters.com/{company}/{job_id}"

                location = ""
                loc = job.get("location") or {}
                if isinstance(loc, dict):
                    parts = [loc.get("city"), loc.get("region"), loc.get("country")]
                    location = ", ".join(p for p in parts if p)

                department = ""
                dept = job.get("department") or {}
                if isinstance(dept, dict):
                    department = (dept.get("label") or "").strip()

                postings.append(
                    JobPosting(
                        company=self.company_name,
                        title=title,
                        url=public_url,
                        location=location,
                        department=department,
                    )
                )

            # If we got fewer than a full page, we're done.
            total = data.get("totalFound", 0)
            if (page + 1) * _PAGE_LIMIT >= total:
                return postings

        raise ScrapeError(
            f"SmartRecruiters pagination for {company!r} exceeded {_MAX_PAGES} pages"
        )
