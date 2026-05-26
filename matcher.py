"""Title matching.

Loads titles.yaml, compiles regexes once, and exposes match() that returns
the matching title category (e.g., "VP of Engineering") or None.

Design note: matching is by category, not by individual pattern. The first
category whose patterns hit (and excludes don't hit) wins. Order in
titles.yaml matters for ambiguous cases — put more specific categories
first (e.g., "CTO" before "Engineering Manager") if you ever add patterns
that could overlap. The default config doesn't have overlapping patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CompiledTitle:
    name: str
    patterns: tuple[re.Pattern, ...]
    excludes: tuple[re.Pattern, ...]


class TitleMatcher:
    def __init__(self, titles: list[CompiledTitle]):
        self._titles = titles

    @classmethod
    def from_yaml(cls, path: str | Path) -> "TitleMatcher":
        with open(path) as f:
            data = yaml.safe_load(f) or {}

        raw_titles = data.get("titles", [])
        if not raw_titles:
            raise ValueError(f"No titles defined in {path}")

        compiled: list[CompiledTitle] = []
        for raw in raw_titles:
            name = raw.get("name")
            if not name:
                raise ValueError(f"Title entry missing 'name' in {path}")
            patterns = raw.get("patterns") or []
            excludes = raw.get("excludes") or []
            if not patterns:
                raise ValueError(f"Title {name!r} has no patterns in {path}")

            compiled.append(
                CompiledTitle(
                    name=name,
                    patterns=tuple(re.compile(p, re.IGNORECASE) for p in patterns),
                    excludes=tuple(re.compile(p, re.IGNORECASE) for p in excludes),
                )
            )
        return cls(compiled)

    def match(self, title: str) -> str | None:
        """Return the matching category name, or None if no match.

        A category matches when (any pattern hits) AND (no exclude hits).
        """
        if not title:
            return None

        for compiled in self._titles:
            if not any(p.search(title) for p in compiled.patterns):
                continue
            if any(e.search(title) for e in compiled.excludes):
                continue
            return compiled.name
        return None
