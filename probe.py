"""ATS detection probe (v2).

For each company with `ats: unknown` in companies.yaml, fetch the careers
URL (following redirects) and try to identify which ATS is in use.

Detection strategies, in order:
  1. Final URL host — boards.greenhouse.io, jobs.lever.co, jobs.ashbyhq.com,
     apply.workable.com, jobs.smartrecruiters.com.
  2. HTML body markers — embedded iframes, script src patterns, known widget
     signatures.
  3. ONE-HOP CRAWL (new in v2) — if the landing page has no ATS signature,
     find candidate "view open roles" / "/jobs" / "/roles" links on the page
     and re-probe those. Catches the common pattern where /careers is a
     marketing page and /careers/roles is the actual job board.

You can override the URL used for probing on a per-company basis with the
optional `probe_url:` field in companies.yaml (use this for stubborn cases).

Output is written to a file (or stdout) in YAML format compatible with
companies.yaml. This script is intentionally read-only: it never modifies
companies.yaml directly. Review the suggestions, then copy/paste.

Usage:
  python probe.py --output probe-output.yaml   # recommended
  python probe.py --all --output ...            # probe every company
  python probe.py                               # print to stdout (PowerShell
                                                # users: pipe through
                                                # Out-File -Encoding utf8)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

USER_AGENT = "salesforce-ventures-jobs-probe/2.0 (+job-search-research)"
FETCH_TIMEOUT = 20

# Hard cap on hops to avoid following infinite-link sites.
MAX_HOPS = 1
# How many candidate links to try per page.
MAX_CANDIDATES_PER_PAGE = 3

# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------

# Map final hostname → ATS name, path regex, slug field.
HOST_SIGNATURES: list[tuple[str, str, re.Pattern, str]] = [
    ("boards.greenhouse.io",        "greenhouse",      re.compile(r"^/embed/job_board\?for=([^&/]+)|^/([^/?]+)"), "board"),
    ("job-boards.greenhouse.io",    "greenhouse",      re.compile(r"^/([^/?]+)"), "board"),
    ("jobs.lever.co",               "lever",           re.compile(r"^/([^/?]+)"), "slug"),
    ("jobs.ashbyhq.com",            "ashby",           re.compile(r"^/([^/?]+)"), "org"),
    ("apply.workable.com",          "workable",        re.compile(r"^/([^/?]+)"), "slug"),
    ("jobs.smartrecruiters.com",    "smartrecruiters", re.compile(r"^/([^/?]+)"), "company"),
    ("careers.smartrecruiters.com", "smartrecruiters", re.compile(r"^/([^/?]+)"), "company"),
]

# Patterns we look for in HTML when the redirect doesn't reveal the ATS
# directly. Order matters — more specific patterns first.
HTML_SIGNATURES: list[tuple[re.Pattern, str, str]] = [
    # Greenhouse — multiple URL shapes
    (re.compile(r'boards\.greenhouse\.io/embed/job_board\?for=([a-z0-9_-]+)', re.I),       "greenhouse",      "board"),
    (re.compile(r'job-boards\.greenhouse\.io/([a-z0-9_-]+)', re.I),                          "greenhouse",      "board"),
    (re.compile(r'boards\.greenhouse\.io/([a-z0-9_-]+)(?:/|"|\'|\?|\s)', re.I),              "greenhouse",      "board"),
    (re.compile(r'boards-api\.greenhouse\.io/v\d+/boards/([a-z0-9_-]+)', re.I),              "greenhouse",      "board"),
    # Lever
    (re.compile(r'jobs\.lever\.co/([a-z0-9_-]+)', re.I),                                     "lever",           "slug"),
    (re.compile(r'api\.lever\.co/v\d+/postings/([a-z0-9_-]+)', re.I),                        "lever",           "slug"),
    # Ashby — multiple URL shapes
    (re.compile(r'jobs\.ashbyhq\.com/([a-z0-9_-]+)', re.I),                                  "ashby",           "org"),
    (re.compile(r'api\.ashbyhq\.com/posting-api/job-board/([a-z0-9_-]+)', re.I),             "ashby",           "org"),
    # Workable
    (re.compile(r'apply\.workable\.com/([a-z0-9_-]+)', re.I),                                "workable",        "slug"),
    (re.compile(r'workable\.com/api/(?:v\d+/)?accounts/([a-z0-9_-]+)', re.I),                "workable",        "slug"),
    # SmartRecruiters
    (re.compile(r'api\.smartrecruiters\.com/v\d+/companies/([a-z0-9_-]+)', re.I),            "smartrecruiters", "company"),
    (re.compile(r'jobs\.smartrecruiters\.com/([a-z0-9_-]+)', re.I),                          "smartrecruiters", "company"),
]

# Link text patterns that strongly suggest "this leads to the job board."
# Used for the one-hop crawl.
LINK_TEXT_PATTERNS = re.compile(
    r"\b("
    r"open\s+(?:roles|positions|jobs)|"
    r"see\s+(?:our\s+)?(?:open\s+)?(?:jobs|roles|positions|openings)|"
    r"view\s+(?:all\s+)?(?:jobs|roles|positions|openings)|"
    r"browse\s+(?:jobs|roles|positions)|"
    r"all\s+(?:jobs|roles|positions|openings)|"
    r"current\s+openings|"
    r"join\s+(?:us|the\s+team|our\s+team)|"
    r"explore\s+(?:roles|jobs|careers|opportunities)"
    r")\b",
    re.I,
)

# href substrings that strongly suggest job board destination.
LINK_HREF_PATTERNS = re.compile(
    r"/(?:roles|jobs|openings|positions|opportunities|apply|open-roles|"
    r"open-positions|all-jobs|all-roles|join-us|join-the-team)(?:/|$|\?)",
    re.I,
)

# href substrings to AVOID following (obvious non-candidates).
LINK_HREF_BLOCKLIST = re.compile(
    r"/(?:login|signup|sign-in|sign-up|privacy|terms|cookie|contact|"
    r"about|blog|press|news|investor|legal|security|trust|partner|"
    r"customer|case-stud|resource)",
    re.I,
)


@dataclass
class ProbeResult:
    company: str
    url: str
    final_url: str = ""
    ats: str = "unknown"
    slug: str = ""
    slug_field: str = ""
    note: str = ""
    hop_count: int = 0


# ---------------------------------------------------------------------------
# Core probing
# ---------------------------------------------------------------------------

def _fetch(url: str) -> requests.Response | None:
    try:
        return requests.get(
            url,
            timeout=FETCH_TIMEOUT,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException:
        return None


def _detect_from_response(resp: requests.Response) -> tuple[str, str, str, str]:
    """Return (ats, slug, slug_field, note) or ('unknown', '', '', note)."""
    host = urlparse(resp.url).netloc.lower()
    path = urlparse(resp.url).path

    # Strategy 1: final hostname match.
    for known_host, ats_name, path_regex, slug_field in HOST_SIGNATURES:
        if host == known_host:
            m = path_regex.search(path)
            slug = ""
            if m:
                slug = next((g for g in m.groups() if g), "")
            return ats_name, slug, slug_field, f"detected via redirect to {host}"

    # Strategy 2: HTML body patterns.
    ctype = resp.headers.get("content-type", "")
    if "html" in ctype.lower():
        body = resp.text
        for pattern, ats_name, slug_field in HTML_SIGNATURES:
            m = pattern.search(body)
            if m:
                return ats_name, m.group(1), slug_field, f"detected via HTML pattern on {host}"

    return "unknown", "", "", f"no ATS signature on {host}{path}"


def _same_etld(host_a: str, host_b: str) -> bool:
    """Naive eTLD+1 match. Works for .com/.io/.ai/.dev/.co — fine for our list.

    Doesn't handle .co.uk correctly; we don't have any .co.uk companies.
    """
    def etld_plus_one(h: str) -> str:
        parts = h.lower().split(".")
        if len(parts) < 2:
            return h.lower()
        return ".".join(parts[-2:])
    return etld_plus_one(host_a) == etld_plus_one(host_b)


def _extract_candidate_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return a list of (candidate_url, link_text) ordered by score descending.

    Scoring: same-eTLD = +2, link text match = +3, href pattern match = +2,
    ATS-host target = +5. Discards links matching the blocklist (about,
    blog, press, etc.) and same-page anchors. Threshold of 3 keeps results
    focused.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    base_host = urlparse(base_url).netloc
    scored: list[tuple[int, str, str]] = []

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        if href.startswith("mailto:") or href.startswith("tel:"):
            continue

        text = a.get_text(strip=True)
        full_url = urljoin(base_url, href)
        full_host = urlparse(full_url).netloc

        # Cross-domain links are off-limits unless they're an ATS host
        # (in which case Strategy 1 would catch them after we follow).
        ats_host = any(full_host == kh for kh, _, _, _ in HOST_SIGNATURES)
        if not _same_etld(base_host, full_host) and not ats_host:
            continue

        if LINK_HREF_BLOCKLIST.search(href):
            continue

        score = 0
        if _same_etld(base_host, full_host):
            score += 2
        if LINK_TEXT_PATTERNS.search(text):
            score += 3
        if LINK_HREF_PATTERNS.search(href):
            score += 2
        # Bonus: ATS host as link target
        if ats_host:
            score += 5

        if score >= 3:
            scored.append((score, full_url, text))

    scored.sort(reverse=True)

    # Dedupe by URL, preserve score order.
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for _, url, text in scored:
        if url in seen:
            continue
        seen.add(url)
        out.append((url, text))
    return out


def probe_one(company: str, url: str, hops_remaining: int = MAX_HOPS,
              visited: set[str] | None = None, depth: int = 0) -> ProbeResult:
    """Probe one company. Recurses up to `hops_remaining` times.

    `visited` is the set of URLs already fetched in this probe (prevents
    revisits and cycles). `depth` is how many hops we've already taken.
    """
    visited = visited if visited is not None else set()
    result = ProbeResult(company=company, url=url, hop_count=depth)

    if url in visited:
        result.note = f"already visited {url}"
        return result
    visited.add(url)

    resp = _fetch(url)
    if resp is None:
        result.note = f"fetch failed at {url}"
        return result

    result.final_url = resp.url
    ats, slug, slug_field, note = _detect_from_response(resp)

    if ats != "unknown":
        result.ats = ats
        result.slug = slug
        result.slug_field = slug_field
        if depth > 0:
            result.note = f"hop {depth}: {note} (followed from previous page)"
        else:
            result.note = note
        return result

    # No detection on this page. If we have hops left, crawl candidate links.
    if hops_remaining > 0:
        candidates = _extract_candidate_links(resp.text or "", base_url=resp.url)
        for cand_url, cand_text in candidates[:MAX_CANDIDATES_PER_PAGE]:
            sub = probe_one(
                company,
                cand_url,
                hops_remaining=hops_remaining - 1,
                visited=visited,
                depth=depth + 1,
            )
            if sub.ats != "unknown":
                sub.note = (
                    f"hop {sub.hop_count}: {sub.note} "
                    f"(via link {cand_text!r} → {cand_url})"
                )
                return sub

    # No detection after exhausting hops.
    result.note = note
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Probe career pages for ATS detection.")
    parser.add_argument(
        "--all", action="store_true",
        help="Probe every company, not just those with ats: unknown.",
    )
    parser.add_argument(
        "--companies-file", default="companies.yaml",
    )
    parser.add_argument(
        "--output", default=None,
        help="Write suggestions to FILE in UTF-8. Without this, prints to "
             "stdout (PowerShell users: use --output to avoid UTF-16).",
    )
    args = parser.parse_args()

    with open(args.companies_file, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    targets: list[tuple[str, str]] = []
    for entry in data.get("companies", []):
        ats = (entry.get("ats") or "unknown").lower()
        if args.all or ats == "unknown":
            # Honor optional probe_url override.
            probe_url = entry.get("probe_url") or entry["url"]
            targets.append((entry["name"], probe_url))

    print(f"# Probing {len(targets)} companies (max {MAX_HOPS} hop(s) per company)...",
          file=sys.stderr)
    print("# Review suggestions before merging into companies.yaml.\n", file=sys.stderr)

    detected: list[ProbeResult] = []
    undetected: list[ProbeResult] = []

    for name, url in targets:
        print(f"# {name} ... ", end="", file=sys.stderr, flush=True)
        result = probe_one(name, url)
        if result.ats != "unknown":
            tag = f"{result.ats} ({result.slug})"
            if result.hop_count > 0:
                tag += f" [hop {result.hop_count}]"
            print(tag, file=sys.stderr)
            detected.append(result)
        else:
            print(f"UNKNOWN — {result.note}", file=sys.stderr)
            undetected.append(result)

    # Build the suggestion document.
    lines: list[str] = []
    lines.append("# ===== Suggested mappings (review before committing) =====\n")
    for r in detected:
        lines.append(f"  - name: {r.company}")
        lines.append(f"    # Probe: {r.note}")
        lines.append(f"    # Final URL: {r.final_url}")
        lines.append(f"    ats: {r.ats}")
        lines.append("    ats_config:")
        lines.append(f"      {r.slug_field}: {r.slug}")
        lines.append("")

    if undetected:
        lines.append("\n# ===== Undetected (manual investigation needed) =====")
        lines.append("# For these, add a `probe_url:` override in companies.yaml")
        lines.append("# pointing to the actual job board (e.g., /careers/roles)")
        lines.append("# and re-run probe.py, OR set `ats: skip` to opt out.\n")
        for r in undetected:
            lines.append(f"# {r.company}: {r.note}")
            lines.append(f"#   Careers URL: {r.url}")
            lines.append(f"#   Final URL:   {r.final_url}")
            lines.append("")

    output = "\n".join(lines)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"\n# Wrote {args.output} (UTF-8).", file=sys.stderr)
    else:
        print(output)

    hop_count = sum(1 for r in detected if r.hop_count > 0)
    print(f"# Summary: {len(detected)} detected ({hop_count} via hop), "
          f"{len(undetected)} undetected.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
