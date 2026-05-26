"""Location matching against locations.yaml.

A posting's location is matched against the allowlist (cities, states, or
remote patterns). If any allow pattern hits AND no exclude pattern hits,
the location is considered a match.

Empty/missing location strings are treated as NON-matching by default —
in practice, ATSes that don't supply location are usually broad postings
("Anywhere" implied) and we'd rather treat them as a soft signal of
non-match than as a soft signal of match. They still appear in the CSV
flagged location_match=false, so nothing is lost.

This module is independent of TitleMatcher and runs against the
JobPosting's location field, not the title.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass
class LocationMatcher:
    """Compiled location rules. Construct via LocationMatcher.from_yaml()."""
    cities: list[str]                  # lowercased plain strings for substring match
    states: list[str]                  # lowercased plain strings for substring match
    regions: list[re.Pattern]          # case-insensitive compiled regexes (country/continent level)
    remote_patterns: list[re.Pattern]  # case-insensitive compiled regexes
    excludes: list[re.Pattern]         # case-insensitive compiled regexes

    @classmethod
    def from_yaml(cls, path: str | Path) -> "LocationMatcher":
        path = Path(path)
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        allow = data.get("allow") or {}
        cities = [s.lower() for s in (allow.get("cities") or [])]
        states = [s.lower() for s in (allow.get("states") or [])]

        regions = [
            re.compile(p, re.IGNORECASE)
            for p in (allow.get("regions") or [])
        ]
        remote_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in (allow.get("remote_patterns") or [])
        ]
        excludes = [
            re.compile(p, re.IGNORECASE)
            for p in (data.get("exclude") or [])
        ]

        log.info(
            "LocationMatcher loaded: %d cities, %d states, %d regions, "
            "%d remote patterns, %d excludes",
            len(cities), len(states), len(regions),
            len(remote_patterns), len(excludes),
        )
        return cls(
            cities=cities,
            states=states,
            regions=regions,
            remote_patterns=remote_patterns,
            excludes=excludes,
        )

    def matches(self, location: str | None) -> bool:
        """Return True if the location passes the filter.

        Empty/None locations return False — we'd rather not assume.
        """
        if not location:
            return False

        normalized = location.strip()
        if not normalized:
            return False

        # Exclude pass first. A foreign-country listing must not match
        # even if it happens to coincide with an allow pattern (defense
        # against ambiguous strings like "Remote, EU, Berlin").
        for exc in self.excludes:
            if exc.search(normalized):
                return False

        lowered = normalized.lower()

        # Allow pass: any match wins.
        for city in self.cities:
            if city in lowered:
                return True
        for state in self.states:
            if state in lowered:
                return True
        for pattern in self.regions:
            if pattern.search(normalized):
                return True
        for pattern in self.remote_patterns:
            if pattern.search(normalized):
                return True

        return False
