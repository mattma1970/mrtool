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

## Running on two machines (browser machine + research machine)

`auth` needs a **display** and a **residential IP**, so it can only run on the
machine whose browser you already use (call it **A**). The store and the
research agent can live on any other machine (call it **B** — e.g. a server).
The tool ships with commands to move state between the two.

If the **launched** browser (the one `auth` opens) gets stuck on the
Cloudflare wall while your **normal** browser loads the site fine, Cloudflare
is detecting the automation. The cleanest fix is the **cookie relay** below —
pass Cloudflare in your trusted browser, then hand the cookies to the tool.

### Cookie relay — when the launched browser is flagged but your normal one works

Cloudflare only runs its challenge (the "Just a moment…" wall) when a request
has no valid clearance. While that challenge is on screen, the page can see a
DevTools client attached and fails. But once a valid `cf_clearance` cookie is
already in the jar, **no challenge is ever issued** — so the automation is never
noticed. The relay puts you in that state by borrowing the clearance your
trusted browser already earned:

```bash
# 1. In your NORMAL browser: open mastersrankings.com and let it clear CF.
# 2. Use your cookie-copy extension to export that tab's cookies to a file
#    (JSON array or cookies.txt both work). Make sure HttpOnly cookies are
#    included — cf_clearance is one of them.
# 3. On machine A, inject them into the tool's profile:
.venv/Scripts/python mrtool.py cookie-import cookies.json
#    (Linux/macOS: ./.venv/bin/python mrtool.py cookie-import cookies.json)
# 4. Verify from A:
.venv/Scripts/python mrtool.py check
```

By default only `mastersrankings.com` cookies are imported (use
`--all-domains` to keep everything). Because the clearance is typically bound
to the **IP + TLS fingerprint** that earned it, this is reliable when you run
`cookie-import` + `check` on the **same machine** that passed Cloudflare
(machine A). Importing the same file onto machine B (a different IP) is a
gamble that `check` will settle — if it fails there, fall through to Pattern 2.

### Pattern 1 — carry the session over (whole tool runs on B)

The entire Cloudflare session lives in one directory: `profile/` (Playwright's
persistent browser profile — cookies, storage, the CF clearance token). It's
portable.

```bash
# on A (has the browser):
./venv/bin/python mrtool.py auth                 # one-time human step
./venv/bin/python mrtool.py export-session       # -> mrtool-session-<stamp>.tar.gz
# copy that tarball to B (scp / usb / however), then on B:
./venv/bin/python mrtool.py import-session mrtool-session-<stamp>.tar.gz
./venv/bin/python mrtool.py check                # <-- the honest test
```

`check` loads a page headlessly **from B's own network** and reports
PASS/FAIL. This matters because **Cloudflare often binds its clearance token to
the IP / fingerprint that earned it** — so a session that works on A can still
403 on B. `check` is the only way to know for sure.

- **PASS** → you're done: run everything (search/fetch/rankings) headless on B.
- **FAIL** → use Pattern 2.

### Pattern 2 — split roles (fetch on A, research on B)

If the session won't carry over, keep the parts that need the good network on
A, and move only the *results* to B:

```bash
# on A: do the fetching (session already valid here)
./venv/bin/python mrtool.py fetch <ids> --store --delay 2
./venv/bin/python mrtool.py sync-export          # -> mrtool-sync-<stamp>.tar.gz
# copy to B, then:
./venv/bin/python mrtool.py sync-import mrtool-sync-<stamp>.tar.gz
./venv/bin/python mrtool.py store                # query the synced data
```

`sync-*` moves `store.db` + `athletes.json` (the research memory), not the
browser. B (and the agent on it) does all the SQL/analysis; A is just the hand
that pulls pages. Both patterns coexist — you can `fetch` on A and `check`
passes on B at the same time; each machine uses whatever state it has.

### What never leaves A

`profile/` (your CF cookies / login) is git-ignored and is the one secret. The
session tarball and any **cookie export** you move around contain it — transfer
them over a channel you trust and delete them from A's disk when you're done.
`store.db`, `athletes.json`, and the `sync`/`session`/`cookies` bundles are all
git-ignored too.
