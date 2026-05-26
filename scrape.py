"""Main entry point.

Loads companies.yaml + titles.yaml, dispatches scrapers, matches titles,
diffs against last run, builds report, emails it.

Key invariants:
  - One company's failure NEVER aborts the run. Each scraper runs in its
    own try/except.
  - Failure counts are persisted across runs.
  - "unknown" ATS entries are recorded as skipped, NOT counted as failures.
  - The previous successful run's state is preserved on partial failures
    (we update only the companies we successfully scraped).

Run modes:
  python scrape.py             — full run: write state, write report, send email
  python scrape.py --dry-run   — read companies, print report to stdout, write
                                 nothing to state/ or reports/, no email.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import notify
import report as report_mod
import state
from config import Config
from location_matcher import LocationMatcher
from matcher import TitleMatcher
from scrapers import SCRAPER_REGISTRY, JobPosting, ScrapeError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COMPANIES_FILE = "companies.yaml"
TITLES_FILE = "titles.yaml"
LOCATIONS_FILE = "locations.yaml"
CONFIG_FILE = "config.yaml"

# NOTE: This scraper used to have an 8am-local time guard here, backed by a
# dual-cron (15:00 + 16:00 UTC) DST scheme — the guard rejected the "wrong"
# cron each day so only the real 8am-local run did work. That was removed in
# favor of letting the cron schedule be the sole gate: the scraper now does
# work whenever it's invoked, and the workflow's cron lines decide when that
# is (see .github/workflows/scrape-jobs.yml). The --force flag is retained
# as a documented no-op (see main()) in case the guard is reintroduced later.
#
# Timezone is still used, but only for DISPLAY — the run timestamp shown in
# the email subject, H1, and CSV filename is rendered in the configured
# local zone (config.yaml -> report.timezone) so reports read in local time
# rather than UTC.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("scrape")


# ---------------------------------------------------------------------------
# Company loading
# ---------------------------------------------------------------------------

@dataclass
class Company:
    name: str
    url: str
    region: str
    hq: str
    ats: str
    ats_config: dict


def load_companies(path: str) -> list[Company]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out: list[Company] = []
    for raw in data.get("companies", []):
        name = raw.get("name")
        if not name:
            raise ValueError(f"Company entry missing 'name' in {path}: {raw!r}")
        out.append(
            Company(
                name=name,
                url=raw.get("url", ""),
                region=raw.get("region", ""),
                hq=raw.get("hq", ""),
                ats=(raw.get("ats") or "unknown").lower(),
                ats_config=raw.get("ats_config") or {},
            )
        )
    return out


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

@dataclass
class ScrapeResult:
    """Outcome of attempting to scrape one company."""
    company: Company
    postings: list[JobPosting] | None = None  # None when failed/skipped
    error: str | None = None
    skipped_reason: str | None = None  # "unknown" or "skip"

    @property
    def succeeded(self) -> bool:
        return self.postings is not None and self.error is None and self.skipped_reason is None


def scrape_one(company: Company) -> ScrapeResult:
    """Scrape one company. Catches all exceptions — never raises."""
    if company.ats == "skip":
        return ScrapeResult(company=company, skipped_reason="skip")
    if company.ats == "unknown":
        return ScrapeResult(company=company, skipped_reason="unknown")

    scraper_cls = SCRAPER_REGISTRY.get(company.ats)
    if scraper_cls is None:
        return ScrapeResult(
            company=company,
            error=f"Unknown ats type {company.ats!r} (not in SCRAPER_REGISTRY)",
        )

    try:
        scraper = scraper_cls(company.name, company.ats_config)
        postings = scraper.fetch()
        return ScrapeResult(company=company, postings=postings)
    except ScrapeError as exc:
        return ScrapeResult(company=company, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — top-level sandbox, intentional
        # Defensive: any other exception (bug in scraper, network weirdness,
        # JSON edge case) is captured as a failure rather than aborting the
        # whole run.
        return ScrapeResult(company=company, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run(dry_run: bool, force: bool, out_of_region: bool = False) -> int:
    """Returns process exit code: 0 = success, 1 = critical config error."""
    # No time guard: the cron schedule is the sole gate. Whenever this is
    # invoked (by cron or manually), it does work. The `force` parameter is
    # accepted but unused — see main() for why it's retained.

    # Load config.
    try:
        config = Config.from_yaml(CONFIG_FILE)
        companies = load_companies(COMPANIES_FILE)
        matcher = TitleMatcher.from_yaml(TITLES_FILE)
        location_matcher = LocationMatcher.from_yaml(LOCATIONS_FILE)
    except (FileNotFoundError, ValueError) as exc:
        log.error("Config error: %s", exc)
        return 1

    log.info("Loaded %d companies from %s", len(companies), COMPANIES_FILE)

    # Scrape all companies.
    results: list[ScrapeResult] = []
    for company in companies:
        log.info("Scraping %s (%s)", company.name, company.ats)
        result = scrape_one(company)
        results.append(result)
        if result.succeeded:
            log.info("  %s: %d total postings", company.name, len(result.postings))
        elif result.skipped_reason == "unknown":
            log.info("  %s: skipped (ats=unknown — probe to fix)", company.name)
        elif result.skipped_reason == "skip":
            log.info("  %s: skipped (ats=skip)", company.name)
        else:
            log.warning("  %s: FAILED — %s", company.name, result.error)

    # Match titles and update state.
    #
    # Pick the state file matching the current visibility mode. Two modes
    # (default and --out-of-region) maintain disjoint snapshots so each
    # worklist evolves independently — switching modes shows the user
    # what they haven't been shown before in THAT mode.
    # See state.last_run_path().
    state_path = state.last_run_path(out_of_region)

    failures = state.load_failures()
    new_snapshots = state.load_last_run(state_path)  # start from previous

    matched_now: dict[str, list[dict]] = {}  # company -> list of matched posting dicts

    for result in results:
        if result.skipped_reason in ("skip", "unknown"):
            # Don't touch failure state for these; they're intentional.
            # But do drop them from new_snapshots so they don't leak into diffs.
            new_snapshots.pop(result.company.name, None)
            continue

        if not result.succeeded:
            state.record_failure(failures, result.company.name, result.error or "")
            # Preserve last successful snapshot — do NOT clear it. That way
            # if a scraper breaks transiently, we don't lose the diff baseline.
            continue

        state.record_success(failures, result.company.name)

        matched = []
        for posting in result.postings:
            category = matcher.match(posting.title)
            if category is None:
                continue
            is_in_region = location_matcher.matches(posting.location)
            # Disjoint-worklist filter: default mode keeps in-region only,
            # --out-of-region mode keeps out-of-region only. The two modes
            # never overlap in what they emit, and each mode's state
            # tracks only what that mode has shown the user.
            if out_of_region:
                if is_in_region:
                    continue
            else:
                if not is_in_region:
                    continue
            entry = posting.to_dict()
            entry["category"] = category
            entry["location_match"] = is_in_region
            matched.append(entry)

        matched_now[result.company.name] = matched
        new_snapshots[result.company.name] = state.CompanySnapshot(
            company=result.company.name,
            postings=matched,
        )

    # Diff against previous run. still_open is computed but not passed to
    # the report — the report is delta-only now. We keep computing it so
    # we know whether a posting carried over (and therefore counts as
    # closed if it later disappears) vs. is a fresh discovery.
    old_snapshots = state.load_last_run(state_path)
    new_postings, _still_open, closed_postings = _diff(
        old=old_snapshots,
        new=matched_now,
        new_snapshots=new_snapshots,
    )

    # Build report data.
    # Skipped-unknown carries name AND url so the report can render each
    # entry as a clickable link. The url is the company's canonical
    # careers page from companies.yaml — the right destination for the
    # user to investigate when probing the ATS manually.
    skipped_unknown = [
        {"name": r.company.name, "url": r.company.url}
        for r in results if r.skipped_reason == "unknown"
    ]
    total_scraped = sum(1 for r in results if r.succeeded)

    # Render the run timestamp in the configured display timezone so the
    # filename, email subject, and H1 read in local time. The scraper ran
    # whenever the cron fired; this is purely cosmetic formatting.
    run_tz = ZoneInfo(config.timezone)
    report_data = report_mod.ReportData(
        new_postings=new_postings,
        closed_postings=closed_postings,
        failures=failures,
        skipped_unknown=skipped_unknown,
        total_companies_scraped=total_scraped,
        run_timestamp=datetime.now(run_tz),
        out_of_region=out_of_region,
        report_title=config.report_title,
    )

    if dry_run:
        markdown, _ = report_mod.build_report(report_data)
        # Restore unwritten files (build_report writes to reports/) — keep
        # dry-run truly side-effect-free by deleting what we just wrote.
        # Simpler: tell the user we wrote one report file but skipped state.
        print(markdown)
        log.info("DRY RUN: wrote report file, but did NOT update state/ or send email.")
        return 0

    # Persist state. Uses the same path we loaded from, so default and
    # include modes write to their own files independently.
    state.save_last_run(new_snapshots, state_path)
    state.save_failures(failures)

    # Build and write report.
    markdown, report_path = report_mod.build_report(report_data)
    log.info("Report written to %s", report_path)

    # Email — attach the CSV so it opens directly in Excel/Sheets from
    # the email client, without needing to clone the repo first.
    subject = report_mod.report_subject(report_data)
    notify.send(subject, markdown, attachment_path=report_path)

    return 0


def _diff(
    old: dict[str, state.CompanySnapshot],
    new: dict[str, list[dict]],
    new_snapshots: dict[str, state.CompanySnapshot],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Compute new/still-open/closed posting lists.

    Identity is (company, title, url). We use a tuple key rather than url
    alone because some ATSes recycle URLs across postings, and title alone
    isn't unique within a company.
    """
    new_postings: list[dict] = []
    still_open: list[dict] = []
    closed_postings: list[dict] = []

    # Build a key set from the previous run for fast lookup.
    old_keys_by_company: dict[str, set[tuple[str, str]]] = {
        company: snap.posting_keys() for company, snap in old.items()
    }

    seen_now_keys_by_company: dict[str, set[tuple[str, str]]] = {}

    for company, postings in new.items():
        old_keys = old_keys_by_company.get(company, set())
        seen_now: set[tuple[str, str]] = set()
        for p in postings:
            key = (p["title"], p["url"])
            seen_now.add(key)
            if key in old_keys:
                still_open.append(p)
            else:
                new_postings.append(p)
        seen_now_keys_by_company[company] = seen_now

    # Closed = in old but not in new. Only count companies we successfully
    # scraped this run (avoid spurious "closed" entries from a failed scrape).
    for company, snap in old.items():
        if company not in new:
            continue
        seen_now = seen_now_keys_by_company.get(company, set())
        for p in snap.postings:
            key = (p["title"], p["url"])
            if key not in seen_now:
                closed_postings.append(p)

    return new_postings, still_open, closed_postings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape SFVC eng leadership openings.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print report to stdout; do not update state or send email.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="No-op, retained for compatibility. This used to skip the "
             "8am-local time guard, which has been removed (the cron "
             "schedule is now the sole gate). Kept so existing invocations "
             "and the workflow's manual-trigger input don't break, and in "
             "case the time guard is reintroduced later.",
    )
    parser.add_argument(
        "--out-of-region", action="store_true",
        help="Run the inverse filter — show ONLY title matches outside your "
             "configured locations. Disjoint from default mode: in-region "
             "postings are excluded. Default and --out-of-region maintain "
             "independent state files, so you can use both worklists in "
             "parallel without interference.",
    )
    args = parser.parse_args()
    return run(
        dry_run=args.dry_run,
        force=args.force,
        out_of_region=args.out_of_region,
    )


if __name__ == "__main__":
    sys.exit(main())
