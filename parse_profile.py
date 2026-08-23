"""
parse_profile — turn a saved capture into normalized athlete/performance rows.

STATUS: FIRST PASS. This is a generic, conservative column detector written
against the *shape* of mastersrankings.com tables (Hy-Tek / Crystal-Reports
style: header row + data rows, one tab per cell). It has NOT been validated
against a real captured profile page yet. It is intentionally safe:

  * it always writes `parsed.json` next to the capture (evidence, reviewable);
  * it scores each mapped row and flags `needs_review` when confidence is low;
  * mrtool only upserts into the store when `--store` is passed (or once the
    parser is validated and the default is flipped).

Once one real capture is available, tune COLUMN_KEYWORDS / the row filter to
the actual layout and set DEFAULT_STORE = True. Everything else (store, fetch,
manifest) already works and does not depend on this file's exact output.
"""
import json
import re
from pathlib import Path

# Field -> keywords that mark a column header (matched case-insensitively).
# Ordered so a header maps to the FIRST matching field (earlier wins).
COLUMN_KEYWORDS = {
    "position":    ("pos", "rank", "place", "position"),
    "name":        ("name", "athlete", "competitor", "athletes"),
    "event":       ("event", "discipline", "event name"),
    "age_group":   ("age group", "agegroup", "class", "category", "age gr"),
    "mark":        ("mark", "result", "performance", "time", "distance",
                    "height", "score", "pts", "total", "finals", "final",
                    "best"),
    "wind":        ("wind", "w"),
    "age_grade":   ("age grade", "age-grade", "agegrade", "ag %", "grade %",
                    "age gr", "grading", "age grading"),
    "date":        ("date", "day", "meeting date"),
    "competition": ("competition", "meet", "championship", "tournament",
                    "meet name"),
    "location":    ("location", "venue", "city", "place held"),
}

# A data row is "result-like" when it has a mark and at least a name or position.
RESULT_LIKE = ("mark",)
RESULT_LIKE_ALT = ("name", "position")


def _split_row(row: str):
    return [c.strip() for c in row.split("\t")]


def _match_field(header_cell: str):
    h = header_cell.lower().strip()
    if not h:
        return None
    for field, keywords in COLUMN_KEYWORDS.items():
        for kw in keywords:
            if len(kw) <= 2:
                # single/two-letter codes (e.g. "W" for wind): exact match only,
                # otherwise they'd substring-match "Walk", "World", ...
                if h == kw:
                    return field
            elif kw in h:
                return field
    return None


def _to_int(s):
    if s is None:
        return None
    m = re.search(r"-?\d+", str(s))
    return int(m.group(0)) if m else None


def _to_float(s):
    if s is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(s).replace("%", ""))
    return float(m.group(0)) if m else None


def map_table(table_rows):
    """Map one table (list of tab-joined row strings) to (fields, rows).

    Returns (column_map, data_rows). column_map is {col_index: field}.
    data_rows is a list of dicts with the mapped fields + raw cells.
    """
    if not table_rows or len(table_rows) < 2:
        return {}, []
    header = _split_row(table_rows[0])
    col_map = {}
    used = set()
    for idx, cell in enumerate(header):
        f = _match_field(cell)
        # first column wins a field; later columns with the same field are
        # left unmapped (keeps us conservative when headers repeat words)
        if f and f not in used:
            col_map[idx] = f
            used.add(f)
    data = []
    for raw in table_rows[1:]:
        cells = _split_row(raw)
        if not any(cells):
            continue
        rec = {"_cells": cells}
        for idx, f in col_map.items():
            rec[f] = cells[idx] if idx < len(cells) else None
        data.append(rec)
    return col_map, data


def _confidence(rec):
    """Fraction of the expected result fields present in a mapped row."""
    expected = ["mark", "name", "event", "date"]
    have = sum(1 for f in expected if rec.get(f))
    return have / len(expected)


def is_result_like(rec):
    return bool(rec.get("mark")) and bool(rec.get("name") or rec.get("position"))


def parse_capture(capture_dir) -> dict:
    """Parse one capture dir (tables.json + meta.json) into normalized rows.

    Returns a dict: {athlete, performances, tables, needs_review, note}.
    `performances` is a list of dicts with: event, age_group, round, position,
    mark, wind, age_grade, date, competition, location, confidence.
    """
    capture_dir = Path(capture_dir)
    meta = {}
    meta_path = capture_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            meta = {}
    tables = []
    tables_path = capture_dir / "tables.json"
    if tables_path.exists():
        try:
            tables = json.loads(tables_path.read_text())
        except Exception:
            tables = []

    candidate_rows = []
    tables_analyzed = 0
    rows_total = 0
    for t in tables:
        col_map, data = map_table(t)
        if not col_map:
            continue
        tables_analyzed += 1
        rows_total += len(data)
        for rec in data:
            if not is_result_like(rec):
                continue
            conf = _confidence(rec)
            candidate_rows.append({
                "position": _to_int(rec.get("position")),
                "name": rec.get("name"),
                "event": _clean_event(rec.get("event")),
                "age_group": rec.get("age_group"),
                "round": None,          # not reliably in the header; TODO
                "mark": rec.get("mark"),
                "wind": rec.get("wind"),
                "age_grade": _to_float(rec.get("age_grade")),
                "date": rec.get("date"),
                "competition": rec.get("competition"),
                "location": rec.get("location"),
                "confidence": round(conf, 2),
            })

    # best-guess athlete name: the page title is the most reliable signal
    # (it is "<Name> - World Masters Rankings"), cross-checked against the
    # most common row name. Fall back to meta["athlete"] (only reliable when
    # the caller knew the name, e.g. search/refresh rather than fetch-by-id).
    title = (meta.get("title") or "").strip()
    for suffix in (" - World Masters Rankings", " - World Masters Athletics",
                   "- World Masters Rankings"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
            break
    generic = {"athlete", "athlete profile", "profile", "world masters rankings",
               "search", "rankings"}
    title_name = title if (title and title.lower() not in generic) else None
    row_name = _most_common([r["name"] for r in candidate_rows])
    athlete_name = title_name or row_name or meta.get("athlete")
    needs_review = (tables_analyzed == 0) or (
        candidate_rows and max(r["confidence"] for r in candidate_rows) < 0.5)

    return {
        "athlete": {"name": athlete_name, "source_url": meta.get("url")},
        "performances": candidate_rows,
        "tables": {"analyzed": tables_analyzed, "rows": rows_total,
                   "result_like": len(candidate_rows)},
        "needs_review": needs_review,
        "note": ("first-pass parser; validate against a real capture before "
                 "relying on stored rows"),
    }


def _clean_event(s):
    if not s:
        return None
    s = str(s).strip()
    # strip leading "Event ####" style prefixes if present
    s = re.sub(r"^event\s*\d+\s*", "", s, flags=re.I)
    return s or None


def _most_common(values):
    values = [v for v in values if v]
    if not values:
        return None
    return max(set(values), key=values.count)


def write_parsed(capture_dir, parsed: dict) -> Path:
    capture_dir = Path(capture_dir)
    out = capture_dir / "parsed.json"
    out.write_text(json.dumps(parsed, indent=1, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    return out


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    p = parse_capture(d)
    print(json.dumps(p["tables"], indent=1))
    print(f"result-like rows: {len(p['performances'])}, "
          f"needs_review={p['needs_review']}")
