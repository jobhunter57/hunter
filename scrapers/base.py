"""Base classes for ATS scrapers.

Every scraper inherits from BaseScraper and returns a list of JobPosting.
The convention: scrapers are pure data fetchers — they do NOT match titles
or apply filters. The matcher handles that. This keeps scrapers reusable
and the data pipeline auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class JobPosting:
    """A single job posting from a company's ATS.

    Frozen so postings are hashable and usable in sets/dict keys for diffing.
    """

    company: str
    title: str
    url: str
    location: str = ""
    department: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScrapeError(Exception):
    """Raised when a scraper cannot fetch postings. Caught at the top level
    so one company's failure never aborts the run.
    """


class BaseScraper:
    """Abstract base. Subclasses implement fetch().

    Subclasses MUST NOT swallow exceptions silently — let them propagate to
    the top-level handler in scrape.py, which records the failure with
    context.
    """

    ATS_NAME: str = "base"  # Override in subclass.
    TIMEOUT_SECONDS: int = 30

    def __init__(self, company_name: str, config: dict[str, Any]):
        self.company_name = company_name
        self.config = config or {}

    def fetch(self) -> list[JobPosting]:
        raise NotImplementedError(
            f"Scraper for {self.company_name} must implement fetch()"
        )

    def _require_config(self, key: str) -> str:
        """Fetch a required config value or raise a clear error."""
        value = self.config.get(key)
        if not value:
            raise ScrapeError(
                f"{self.ATS_NAME} scraper for {self.company_name!r} "
                f"requires ats_config.{key} in companies.yaml"
            )
        return str(value)
