"""State persistence.

Three files in state/:
  - last_run.json:              default-mode snapshot — postings that pass
                                the locations.yaml filter (in-region). This
                                is what default scraper runs (no flag)
                                read and write.
  - last_run_out_of_region.json: out-of-region snapshot — postings that
                                FAIL the locations.yaml filter. This is
                                what --out-of-region runs read and write.
  - failures.json:              consecutive failure count and last error
                                per company. Mode-independent.

Why two snapshot files: the two modes maintain DISJOINT worklists. Default
mode tracks in-region postings the user has seen; --out-of-region tracks
foreign postings the user has seen. Together they cover the full title-
matched set, but neither alone surfaces the other's territory. This lets
the user keep two independent worklists (e.g., a weekly in-region check
and a monthly foreign-roles check) without one mode's state polluting
the other's diff. See last_run_path().

All three files are committed to the repo by the workflow, giving you a
free audit log. State files are idempotent — re-running the same data
overwrites cleanly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from scrapers import JobPosting

STATE_DIR = Path("state")
LAST_RUN_FILE = STATE_DIR / "last_run.json"                          # default (in-region)
LAST_RUN_OOR_FILE = STATE_DIR / "last_run_out_of_region.json"        # --out-of-region
FAILURES_FILE = STATE_DIR / "failures.json"


def last_run_path(out_of_region: bool) -> Path:
    """Pick the state file matching the current visibility mode."""
    return LAST_RUN_OOR_FILE if out_of_region else LAST_RUN_FILE


# ---------------------------------------------------------------------------
# Last-run snapshot
# ---------------------------------------------------------------------------

@dataclass
class CompanySnapshot:
    """All matched postings for a single company from the last run."""
    company: str
    postings: list[dict]  # JobPosting.to_dict() form, with added "category"

    def posting_keys(self) -> set[tuple[str, str]]:
        """Stable identity for a posting: (title, url). URL alone isn't
        reliable because some ATSes recycle URLs across postings.
        """
        return {(p["title"], p["url"]) for p in self.postings}


def load_last_run(path: Path | None = None) -> dict[str, CompanySnapshot]:
    """Load the snapshot from the given path, or the default-mode file
    if no path provided."""
    p = path if path is not None else LAST_RUN_FILE
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return {
        company: CompanySnapshot(company=company, postings=postings)
        for company, postings in data.get("companies", {}).items()
    }


def save_last_run(
    snapshots: dict[str, CompanySnapshot],
    path: Path | None = None,
) -> None:
    """Save the snapshot to the given path, or the default-mode file
    if no path provided."""
    p = path if path is not None else LAST_RUN_FILE
    STATE_DIR.mkdir(exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "companies": {
            company: snap.postings for company, snap in snapshots.items()
        },
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Failure tracking
# ---------------------------------------------------------------------------

# After this many consecutive failures, the company is flagged in the
# report header AND the email subject. Three is enough to filter out
# transient hiccups (one bad cron run, one ATS deploy) while still
# catching real breakage within a week.
FAILURE_ALERT_THRESHOLD = 3


@dataclass
class FailureRecord:
    company: str
    consecutive_failures: int = 0
    last_error: str = ""
    last_failed_at: str = ""

    @property
    def is_alerting(self) -> bool:
        return self.consecutive_failures >= FAILURE_ALERT_THRESHOLD


def load_failures() -> dict[str, FailureRecord]:
    if not FAILURES_FILE.exists():
        return {}
    with open(FAILURES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return {
        company: FailureRecord(company=company, **fields)
        for company, fields in data.get("companies", {}).items()
    }


def save_failures(failures: dict[str, FailureRecord]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "alert_threshold": FAILURE_ALERT_THRESHOLD,
        "companies": {
            company: {
                "consecutive_failures": rec.consecutive_failures,
                "last_error": rec.last_error,
                "last_failed_at": rec.last_failed_at,
            }
            for company, rec in failures.items()
        },
    }
    with open(FAILURES_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)


def record_success(failures: dict[str, FailureRecord], company: str) -> None:
    """Reset the failure counter on a successful scrape."""
    failures.pop(company, None)


def record_failure(
    failures: dict[str, FailureRecord], company: str, error: str
) -> FailureRecord:
    rec = failures.get(company) or FailureRecord(company=company)
    rec.consecutive_failures += 1
    rec.last_error = error
    rec.last_failed_at = datetime.now(timezone.utc).isoformat()
    failures[company] = rec
    return rec
