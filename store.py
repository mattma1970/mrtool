"""
store — the local research database for mrtool.

One SQLite file is the memory of the whole system. mrtool's acquisition
step writes normalized rows into it; the intelligence layer (the agent)
answers research questions by running SQL against it. Keeping this in a
single file means: no server, trivially backed up, and SQL is a language
the agent is already excellent at — so broad queries are cheap instead of
re-scrapes.

Design rules baked in here:
  * x8 id is the canonical athlete key. Never join on name.
  * performances are deduped on a natural key so re-fetches don't create
    duplicates and updated marks overwrite cleanly.
  * rankings / meets / crawl-manifest tables support the "broad" queries
    (top-N%, upcoming events, resumable cohort pulls).

Run:  ./.venv/bin/python -c "import store; c=store.connect(); print(store.stats(c))"
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def store_path() -> Path:
    p = os.environ.get("MRTOOL_DB")
    if p:
        return Path(p)
    return ROOT / "store.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS athletes (
  id            TEXT PRIMARY KEY,        -- x8 id, the canonical key
  name          TEXT,
  first         TEXT,
  last          TEXT,
  sex           TEXT,
  birth_year    TEXT,
  nationality   TEXT,
  source_url    TEXT,
  added_at      TEXT,
  last_fetched  TEXT
);

CREATE TABLE IF NOT EXISTS performances (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  athlete_id    TEXT NOT NULL REFERENCES athletes(id),
  date          TEXT,                    -- ISO YYYY-MM-DD (best effort)
  competition   TEXT,
  location      TEXT,
  age_group     TEXT,                    -- M35, W40, ...
  event         TEXT,                    -- PV, LJ, 10000M, ...
  round         TEXT,                    -- Final, Semis, ...
  position      INTEGER,
  mark          TEXT,                    -- raw "4.85" / "1:23:45"
  wind          TEXT,
  age_grade     REAL,
  source        TEXT,                    -- capture id / url
  dedupe_key    TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_perf_athlete ON performances(athlete_id, date);
CREATE INDEX IF NOT EXISTS idx_perf_event   ON performances(event, age_group, date);

CREATE TABLE IF NOT EXISTS rankings (
  captured_at   TEXT,
  season        TEXT,
  event         TEXT,
  age_group     TEXT,
  surface       TEXT,                    -- Outdoor / Indoor
  rank          INTEGER,
  athlete_id    TEXT,
  mark          TEXT,
  name          TEXT,
  PRIMARY KEY (captured_at, event, age_group, surface, rank)
);

CREATE TABLE IF NOT EXISTS meets (
  id            TEXT PRIMARY KEY,
  name          TEXT,
  date          TEXT,
  location      TEXT,
  url           TEXT
);

CREATE TABLE IF NOT EXISTS meet_entries (
  meet_id       TEXT,
  athlete_id    TEXT,
  event         TEXT,
  PRIMARY KEY (meet_id, athlete_id, event)
);

-- crawl manifest: lets a cohort pull resume where it left off
CREATE TABLE IF NOT EXISTS crawl (
  id            TEXT PRIMARY KEY,
  kind          TEXT,                    -- top-pct | meet | manual
  cohort        TEXT,                    -- human description of the cohort
  requested_at  TEXT,
  status        TEXT,                    -- pending | running | done | failed
  notes         TEXT
);

CREATE TABLE IF NOT EXISTS crawl_items (
  crawl_id      TEXT,
  athlete_id    TEXT,
  status        TEXT,                    -- pending | fetched | failed | skipped
  fetched_at    TEXT,
  PRIMARY KEY (crawl_id, athlete_id)
);
"""


def connect(path=None) -> sqlite3.Connection:
    p = Path(path) if path else store_path()
    # autocommit: every write is durable immediately. That is what a
    # resumable crawl wants — if a fetch is interrupted, what was already
    # stored stays stored, and no second connection ever sees stale data.
    conn = sqlite3.connect(str(p), autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dedupe(athlete_id, date, event, age_group, round_, position, mark, source):
    raw = "|".join(str(x) if x is not None else "" for x in
                   (athlete_id, date, event, age_group, round_, position, mark, source))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- upserts ----

def upsert_athlete(conn, athlete_id, **fields):
    """Create or touch an athlete row. Only provided fields are written."""
    athlete_id = str(athlete_id)
    existing = conn.execute("SELECT * FROM athletes WHERE id=?", (athlete_id,)).fetchone()
    cols = ("name", "first", "last", "sex", "birth_year",
            "nationality", "source_url")
    if existing is None:
        row = {c: fields.get(c) for c in cols}
        conn.execute(
            "INSERT INTO athletes(id, name, first, last, sex, birth_year,"
            " nationality, source_url, added_at, last_fetched) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (athlete_id, row["name"], row["first"], row["last"], row["sex"],
             row["birth_year"], row["nationality"], row["source_url"],
             _now(), _now()))
    else:
        sets, vals = [], []
        for c in cols:
            if fields.get(c) is not None:
                sets.append(f"{c}=?")
                vals.append(fields[c])
        sets.append("last_fetched=?")
        vals.append(_now())
        if sets:
            vals.append(athlete_id)
            conn.execute(f"UPDATE athletes SET {', '.join(sets)} WHERE id=?", vals)
    return athlete_id


