#!/usr/bin/env python3
"""
mrtool — targeted World Masters Rankings (mastersrankings.com) capture tool.

Design goals:
  * Athlete-first: you maintain a small hand-picked list ("registry").
  * Targeted: never scrapes the whole site — only searches for named athletes
    and captures their profile pages.
  * Human-in-the-loop where needed: first run passes Cloudflare by hand;
    ambiguous name searches are resolved by picking a numbered candidate.
  * Disambiguation happens ONCE: the chosen athlete's stable profile URL is
    stored in the registry, so all future runs go straight to the profile.
  * Evidence-first: every capture keeps raw HTML, extracted tables, and a log
    of the JSON/XHR network traffic the page itself made — so the parsing can
    be upgraded to a direct-API mode without re-asking the site anything.

Requires: playwright  (python3 -m venv .venv && .venv/bin/pip install playwright
                      && .venv/bin/python -m playwright install chromium)

Usage:
  python3 mrtool.py auth                     # one-time: open browser, pass Cloudflare
  python3 mrtool.py search "Jane Smith"      # find + register an athlete
  python3 mrtool.py search "Jane Smith" --pick 2
  python3 mrtool.py refresh [NAME|ALL]       # re-capture registered athletes
  python3 mrtool.py list [--json]            # registry + capture status
  python3 mrtool.py profile <url>            # capture a profile by direct URL

Research / broad queries (the "crawler" surface):
  python3 mrtool.py store                    # store stats
  python3 mrtool.py store --query "SELECT …" # run SQL, rows as JSON
  python3 mrtool.py store --history X8ID     # one athlete's history as JSON
  python3 mrtool.py store --top "PV|M40" --n 10
  python3 mrtool.py fetch X8A X8B --store    # bulk-fetch ids into the store
  python3 mrtool.py fetch --from-file ids.txt --store --delay 2
  python3 mrtool.py rankings --event PV --age M40 --year 2026 --n 10 --json

Two-machine coordination (browser on machine A, research/agent on machine B):
  # A (has a browser):  auth, then...
  python3 mrtool.py export-session          # -> mrtool-session-<stamp>.tar.gz
  # move the file to B, then B:
  python3 mrtool.py import-session mrtool-session-<stamp>.tar.gz
  python3 mrtool.py check                   # does the session pass CF from here?
  # if check fails (CF bound the token to A's IP), use the split setup instead:
  python3 mrtool.py sync-export             # A: tar up store.db + registry
  # move to B:  python3 mrtool.py sync-import mrtool-sync-<stamp>.tar.gz
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import tarfile
import time
import uuid
from pathlib import Path

BASE_URL = "https://mastersrankings.com"
SEARCH_PATH = "/athlete-search/"
RANKINGS_PATH = "/rankings/"
ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT / "profile"
DATA_DIR = ROOT / "data"

# Prefer a workspace-local browser install (./browsers) when present, so the
# tool works even if ~/.cache/ms-playwright isn't writable.
_local_browsers = ROOT / ".browsers"
if _local_browsers.exists() and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_local_browsers)
REGISTRY_PATH = ROOT / "athletes.json"

CHALLENGE_MARKERS = ("just a moment", "challenge-platform", "cf-turnstile",
                     "checking your browser", "verify you are human")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")


# ---------------------------------------------------------------- helpers ---

def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "athlete"


def norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower().strip())


def is_challenge(title: str, html: str) -> bool:
    blob = (title + " " + html[:8000]).lower()
    return any(m in blob for m in CHALLENGE_MARKERS)


def log(msg: str) -> None:
    print(f"[mrtool] {msg}", flush=True)


def load_registry() -> dict:
    if REGISTRY_PATH.exists():
        try:
            return json.loads(REGISTRY_PATH.read_text())
        except Exception:
            log(f"warning: could not parse {REGISTRY_PATH}; starting fresh")
    return {"athletes": []}


def save_registry(reg: dict) -> None:
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2) + "\n")


def find_registered(name: str, reg: dict):
    target = norm_name(name)
    for a in reg["athletes"]:
        if norm_name(a.get("name", "")) == target:
            return a
    return None


def browser_kind(args):
    """Return (kind, executable or None)."""
    if args.browser == "chromium":
        return "chromium", None
    if args.browser == "chrome":
        exe = shutil.which("google-chrome") or shutil.which("chromium") \
            or shutil.which("chromium-browser") or _win_executable("chrome")
        if not exe:
            sys.exit("error: no google-chrome/chromium found; "
                     "use default (playwright chromium) or install Chrome")
        return "chrome", exe
    if args.browser == "msedge":
        exe = shutil.which("msedge") or shutil.which("microsoft-edge") \
            or _win_executable("edge")
        if not exe:
            sys.exit("error: no msedge found on this system")
        return "msedge", exe
    sys.exit(f"error: unknown browser {args.browser}")


def _win_executable(app: str):
    """Locate a Windows Chrome/Edge install (not always on PATH)."""
    rel = {"chrome": ("Google", "Chrome", "Application", "chrome.exe"),
           "edge": ("Microsoft", "Edge", "Application", "msedge.exe")}[app]
    for base in (os.environ.get("PROGRAMFILES"),
                 os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base).joinpath(*rel)
            if p.exists():
                return str(p)
    return None


def launch(p, args, headless: bool, wait_for_manual: bool = False):
    """Launch a persistent-context browser; returns (context, page)."""
    kind, exe = browser_kind(args)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1440, "height": 900},
        locale="en-AU",
        user_agent=None,  # keep the real engine fingerprint (better for CF)
        args=["--disable-blink-features=AutomationControlled"],
    )
    if kind == "chromium":
        ctx = p.chromium.launch_persistent_context(**kwargs)
    else:
        kwargs["channel"] = kind
        if exe:
            kwargs["executable_path"] = exe
        ctx = p.chromium.launch_persistent_context(**kwargs)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def watch_network(page, out_path: Path) -> None:
    """Log JSON-ish XHR traffic the page itself makes (API discovery)."""
    def on_response(resp):
        try:
            ct = (resp.headers or {}).get("content-type", "")
            url = resp.url
            if "json" not in ct and "/api/" not in url and "ajax" not in url:
                return
            body = ""
            try:
                body = resp.text()
            except Exception:
                pass
            rec = {
                "ts": dt.datetime.now().isoformat(timespec="seconds"),
                "url": url,
                "status": resp.status,
                "content_type": ct.split(";")[0],
                "post_data": resp.request.post_data,
                "body": body[:200_000] if body else None,
            }
            with out_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
    page.on("response", on_response)


def extract_tables(page) -> list:
    """Best-effort: pull every <table> on the page into JSON-friendly rows."""
    try:
        return page.eval_on_selector_all(
            "table",
            "ts => ts.map(t => Array.from(t.querySelectorAll('tr')).map(tr =>"
            " Array.from(tr.cells).map(c => c.innerText.trim()).join('\\t')))"
            ".filter(r => r.length)")
    except Exception:
        return []


def save_capture(slug: str, name: str, url: str, page, candidates=None,
                 note=None) -> Path:
    stamp = now_stamp()
    out = DATA_DIR / slug / stamp
    out.mkdir(parents=True, exist_ok=True)
    title = page.title()
    html = page.content()
    text = ""
    try:
        text = page.evaluate("() => document.body.innerText")
    except Exception:
        pass
    tables = extract_tables(page)
    (out / "profile.html").write_text(html, encoding="utf-8")
    (out / "profile.txt").write_text(text, encoding="utf-8")
    (out / "tables.json").write_text(
        json.dumps(tables, indent=1, ensure_ascii=False), encoding="utf-8")
    meta = {
        "athlete": name,
        "url": url,
        "final_url": page.url,
        "title": title,
        "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
        "candidates": candidates,
        "note": note,
        "tables_found": len(tables),
        "table_rows": sum(len(t) for t in tables),
        "challenge_page": is_challenge(title, html),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n",
                                   encoding="utf-8")
    log(f"captured {slug}/{stamp}: title={title!r}, tables={meta['tables_found']}, "
        f"rows={meta['table_rows']}, challenge={meta['challenge_page']}")
    return out


# ---------------------------------------------------------------- commands ---

def cmd_auth(args) -> None:
    if not shutil.which("Xvfb") and not _has_display():
        sys.exit("error: no display available. Run this on a machine with a GUI "
                 "(the one whose browser you already used), or provide DISPLAY.")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = launch(p, args, headless=False)
        log("Opening athlete search. In the browser window that appears:")
        log("  1. Click the Cloudflare 'verify you are human' checkbox if shown.")
        log("  2. Log in (AC member number + birthdate) if the site asks.")
        log("  3. As a sanity check, search one athlete you know and open the profile.")
        log("Then come back here and press ENTER to verify the session.")
        input("[mrtool] Press ENTER when the site looks normal in the browser... ")
        page.goto(BASE_URL + RANKINGS_PATH, wait_until="domcontentloaded")
        time.sleep(4)
        title = page.title()
        html = page.content()
        if is_challenge(title, html):
            log("STILL seeing a challenge page. Try again (refresh in the browser,")
            log("pass the checkbox, then re-run:  python3 mrtool.py auth)")
            ctx.close()
            sys.exit(1)
        cookies = ctx.cookies()
        cf = [c["name"] for c in cookies if c["name"].startswith(("cf_", "PHPSESSID", "wordpress"))]
        log(f"Session verified. title={title!r}")
        log(f"Session cookies stored in profile/: {cf or '(none matched common names)'}")
        (PROFILE_DIR / "mr_auth.json").write_text(json.dumps({
            "ok": True, "verified_at": dt.datetime.now().isoformat(),
            "title": title, "cookies": cf,
        }, indent=1) + "\n")
        input("[mrtool] Press ENTER to close the browser... ")
        ctx.close()


def _has_display() -> bool:
    import os
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _do_search(ctx, page, name: str, candidates_out: Path):
    """Run the on-site search for NAME; return list of candidate dicts."""
    page.goto(BASE_URL + SEARCH_PATH, wait_until="domcontentloaded")
    time.sleep(2)
    title = page.title()
    if is_challenge(title, page.content()[:8000]):
        sys.exit("error: Cloudflare challenge appeared during search.\n"
                 "        Re-run auth, or retry with --headed.")
    # Find the search input: try a ladder of likely selectors.
    sel_ladder = [
        'input[type="search"]', 'input[name="q"]', 'input[name="s"]',
        '#search-input', '.search-input input', 'input[type="text"]',
    ]
    box = None
    for sel in sel_ladder:
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                box = loc
                break
        except Exception:
            continue
    if box is None:
        page.screenshot(path=str(candidates_out.parent / "search_page.png"))
        sys.exit(f"error: could not find the search input (selectors tried: "
                 f"{sel_ladder}). Screenshot saved for inspection:\n"
                 f"        {candidates_out.parent / 'search_page.png'}")
    box.click()
    box.fill(name)
    box.press("Enter")
    time.sleep(5)  # let XHR settle
    return _read_candidates(page)


def _read_candidates(page) -> list:
    """Find athlete profile links on the current page; dedupe by href."""
    raw = page.eval_on_selector_all(
        'a[href*="athlete-profile"]',
        """els => els.map(a => ({
             href: a.href,
             text: (a.innerText || '').trim().slice(0, 200),
             row: ((a.closest('tr') || a.closest('li') || a.closest('div'))
                    ? (a.closest('tr') || a.closest('li') || a.closest('div'))
                         .innerText.trim().slice(0, 500) : '')
           }))"""
    )
    seen, out = set(), []
    for r in raw:
        # All athlete profiles share the path /athlete-profile/ and differ by
        # the x8= id, so the dedup key must include the x8 value (extra query
        # params like &from=search are ignored).
        key = (re.sub(r"[?#].*$", "", r["href"]).rstrip("/"),
               _extract_x8(r["href"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def cmd_search(args) -> None:
    reg = load_registry()
    existing = find_registered(args.name, reg)
    if existing:
        log(f"'{args.name}' already registered -> {existing['url']}")
        log("Refreshing instead (this is the normal fast path). "
            "Use --force to re-run the search UI.")
        if not args.force:
            return cmd_refresh(args.__dict__ | {"name": existing["name"], "all": False})

    from playwright.sync_api import sync_playwright
    slug = slugify(args.name)
    stamp = now_stamp()
    work = DATA_DIR / slug / stamp / "search"
    work.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx, page = launch(p, args, headless=not args.headed)
        watch_network(page, work / "network.jsonl")
        cands = _do_search(ctx, page, args.name, work / "candidates.json")
        (work / "candidates.json").write_text(
            json.dumps(cands, indent=1, ensure_ascii=False), encoding="utf-8")
        (work / "search.html").write_text(page.content(), encoding="utf-8")
        log(f"candidates found: {len(cands)}")

        if not cands:
            log("No athlete-profile links on the results page. Possible causes:")
            log("  - no such athlete in the database;")
            log("  - login required (re-run `auth` and log in in the browser);")
            log("  - results layout differs from expectations.")
            log(f"  Raw evidence saved under: {work}")
            sys.exit(2)

        choice = None
        if len(cands) == 1:
            choice = cands[0]
            log(f"auto-selected single match: {choice['text'][:100]}")
        else:
            log("\n  #  candidate")
            for i, c in enumerate(cands, 1):
                show = c["row"] or c["text"]
                show = re.sub(r"\s+", " ", show)[:110]
                print(f"  {i:2d}  {show}")
            log("")
            if args.pick:
                if not (1 <= args.pick <= len(cands)):
                    sys.exit(f"error: --pick {args.pick} out of range 1..{len(cands)}")
                choice = cands[args.pick - 1]
            else:
                while True:
                    ans = input(f"[mrtool] Pick a candidate number (1-{len(cands)}): ").strip()
                    if ans.isdigit() and 1 <= int(ans) <= len(cands):
                        choice = cands[int(ans) - 1]
                        break
                    log("  (enter a number from the list above)")
            log(f"selected: {choice['text'][:100]}")
            # Human-in-the-loop note: confirm before writing to the registry.
            ok = input(f"[mrtool] Store '{args.name}' -> this profile in the registry? [Y/n] ").strip().lower()
            if ok not in ("", "y", "yes"):
                log("Registry not updated (raw capture kept).")
                ctx.close()
                return

        save_capture(slug, args.name, choice["href"], page, candidates=cands,
                     note="registered via search")
        meta = {"name": args.name, "url": choice["href"],
                "x8": _extract_x8(choice["href"]),
                "selected_row": re.sub(r"\s+", " ", (choice["row"] or choice["text"]))[:200],
                "added": dt.datetime.now().isoformat(timespec="seconds")}
        reg["athletes"] = [a for a in reg["athletes"]
                           if norm_name(a.get("name", "")) != norm_name(args.name)]
        reg["athletes"].append(meta)
        save_registry(reg)
        log(f"Registered {args.name} -> {choice['href']}")
        log("Future runs:  python3 mrtool.py refresh " + repr(args.name))
        ctx.close()


def _extract_x8(url: str):
    m = re.search(r"[?&]x8=([^&]+)", url)
    return m.group(1) if m else None


def _capture_one(ctx, page, athlete: dict, slug: str):
    url = athlete["url"]
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(4)
    title = page.title()
    html = page.content()
    if is_challenge(title, html):
        log(f"  {slug}: challenge page hit during refresh (session may have expired).")
        log("           Re-run:  python3 mrtool.py auth")
        return None
    return save_capture(slug, athlete["name"], url, page)


def cmd_refresh(args) -> None:
    reg = load_registry()
    if not reg["athletes"]:
        sys.exit("error: registry is empty — run `search` first.")
    if args.name:
        a = find_registered(args.name, reg)
        if not a:
            sys.exit(f"error: '{args.name}' not in registry. `list` to see names.")
        targets = [a]
    else:
        targets = reg["athletes"]

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = launch(p, args, headless=not args.headed)
        stamp_dir = None
        for a in targets:
            slug = slugify(a["name"])
            stamp_dir = DATA_DIR / slug / now_stamp() / "refresh"
            stamp_dir.mkdir(parents=True, exist_ok=True)
            watch_network(page, stamp_dir / "network.jsonl")
            log(f"refreshing {slug} ...")
            out = _capture_one(ctx, page, a, slug)
            if out is None:
                break
            if getattr(args, "store", False) and a.get("x8"):
                summary = _store_capture(out, a["x8"], store=True)
                log(f"   stored={summary.get('stored', 0)} "
                    f"needs_review={summary.get('needs_review')}")
        ctx.close()
    log("done. Inspect with:  python3 mrtool.py store")


def cmd_list(args) -> None:
    reg = load_registry()
    if getattr(args, "json", False):
        out = {"registry": [], "db": str(_db_path())}
        try:
            import store
            conn = store.connect()
            out["store"] = store.stats(conn)
            conn.close()
        except Exception as e:
            out["store"] = {"error": str(e)}
        for a in reg["athletes"]:
            slug = slugify(a["name"])
            caps = sorted(DATA_DIR.glob(f"{slug}/*/"))
            out["registry"].append({
                "name": a["name"], "x8": a.get("x8"), "url": a.get("url"),
                "captures": len(caps), "last": caps[-1].name if caps else None,
            })
        print(json.dumps(out, indent=1, default=str))
        return
    if not reg["athletes"]:
        print("(registry empty)")
        return
    print(f"{'athlete':<28} {'x8 id':<26} last capture   rows")
    for a in reg["athletes"]:
        slug = slugify(a["name"])
        caps = sorted(DATA_DIR.glob(f"{slug}/*/"))
        last, rows = "-", 0
        if caps:
            cap = caps[-1]
            last = cap.name
            try:
                m = json.loads((cap / "meta.json").read_text())
                rows = m.get("table_rows", 0)
            except Exception:
                pass
        print(f"{a['name']:<28} {str(a.get('x8') or '-')[:26]:<26} {last:<14} {rows}")
    print(f"\nregistry file: {REGISTRY_PATH}")
    print(f"captures dir:  {DATA_DIR}")
    print(f"store db:      {_db_path()}")


def _db_path():
    import store
    return store.store_path()


def cmd_profile(args) -> None:
    reg = load_registry()
    name = args.name or "manual"
    slug = slugify(name)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = launch(p, args, headless=not args.headed)
        stamp = now_stamp()
        work = DATA_DIR / slug / stamp / "profile"
        work.mkdir(parents=True, exist_ok=True)
        watch_network(page, work / "network.jsonl")
        page.goto(args.url, wait_until="domcontentloaded")
        time.sleep(4)
        save_capture(slug, name, args.url, page, note="direct url")
        if args.register:
            reg["athletes"] = [a for a in reg["athletes"]
                               if norm_name(a.get("name", "")) != norm_name(name)]
            reg["athletes"].append({
                "name": name, "url": args.url, "x8": _extract_x8(args.url),
                "selected_row": "", "added": dt.datetime.now().isoformat(timespec="seconds"),
                "note": "added via profile command",
            })
            save_registry(reg)
            log(f"registered {name}")
        ctx.close()


# ----------------------------------------------------- store + broad cmds --

def _store_capture(capture_dir, athlete_id, store=True, force=False) -> dict:
    """Parse a capture dir and (optionally) upsert its rows into the store.

    Returns a summary dict. When store is False it only writes parsed.json
    (evidence) — the safe default until the parser is validated on a real
    page.
    """
    import store as S
    import parse_profile as P
    capture_dir = Path(capture_dir)
    parsed = P.parse_capture(capture_dir)
    P.write_parsed(capture_dir, parsed)
    summary = {"athlete_id": athlete_id,
               "tables": parsed["tables"],
               "needs_review": parsed["needs_review"],
               "stored": 0, "athlete_row": False}
    if store:
        conn = S.connect()
        src = str(capture_dir)
        S.upsert_athlete(conn, athlete_id, name=parsed["athlete"].get("name"),
                         source_url=parsed["athlete"].get("source_url"))
        summary["athlete_row"] = True
        for r in parsed["performances"]:
            S.upsert_performance(
                conn, athlete_id, date=r.get("date"),
                competition=r.get("competition"), location=r.get("location"),
                age_group=r.get("age_group"), event=r.get("event"),
                round_=r.get("round"), position=r.get("position"),
                mark=r.get("mark"), wind=r.get("wind"),
                age_grade=r.get("age_grade"), source=src)
            summary["stored"] += 1
        conn.close()
    return summary


def cmd_store(args) -> None:
    """Query the local research store. `--query SQL` runs a statement and
    prints rows as JSON; with no args it prints store stats."""
    import store as S
    conn = S.connect()
    try:
        if getattr(args, "query", None):
            rows = [dict(r) for r in conn.execute(args.query).fetchall()]
            print(json.dumps(rows, indent=1, default=str))
        elif getattr(args, "history", None):
            rows = S.athlete_history(conn, args.history,
                                     event=getattr(args, "event", None),
                                     since=getattr(args, "since", None))
            print(json.dumps(rows, indent=1, default=str))
        elif getattr(args, "top", None):
            rows = S.top_ranked(conn, *args.top.split("|"),
                                n=getattr(args, "n", 10))
            print(json.dumps(rows, indent=1, default=str))
        else:
            print(json.dumps(S.stats(conn), indent=1, default=str))
    finally:
        conn.close()


def _fetch_one(page, url, slug, name, x8, store) -> tuple:
    """Navigate to one profile, save the capture, optionally store rows.
    Returns (capture_dir or None, summary or None)."""
    page.goto(url, wait_until="domcontentloaded")
    time.sleep(4)
    title, html = page.title(), page.content()
    if is_challenge(title, html):
        log(f"  {slug}: challenge page (session expired) — re-run `auth`")
        return None, None
    cap = save_capture(slug, name, url, page, note="fetch")
    if x8:
        summary = _store_capture(cap, x8, store=store)
    else:
        summary = {"athlete_id": None, "note": "no x8 id in url"}
    return cap, summary


def cmd_fetch(args) -> None:
    """Bulk-fetch a list of athlete x8 ids (or profile urls) into the store.
    Rate-limited and resumable via the crawl manifest in store.db.

    id sources: --ids X1 X2 ...  or  --from-file list.txt  (one id/url per line)
    """
    import store as S
    raw = list(args.ids or [])
    if args.from_file:
        for line in Path(args.from_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw.append(line)

    # Normalize every entry to (x8, url); x8 is the canonical key so a bare
    # id and a full url for the same athlete dedupe to one fetch.
    entries, seen = [], {}
    for x in raw:
        if x.startswith("http"):
            x8, url = _extract_x8(x), x
        else:
            x8, url = x, f"{BASE_URL}/athlete-profile/?x8={x}"
        if x8 is None:
            log(f"  skipping (no x8 id found): {x}")
            continue
        if x8 not in seen:
            seen[x8] = url
            entries.append((x8, url))
    if not entries:
        sys.exit("error: no usable x8 ids given (use --ids or --from-file)")

    crawl_id = args.crawl or f"crawl-{now_stamp()}-{uuid.uuid4().hex[:6]}"
    conn = S.connect()
    S.start_crawl(conn, crawl_id, args.kind, args.cohort or ", ".join(x for x, _ in entries[:20]))
    S.add_crawl_items(conn, crawl_id, [x for x, _ in entries])
    conn.close()
    log(f"crawl {crawl_id}: {len(entries)} ids, store={args.store}, delay={args.delay}s")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = launch(p, args, headless=not args.headed)
        net_dir = DATA_DIR / "_fetch" / crawl_id
        net_dir.mkdir(parents=True, exist_ok=True)
        watch_network(page, net_dir / "network.jsonl")
        for i, (xid, url) in enumerate(entries, 1):
            slug = slugify(xid)
            log(f"[{i}/{len(entries)}] {xid}")
            cap, summary = _fetch_one(page, url, slug, xid, xid, args.store)
            c = S.connect()
            S.mark_crawl_item(c, crawl_id, xid, "fetched" if cap else "failed")
            c.close()
            if cap and summary:
                log(f"   tables={summary['tables']['analyzed']} "
                    f"result_rows={summary['tables']['result_like']} "
                    f"stored={summary.get('stored', 0)} "
                    f"needs_review={summary.get('needs_review')}")
            if args.delay > 0 and i < len(entries):
                time.sleep(args.delay)
        c = S.connect()
        S.finish_crawl(c, crawl_id)
        c.close()
        ctx.close()
    log("done. Inspect with:  mrtool.py store   (or `store --query 'SELECT …'`)")


def cmd_rankings(args) -> None:
    """Capture a rankings page for a cohort and (best-effort) parse it into
    store.rankings + emit the top-N athlete ids as JSON.

    FIRST PASS: the page layout is not yet validated, so by default this
    saves the capture + writes candidates.json (evidence) and emits the ids
    it can find; pass --store to also upsert rankings rows.
    """
    import store as S
    from urllib.parse import urlencode
    params = {}
    if args.year:
        params["x1"] = args.year
    if args.age:
        params["x10"] = args.age
    if args.event:
        params["x7"] = args.event
    params["x2"] = args.surface
    url = f"{BASE_URL}/rankings/?{urlencode(params)}"
    slug = slugify(f"rankings-{args.event or 'x'}-{args.age or 'x'}")
    stamp = now_stamp()
    work = DATA_DIR / slug / stamp / "rankings"
    work.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = launch(p, args, headless=not args.headed)
        watch_network(page, work / "network.jsonl")
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(5)
        title, html = page.title(), page.content()
        if is_challenge(title, html):
            sys.exit("error: challenge page — run `auth` first.")
        (work / "rankings.html").write_text(html, encoding="utf-8")
        (work / "meta.json").write_text(json.dumps(
            {"url": url, "title": title,
             "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
             "params": params}, indent=1) + "\n", encoding="utf-8")
        # first-pass: pull athlete-profile links in order (rank order on page)
        raw = page.eval_on_selector_all(
            'a[href*="athlete-profile"]',
            "els => els.map(a => ({href:a.href,"
            " row:((a.closest('tr')||a.closest('li')||a.closest('div'))"
            " ?(a.closest('tr')||a.closest('li')||a.closest('div'))"
            ".innerText.trim().slice(0,300):'')}))")
        seen, cands = set(), []
        for r in raw:
            x8 = _extract_x8(r["href"])
            key = (re.sub(r"[?#].*$", "", r["href"]).rstrip("/"), x8)
            if key in seen:
                continue
            seen.add(key)
            cands.append({"x8": x8, "row": re.sub(r"\s+", " ", r["row"])[:120]})
        (work / "candidates.json").write_text(
            json.dumps(cands, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8")
        log(f"rankings page captured: {len(cands)} athlete links")

        top = cands[:args.n]
        if args.store and top:
            conn = S.connect()
            cap = dt.datetime.now().isoformat(timespec="seconds")
            for i, c in enumerate(top, 1):
                S.upsert_ranking(conn, captured_at=cap, season=args.year,
                                 event=args.event, age_group=args.age,
                                 surface=args.surface, rank=i,
                                 athlete_id=c["x8"], name=None)
            conn.close()
            log(f"stored top {len(top)} ranking rows")
        if getattr(args, "json", False):
            print(json.dumps({"url": url, "total_found": len(cands),
                              "top": top}, indent=1))
        else:
            for i, c in enumerate(top, 1):
                print(f"  {i:3d}  {c['x8'] or '-':<24} {c['row'][:70]}")
            if len(cands) > args.n:
                print(f"  … {len(cands) - args.n} more (see candidates.json)")
        ctx.close()


# ------------------------------------------------------------------- main ---

# ------------------------------------------- two-machine coordination -------
#
# The browser (and therefore the one-time `auth`) can only run where there is
# a display and a residential IP. The research store + agent can live anywhere.
# These commands move the *state* between the two machines:
#
#   export-session / import-session  move the browser session (cookies + the
#                                    Cloudflare clearance) so headless fetches
#                                    can run on the second machine
#   check                            does this session actually pass CF *here*?
#   sync-export   / sync-import      move the research data (store.db +
#                                    registry) the other way — for the split
#                                    setup where fetching stays on machine A


def _profile_locked() -> bool:
    """True if the browser profile has live lock files (a browser is using it)."""
    if not PROFILE_DIR.exists():
        return False
    for p in PROFILE_DIR.rglob("*"):
        if p.name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            return True
    return False


def _tar_filter(exclude_names) -> "callable":
    def f(ti):
        parts = Path(ti.name).parts
        if any(part in exclude_names for part in parts):
            return None
        return ti
    return f


def _extract_tar(tf, dest: Path) -> None:
    try:
        tf.extractall(dest, filter="data")   # py>=3.12 safe extraction
    except TypeError:
        tf.extractall(dest)                 # older python: no filter kwarg


SESSION_SKIP = {"SingletonLock", "SingletonSocket", "SingletonCookie",
                "Crashpad", "GPUPersistentCache"}


def cmd_export_session(args) -> None:
    """Tar up the browser profile/ (cookies + CF clearance) for transfer."""
    if not PROFILE_DIR.exists():
        sys.exit("error: no profile/ yet — run `auth` first (on the machine "
                 "that has the browser)")
    if _profile_locked():
        sys.exit("error: the browser profile is in use (lock files present).\n"
                 "        Close the browser / stop any running mrtool, then retry.")
    out = (Path(args.out) if args.out
           else Path.cwd() / f"mrtool-session-{now_stamp()}.tar.gz")
    with tarfile.open(out, "w:gz") as tf:
        tf.add(PROFILE_DIR, arcname="profile", filter=_tar_filter(SESSION_SKIP))
    log(f"session exported -> {out}  ({out.stat().st_size/1024:.0f} KB)")
    log("Move that file to the other machine, then there run:")
    log(f"  python3 mrtool.py import-session {out.name}")
    log("  python3 mrtool.py check")


def cmd_import_session(args) -> None:
    """Extract a session tarball into profile/, replacing any existing one."""
    src = Path(args.file)
    if not src.exists():
        sys.exit(f"error: no such file: {src}")
    if _profile_locked():
        sys.exit("error: browser profile in use — close the browser first.")
    if PROFILE_DIR.exists():
        bak = PROFILE_DIR.with_name(f"profile.bak-{now_stamp()}")
        shutil.move(str(PROFILE_DIR), str(bak))
        log(f"existing profile/ moved aside -> {bak.name}")
    with tarfile.open(src, "r:gz") as tf:
        _extract_tar(tf, ROOT)
    log(f"session imported from {src.name}")
    log("Now verify it works from THIS machine's network:")
    log("  python3 mrtool.py check")


def cmd_check(args) -> None:
    """Headless probe: does the current profile pass Cloudflare from here?"""
    if not PROFILE_DIR.exists() or not any(PROFILE_DIR.iterdir()):
        sys.exit("error: no profile/ — import a session first "
                 "(import-session FILE) or run `auth`")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = launch(p, args, headless=True)
        page.goto(BASE_URL + RANKINGS_PATH, wait_until="domcontentloaded")
        time.sleep(5)
        title, html = page.title(), page.content()
        cookies = ctx.cookies()
        cf = [c["name"] for c in cookies
              if c["name"].startswith(("cf_", "PHPSESSID", "wordpress"))]
        ctx.close()
    if is_challenge(title, html):
        log("CHECK FAILED — still seeing a Cloudflare challenge from this machine.")
        log(f"  title={title!r}  cookies={cf or '(none)'}")
        log("The clearance likely did not carry over (Cloudflare often binds its")
        log("token to the IP / fingerprint that earned it). Use the split setup:")
        log("  - keep fetching on your machine, then `sync-export` + `sync-import` here, OR")
        log("  - re-run `auth` here if this machine has a browser you can drive.")
        sys.exit(2)
    log(f"CHECK PASSED — the session works from this machine. title={title!r}")
    log(f"cookies present: {cf or '(none matched common names)'}")
    log("You can now run headless:  python3 mrtool.py fetch <ids> --store")


def cmd_sync_export(args) -> None:
    """Tar up the research data (store.db + registry [+ data/]) for transfer."""
    db = _db_path()
    out = (Path(args.out) if args.out
           else Path.cwd() / f"mrtool-sync-{now_stamp()}.tar.gz")
    with tarfile.open(out, "w:gz") as tf:
        if db.exists():
            tf.add(db, arcname="store.db")
            for sib in (db.parent / f"{db.name}-wal", db.parent / f"{db.name}-shm",
                        db.parent / f"{db.name}-journal"):
                if sib.exists():
                    tf.add(sib, arcname=sib.name)
        if REGISTRY_PATH.exists():
            tf.add(REGISTRY_PATH, arcname="athletes.json")
        if args.with_data and DATA_DIR.exists():
            tf.add(DATA_DIR, arcname="data")
    log(f"sync exported -> {out}  ({out.stat().st_size/1024:.0f} KB)")
    log("Move it to the research machine, then:")
    log(f"  python3 mrtool.py sync-import {out.name}")


def cmd_sync_import(args) -> None:
    """Extract a sync tarball (store.db + registry) into this checkout."""
    src = Path(args.file)
    if not src.exists():
        sys.exit(f"error: no such file: {src}")
    db = _db_path()
    if db.exists():
        bak = db.with_name(f"{db.name}.bak-{now_stamp()}")
        shutil.move(str(db), str(bak))
        log(f"existing store.db moved aside -> {bak.name}")
    with tarfile.open(src, "r:gz") as tf:
        _extract_tar(tf, ROOT)
    log(f"sync imported from {src.name}")
    log("Verify:  python3 mrtool.py store")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # --browser/--headed are accepted both before and after the subcommand
    # (mrtool.py --browser chrome auth  ==  mrtool.py auth --browser chrome).
    # The subparser copies live with default=SUPPRESS so that, when the flag is
    # only given before the subcommand, the top-level value is not clobbered
    # by a subparser default (the classic argparse subparser pitfall).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--browser", choices=["chromium", "chrome", "msedge"],
                        default=argparse.SUPPRESS,
                        help="browser engine (default: playwright's bundled chromium)")
    common.add_argument("--headed", action="store_true", default=argparse.SUPPRESS,
                        help="run with a visible window (fallback if headless hits Cloudflare)")
    ap.add_argument("--browser", choices=["chromium", "chrome", "msedge"],
                    default="chromium",
                    help=argparse.SUPPRESS)
    ap.add_argument("--headed", action="store_true",
                    help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth", parents=[common],
                   help="one-time: open browser, pass Cloudflare, verify session")

    sp = sub.add_parser("search", parents=[common],
                        help="search an athlete by name; register the chosen one")
    sp.add_argument("name")
    sp.add_argument("--pick", type=int, help="candidate number (1-based) for non-interactive runs")
    sp.add_argument("--force", action="store_true", help="re-run search even if already registered")

    rp = sub.add_parser("refresh", parents=[common],
                        help="re-capture profile(s) for registered athletes")
    rp.add_argument("name", nargs="?", default=None,
                    help="athlete name (default: all)")
    rp.add_argument("--all", action="store_true", default=None)
    rp.add_argument("--store", action="store_true",
                    help="upsert parsed rows into store.db (default: evidence only)")

    lp = sub.add_parser("list", parents=[common],
                        help="show registry and latest capture status")
    lp.add_argument("--json", action="store_true", help="emit JSON (for the agent)")

    stp = sub.add_parser("store", parents=[common],
                         help="query the local research store (SQLite)")
    stp.add_argument("--query", help="run a SQL statement; rows print as JSON")
    stp.add_argument("--history", metavar="X8ID",
                     help="print one athlete's performance history as JSON")
    stp.add_argument("--event", help="filter --history to one event")
    stp.add_argument("--since", help="filter --history to date >= (YYYY-MM-DD)")
    stp.add_argument("--top", metavar="EVENT|AGE",
                     help="top-N from latest ranking, e.g. 'PV|M40'")
    stp.add_argument("--n", type=int, default=10, help="N for --top (default 10)")

    fp = sub.add_parser("fetch", parents=[common],
                        help="bulk-fetch x8 ids / profile urls into the store")
    fp.add_argument("ids", nargs="*", help="x8 ids and/or full profile urls")
    fp.add_argument("--from-file", help="file with one id/url per line")
    fp.add_argument("--store", action="store_true",
                    help="upsert parsed rows into store.db (default: evidence only)")
    fp.add_argument("--delay", type=float, default=2.0,
                    help="seconds between fetches (default 2)")
    fp.add_argument("--crawl", help="crawl id (default: auto)")
    fp.add_argument("--kind", default="manual",
                    help="crawl kind: manual | top-pct | meet")
    fp.add_argument("--cohort", help="human description of the cohort")
    fp.add_argument("--force", action="store_true",
                    help="re-fetch even if already captured recently")

    rk = sub.add_parser("rankings", parents=[common],
                        help="capture + parse a rankings page for a cohort")
    rk.add_argument("--event", help="event code, e.g. PV, LJ, 10000M")
    rk.add_argument("--age", help="age group, e.g. M40, W45")
    rk.add_argument("--year", help="season/year, e.g. 2026")
    rk.add_argument("--surface", default="Outdoor", choices=["Outdoor", "Indoor"])
    rk.add_argument("--n", type=int, default=10, help="how many top rows to show")
    rk.add_argument("--store", action="store_true",
                    help="upsert top-N into store.rankings")
    rk.add_argument("--json", action="store_true", help="emit JSON (for the agent)")

    pp = sub.add_parser("profile", parents=[common],
                        help="capture a profile page by direct URL")
    pp.add_argument("url")
    pp.add_argument("--name", help="athlete name to use for storage/registry")
    pp.add_argument("--register", action="store_true", help="also add to registry")

    es = sub.add_parser("export-session", parents=[common],
                        help="tar up the browser session (cookies + CF clearance)")
    es.add_argument("out", nargs="?", help="output file (default: mrtool-session-<stamp>.tar.gz)")

    im = sub.add_parser("import-session", parents=[common],
                        help="install a session tarball into profile/")
    im.add_argument("file", help="the mrtool-session-*.tar.gz to install")

    sub.add_parser("check", parents=[common],
                   help="does the current profile pass Cloudflare from here? (headless)")

    sx = sub.add_parser("sync-export", parents=[common],
                        help="tar up the research data (store.db + registry)")
    sx.add_argument("out", nargs="?", help="output file (default: mrtool-sync-<stamp>.tar.gz)")
    sx.add_argument("--with-data", action="store_true",
                    help="also include raw capture evidence (data/) — bigger")

    si = sub.add_parser("sync-import", parents=[common],
                        help="install a sync tarball into this checkout")
    si.add_argument("file", help="the mrtool-sync-*.tar.gz to install")

    args = ap.parse_args()
    {"auth": cmd_auth, "search": cmd_search, "refresh": cmd_refresh,
     "list": cmd_list, "profile": cmd_profile, "store": cmd_store,
     "fetch": cmd_fetch, "rankings": cmd_rankings,
     "export-session": cmd_export_session, "import-session": cmd_import_session,
     "check": cmd_check, "sync-export": cmd_sync_export,
     "sync-import": cmd_sync_import}[args.cmd](args)


if __name__ == "__main__":
    main()
