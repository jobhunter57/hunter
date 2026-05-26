"""Report generation — delta-only.

The scraper is a change-detector, not a snapshot tool. Every run produces:

  - A CSV at reports/jobs/YYYY-MM-DD HH-MM Job Delta.csv listing only what
    changed since the previous run: postings newly discovered (status=new)
    and postings no longer listed (status=closed). The user's spreadsheet
    is their durable record of inventory; this CSV is a worklist to append
    or update against it.

    If nothing changed, the CSV still gets written, with a single heartbeat
    row (status=no_changes). Heartbeat rows serve two purposes: they prove
    the scraper ran successfully on that date, and they let the user keep
    a master run-log sheet by appending the heartbeat rows of empty runs.

  - A Markdown string returned for the email body, structured as a diff
    report rather than a snapshot. The "Still open" section is gone —
    those postings are assumed to be in the user's spreadsheet from
    previous runs.

Out-of-region postings are filtered from both the email and CSV by default.
The CLI flag --out-of-region runs the inverse filter, showing only the
foreign roles instead. Scraper failures and ats:unknown companies appear
in the email markdown (per-company metadata, different schema) but not
the CSV.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from state import CompanySnapshot, FailureRecord, FAILURE_ALERT_THRESHOLD

REPORTS_DIR = Path("reports/jobs")

# Windows filename-reserved characters. The subject and H1 keep these
# verbatim — they're only stripped from the disk filename. A user who
# picks `title: "A/B Roles: Q3"` gets a clean subject/H1 with the
# slash and colon intact, but the file lands as `A_B Roles_ Q3.csv`.
_WINDOWS_RESERVED_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')


def _sanitize_for_filename(s: str) -> str:
    """Replace Windows-reserved filename chars with underscore.

    Linux/macOS would tolerate most of these (only `/` and `\\0` are
    truly reserved on POSIX), but cross-platform safety matters when
    forks may develop on one OS and deploy on another. Cheap to be safe.
    """
    return _WINDOWS_RESERVED_FILENAME_CHARS.sub("_", s).strip()


# CSV column order. `scraped_at` first so it survives concatenation across
# runs (every row knows when it was captured) and so the user's spreadsheet
# can sort/group by date. `status` second for filter prominence.
# `location_match` mid-row so it's visible alongside the location string.
CSV_FIELDS = [
    "scraped_at",     # YYYY-MM-DD HH:MM in run-local time
    "status",         # new | closed | no_changes
    "category",       # CTO | VP of Engineering | Director of Engineering | Engineering Manager
    "location_match", # true | false — passed locations.yaml filter
    "company",
    "title",
    "location",
    "department",
    "url",
]

# Sort orderings.
_STATUS_ORDER = {"new": 0, "closed": 1, "no_changes": 99}
_CATEGORY_ORDER = [
    "CTO",
    "VP of Engineering",
    "Director of Engineering",
    "Engineering Manager",
]


@dataclass
class ReportData:
    """Inputs to report rendering.

    All filtering by location is done upstream in scrape.py before this
    is built — new_postings and closed_postings contain exactly what
    should be shown and written. The out_of_region flag is passed through
    only so the report can label its outputs (filename suffix and email
    heading) to distinguish in-region runs from out-of-region runs.

    report_title is the configurable string that flows into the email
    subject, the email H1, and the CSV filename — same string in all
    three places to avoid the "three labels, one thing" confusion.
    Source: config.yaml -> report.title.
    """
    new_postings: list[dict]          # postings added since last run
    closed_postings: list[dict]       # postings in last run but not this one
    failures: dict[str, FailureRecord]
    skipped_unknown: list[dict]       # companies with ats: unknown — each entry is {"name": ..., "url": ...}
    total_companies_scraped: int
    run_timestamp: datetime
    report_title: str                 # from config.yaml -> report.title; flows to subject, H1, filename
    out_of_region: bool = False       # which worklist this run represents


def build_report(data: ReportData) -> tuple[str, Path]:
    """Write delta CSV to disk, return (markdown_for_email_body, csv_path)."""
    date_str = data.run_timestamp.strftime("%Y-%m-%d")
    time_str = data.run_timestamp.strftime("%H-%M")
    # Mode label flows into both the CSV filename and the email H1 so the
    # two stay in sync. Empty string for the default in-region case keeps
    # the existing filename/heading exactly as they were.
    mode_label = " Out Of Region" if data.out_of_region else ""
    # Sanitize for cross-platform filename safety. The subject and H1
    # use the verbatim title; only the disk filename strips reserved chars.
    safe_title = _sanitize_for_filename(data.report_title)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"{date_str} {time_str} {safe_title}{mode_label}.csv"

    _write_csv(data, out_path)
    markdown = _build_markdown(data)
    return markdown, out_path


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def _write_csv(data: ReportData, out_path: Path) -> None:
    """Write the delta CSV: new + closed rows only, or a heartbeat row.

    Location filtering is done upstream in scrape.py based on the
    --out-of-region flag, so data.new_postings and data.closed_postings
    already contain exactly what should be written.

    UTF-8 with BOM: Excel on Windows defaults to cp1252 unless it sees a
    BOM, in which case it correctly decodes as UTF-8. Without this, an em-
    dash or accented character in a job title displays as garbage. Google
    Sheets handles both equally well.

    newline="": required on Windows. Without it, csv writes \\r\\n line
    endings and Python's default newline translation adds another \\r,
    producing \\r\\r\\n which confuses some downstream tools.
    """
    scraped_at = data.run_timestamp.strftime("%Y-%m-%d %H:%M")

    rows: list[dict] = []
    rows.extend(_postings_to_rows(data.new_postings, "new", scraped_at))
    rows.extend(_postings_to_rows(data.closed_postings, "closed", scraped_at))

    # Heartbeat row when there's nothing to report. Single row, status=
    # no_changes, all posting-specific fields blank. This makes empty runs
    # visible in the user's master log instead of silently producing an
    # empty file.
    if not rows:
        rows.append(_heartbeat_row(scraped_at))

    rows.sort(key=_row_sort_key)

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _postings_to_rows(postings: list[dict], status: str, scraped_at: str) -> list[dict]:
    """Convert posting dicts to CSV row dicts with status and timestamp injected."""
    return [
        {
            "scraped_at":     scraped_at,
            "status":         status,
            "category":       p.get("category", ""),
            "location_match": "true" if p.get("location_match") else "false",
            "company":        p.get("company", ""),
            "title":          p.get("title", ""),
            "location":       p.get("location", ""),
            "department":     p.get("department", ""),
            "url":            p.get("url", ""),
        }
        for p in postings
    ]


def _heartbeat_row(scraped_at: str) -> dict:
    """Single 'no changes this run' row for empty deltas.

    All posting-specific fields are blank — they're meaningless for a
    heartbeat. The user's spreadsheet can sort/filter on status to
    surface or hide these depending on whether they want the run log
    or just the actionable changes.
    """
    return {
        "scraped_at":     scraped_at,
        "status":         "no_changes",
        "category":       "",
        "location_match": "",
        "company":        "",
        "title":          "",
        "location":       "",
        "department":     "",
        "url":            "",
    }


def _row_sort_key(row: dict) -> tuple:
    """Sort: status priority, then location-match, then category, then company."""
    cat = row.get("category", "")
    cat_rank = _CATEGORY_ORDER.index(cat) if cat in _CATEGORY_ORDER else 99
    loc_rank = 0 if row.get("location_match") == "true" else 1
    return (
        _STATUS_ORDER.get(row["status"], 99),
        loc_rank,
        cat_rank,
        row.get("company", ""),
        row.get("title", ""),
    )


# ---------------------------------------------------------------------------
# Markdown (for email body)
# ---------------------------------------------------------------------------

def _build_markdown(data: ReportData) -> str:
    """Build the Markdown email body — a diff report.

    No "Still open" section: those postings are assumed to be in the
    reader's spreadsheet from earlier runs. Email = change worklist.

    Filtering by location is done upstream in scrape.py based on the
    --out-of-region flag, so data.new_postings and data.closed_postings
    already contain exactly what should be shown. The location_match
    field is preserved on each entry for the ✓/✗ marker in the rendered
    tables.
    """
    # Date + time (colon form for human-readable surfaces; the filename
    # uses dashes since colons are illegal in Windows filenames). Time is
    # included because there can be multiple runs per day — date alone
    # would be ambiguous in the inbox and the heading.
    datetime_str = data.run_timestamp.strftime("%Y-%m-%d %H:%M")
    mode_label = " — Out Of Region" if data.out_of_region else ""

    lines: list[str] = []
    lines.append(f"# {data.report_title}{mode_label} — {datetime_str}")
    lines.append("")
    lines.append(_build_summary_line(data))
    lines.append("")

    # Alerting failures get a prominent header callout so you don't miss it.
    alerting = [r for r in data.failures.values() if r.is_alerting]
    if alerting:
        lines.append("> ⚠️ **Scraper alerts:** "
                     f"{len(alerting)} compan{'y' if len(alerting) == 1 else 'ies'} "
                     f"failing {FAILURE_ALERT_THRESHOLD}+ runs in a row. "
                     "See *Failed scrapes* section.")
        lines.append("")

    lines.append("---")
    lines.append("")

    # No-changes case: explicit acknowledgment rather than empty sections.
    # This matches the CSV heartbeat row — both surfaces say the same
    # thing on a quiet day.
    if not data.new_postings and not data.closed_postings:
        lines.append("## No changes this run")
        lines.append("")
        lines.append("_No new postings discovered, no previously-listed postings closed. "
                     "Scraper ran successfully against "
                     f"{data.total_companies_scraped} companies._")
        lines.append("")
    else:
        # ----- New since last run -----
        lines.append(f"## New since last run ({len(data.new_postings)})")
        lines.append("")
        if data.new_postings:
            lines.extend(_render_posting_table(data.new_postings))
        else:
            lines.append("_No new postings since last run._")
        lines.append("")

        # ----- Closed since last run -----
        if data.closed_postings:
            lines.append(f"## Closed since last run ({len(data.closed_postings)})")
            lines.append("")
            lines.append("_Postings present in the previous run but no longer listed. "
                         "May indicate filled, withdrawn, or temporarily-hidden roles. "
                         "Worth updating these rows in your spreadsheet._")
            lines.append("")
            lines.extend(_render_posting_table(data.closed_postings))
            lines.append("")

    # ----- Failures -----
    if data.failures or data.skipped_unknown:
        lines.append("## Failed scrapes")
        lines.append("")
        if data.failures:
            lines.append("| Company | Consecutive failures | Last error |")
            lines.append("|---|---|---|")
            # Sort: alerting first, then by failure count descending.
            sorted_fails = sorted(
                data.failures.values(),
                key=lambda r: (-int(r.is_alerting), -r.consecutive_failures, r.company),
            )
            for rec in sorted_fails:
                flag = "🔴 " if rec.is_alerting else ""
                # Truncate error messages to keep tables readable. Full
                # errors are in state/failures.json.
                err = (rec.last_error or "").replace("|", "\\|").replace("\n", " ")
                if len(err) > 140:
                    err = err[:137] + "..."
                lines.append(f"| {flag}{rec.company} | {rec.consecutive_failures} | {err} |")
            lines.append("")
        if data.skipped_unknown:
            lines.append("### Companies with unknown ATS (skipped)")
            lines.append("")
            lines.append("Run `python probe.py` and update `companies.yaml`:")
            lines.append("")
            # Sort by name for stable ordering — case-insensitive matches
            # how the user would scan the list visually. Each entry
            # renders as a Markdown link so the URL is clickable in the
            # email and copyable from the source if you ever view the
            # CSV file alongside.
            for entry in sorted(data.skipped_unknown, key=lambda e: e["name"].lower()):
                name = entry["name"]
                url = entry.get("url", "")
                if url:
                    lines.append(f"- {name} — <{url}>")
                else:
                    lines.append(f"- {name}")
            lines.append("")

    return "\n".join(lines)


def _build_summary_line(data: ReportData) -> str:
    """Top-of-email summary."""
    parts = [
        f"**Scraped:** {data.total_companies_scraped} companies",
        f"**New:** {len(data.new_postings)}",
        f"**Closed:** {len(data.closed_postings)}",
    ]
    if data.failures:
        parts.append(f"**Failures:** {len(data.failures)}")
    if data.skipped_unknown:
        parts.append(f"**Unknown ATS:** {len(data.skipped_unknown)}")
    return " · ".join(parts)


def _render_posting_table(postings: list[dict]) -> list[str]:
    out = ["| Loc | Category | Company | Title | Location | Link |",
           "|---|---|---|---|---|---|"]

    def sort_key(p: dict):
        # Location-matched rows first (so the actionable ones surface),
        # then by category seniority, then by company and title.
        cat = p.get("category", "")
        cat_rank = _CATEGORY_ORDER.index(cat) if cat in _CATEGORY_ORDER else 99
        loc_rank = 0 if p.get("location_match") else 1
        return (loc_rank, cat_rank, p.get("company", ""), p.get("title", ""))

    for p in sorted(postings, key=sort_key):
        title = p["title"].replace("|", "\\|")
        location = (p.get("location") or "").replace("|", "\\|")
        loc_mark = "✓" if p.get("location_match") else "✗"
        out.append(
            f"| {loc_mark} | {p.get('category', '')} | {p['company']} | {title} | {location} | "
            f"[apply]({p['url']}) |"
        )
    return out


def report_subject(data: ReportData) -> str:
    """Email subject line. Reflects the delta-report nature: new/closed
    counts are what matter, not the inventory total. Alerting failures
    surface so they aren't lost.
    """
    datetime_str = data.run_timestamp.strftime("%Y-%m-%d %H:%M")
    mode_label = " Out Of Region" if data.out_of_region else ""
    alerting = sum(1 for r in data.failures.values() if r.is_alerting)
    new_count = len(data.new_postings)
    closed_count = len(data.closed_postings)

    parts = [f"[{data.report_title}{mode_label} {datetime_str}]"]

    if new_count == 0 and closed_count == 0:
        parts.append("no changes")
    else:
        parts.append(f"{new_count} new")
        if closed_count > 0:
            parts.append(f"{closed_count} closed")

    if alerting:
        parts.append(f"⚠️ {alerting} broken scraper{'s' if alerting != 1 else ''}")
    return " · ".join(parts)
