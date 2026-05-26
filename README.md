# RoleDar

Monitors a configurable list of companies for job openings that match your
target titles and locations, then emails you a delta of what changed since
the last run. Built to run hands-free on GitHub Actions.

It hits each company's Applicant Tracking System (ATS) API directly,
Greenhouse, Lever, Ashby, Workable, SmartRecruiters, rather than scraping
HTML, so it's fast and resilient to front-end redesigns. The default
configuration targets senior engineering leadership roles (Engineering
Manager → CTO) at a set of companies, but every part of that is 
configurable: edit the YAML files to track any titles, locations, and
companies you like.

> **Originally built as a personal job-search tool and open-sourced.** If you
> fork it, the things you'll most likely change are `companies.yaml` (who),
> `titles.yaml` (what roles), `locations.yaml` (where), and `config.yaml`
> (branding + timezone). No Python changes needed for normal customization.

## Table of contents

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [The delta model](#the-delta-model)
- [Location filtering](#location-filtering)
- [Out-of-region mode](#out-of-region-mode)
- [Discovering ATS mappings](#discovering-ats-mappings)
- [Probe audit (ATS slug health)](#probe-audit-ats-slug-health)
- [Running locally](#running-locally)
- [Deployment on GitHub Actions](#deployment-on-github-actions)
- [Managing the workflows](#managing-the-workflows)
- [File layout](#file-layout)
- [Maintenance](#maintenance)
- [Troubleshooting](#troubleshooting)

## How it works

Each run:

1. Reads `companies.yaml` and, for each company, calls its ATS API to fetch
   current openings.
2. Keeps only postings whose titles match `titles.yaml` and whose locations
   match `locations.yaml`.
3. Diffs the result against the previous run's saved state to find what's
   **new** and what's **closed**.
4. Writes a CSV of just those changes, emails you a summary with the CSV
   attached, and commits the updated state back to the repo.

One company's failure never aborts the run, each ATS call is isolated, and
repeated failures are tracked and surfaced (see
[Troubleshooting](#troubleshooting)).

## Quick start

### Local setup

This is a **template repository**. Don't clone it directly, instead, create
your own copy from it first:

1. On the repo's GitHub page, click the green **"Use this template"** button →
   **Create a new repository**. Give it a name (e.g. `my-job-scraper`) and
   choose public or private (see [Deployment on GitHub
   Actions](#deployment-on-github-actions) for why that choice matters for
   scheduled runs).
2. On your local machine, clone *your new repo* (**not this template**) and set up the environment:

```bash
git clone https://github.com/<your-username>/<your-new-repo>.git
cd <your-new-repo>
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

Working from your own template-derived repo (rather than a fork or a direct
clone) means you get a clean copy with no upstream link, it's yours to
configure and commit to freely.

### Set up email (Resend): optional

Email is **optional**. If you skip this, nothing breaks, the scraper still runs
and still writes its CSV reports to `reports/jobs/`; you just won't get an email
and will read the files directly instead. This is a perfectly valid way to use
the tool, especially for a [local install](#running-locally).
Email is a convenience layer; the CSV is the source of truth. If you don't want
email, skip to [First run](#first-run).

If you *do* want email notifications, the scraper sends via
[Resend](https://resend.com) (free tier: 100/day, 3,000/month, far more than
enough).

1. Sign up at resend.com and create an API key.
2. For a sender address, you can use Resend's default `onboarding@resend.dev`
   without verifying a domain, fine for sending to yourself. For sending to
   others or a more professional look, verify your own domain in the Resend
   dashboard and use e.g. `jobs@yourdomain.com`.
3. The NOTIFY_EMAIL value should be your email address and currently can 
   only be one email address.
4. Set three environment variables:

   ```bash
   # Windows PowerShell (session-only):
   $env:RESEND_API_KEY="re_..."
   $env:FROM_EMAIL="onboarding@resend.dev"
   $env:NOTIFY_EMAIL="you@example.com"

   # macOS/Linux:
   export RESEND_API_KEY="re_..."
   export FROM_EMAIL="onboarding@resend.dev"
   export NOTIFY_EMAIL="you@example.com"
   ```

   If any of these are unset, the scraper logs a warning, skips the email, and
   continues normally, the report files are written either way.

### First run

This first run uses the **example company list that ships with the template**,
a small set covering the supported ATS types, enough to confirm everything
works. Run it as-is first to verify your installation (email, scraping, state).
Once you've seen it produce a report, swap in your own companies: see
[`companies.yaml`](#companiesyaml) for the format and [Building your companies
list with AI](#building-your-companies-list-with-ai) for the fast way to
assemble one.

```bash
# Dry run: prints the report, writes nothing, sends nothing.
python scrape.py --dry-run --force

# Real run: writes state, writes the CSV, sends the email.
python scrape.py --force
```

`--force` is a documented no-op retained for compatibility (see
[the time-guard note](#a-note-on-time-and-dst)); it's harmless to include.

The first real run treats every matching posting as "new" because there's no
prior state to diff against. Subsequent runs show only changes.

## Configuration

Four YAML files drive everything. None require code changes to edit.

### `config.yaml`

```yaml
report:
  title: "Job Openings"
  timezone: "America/Los_Angeles"
```

- **`title`** is used verbatim in three places: the email subject prefix, the
  email H1 heading, and the CSV filename. One string, three surfaces, so they
  never drift out of sync. Pick anything; if it contains characters illegal in
  filenames (`< > : " / \ | ? *`) those are replaced with underscores in the
  filename only, the subject and heading keep them as typed.
- **`timezone`** is an [IANA timezone name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)
  controlling how run timestamps are *displayed* in the subject, heading, and
  filename. It does not affect *when* the scraper runs (that's the cron, see
  [deployment](#deployment-on-github-actions)). A bad value fails fast at
  startup with a clear message.

### `companies.yaml`

One entry per company:

```yaml
companies:
  - name: Example Co
    url: https://example.com/careers   # human reference only
    region: Bay Area                   # informational
    hq: San Francisco                  # informational
    ats: greenhouse                    # greenhouse | lever | ashby | workable | smartrecruiters | unknown | skip
    ats_config:
      board: exampleco                 # slug field name varies by ATS (see below)
```

The slug field name inside `ats_config` differs per ATS:

| ATS | `ats_config` field | Example |
|---|---|---|
| greenhouse | `board` | `board: airtable` |
| lever | `slug` | `slug: lyrahealth` |
| ashby | `org` | `org: Replicant` (case-sensitive!) |
| workable | `slug` | `slug: huggingface` |
| smartrecruiters | `company` | `company: gong` |

- `ats: unknown` means the company is listed but not yet mapped, it's skipped
  during scraping and surfaced in the email so you remember to map it. Use
  `probe.py` / `probe_direct.py` to discover the mapping (see
  [file layout](#file-layout)).
- `ats: skip` deliberately excludes a company without deleting its entry.
- Ashby slugs are **case-sensitive**, `Replicant` and `replicant` are
  different boards. Several others too. The probe audit catches these.

### Building your companies list with AI

The most tedious part of setup is assembling `companies.yaml`. An AI
assistant (Claude, ChatGPT, etc.) can do most of the heavy lifting, but
there's one hard rule:

> **⚠️ Never trust AI-generated ATS mappings.** An AI will happily produce a
> confident `ats:` value and slug for every company, and a meaningful fraction
> will be wrong, wrong ATS, wrong slug, or a plausible-looking slug that
> doesn't exist. These fail silently on your first run. Use AI to build the
> **list of companies and careers URLs**; use the included probe scripts to
> resolve the **ATS mappings** (see [Discovering ATS
> mappings](#discovering-ats-mappings)). That division of labor is the whole
> trick: AI proposes names and URLs, the probe tooling confirms the mechanics.

So the workflow is always: **AI builds a draft with `ats: unknown` on every
entry → you probe → you review → you commit.**

#### The prompt

Paste this into your AI tool, then fill in either a URL or a list (see the two
options below):

```
I'm building a YAML config for a job-monitoring tool. Produce a companies.yaml
in exactly this format:

companies:
  - name: <Company Name>
    url: <careers or jobs page URL>
    region: <optional, e.g. "Bay Area" — leave blank if unknown>
    hq: <optional, e.g. "San Francisco" — leave blank if unknown>
    ats: unknown

Rules:
- Set `ats: unknown` for EVERY company. Do not guess the ATS or add an
  ats_config block — a separate tool resolves those.
- `url` should be the company's careers/jobs page if you know it; otherwise
  their main domain. Do not invent URLs you're unsure of.
- Only include companies you're actually confident exist. Don't pad the list.
- Output only the YAML, no commentary.

The companies are: [PASTE URL OR LIST HERE]
```

The `ats: unknown` instruction is the important one, it stops the AI from
guessing the part it's bad at, and `unknown` is exactly what the probe scripts
expect to find.

#### Option 1: point the AI at a URL

Give the AI a page that lists companies, a VC portfolio page, an accelerator
batch, an industry list. For example, a venture portfolio page:

```
https://salesforce.com/company/ventures/portfolio/
```

or a Y Combinator batch (`https://www.ycombinator.com/companies?batch=Summer%202025`),
an industry "top 100" list, etc.

**Caveat:** this only works if your AI tool can actually *read* the URL (live
browsing or fetch). Many portfolio and directory pages are JavaScript-rendered
behind filters, and a plain fetch returns an empty shell, the AI then either
fails or, worse, hallucinates a list. If the page is JS-heavy or filtered, fall
back to Option 2: open the page yourself, copy the visible company names, and
paste them in.

#### Option 2: give the AI a list you already have

If you already have companies in mind, just paste them, names alone, or names
with careers URLs:

```
Stripe — https://stripe.com/jobs
Airtable — https://airtable.com/careers
Notion — https://notion.so/careers
```

The AI fills in the structure and sets `ats: unknown` throughout. Providing the
careers URL when you have it helps the next step, the probe uses the URL's
domain as a slug candidate. If you only have a company name and no URL, that's
fine too; list it on its own line and the probe will still try slug candidates
derived from the name.

#### Then: resolve the mappings (required: the list isn't usable yet)

At this point you have a `companies.yaml` full of `ats: unknown` entries. **As-is
it does nothing**, companies marked `unknown` are skipped during scraping. You
now need to use the probe functionality to complete the mappings so the list is
actually useful: it turns each `ats: unknown` into a real ATS + slug that the
scraper can fetch.

Head to [Discovering ATS mappings](#discovering-ats-mappings) for how to run the
probe tools (start with `probe_direct.py`), review the suggestions, and merge the
good ones in. Once you've done that, confirm the whole thing works end to end:

```bash
python scrape.py --dry-run --force
```

### `titles.yaml`

Regex patterns defining which job titles count as a match, with an `excludes`
list per category for filtering false positives (e.g. excluding "Engineering
Program Manager" from the "Engineering Manager" category). Tune this after your
first couple of runs, the matcher is intentionally conservative, so you'll
likely add both new matches and new excludes as you see real data.

### `locations.yaml`

See [Location filtering](#location-filtering).

## The delta model

The scraper is a **change detector**, not an inventory snapshot. Each run's
CSV contains only:

- **`new`**, postings discovered since the last run
- **`closed`**, postings that were present last run but are gone now

The idea is that *your spreadsheet* is the durable inventory: you copy `new`
rows into it and add your own columns (date applied, hiring manager, notes,
interview status, etc.), and you mark `closed` rows as filled/withdrawn. Each
run hands you a small worklist rather than a fresh dump of everything.

On a run with no changes, the CSV still gets written, with a single
`no_changes` heartbeat row, and the email says "No changes this run." The
heartbeat proves the run happened and lets you keep a per-run log by appending
every CSV if you want.

The CSV columns: `scraped_at`, `status`, `category`, `location_match`,
`company`, `title`, `location`, `department`, `url`. It's UTF-8 with a BOM so
Excel on Windows opens it cleanly, and it's attached to every email.

## Location filtering

`locations.yaml` decides which postings count as "in region." A posting
matches if it satisfies **any** allow rule **and no** exclude rule.

```yaml
allow:
  cities:           # substring match
    - San Francisco
    - New York
  states:           # substring match
    - California
  regions:          # regex (so short codes can be word-anchored)
    - "United States"
    - "\\bUSA\\b"
    - "\\bCA\\b"
  remote_patterns:  # regex
    - "Remote.*United States"
    - "^Remote$"
exclude:            # regex; takes precedence over allow
    - "Remote.*Canada"
    - "\\bLondon\\b"
```

- **Cities and states** are case-insensitive substring matches.
- **Regions and remote patterns** are regexes, so short ambiguous tokens like
  `USA` or `CA` are word-boundary–anchored (`\bCA\b`) to avoid matching inside
  "Canada", "Casablanca", etc. Note `OR` (Oregon) is deliberately omitted from
  region codes because it collides with the common "City OR Remote" phrasing.
- **Excludes win.** A posting matching both an allow and an exclude is rejected.

Postings that don't match are flagged `location_match=false` and, by default,
filtered out of both the email and CSV. Tuning is just editing this file, the
matcher reloads it every run and logs how many rules it loaded.

## Out-of-region mode

`python scrape.py --out-of-region` runs the **inverse** location filter: it
shows only title-matched postings *outside* your configured locations. It's a
disjoint worklist, not a superset, default mode and out-of-region mode never
show the same posting.

The two modes keep **independent state files** (`state/last_run.json` vs
`state/last_run_out_of_region.json`), so switching to out-of-region for the
first time correctly shows everything as "new" (you've never seen those foreign
postings), and the two worklists evolve without interfering. The out-of-region
email and CSV are labeled "Out Of Region" in the subject, heading, and filename.

This exists because for senior roles a handful of out-of-region listings is
manageable, but for more junior searches (if you fork and retarget) the foreign
volume can be overwhelming noise, so it's opt-in and separate.

There's a dedicated workflow for it
(`.github/workflows/scrape-jobs-out-of-region.yml`) that's **manual-only by
default**. See [managing the workflows](#managing-the-workflows) to enable a
schedule.

## Discovering ATS mappings

When you add a company you don't have a mapping for, it sits as `ats: unknown`
and is skipped during scraping. Two included tools turn `unknown` into a working
`ats` + `ats_config` mapping. They're for *discovery*, finding mappings you
don't have yet. (A third tool, the [probe audit](#probe-audit-ats-slug-health),
is different: it *monitors* mappings you already have for drift over time.)

**`probe_direct.py`, the one to reach for first.** It ignores company websites
entirely and hits each ATS's public API directly, trying candidate slugs
generated from the company name and its careers-URL domain. For each company it
reports the first ATS/slug that returns a real job board, along with the job
count so you can sanity-check the match:

```bash
python probe_direct.py
```

It reads `companies.yaml`, probes only the `ats: unknown` entries, and prints
paste-ready YAML suggestions to stdout. It **never modifies `companies.yaml`**,
you review the suggestions and paste in the ones you trust. This is the right
tool for resolving a freshly built list (e.g. the output of [Building your
companies list with AI](#building-your-companies-list-with-ai)).

**`probe.py`, the fallback.** Instead of hitting ATS APIs, it crawls each
company's careers *page* and inspects where it redirects and what ATS widgets
are embedded in the HTML. It's useful when `probe_direct.py` can't resolve a
company but you have a careers URL that visibly redirects to a known ATS. For
bulk work, prefer `probe_direct.py`; reach for `probe.py` on the stragglers.

```bash
python probe.py
```

**Workflow:** run `probe_direct.py`, eyeball the suggested mappings (a real
match for an actively hiring company usually shows several jobs, a zero-job
match is plausible but worth verifying), paste the good ones into
`companies.yaml`, and re-run on whatever's still `unknown`. Companies that
resolve to nothing, those running a custom/proprietary ATS, or using an
unusual slug, stay `ats: unknown` (surfaced in each email) or can be set to
`ats: skip` to silence them. Confirm the result with `python scrape.py
--dry-run --force` before committing.

## Probe audit (ATS slug health)

Companies occasionally migrate ATSes or change their slug (a real example: a
company moved from Lever to Ashby, and the old slug started 404ing). The probe
audit is a **read-only weekly health check** that catches this proactively
instead of waiting for the scraper's failure counter to trip.

`python probe_audit.py` re-probes every company against its configured slug and
produces a report in three buckets:

1. **Slug drift (action needed)**, a configured slug returns 404 or 0 jobs for
   **two consecutive** audit runs *and* a working replacement slug is found. The
   report gives you side-by-side old-vs-new with verification URLs and
   paste-ready YAML.
2. **New mappings**, companies currently `ats: unknown` where a probe now finds
   a working slug with jobs.
3. **Divergent (informational)**, a different slug also works, but the
   configured one is fine. No action needed; logged in case it's meaningful.

A slug that looks broken only **once** goes on a "watch" list rather than being
reported as drift, this two-run confirmation threshold filters transient ATS
hiccups (a deploy in progress, a brief outage) from real migrations. The streak
is tracked in `state/probe_audit.json`.

The audit **never modifies `companies.yaml`**, it surfaces discrepancies for
you to verify and apply by hand. It always produces a report (even "All
clear ✓"), emails it, and writes a CSV to `reports/probe/`.

Test it locally:

```bash
python probe_audit.py --dry-run
```

(Note: `--dry-run` still updates the two-run streak counter in
`state/probe_audit.json`, because the streak is the whole point of the audit.
Copy that file aside first if you need a truly read-only test.)

Its workflow (`.github/workflows/probe-audit.yml`) is **manual-only by
default**; the commented schedule is Monday 5am Pacific.

## Running locally

The scraper is an ordinary Python script, you can run it on your own machine on
a local schedule and read the CSV reports directly, for full local control and
zero external dependencies. If you'd rather run it on a server for convenience,
GitHub Actions is a great option; see [Deployment on GitHub
Actions](#deployment-on-github-actions) for how to set that up.

What's different in the local model:

- **No Resend required.** Skip the email setup entirely (see [Set up email](#set-up-email-resend-optional)).
  Reports land in `reports/jobs/` and you read them there.
- **Email is opt-in via one settings file.** Scheduled runs don't see the
  `export`/`$env:` commands from the email-setup section (those only last for
  the terminal window you typed them in). Instead, the repo ships a
  `local-secrets` template you copy and fill in once; the run script reads it
  automatically. Don't want email? Don't copy it, and you get CSV reports only.
  See [Setting up a local schedule](#setting-up-a-local-schedule) — it's a quick
  copy-and-edit, no commands to memorize.
- **No public-repo-or-paid-plan requirement.** That's a GitHub Actions
  constraint; it doesn't apply when you run locally.
- **State just works.** The diff baseline (`state/last_run.json`) persists
  naturally on disk between runs, there's no commit-back step to worry about.
  The next run reads it right where the last one left off.
- **Local time, no UTC math.** Your OS scheduler runs in your local time, so
  there's none of the UTC conversion the Actions cron needs.
- **Your machine must be awake at run time.** This is the one real tradeoff. A
  laptop that's asleep or off at the scheduled time simply misses that run (the
  next run still works, it just diffs against the last successful run). If you
  need guaranteed runs, Actions or an always-on machine is the better fit.

### Setting up a local schedule

The repo ships ready-made scripts so you don't have to write any yourself. You
fill in one settings file (only if you want email), then point your computer's
scheduler at the run script. The run script already handles the two things that
trip people up, running from the right folder and using the right Python.

The files involved (all live in the repo folder):

| File | What it's for | Do you edit it? |
|---|---|---|
| `run-scrape.sh` / `run-scrape.bat` | The thing your scheduler runs | No |
| `local-secrets.sh.example` / `local-secrets.bat.example` | A ready-to-edit template for your email settings | You copy it (Step 1), only if you want email |

#### Step 1 — (Optional) turn on email

**This whole step is optional. Skip it and you get CSV reports only** — the
scraper still runs and still writes to `reports/jobs/`; you just won't get an
email. The run script prints "writing CSV reports only" in its log when email is
off, so you can always tell which mode you're in. If that's all you want, jump
to [Step 2](#step-2--tell-your-computer-when-to-run-it).

If you *do* want email, the repo ships a blank template,
`local-secrets.sh.example` (macOS/Linux) or `local-secrets.bat.example`
(Windows). You copy it to its real name, fill in the three values, and tell git
to ignore your copy so your API key is never uploaded. Three short steps:

**1a. Copy the template to its real name** (the run script looks for the name
*without* `.example`):

```bash
# macOS / Linux:
cp local-secrets.sh.example local-secrets.sh

# Windows (Command Prompt):
copy local-secrets.bat.example local-secrets.bat
```
This can also be done manually with whatever file browser you prefer, either 
way is fine, just copy, don't remane.

**1b. Confirm git is ignoring your copy.** Your real `local-secrets` file holds
your Resend API key, so it must never be committed. The repo's `.gitignore`
already lists both real names for you:

```gitignore
local-secrets.sh
local-secrets.bat
```

So there's normally nothing to do here, just verify: run `git status` after
copying, and your `local-secrets.sh`/`.bat` should *not* appear in the list of
changes. (If it does appear, add the two lines above to `.gitignore`.) Only the
real names are ignored; the `.example` templates stay tracked so they ship with
the repo.

**On macOS/Linux**, also make the scripts runnable, a one-time command:

```bash
chmod +x run-scrape.sh local-secrets.sh
```

**1c. Fill in your settings.** Open your new `local-secrets.sh`/`.bat` in any
text editor and fill in the three blank values, then save. This is similar to 
what's described in [Set up email](#set-up-email-resend-optional), which also 
describes how to obtain the values if you've forgotten. If you copy the file 
but forget this step, no harm, the run script sees the blank values and falls 
back to CSV-only rather than trying to send with no key.

The contents of the file should be

```
RESEND_API_KEY=re_...
FROM_EMAIL=onboarding@resend.dev
NOTIFY_EMAIL=you@example.com
```
The keys are already in the file, you just need to provide their values.

**IMPORTANT** On Windows local-secrets.bat file **DO NOT** use quotation marks, 
either single or double, around the values you input. This will cause 
malformed values to be sent to Resend and it will fail. So

```
FROM_EMAIL=onboarding@resend.dev
```

is right and

```
FROM_EMAIL="onboarding@resend.dev"
```
is wrong.

It is fine to use quotation marks in the local-secrets.sh on macOS/Linux as 
they will be stripped by the script, or you can leave them unquoted, either way. 


#### Step 2 — Tell your computer when to run it

**macOS / Linux:** run `crontab -e` and add one line pointing at the run script.
Use the full path to where your repo lives:

```bash
# 8am daily. Replace the path with the actual path to your repo folder.
0 8 * * * /home/you/your-repo/run-scrape.sh >> /home/you/your-repo/cron.log 2>&1
```

The `>> cron.log 2>&1` part saves a log you can check if something looks off.

**Windows:** open Task Scheduler → Create Basic Task → pick your schedule (e.g.
Daily, 8:00 AM) → on the Action step choose "Start a program" and set:

- **Program/script:** the full path to `run-scrape.bat`, e.g.
  `C:\Users\you\your-repo\run-scrape.bat`
- **Add arguments:** *(leave empty)*
- **Start in:** your repo folder, e.g. `C:\Users\you\your-repo`

That's it. To also schedule the out-of-region or probe-audit runs, copy
`run-scrape.sh`/`.bat` to a new name, change the last line to call
`scrape.py --out-of-region` or `probe_audit.py`, and give it its own schedule.

### A note on git noise

If your local install is still a git repo (e.g. you cloned it), every run will
leave `state/` and `reports/` showing as modified in `git status`. That's
harmless. You can commit them periodically if you want a history, ignore the
noise, or, if you never intend to use git locally, stop tracking them with
`git rm -r --cached state reports` (the files stay on disk; git just stops
watching them).

## Deployment on GitHub Actions

The scraper is designed to run on GitHub Actions with no server to maintain.

### ⚠️ Scheduled workflows require a public repo OR a paid plan

This is the single biggest gotcha. **On a free GitHub account, scheduled
(`cron`) workflows only run in _public_ repositories.** If your repo is private
and you're on the free plan, manual runs (`workflow_dispatch`) will work but the
cron will silently never fire, no error, no email, nothing.

Two ways to resolve:

- **Make the repo public** (your code and committed CSVs become visible;
  your *secrets* stay encrypted regardless), or
- **Upgrade to a paid plan** (GitHub Pro is inexpensive) to keep it private.

### Setup steps

1. **Push the repo to GitHub.** When creating the GitHub repo, do **not**
   initialize it with a README/license/`.gitignore`, push your existing local
   work into an empty repo to avoid a merge conflict on the first push.
2. **Confirm `main` is the default branch.** Scheduled workflows only run from
   the workflow files on the default branch.
3. **Set Actions to read/write.** Settings → Actions → General → Workflow
   permissions → "Read and write permissions." Required so the workflow can
   commit state and reports back to the repo.
4. **Add three repository secrets.** Settings → Secrets and variables →
   Actions → New repository secret:
   - `RESEND_API_KEY`
   - `FROM_EMAIL`
   - `NOTIFY_EMAIL`
5. **Test with a manual run.** Actions tab → "Scrape Jobs" → "Run
   workflow." Watch the steps; confirm you get an email and that a new commit
   from `github-actions[bot]` appears with updated `state/` and `reports/`.

### A note on time and DST

The main workflow runs on a fixed UTC cron and the scraper does **no DST
handling**, by design, so a fork in any timezone can just pick a UTC time and
own it. The `config.yaml` `timezone` only affects how times are *displayed* in
reports, not when the cron fires. When daylight saving shifts, your local run
time shifts by an hour; adjust the cron yourself if it bothers you. (There used
to be a "run only at 8am local" time guard; it was removed in favor of letting
the cron be the sole gate. The `--force` flag is a no-op kept from that era.)

GitHub's scheduled runs are best-effort: delays of 15–60 minutes are normal and
runs are occasionally dropped under load. After editing a workflow or changing
plan, GitHub can take 15–60 minutes to register a cron, and pushing any commit
to `main` nudges it to re-register. For example

```bash
git commit --allow-empty -m "Nudge scheduler"
git push
```

## Managing the workflows

There are three workflows:

| Workflow | File | Default |
|---|---|---|
| Main scraper | `scrape-jobs.yml` | Scheduled (daily) |
| Out-of-region | `scrape-jobs-out-of-region.yml` | Manual-only |
| Probe audit | `probe-audit.yml` | Manual-only |

### Enabling a commented-out schedule

The out-of-region and probe-audit workflows ship with their `schedule:` block
commented out. To enable: open the workflow file, remove the leading `# ` from
the two `schedule:`/`cron:` lines, commit, and push to `main`. Give GitHub up to
an hour to register it.

When enabling a schedule, mind the **UTC day rollover**: the out-of-region cron
is "Sunday 9pm Pacific," which is `05:00 UTC Monday`, so its cron day-of-week
is Monday (`1`), not Sunday. The file documents this inline.

Keep roughly a 2-hour gap between any two workflows' scheduled times. They write
different files so there's no data conflict, and their commit steps use
`git pull --rebase` to absorb a race, but spacing them out avoids relying on
that under load.

### Turning a workflow off

- **Pause it (reversible):** Actions tab → the workflow → "···" → Disable
  workflow. Stops both scheduled and manual runs until re-enabled.
- **Remove it (permanent):** delete the workflow `.yml` file, commit, push.
- **Keep it manual-only:** comment out (or remove) just the `schedule:` block,
  leaving `workflow_dispatch:`.
- **Do NOT** delete a `state/*.json` file expecting it to disable anything,
  that only resets the diff baseline, so the next run treats everything as new.

## File layout

```
.
├── config.yaml                 # Title + display timezone
├── config.py                   # Loads & validates config.yaml
├── companies.yaml              # Company → ATS mapping
├── titles.yaml                 # Title-match regexes (tunable)
├── locations.yaml              # Location allow/exclude rules (tunable)
├── matcher.py                  # Title matching
├── location_matcher.py         # Location matching
├── scrape.py                   # Main entry point
├── run-scrape.sh               # Local scheduler script (macOS/Linux)
├── run-scrape.bat              # Local scheduler script (Windows)
├── local-secrets.sh.example    # Email-settings template, macOS/Linux (you copy + fill in)
├── local-secrets.bat.example   # Email-settings template, Windows (you copy + fill in)
├── report.py                   # CSV + email-body rendering
├── notify.py                   # Email via Resend (markdown→HTML, CSV attach)
├── state.py                    # Scraper state + failure tracking
├── probe.py                    # ATS detection by crawling careers pages
├── probe_direct.py             # ATS detection by direct API probing
├── probe_audit.py              # Weekly read-only slug-health audit
├── probe_state.py              # Probe-audit streak state
├── scrapers/
│   ├── __init__.py             # SCRAPER_REGISTRY
│   ├── base.py                 # JobPosting dataclass + BaseScraper
│   ├── greenhouse.py
│   ├── lever.py
│   ├── ashby.py
│   ├── workable.py
│   └── smartrecruiters.py
├── state/                      # Committed by the workflows (your audit log)
│   ├── last_run.json           # Default-mode diff baseline
│   ├── last_run_out_of_region.json
│   ├── failures.json           # Consecutive scraper failures
│   └── probe_audit.json        # Probe-audit drift streaks
├── reports/
│   ├── jobs/                   # Job delta CSVs
│   └── probe/                  # Probe-audit reports
└── .github/workflows/
    ├── scrape-jobs.yml
    ├── scrape-jobs-out-of-region.yml
    └── probe-audit.yml
```

### Adding support for a new ATS

1. Create `scrapers/myats.py` with a class inheriting `BaseScraper` that
   implements `fetch() -> list[JobPosting]`.
2. Register it in `scrapers/__init__.py`'s `SCRAPER_REGISTRY`.
3. Use `ats: myats` in `companies.yaml`.

## Maintenance

- **Tune `titles.yaml` and `locations.yaml`** after your first few runs based on
  the false positives/negatives you see. Both reload every run; no code change.
- **Action version bumps.** GitHub periodically deprecates the Node runtime its
  actions run on (you'll see a warning in the run logs). When that happens, bump
  the `actions/checkout` and `actions/setup-python` versions in all three
  workflow files. To automate this, add a `.github/dependabot.yml` enabling
  Dependabot for the `github-actions` ecosystem and it'll open version-bump PRs
  for you.
- **The "Out Of Region" label is hardcoded English** in `report.py`. If you want
  it translated, edit it there, it's not in `config.yaml` because it's coupled
  to other in-code labels.
- **Reports accumulate in git.** Each run commits a small CSV. Over a year of
  daily runs that's a few hundred files in `reports/jobs/`, harmless, but you
  can prune with `git rm reports/jobs/2026-0*` and commit if you like.

## Troubleshooting

**A company shows up in "Failed scrapes."** Check `state/failures.json` for the
consecutive-failure count and last error. After 3 consecutive failures it's
flagged in the report header and the email subject. Most common cause is a slug
change or ATS migration, run the [probe audit](#probe-audit-ats-slug-health) to
find the new mapping.

**The cron never fires.** Almost always the public-repo-or-paid-plan issue (see
[deployment](#deployment-on-github-actions)). Otherwise: confirm `main` is the
default branch, confirm the workflow appears in the Actions sidebar, and push a
commit to `main` to re-trigger schedule registration.

**State isn't persisting between runs.** Confirm Actions has read/write
permissions and that the "Commit state and report" step actually committed (its
log should show a commit, not "nothing to commit"). The commit step uses
`git add` then `git diff --cached --quiet`, which correctly picks up brand-new
files (a plain `git diff` would miss them).

**The email lands in spam.** First emails from `onboarding@resend.dev` sometimes
do. Mark "Not spam" once and your client learns. For better deliverability,
verify your own domain in Resend and use a sender on it.

**False positives / missed roles in matches.** Tune `titles.yaml`. Each category
has an `excludes` list for negative patterns.

**A git push from a workflow was rejected.** The out-of-region and probe-audit
workflows `git pull --rebase` before pushing to handle this. If the main daily
workflow ever hits it (only likely if you retime workflows close together), add
the same `git pull --rebase origin main` line to its commit step.
