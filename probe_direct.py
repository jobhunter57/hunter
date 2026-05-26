"""ATS detection by direct API probing (probe v3).

Strategy: for each company with `ats: unknown`, generate 4-5 candidate slugs
from the company name and the careers URL domain, then hit each ATS's
public API directly to see which returns a real job board.

Unlike probe.py (which crawls career pages and looks at HTML), this probe
never touches the company's website. It asks the ATSes directly. This
sidesteps SPA pages entirely — Airtable's /careers is a JS shell, but the
Greenhouse API at boards-api.greenhouse.io/v1/boards/airtable/jobs returns
real data.

Validation: an ATS API returns 404 for invalid slugs and 200 with a known
schema (`jobs` array, `results` array, etc.) for valid ones. A 200 with
the right schema is a candidate match.

Match policy: when a 200+schema match is found, the probe checks the job
count. A jobs > 0 match is returned immediately as the answer. A 0-job
match is held aside as a fallback and the probe keeps searching — there
may be a real, populated match on a different slug or ATS. Only if the
entire candidate space is exhausted does the probe fall back to the 0-job
result (still reported, but flagged for manual verification).

This matters for case-sensitive ATSes: 'ifttt' (lowercase) returns 0 jobs
on SmartRecruiters and would short-circuit the probe in an "always-return-
first-match" design, hiding the real 'IFTTT' (capital) board behind it.

Usage:
  python probe_direct.py --output direct-output.yaml

Reads companies.yaml. Only probes entries with `ats: unknown`. Never
modifies companies.yaml. Review suggestions and paste in manually.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

USER_AGENT = "salesforce-ventures-jobs-probe-direct/3.0 (+job-search-research)"
TIMEOUT = 15
DELAY_BETWEEN_REQUESTS = 0.2  # be polite — 5 req/sec ceiling per ATS


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------

def generate_slug_candidates(name: str, url: str) -> list[str]:
    """Generate ordered slug candidates for a company.

    Order matters: we try the most likely shapes first. The most common
    convention across ATSes is lowercased-alphanumeric or hyphenated-
    lowercase.
    """
    candidates: list[str] = []

    def _add(s: str) -> None:
        s = s.strip()
        if s and s not in candidates:
            candidates.append(s)

    name_lower = name.lower()

    # ----- Lowercase candidates first -----

    # 1. Lowercase, alphanumeric only ("Modern Treasury" → "moderntreasury")
    _add(re.sub(r"[^a-z0-9]", "", name_lower))

    # 2. Lowercase, hyphenated ("Modern Treasury" → "modern-treasury")
    hyphenated = re.sub(r"\s+", "-", name_lower)
    hyphenated = re.sub(r"[^a-z0-9-]", "", hyphenated)
    _add(hyphenated)

    # 3. Domain-derived from URL ("https://www.faros.ai/careers" → "faros").
    # Skip if URL points at a known ATS host (e.g., apply.workable.com/x);
    # the company's slug isn't "workable".
    KNOWN_ATS_HOSTS = ("workable.com", "greenhouse.io", "lever.co",
                       "ashbyhq.com", "smartrecruiters.com")
    if url:
        try:
            host = urlparse(url).netloc.lower()
            if not any(host.endswith(h) for h in KNOWN_ATS_HOSTS):
                host = re.sub(r"^(www|careers|jobs|apply|about|corp)\.", "", host)
                domain_root = host.split(".")[0]
                _add(domain_root)
        except Exception:
            pass

    # 4. Name's first dot-segment ("Recall.ai" → "recall", "You.com" → "you").
    # Handles companies whose display name embeds a TLD-like suffix.
    first_seg = name_lower.split(".")[0]
    _add(re.sub(r"[^a-z0-9]", "", first_seg))

    # 5. First space-delimited word ("Modern Treasury" → "modern")
    first_word = name_lower.split()[0] if name_lower else ""
    _add(re.sub(r"[^a-z0-9]", "", first_word))

    # ----- Then capitalized variants of pure-alpha lowercase candidates -----
    # Ashby preserves case; several portfolio entries use Capital-first
    # form (Deepgram, Influxdata, Electric, Recall). Hyphenated slugs are
    # always all-lowercase by convention so we skip those.
    for slug in list(candidates):
        if slug and "-" not in slug and slug == slug.lower():
            _add(slug[0].upper() + slug[1:])

    # 6. CamelCase preserving the original name's caps ("Modern Treasury" →
    # "ModernTreasury", "TRM Labs" → "TRMLabs")
    camel = re.sub(r"[^A-Za-z0-9]", "", name)
    _add(camel)

    return candidates


# ---------------------------------------------------------------------------
# Per-ATS API probes
#
# Each returns (matched: bool, job_count: int, api_url: str).
# matched == True means: status 200 AND response shape matches the ATS's
# expected schema. Empty job arrays still count as a match (the slug is
# registered, it just has no openings right now).
# ---------------------------------------------------------------------------

def try_greenhouse(slug: str) -> tuple[bool, int, str]:
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        resp = requests.get(api_url, timeout=TIMEOUT,
                            headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("jobs"), list):
                return True, len(data["jobs"]), api_url
    except (requests.RequestException, ValueError):
        pass
    return False, 0, api_url


def try_lever(slug: str) -> tuple[bool, int, str]:
    api_url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        resp = requests.get(api_url, timeout=TIMEOUT,
                            headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return True, len(data), api_url
    except (requests.RequestException, ValueError):
        pass
    return False, 0, api_url


def try_ashby(slug: str) -> tuple[bool, int, str]:
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        resp = requests.get(api_url, timeout=TIMEOUT,
                            headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("jobs"), list):
                return True, len(data["jobs"]), api_url
    except (requests.RequestException, ValueError):
        pass
    return False, 0, api_url


def try_workable(slug: str) -> tuple[bool, int, str]:
    api_url = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
    try:
        resp = requests.post(api_url, json={"query": ""}, timeout=TIMEOUT,
                             headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("results"), list):
                return True, len(data["results"]), api_url
    except (requests.RequestException, ValueError):
        pass
    return False, 0, api_url


def try_smartrecruiters(slug: str) -> tuple[bool, int, str]:
    api_url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    try:
        resp = requests.get(api_url, timeout=TIMEOUT,
                            headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("content"), list):
                return True, len(data["content"]), api_url
    except (requests.RequestException, ValueError):
        pass
    return False, 0, api_url


# ATS probe order. Based on probe v2's distribution, Ashby is the dominant
# choice for this cohort, then Greenhouse, then the rest. Hitting the
# most likely one first means earlier exit and fewer total requests.
ATS_PROBES: list[tuple[str, str, callable]] = [
    ("ashby",           "org",     try_ashby),
    ("greenhouse",      "board",   try_greenhouse),
    ("lever",           "slug",    try_lever),
    ("workable",        "slug",    try_workable),
    ("smartrecruiters", "company", try_smartrecruiters),
]


# ---------------------------------------------------------------------------
# Per-company probe
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    company: str
    url: str
    ats: str = "unknown"
    slug: str = ""
    slug_field: str = ""
    job_count: int = 0
    api_url: str = ""
    note: str = ""
    attempts: int = 0


def probe_direct(name: str, url: str) -> ProbeResult:
    """Try slug candidates against each ATS API. Return best match.

    Slug-major iteration order: try slug A against all ATSes, then slug B
    against all ATSes, etc. This gives more weight to slug-shape preference
    than to ATS preference, since slug shape is more predictive than ATS
    pop counts for an arbitrary unknown company.

    Match policy:
      - jobs > 0  → return immediately (clearly correct slug).
      - 0 jobs    → remember as a fallback but keep searching.
      - no match  → continue.

    This handles cases where one slug shape collides with a different
    company's empty board (returning 0 jobs) while another slug shape on
    a different ATS has the real, populated board. Without this, IFTTT's
    lowercase 'ifttt' matched SmartRecruiters with 0 jobs and probe v3
    returned before trying 'IFTTT' (capital), which has the real jobs.
    Case-sensitive ATSes (Ashby, SmartRecruiters) make this necessary.
    """
    candidates = generate_slug_candidates(name, url)
    result = ProbeResult(company=name, url=url)

    # First-seen 0-job match. We keep searching for a jobs > 0 result
    # but fall back to this if the entire candidate space is exhausted
    # without finding one.
    fallback: ProbeResult | None = None

    for slug in candidates:
        for ats_name, slug_field, fn in ATS_PROBES:
            result.attempts += 1
            matched, count, api_url = fn(slug)
            time.sleep(DELAY_BETWEEN_REQUESTS)

            if not matched:
                continue

            if count > 0:
                # Real match — return immediately.
                result.ats = ats_name
                result.slug = slug
                result.slug_field = slug_field
                result.job_count = count
                result.api_url = api_url
                result.note = (
                    f"matched {ats_name} API with slug {slug!r} "
                    f"({count} job{'s' if count != 1 else ''})"
                )
                return result

            # 0-job match — remember the first one and keep going.
            if fallback is None:
                fallback = ProbeResult(
                    company=name, url=url,
                    ats=ats_name, slug=slug, slug_field=slug_field,
                    job_count=0, api_url=api_url,
                )

    # Exhausted all candidates without finding jobs > 0.
    if fallback is not None:
        fallback.attempts = result.attempts
        fallback.note = (
            f"matched {fallback.ats} API with slug {fallback.slug!r} "
            f"(0 jobs — best match after exhausting "
            f"{result.attempts} attempts)"
        )
        return fallback

    result.note = (
        f"no match found across {len(candidates)} candidate slugs "
        f"× {len(ATS_PROBES)} ATSes ({result.attempts} API calls)"
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe ATS APIs directly for companies with ats: unknown.",
    )
    parser.add_argument("--companies-file", default="companies.yaml")
    parser.add_argument(
        "--output", default=None,
        help="Write suggestions to FILE in UTF-8. Without this, prints to stdout.",
    )
    args = parser.parse_args()

    with open(args.companies_file, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    targets: list[tuple[str, str]] = []
    for entry in data.get("companies", []):
        ats = (entry.get("ats") or "unknown").lower()
        if ats != "unknown":
            continue
        # Honor probe_url override if set, else use canonical url.
        target_url = entry.get("probe_url") or entry.get("url", "")
        targets.append((entry["name"], target_url))

    print(f"# Direct API probe: {len(targets)} companies with ats: unknown.",
          file=sys.stderr)
    print(f"# {DELAY_BETWEEN_REQUESTS}s delay between requests; "
          f"estimated max runtime: "
          f"~{len(targets) * 5 * 4 * DELAY_BETWEEN_REQUESTS / 60:.1f} min "
          f"if no matches found.\n", file=sys.stderr)

    confirmed: list[ProbeResult] = []        # matched AND jobs > 0
    needs_verify: list[ProbeResult] = []     # matched but 0 jobs — suspect
    undetected: list[ProbeResult] = []       # no match at all

    for name, url in targets:
        print(f"# {name} ... ", end="", file=sys.stderr, flush=True)
        result = probe_direct(name, url)
        if result.ats == "unknown":
            print(f"UNKNOWN after {result.attempts} attempts",
                  file=sys.stderr)
            undetected.append(result)
        elif result.job_count > 0:
            print(f"{result.ats} ({result.slug}, {result.job_count} jobs) "
                  f"after {result.attempts} attempts", file=sys.stderr)
            confirmed.append(result)
        else:
            print(f"{result.ats} ({result.slug}, 0 jobs — VERIFY) "
                  f"after {result.attempts} attempts", file=sys.stderr)
            needs_verify.append(result)

    # ---------- Build the output document ----------
    lines: list[str] = []

    # Confirmed section
    lines.append(f"# ===== Confirmed matches ({len(confirmed)}) =====")
    lines.append("# Generated by probe_direct.py.")
    lines.append("# These all returned a populated job board — likely safe to paste in.")
    lines.append("")
    for r in confirmed:
        lines.append(f"  - name: {r.company}")
        lines.append(f"    # Probe v3: matched {r.ats} API with slug {r.slug!r}")
        lines.append(f"    # Jobs found: {r.job_count}")
        lines.append(f"    # Verified at: {r.api_url}")
        lines.append(f"    ats: {r.ats}")
        lines.append("    ats_config:")
        lines.append(f"      {r.slug_field}: {r.slug}")
        lines.append("")

    # Needs-verification section
    if needs_verify:
        lines.append(f"\n# ===== Needs verification ({len(needs_verify)}) =====")
        lines.append("# These matched an ATS API but returned ZERO jobs. A real VC-backed")
        lines.append("# company actively hiring should have non-zero openings, so a 0-job")
        lines.append("# result is most likely one of:")
        lines.append("#   - a slug collision (the slug belongs to a different company)")
        lines.append("#   - a stale/inactive board the company doesn't use anymore")
        lines.append("#   - the company has paused hiring (rare but possible)")
        lines.append("# Click the Verified-at URL and confirm before pasting. If it's")
        lines.append("# wrong, leave the entry as ats: unknown and try DevTools lookup.")
        lines.append("")
        for r in needs_verify:
            lines.append(f"  - name: {r.company}")
            lines.append(f"    # Probe v3: matched {r.ats} API with slug {r.slug!r}")
            lines.append(f"    # Jobs found: 0  ⚠️  VERIFY MANUALLY")
            lines.append(f"    # Verified at: {r.api_url}")
            lines.append(f"    ats: {r.ats}")
            lines.append("    ats_config:")
            lines.append(f"      {r.slug_field}: {r.slug}")
            lines.append("")

    # Undetected section
    if undetected:
        lines.append(f"\n# ===== Still undetected ({len(undetected)}) =====")
        lines.append("# These either run a custom ATS (Stripe, Plaid likely),")
        lines.append("# use a non-standard slug, or use an ATS we don't probe yet.")
        lines.append("# Manual lookup via Chrome DevTools is the next step:")
        lines.append("# open careers page, open DevTools → Network → Fetch/XHR,")
        lines.append("# reload, look for requests to a known ATS host.")
        lines.append("")
        for r in undetected:
            lines.append(f"# {r.company}: {r.note}")
            lines.append(f"#   Careers URL: {r.url}")
            lines.append("")

    output = "\n".join(lines)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"\n# Wrote {args.output} (UTF-8).", file=sys.stderr)
    else:
        print(output)

    print(
        f"# Summary: {len(confirmed)} confirmed (jobs>0), "
        f"{len(needs_verify)} need verification (0 jobs), "
        f"{len(undetected)} still unknown.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
