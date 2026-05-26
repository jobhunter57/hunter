"""Probe-audit state persistence.

A single file in state/:
  - probe_audit.json: per-company record of how many CONSECUTIVE probe-audit
    runs have found the configured slug broken (404 or 0 jobs). Used to
    enforce the two-run threshold before promoting a company to the
    "slug drift" bucket — mirrors the scraper's 3-strike failure logic,
    but at the weekly probe-audit cadence so two strikes ≈ two weeks.

Why a threshold at all: ATS APIs occasionally return a transient 404 or a
momentarily-empty board (deploy in progress, brief outage). A single bad
probe shouldn't trigger a "your slug is broken, here's a replacement"
alert — that would cry wolf. Requiring the SAME slug to look broken across
two consecutive weekly probes filters transients while still catching real
migrations within ~two weeks.

This file is committed to the repo by the probe-audit workflow, same as
the scraper's state files. It is NEVER touched by scrape.py — the probe
audit and the scraper share no state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path("state")
PROBE_AUDIT_FILE = STATE_DIR / "probe_audit.json"

# How many consecutive probe-audit runs must find a configured slug broken
# before it's promoted from "watch" to "drift" (actionable in the report).
# Two weekly runs ≈ two weeks of confirmation. Mirrors the scraper's
# FAILURE_ALERT_THRESHOLD philosophy (filter transients, catch real breakage).
DRIFT_CONFIRM_THRESHOLD = 2


@dataclass
class BrokenSlugRecord:
    """Tracks how long a company's configured slug has looked broken."""
    company: str
    consecutive_broken: int = 0
    last_seen_broken_at: str = ""
    last_error: str = ""  # "404" or "0 jobs" or an exception summary

    @property
    def is_confirmed_drift(self) -> bool:
        return self.consecutive_broken >= DRIFT_CONFIRM_THRESHOLD


def load_probe_audit() -> dict[str, BrokenSlugRecord]:
    if not PROBE_AUDIT_FILE.exists():
        return {}
    with open(PROBE_AUDIT_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return {
        company: BrokenSlugRecord(company=company, **fields)
        for company, fields in data.get("companies", {}).items()
    }


def save_probe_audit(records: dict[str, BrokenSlugRecord]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "drift_confirm_threshold": DRIFT_CONFIRM_THRESHOLD,
        "companies": {
            company: {
                "consecutive_broken": rec.consecutive_broken,
                "last_seen_broken_at": rec.last_seen_broken_at,
                "last_error": rec.last_error,
            }
            for company, rec in records.items()
        },
    }
    with open(PROBE_AUDIT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)


def record_slug_ok(records: dict[str, BrokenSlugRecord], company: str) -> None:
    """Configured slug is healthy this run — reset the broken counter."""
    records.pop(company, None)


def record_slug_broken(
    records: dict[str, BrokenSlugRecord], company: str, error: str
) -> BrokenSlugRecord:
    """Configured slug looked broken this run — increment the counter."""
    rec = records.get(company) or BrokenSlugRecord(company=company)
    rec.consecutive_broken += 1
    rec.last_error = error
    rec.last_seen_broken_at = datetime.now(timezone.utc).isoformat()
    records[company] = rec
    return rec
