"""Project configuration loaded from config.yaml.

Two fields for now (report title + display timezone), but the dataclass
and YAML structure are set up so additional config knobs (alert
thresholds, etc.) can be added without restructuring.

Forks that want different branding or a different display timezone edit
config.yaml. No code changes needed for the common case.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

_DEFAULT_TITLE = "Job Openings"
_DEFAULT_TIMEZONE = "America/Los_Angeles"


@dataclass
class Config:
    """Top-level project config. Loaded once at scrape startup."""
    report_title: str
    timezone: str  # IANA name, validated to resolve to a real ZoneInfo

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Config":
        """Read config.yaml. Falls back to sensible defaults for any
        missing fields so a fork can't break the build by deleting a
        single key.

        Raises ValueError if the timezone string doesn't resolve to a
        real IANA zone — caught at startup so a typo fails fast with a
        clear message rather than crashing mid-run when the first
        timestamp is formatted.
        """
        path = Path(path)
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}

        report = data.get("report") or {}
        title = report.get("title") or _DEFAULT_TITLE
        timezone = report.get("timezone") or _DEFAULT_TIMEZONE

        # Validate the timezone resolves. ZoneInfo raises
        # ZoneInfoNotFoundError for unknown names; surface it as a
        # ValueError with actionable context.
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"Invalid timezone {timezone!r} in {path} (report.timezone). "
                f"Use an IANA name like 'America/Los_Angeles' or 'Europe/London'. "
                f"Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
            ) from exc

        return cls(report_title=title, timezone=timezone)
