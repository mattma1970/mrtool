# mrtool — World Masters Rankings capture + research store

A targeted tool for pulling individual masters-athlete data from
[mastersrankings.com](https://mastersrankings.com/) (the official World
Masters Rankings: ~5.1M performances, ~480k athletes, includes Australian
results uploaded by the AMAA) — and a local store for doing **broader
research** on top of it.

It is deliberately **targeted, not a site scraper**: you (or the agent
driving it) only ever fetch named athletes or explicitly-chosen cohorts.

## The three layers

```
  you / the agent ("Deep Sea Crawler")
   plans a query · resolves ambiguity · runs SQL · interprets
        │  drives via bash          │  queries
        ▼                          ▼
  ACQUISITION (mrtool.py)      LOCAL STORE (store.db, SQLite)
  browser · auth (once) ·       athletes / performances /
  search→resolve→x8 ·           rankings / meets / crawl
  fetch + parse                 manifest (resumable, deduped)
        │
        ▼
  mastersrankings.com  (Cloudflare, residential IP)
```

- **Acquisition** (`mrtool.py`) is the "hands": the only thing that talks to
  the site. It owns the one-time human Cloudflare auth, the search→resolve→
  `x8` id, and fetching.
- **The store** (`store.db`) is the memory. Once performances are normalized,
  broad questions ("top 10 PV M40s", "5-year trend") are **SQL**, not scrapes.
- **The intelligence layer** (an agent, driven over bash) plans the query,
  calls mrtool to fill gaps, runs SQL, and answers. `x8` id is the canonical
  athlete key everywhere — names collide, ids don't.

## Setup

```bash
cd mrtool
bash install.sh          # venv + playwright + self-contained Chromium in ./.browsers
```

Or manually:
```bash
python3 -m venv .venv
./.venv/bin/pip install playwright
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" ./.venv/bin/python -m playwright install chromium
```
`mrtool.py` auto-uses `./.browsers` when present.

## Targeted use (a handful of known athletes)

```bash
./.venv/bin/python mrtool.py auth                  # ONE-TIME: visible browser,
                                                   #   pass Cloudflare, log in, verify
./.venv/bin/python mrtool.py search "Jane Smith"   # find + register (pick # if ambiguous)
./.venv/bin/python mrtool.py list                  # registry + capture freshness
./.venv/bin/python mrtool.py refresh               # re-capture all, headless, unattended
```

After `auth`, everything runs unattended; a saved profile is reused. If
Cloudflare ever expires the session (typically after weeks), re-run `auth`.

## Broad / research use (the "crawler" surface)

These are the commands an agent drives. All emit **JSON to stdout** so they're
machine-readable.

```bash
# The store (SQLite). The agent's main query path.
./.venv/bin/python mrtool.py store                          # stats
./.venv/bin/python mrtool.py store --query "SELECT …"       # any SQL, rows as JSON
./.venv/bin/python mrtool.py store --history X8ID           # one athlete's history
./.venv/bin/python mrtool.py store --top "PV|M40" --n 10    # top-N from latest ranking

# Bulk-fetch a cohort of athletes into the store (rate-limited, resumable).
./.venv/bin/python mrtool.py fetch X8A X8B --store
./.venv/bin/python mrtool.py fetch --from-file ids.txt --store --delay 2

# Capture a rankings page for a cohort -> top-N ids (the "top few %" entry point).
./.venv/bin/python mrtool.py rankings --event PV --age M40 --year 2026 --n 10 --json
```

**How a research question runs end-to-end** (the agent's tool loop):

1. `rankings --event PV --age M40 --year 2026 --n 10 --json` → top-10 `x8` ids
2. `store --query "SELECT id FROM athletes WHERE id IN(…)"` → which are cached
3. `fetch <the missing ids> --store` → fetch + upsert (rate-limited)
4. `store --query "SELECT date,event,mark,age_grade … WHERE athlete_id IN(…)"`
5. interpret + answer.

The agent only *scrapes* the genuinely-missing profiles; everything else is a
query.

## Parser status (read this)

`parse_profile.py` is a **first pass**: a generic, conservative column detector
written against the *shape* of the site's tables. It has **not** been validated
against a real captured profile page. It is safe by default:

- it always writes `parsed.json` next to each capture (reviewable evidence);
- it scores each row and flags `needs_review` when confidence is low;
- `fetch`/`refresh` write to the store **only when `--store` is passed**.

**Next step:** after your first real `auth` + `search` + `refresh`, send one
capture dir (`profile.html` + `tables.json` + `network.jsonl`). I'll tune the
parser to the actual layout and flip the default to store-by-default.

## Policy note

The site's robots.txt is `search=yes, use=reference, ai-train=no`. A bounded,
hard-cached pull of the specific cohorts you ask about is reference use. Don't
mirror large fractions of the DB or train on it. `fetch` rate-limits
(`--delay`) and the store dedupes, so re-runs stay cheap and polite.

## Run it on the right machine

The one-time `auth` needs a display, and Cloudflare clears far more reliably
from a residential IP than a datacenter one. Run it on the machine whose
network you already pass (the saved profile only helps there).
