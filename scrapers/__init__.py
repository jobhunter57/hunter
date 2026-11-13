"""Scraper registry. To add a new ATS:

1. Create scrapers/myats.py with a class inheriting BaseScraper.
2. Add it to SCRAPER_REGISTRY below.
3. Reference it from companies.yaml as `ats: myats`.

The "unknown" and "skip" sentinels are handled in scrape.py, not here:
- unknown: skipped with a note to run probe.py
- skip:    skipped silently (use for retired scrapers)
"""

from .base import BaseScraper, JobPosting, ScrapeError
from .ashby import AshbyScraper
from .greenhouse import GreenhouseScraper
from .lever import LeverScraper
from .smartrecruiters import SmartRecruitersScraper
from .workable import WorkableScraper
from .workday import WorkdayScraper

SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "greenhouse": GreenhouseScraper,
    "lever": LeverScraper,
    "ashby": AshbyScraper,
    "workable": WorkableScraper,
    "smartrecruiters": SmartRecruitersScraper,
    "workday": WorkdayScraper,
}

__all__ = [
    "BaseScraper",
    "JobPosting",
    "ScrapeError",
    "SCRAPER_REGISTRY",
]
