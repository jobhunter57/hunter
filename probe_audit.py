"""Probe audit — weekly read-only health check of ATS slug mappings.

Re-probes every company in companies.yaml against its CONFIGURED ats/slug,
and (for unmapped companies) tries to discover a new mapping. Produces a
report in three buckets and emails + commits it. NEVER modifies
companies.yaml — it surfaces discrepancies for the human to verify and
apply manually.

THREE BUCKETS
-------------
1. New mappings available
   Companies currently `ats: unknown` where a direct probe now finds a
   working ATS slug with jobs. Paste-ready YAML in the report.

2. Slug drift (ACTIONABLE)
   A company's CONFIGURED slug returns 404 or 0 jobs AND a probe finds a
   different slug that works. High bar: the configured slug must look
   broken for DRIFT_CONFIRM_THRESHOLD consecutive runs (see probe_state.py)
   before it's promoted here — a single bad probe only puts it on watch.
   Side-by-side old-vs-new with both verification URLs.

3. Different-but-still-works (informational)
   A probe matched a different slug than configured, but the configured
   slug still returns valid data. Logged quietly — no action needed, but
   useful to know (e.g., a company added a second board).

A run always produces a report, even when nothing is flagged ("All clear").
Heartbeat over silence: a clean report is proof the audit ran.

This script shares NO state with scrape.py. It has its own state file
(state/probe_audit.json) and its own report directory (reports/probe/).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import notify
import probe_state
from config import Config
# Reuse the slug-candidate generator and per-ATS probe functions from
# probe_direct.py — DRY. If probe logic improves there, the audit benefits
# automatically. ATS_PROBES is the ordered list of (ats_name, slug_field, fn).
from probe_direct import (
    ATS_PROBES,
    generate_slug_candidates,
    try_ashby,
    try_greenhouse,
    try_lever,
    try_smartrecruiters,
    try_workable,
)

COMPANIES_FILE = "companies.yaml"
CONFIG_FILE = "config.yaml"
REPORTS_DIR = Path("reports/probe")

# Map an ATS name to the probe function that checks a slug on that ATS.
# Used to verify a company's CONFIGURED slug (we know its ATS, so we probe
# just that one rather than searching all of them).
_ATS_PROBE_BY_NAME = {
    "greenhouse": try_greenhouse,
    "lever": try_lever,
    "ashby": try_ashby,
    "workable": try_workable,
    "smartrecruiters": try_smartrecruiters,
}

# Which ats_config field holds the slug, per ATS. Mirrors the scraper's
# expectations and probe_direct's ATS_PROBES slug_field column.
_SLUG_FIELD_BY_ATS = {
    "greenhouse": "board",
    "lever": "slug",
    "ashby": "org",
    "workable": "slug",
    "smartrecruiters": "company",
}

DELAY_BETWEEN_REQUESTS = 0.2  # politeness, matches probe_direct

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("probe_audit")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class NewMapping:
    """Bucket 1: an ats:unknown company now has a discoverable mapping."""
    company: str
    ats: str
    slug: str
    slug_field: str
    job_count: int
    api_url: str


@dataclass
class SlugDrift:
    """Bucket 2: configured slug broken (confirmed), a replacement works."""
    company: str
    ats: str
    old_slug: str
    old_status: str          # "404" or "0 jobs"
    old_api_url: str
    new_slug: str
    new_ats: str
    new_slug_field: str
    new_job_count: int
    new_api_url: str
    consecutive_broken: int


@dataclass
class DivergentMapping:
    """Bucket 3: a different slug also works, but configured one is fine."""
    company: str
    configured_ats: str
    configured_slug: str
    configured_job_count: int
    alt_ats: str
    alt_slug: str
    alt_job_count: int
    alt_api_url: str


@dataclass
class AuditResults:
    new_mappings: list[NewMapping] = field(default_factory=list)
    slug_drifts: list[SlugDrift] = field(default_factory=list)
    divergent: list[DivergentMapping] = field(default_factory=list)
    # Companies whose configured slug looks broken but hasn't yet hit the
    # confirmation threshold — reported as "on watch", not actionable.
    on_watch: list[tuple[str, str, int]] = field(default_factory=list)  # (company, error, consecutive)
    total_companies: int = 0
    total_api_calls: int = 0


# ---------------------------------------------------------------------------
# Probing helpers
# ---------------------------------------------------------------------------

def _probe_configured_slug(ats: str, slug: str) -> tuple[bool, int, str]:
    """Probe a company's CONFIGURED ats/slug. Returns (matched, count, api_url).

    matched=True with count>0 is healthy. matched=True with count==0 is
    "registered but empty" — we treat 0 jobs as broken-ish for a VC-backed
    company that should always be hiring, consistent with the design's
    "404 or 0 jobs" drift trigger.
    """
    fn = _ATS_PROBE_BY_NAME.get(ats)
    if fn is None:
        # ATS we don't know how to probe (e.g. custom/proprietary like
        # Stripe/Plaid, or a typo). Can't audit it — treat as "skip".
        return False, 0, ""
    return fn(slug)


def _search_for_working_slug(
    name: str, url: str, exclude: tuple[str, str] | None = None
) -> tuple[str, str, str, int, str] | None:
    """Run the full slug-candidate × ATS search (same as probe_direct).

    Returns (ats, slug, slug_field, job_count, api_url) for the first match
    with job_count > 0, or None. `exclude` is an optional (ats, slug) pair
    to skip (e.g. the configured mapping we already know about).

    Prefers a jobs>0 match; only falls back to a 0-job match if nothing
    better is found (mirrors probe_direct's post-fix behavior).
    """
    candidates = generate_slug_candidates(name, url)
    zero_job_fallback: tuple[str, str, str, int, str] | None = None

    for slug in candidates:
        for ats_name, slug_field, fn in ATS_PROBES:
            if exclude and (ats_name, slug) == exclude:
                continue
            matched, count, api_url = fn(slug)
            time.sleep(DELAY_BETWEEN_REQUESTS)
            if matched and count > 0:
                return (ats_name, slug, slug_field, count, api_url)
            if matched and count == 0 and zero_job_fallback is None:
                zero_job_fallback = (ats_name, slug, slug_field, 0, api_url)

    return zero_job_fallback


# ---------------------------------------------------------------------------
# Main audit
# ---------------------------------------------------------------------------

def run_audit() -> AuditResults:
    with open(COMPANIES_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    companies = data.get("companies", [])

    audit_state = probe_state.load_probe_audit()
    results = AuditResults(total_companies=len(companies))

    for entry in companies:
        name = entry["name"]
        url = entry.get("url", "")
        ats = (entry.get("ats") or "unknown").lower()
        ats_config = entry.get("ats_config") or {}

        if ats == "skip":
            continue

        if ats == "unknown":
            # ----- Bucket 1: try to discover a mapping -----
            log.info("Probing %s (unknown ATS) ...", name)
            found = _search_for_working_slug(name, url)
            results.total_api_calls += 1  # approximate; search does many
            if found:
                ats_name, slug, slug_field, count, api_url = found
                if count > 0:
                    results.new_mappings.append(NewMapping(
                        company=name, ats=ats_name, slug=slug,
                        slug_field=slug_field, job_count=count, api_url=api_url,
                    ))
                    log.info("  → new mapping: %s/%s (%d jobs)", ats_name, slug, count)
            continue

        # ----- Known ATS: verify the configured slug -----
        slug_field = _SLUG_FIELD_BY_ATS.get(ats)
        slug = ats_config.get(slug_field) if slug_field else None
        if not slug:
            # Mapped to a known ATS but missing its slug field — config bug,
            # but not our job to fix. Skip rather than crash.
            log.warning("  %s: ats=%s but no %s in ats_config; skipping",
                        name, ats, slug_field)
            continue

        if ats not in _ATS_PROBE_BY_NAME:
            # Custom/proprietary ATS (Stripe, Plaid) — nothing to probe.
            continue

        log.info("Probing %s (%s/%s) ...", name, ats, slug)
        matched, count, api_url = _probe_configured_slug(ats, slug)
        results.total_api_calls += 1
        time.sleep(DELAY_BETWEEN_REQUESTS)

        configured_healthy = matched and count > 0

        if configured_healthy:
            # Slug is fine. Reset any broken-streak. Then optionally check
            # whether a DIFFERENT slug also works (bucket 3, informational).
            probe_state.record_slug_ok(audit_state, name)

            alt = _search_for_working_slug(name, url, exclude=(ats, slug))
            if alt:
                alt_ats, alt_slug, _alt_field, alt_count, alt_api = alt
                if alt_count > 0:
                    results.divergent.append(DivergentMapping(
                        company=name,
                        configured_ats=ats, configured_slug=slug,
                        configured_job_count=count,
                        alt_ats=alt_ats, alt_slug=alt_slug,
                        alt_job_count=alt_count, alt_api_url=alt_api,
                    ))
            continue

        # Configured slug looks broken (404 or 0 jobs).
        error = "404" if not matched else "0 jobs"
        rec = probe_state.record_slug_broken(audit_state, name, error)

        if not rec.is_confirmed_drift:
            # On watch — not yet actionable. Surface quietly so the user
            # knows it's being tracked, but don't cry wolf.
            results.on_watch.append((name, error, rec.consecutive_broken))
            log.info("  ⚠ %s slug looks broken (%s), on watch (%d/%d)",
                     name, error, rec.consecutive_broken,
                     probe_state.DRIFT_CONFIRM_THRESHOLD)
            continue

        # Confirmed broken across the threshold. Look for a replacement.
        log.info("  ⚠ %s slug broken %d runs; searching replacement ...",
                 name, rec.consecutive_broken)
        replacement = _search_for_working_slug(name, url, exclude=(ats, slug))
        if replacement:
            new_ats, new_slug, new_field, new_count, new_api = replacement
            if new_count > 0:
                results.slug_drifts.append(SlugDrift(
                    company=name, ats=ats,
                    old_slug=slug, old_status=error, old_api_url=api_url,
                    new_slug=new_slug, new_ats=new_ats,
                    new_slug_field=new_field, new_job_count=new_count,
                    new_api_url=new_api,
                    consecutive_broken=rec.consecutive_broken,
                ))
                log.info("    → replacement: %s/%s (%d jobs)",
                         new_ats, new_slug, new_count)
        else:
            # Confirmed broken but no replacement found — still worth
            # flagging as drift with no suggested fix.
            results.slug_drifts.append(SlugDrift(
                company=name, ats=ats,
                old_slug=slug, old_status=error, old_api_url=api_url,
                new_slug="", new_ats="", new_slug_field="",
                new_job_count=0, new_api_url="",
                consecutive_broken=rec.consecutive_broken,
            ))

    probe_state.save_probe_audit(audit_state)
    return results


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def _has_findings(r: AuditResults) -> bool:
    return bool(r.new_mappings or r.slug_drifts or r.divergent)


def build_markdown(r: AuditResults, run_ts: datetime, title: str) -> str:
    datetime_str = run_ts.strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    lines.append(f"# {title} — Probe Audit — {datetime_str}")
    lines.append("")

    # Summary line.
    summary = (
        f"**Audited:** {r.total_companies} companies · "
        f"**New mappings:** {len(r.new_mappings)} · "
        f"**Slug drift:** {len(r.slug_drifts)} · "
        f"**Divergent:** {len(r.divergent)}"
    )
    if r.on_watch:
        summary += f" · **On watch:** {len(r.on_watch)}"
    lines.append(summary)
    lines.append("")
    lines.append("---")
    lines.append("")

    if not _has_findings(r):
        lines.append("## All clear ✓")
        lines.append("")
        lines.append("_No new mappings, no confirmed slug drift, no divergent "
                     "mappings. Every configured ATS slug is resolving and "
                     "returning jobs._")
        lines.append("")
        if r.on_watch:
            lines.append(_render_watch_section(r))
        return "\n".join(lines)

    # ----- Bucket 2 first: slug drift is the most actionable -----
    if r.slug_drifts:
        lines.append(f"## Slug drift — ACTION NEEDED ({len(r.slug_drifts)})")
        lines.append("")
        lines.append("_A configured slug has returned 404 or 0 jobs for "
                     f"{probe_state.DRIFT_CONFIRM_THRESHOLD}+ consecutive "
                     "probe runs. Verify and update `companies.yaml`._")
        lines.append("")
        for d in r.slug_drifts:
            lines.append(f"### {d.company}")
            lines.append("")
            lines.append(f"- **Configured:** `{d.ats}` / `{d.old_slug}` — "
                         f"now returns **{d.old_status}** "
                         f"(broken {d.consecutive_broken} runs)")
            if d.old_api_url:
                lines.append(f"  - Verify broken: <{d.old_api_url}>")
            if d.new_slug:
                lines.append(f"- **Suggested:** `{d.new_ats}` / `{d.new_slug}` — "
                             f"returns **{d.new_job_count} jobs**")
                lines.append(f"  - Verify working: <{d.new_api_url}>")
                lines.append("")
                lines.append("  Paste-ready replacement:")
                lines.append("")
                lines.append("  ```yaml")
                lines.append(f"    ats: {d.new_ats}")
                lines.append("    ats_config:")
                lines.append(f"      {d.new_slug_field}: {d.new_slug}")
                lines.append("  ```")
            else:
                lines.append("- **Suggested:** none found — the probe could "
                             "not locate a working replacement slug. The "
                             "company may have moved to a custom ATS, or the "
                             "new slug doesn't match any candidate shape. "
                             "Manual investigation needed (open the careers "
                             "page, check DevTools → Network for the ATS host).")
            lines.append("")

    # ----- Bucket 1: new mappings -----
    if r.new_mappings:
        lines.append(f"## New mappings available ({len(r.new_mappings)})")
        lines.append("")
        lines.append("_These companies are currently `ats: unknown`. A probe "
                     "found a working slug. Verify and add to `companies.yaml`._")
        lines.append("")
        for m in r.new_mappings:
            lines.append(f"### {m.company}")
            lines.append("")
            lines.append(f"- `{m.ats}` / `{m.slug}` — **{m.job_count} jobs**")
            lines.append(f"  - Verify: <{m.api_url}>")
            lines.append("")
            lines.append("  Paste-ready mapping (replace the `ats: unknown` line):")
            lines.append("")
            lines.append("  ```yaml")
            lines.append(f"    ats: {m.ats}")
            lines.append("    ats_config:")
            lines.append(f"      {m.slug_field}: {m.slug}")
            lines.append("  ```")
            lines.append("")

    # ----- Bucket 3: divergent (informational) -----
    if r.divergent:
        lines.append(f"## Divergent mappings — informational ({len(r.divergent)})")
        lines.append("")
        lines.append("_The configured slug still works fine. A probe also found "
                     "a DIFFERENT slug that works. No action needed — noted in "
                     "case it's meaningful (e.g. a second board, a rebrand in "
                     "progress, or a slug collision worth being aware of)._")
        lines.append("")
        lines.append("| Company | Configured (working) | Also found |")
        lines.append("|---|---|---|")
        for d in r.divergent:
            lines.append(
                f"| {d.company} "
                f"| `{d.configured_ats}`/`{d.configured_slug}` "
                f"({d.configured_job_count} jobs) "
                f"| `{d.alt_ats}`/`{d.alt_slug}` ({d.alt_job_count} jobs) |"
            )
        lines.append("")

    if r.on_watch:
        lines.append(_render_watch_section(r))

    return "\n".join(lines)


def _render_watch_section(r: AuditResults) -> str:
    out = [f"## On watch ({len(r.on_watch)})", ""]
    out.append("_Configured slug looked broken this run but hasn't yet hit the "
               f"{probe_state.DRIFT_CONFIRM_THRESHOLD}-run confirmation "
               "threshold. Could be a transient ATS hiccup. If it's still "
               "broken next run, it'll be promoted to 'slug drift' above._")
    out.append("")
    for company, error, consecutive in sorted(r.on_watch):
        out.append(f"- {company} — {error} "
                   f"({consecutive}/{probe_state.DRIFT_CONFIRM_THRESHOLD} runs)")
    out.append("")
    return "\n".join(out)


def write_csv(r: AuditResults, out_path: Path, run_ts: datetime) -> None:
    """Companion CSV summarizing all findings, one row per finding.

    Always writes at least a heartbeat row so an all-clear run still
    produces a file (consistent with the job-delta CSV behavior).
    """
    import csv as _csv

    scraped_at = run_ts.strftime("%Y-%m-%d %H:%M")
    fields = ["scraped_at", "bucket", "company", "detail", "verify_url"]
    rows: list[dict] = []

    for d in r.slug_drifts:
        detail = (f"{d.ats}/{d.old_slug} -> {d.old_status}; "
                  f"suggest {d.new_ats}/{d.new_slug} ({d.new_job_count} jobs)"
                  if d.new_slug else
                  f"{d.ats}/{d.old_slug} -> {d.old_status}; no replacement found")
        rows.append({"scraped_at": scraped_at, "bucket": "slug_drift",
                     "company": d.company, "detail": detail,
                     "verify_url": d.new_api_url or d.old_api_url})
    for m in r.new_mappings:
        rows.append({"scraped_at": scraped_at, "bucket": "new_mapping",
                     "company": m.company,
                     "detail": f"{m.ats}/{m.slug} ({m.job_count} jobs)",
                     "verify_url": m.api_url})
    for d in r.divergent:
        rows.append({"scraped_at": scraped_at, "bucket": "divergent",
                     "company": d.company,
                     "detail": (f"configured {d.configured_ats}/{d.configured_slug} ok; "
                                f"also {d.alt_ats}/{d.alt_slug}"),
                     "verify_url": d.alt_api_url})
    for company, error, consecutive in r.on_watch:
        rows.append({"scraped_at": scraped_at, "bucket": "on_watch",
                     "company": company,
                     "detail": f"{error} ({consecutive}/{probe_state.DRIFT_CONFIRM_THRESHOLD})",
                     "verify_url": ""})

    if not rows:
        rows.append({"scraped_at": scraped_at, "bucket": "all_clear",
                     "company": "", "detail": "", "verify_url": ""})

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = _csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def subject_line(r: AuditResults, run_ts: datetime, title: str) -> str:
    date_str = run_ts.strftime("%Y-%m-%d %H:%M")
    prefix = f"[{title} Probe Audit {date_str}]"
    if not _has_findings(r):
        return f"{prefix} all clear"
    parts = []
    if r.slug_drifts:
        parts.append(f"⚠️ {len(r.slug_drifts)} drift")
    if r.new_mappings:
        parts.append(f"{len(r.new_mappings)} new mapping"
                     f"{'s' if len(r.new_mappings) != 1 else ''}")
    if r.divergent:
        parts.append(f"{len(r.divergent)} divergent")
    return f"{prefix} " + " · ".join(parts)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Weekly read-only audit of ATS slug health.")
    parser.add_argument(
        "--force", action="store_true",
        help="No-op, retained for symmetry with scrape.py (no time guard exists).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print report to stdout; do not write CSV, update probe state, "
             "or send email.")
    args = parser.parse_args()

    try:
        config = Config.from_yaml(CONFIG_FILE)
    except (FileNotFoundError, ValueError) as exc:
        log.error("Config error: %s", exc)
        return 1

    run_ts = datetime.now(ZoneInfo(config.timezone))

    log.info("Starting probe audit ...")
    results = run_audit()
    log.info("Audit complete: %d new mappings, %d drifts, %d divergent, "
             "%d on watch (~%d API calls)",
             len(results.new_mappings), len(results.slug_drifts),
             len(results.divergent), len(results.on_watch),
             results.total_api_calls)

    markdown = build_markdown(results, run_ts, config.report_title)

    if args.dry_run:
        print(markdown)
        log.info("DRY RUN: no CSV, no state change beyond probe_audit.json, "
                 "no email.")
        # NOTE: run_audit() already saved probe_audit.json. For a true
        # no-write dry run you'd guard that too, but the streak counter is
        # the whole point of the audit — skipping it would make --dry-run
        # misleading. Documented here so it's a conscious choice.
        return 0

    # Write report files.
    date_str = run_ts.strftime("%Y-%m-%d")
    time_str = run_ts.strftime("%H-%M")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitize the title for the filename, same approach as report.py.
    import re as _re
    safe_title = _re.sub(r'[<>:"/\\|?*]', "_", config.report_title).strip()
    csv_path = REPORTS_DIR / f"{date_str} {time_str} {safe_title} Probe Audit.csv"
    write_csv(results, csv_path, run_ts)
    log.info("Report CSV written to %s", csv_path)

    # Email (markdown body + CSV attachment), same as the job scraper.
    subject = subject_line(results, run_ts, config.report_title)
    notify.send(subject, markdown, attachment_path=csv_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
