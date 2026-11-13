"""Workday public career-site API (the JSON behind *.myworkdayjobs.com).

ats_config keys (companies.yaml):
  tenant        required, e.g. nvidia
  host          required, e.g. wd5
  site          required, e.g. NVIDIAExternalCareerSite
  search_terms  optional list of search phrases, each searched separately.

Hard limits keep one slow company from stalling the whole run.
"""

from __future__ import annotations

import logging
import re
import time

import requests

from .base import BaseScraper, JobPosting, ScrapeError

log = logging.getLogger("scrape.workday")

PAGE_SIZE = 20
MAX_PAGES = 15              # 300 jobs per search term
MAX_DETAIL_LOOKUPS = 40     # location lookups per company
TIME_BUDGET_SECONDS = 90    # wall-clock cap per company
DEFAULT_TERMS = ["engineering manager", "head of engineering"]
MULTI_LOCATION = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)
FALLBACK_TITLE = re.compile(r"senior|sr\.?|head of|manager", re.IGNORECASE)
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; job-alert-bot)",
}


def _title_filter():
    """Same rules as titles.yaml, so only relevant jobs get detail lookups."""
    try:
        from matcher import TitleMatcher
        matcher = TitleMatcher.from_yaml("titles.yaml")
        return lambda title: matcher.match(title) is not None
    except Exception:  # noqa: BLE001
        return lambda title: FALLBACK_TITLE.search(title) is not None


class WorkdayScraper(BaseScraper):
    ATS_NAME = "workday"

    def _cfg(self) -> dict:
        for name in ("ats_config", "config", "cfg", "_config"):
            value = getattr(self, name, None)
            if isinstance(value, dict):
                return value
        for value in vars(self).values():
            if isinstance(value, dict) and "tenant" in value:
                return value
        return {}

    def fetch(self) -> list[JobPosting]:
        tenant = self._require_config("tenant")
        host = self._require_config("host")
        site = self._require_config("site")
        terms = self._cfg().get("search_terms") or DEFAULT_TERMS
        if isinstance(terms, str):
            terms = [terms]

        deadline = time.monotonic() + TIME_BUDGET_SECONDS
        base = f"https://{tenant}.{host}.myworkdayjobs.com"
        list_url = f"{base}/wday/cxs/{tenant}/{site}/jobs"

        found: dict[str, dict] = {}
        for term in terms:
            self._search(list_url, term, found, tenant, deadline)
        log.info("  %s: %d unique jobs for search terms %s",
                 self.company_name, len(found), terms)

        wanted = _title_filter()
        lookups = 0
        postings = []
        for path, job in found.items():
            title = (job.get("title") or "").strip()
            if not title:
                continue
            location = (job.get("locationsText") or "").strip()
            if MULTI_LOCATION.match(location) and wanted(title):
                if lookups >= MAX_DETAIL_LOOKUPS:
                    log.warning("  %s: location lookup cap reached", self.company_name)
                else:
                    if time.monotonic() > deadline:
                        raise ScrapeError(f"Workday time budget exceeded for {tenant!r}")
                    location = self._detail_location(base, tenant, site, path) or location
                    lookups += 1
            postings.append(
                JobPosting(
                    company=self.company_name,
                    title=title,
                    url=f"{base}/en-US/{site}{path}",
                    location=location,
                    department="",
                )
            )
        return postings

    def _search(self, url: str, term: str, found: dict, label: str, deadline: float) -> None:
        offset = 0
        total = None
        for _ in range(MAX_PAGES):
            if time.monotonic() > deadline:
                raise ScrapeError(f"Workday time budget exceeded for {label!r}")
            body = {"appliedFacets": {}, "limit": PAGE_SIZE,
                    "offset": offset, "searchText": term}
            try:
                resp = requests.post(url, json=body, headers=HEADERS,
                                     timeout=self.TIMEOUT_SECONDS)
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as exc:
                raise ScrapeError(f"Workday fetch failed for {label!r}: {exc} ({url})")
            except ValueError as exc:
                raise ScrapeError(f"Workday returned non-JSON for {label!r}: {exc}")

            if not isinstance(data, dict):
                raise ScrapeError(f"Workday returned unexpected shape for {label!r}")

            page = data.get("jobPostings") or []
            if total is None:
                total = int(data.get("total") or 0)
            if not page:
                break
            for job in page:
                path = (job.get("externalPath") or "").strip()
                if path and path not in found:
                    found[path] = job
            offset += PAGE_SIZE
            if offset >= total:
                break
            time.sleep(0.2)

    def _detail_location(self, base: str, tenant: str, site: str, path: str) -> str:
        url = f"{base}/wday/cxs/{tenant}/{site}{path}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=self.TIMEOUT_SECONDS)
            resp.raise_for_status()
            info = resp.json().get("jobPostingInfo") or {}
        except (requests.RequestException, ValueError, AttributeError):
            return ""
        time.sleep(0.2)
        parts = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
        return "; ".join(p.strip() for p in parts if p and p.strip())