def upsert_performance(conn, athlete_id, *, date=None, competition=None,
                       location=None, age_group=None, event=None, round_=None,
                       position=None, mark=None, wind=None, age_grade=None,
                       source=None):
    """Insert-or-update one performance row, keyed on its natural identity."""
    key = _dedupe(athlete_id, date, event, age_group, round_, position, mark, source)
    existing = conn.execute(
        "SELECT id FROM performances WHERE dedupe_key=?", (key,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE performances SET date=?, competition=?, location=?,"
            " age_group=?, event=?, round=?, position=?, mark=?, wind=?,"
            " age_grade=?, source=? WHERE id=?",
            (date, competition, location, age_group, event, round_, position,
             mark, wind, age_grade, source, existing["id"]))
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO performances(athlete_id, date, competition, location,"
        " age_group, event, round, position, mark, wind, age_grade, source,"
        " dedupe_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (athlete_id, date, competition, location, age_group, event, round_,
         position, mark, wind, age_grade, source, key))
    return cur.lastrowid


def upsert_ranking(conn, *, captured_at, season, event, age_group, surface,
                   rank, athlete_id, mark=None, name=None):
    conn.execute(
        "INSERT OR REPLACE INTO rankings(captured_at, season, event, age_group,"
        " surface, rank, athlete_id, mark, name) VALUES(?,?,?,?,?,?,?,?,?)",
        (captured_at, season, event, age_group, surface, rank, athlete_id,
         mark, name))


def upsert_meet(conn, meet_id, *, name=None, date=None, location=None, url=None):
    conn.execute(
        "INSERT OR REPLACE INTO meets(id, name, date, location, url)"
        " VALUES(?,?,?,?,?)", (meet_id, name, date, location, url))


# ------------------------------------------------- crawl-manifest helpers --

def start_crawl(conn, crawl_id, kind, cohort, notes=None):
    conn.execute("INSERT OR REPLACE INTO crawl(id, kind, cohort, requested_at,"
                 " status, notes) VALUES(?,?,?,?,?,?)",
                 (crawl_id, kind, cohort, _now(), "running", notes))
    return crawl_id


def crawl_pending(conn, crawl_id):
    return [r["athlete_id"] for r in conn.execute(
        "SELECT athlete_id FROM crawl_items WHERE crawl_id=? AND status='pending'",
        (crawl_id,))]


def add_crawl_items(conn, crawl_id, athlete_ids):
    for a in athlete_ids:
        conn.execute(
            "INSERT OR IGNORE INTO crawl_items(crawl_id, athlete_id, status)"
            " VALUES(?,?,?)", (crawl_id, str(a), "pending"))


def mark_crawl_item(conn, crawl_id, athlete_id, status):
    conn.execute("UPDATE crawl_items SET status=?, fetched_at=? WHERE crawl_id=?"
                 " AND athlete_id=?", (status, _now(), crawl_id, str(athlete_id)))


def finish_crawl(conn, crawl_id, status="done"):
    conn.execute("UPDATE crawl SET status=? WHERE id=?", (status, crawl_id))


# ----------------------------------------------------------- query helpers --

def athlete_history(conn, athlete_id, event=None, since=None):
    """All performances for one athlete, newest first, optionally filtered."""
    q = ("SELECT p.*, a.name FROM performances p JOIN athletes a ON a.id=p.athlete_id"
         " WHERE p.athlete_id=?")
    args = [str(athlete_id)]
    if event:
        q += " AND p.event=?"
        args.append(event)
    if since:
        q += " AND p.date>=?"
        args.append(since)
    q += " ORDER BY p.date DESC, p.position ASC"
    return [dict(r) for r in conn.execute(q, args)]


def top_ranked(conn, event, age_group, surface="Outdoor", season=None, n=10):
    """Top-N from the most recent captured ranking for a cohort."""
    if season:
        rows = conn.execute(
            "SELECT * FROM rankings WHERE event=? AND age_group=? AND surface=? AND"
            " season=? ORDER BY rank LIMIT ?",
            (event, age_group, surface, season, n)).fetchall()
    else:
        cap = conn.execute(
            "SELECT MAX(captured_at) c FROM rankings WHERE event=? AND age_group=?"
            " AND surface=?", (event, age_group, surface)).fetchone()["c"]
        if not cap:
            return []
        rows = conn.execute(
            "SELECT * FROM rankings WHERE captured_at=? AND event=? AND age_group=?"
            " AND surface=? ORDER BY rank LIMIT ?",
            (cap, event, age_group, surface, n)).fetchall()
    return [dict(r) for r in rows]


def athletes_needing_fetch(conn, athlete_ids, max_age_hours=72):
    """Which of these x8 ids have no capture within max_age_hours."""
    out = []
    cutoff = _hours_ago(max_age_hours)
    for a in athlete_ids:
        row = conn.execute("SELECT last_fetched FROM athletes WHERE id=?",
                           (str(a),)).fetchone()
        if not row or not row["last_fetched"] or row["last_fetched"] < cutoff:
            out.append(str(a))
    return out


def _hours_ago(h):
    from datetime import timedelta
    return (datetime.now() - timedelta(hours=h)).isoformat(timespec="seconds")


def stats(conn) -> dict:
    def n(t):
        return conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
    return {
        "db": str(store_path()),
        "athletes": n("athletes"),
        "performances": n("performances"),
        "rankings": n("rankings"),
        "meets": n("meets"),
        "crawls": n("crawl"),
    }


if __name__ == "__main__":
    import sys
    c = connect()
    if len(sys.argv) > 1 and sys.argv[1] == "--query":
        rows = c.execute(sys.argv[2]).fetchall()
        print(json.dumps([dict(r) for r in rows], indent=1, default=str))
    else:
        print(json.dumps(stats(c), indent=1))
