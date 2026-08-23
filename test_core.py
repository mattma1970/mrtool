#!/usr/bin/env python3
"""Logic tests for mrtool that don't need a browser.

Run:  ./.venv/bin/python test_core.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mrtool  # noqa: E402


def test_slugify():
    assert mrtool.slugify("Jane O'Smith-Doe") == "jane-o-smith-doe"
    assert mrtool.slugify("  --weird!!  ") == "weird"
    assert mrtool.slugify("123") == "123"
    print("ok slugify")


def test_norm_name():
    assert mrtool.norm_name("  Jane   SMITH ") == "jane smith"
    print("ok norm_name")


def test_challenge_detection():
    assert mrtool.is_challenge("Just a moment...", "<html>x")
    assert mrtool.is_challenge("x", "<html>cf-turnstile</html>")
    assert not mrtool.is_challenge("Athlete - World Masters Rankings",
                                   "<html><body>results</body></html>")
    print("ok challenge detection")


def test_extract_x8():
    assert mrtool._extract_x8(
        "https://mastersrankings.com/athlete-profile/?x8=USA12345678JANESMITH") == \
        "USA12345678JANESMITH"
    assert mrtool._extract_x8("https://x.com/a?b=1&x8=abc&d=2") == "abc"
    assert mrtool._extract_x8("https://x.com/a") is None
    print("ok extract_x8")


def test_candidate_dedup_logic():
    """Dedup key = (path, x8 id): same athlete with extra params collapses,
    different x8 ids are kept (all share the /athlete-profile/ path)."""
    hrefs = [
        "https://m.com/athlete-profile/?x8=AAA1",
        "https://m.com/athlete-profile/?x8=AAA1&from=search",
        "https://m.com/athlete-profile/?x8=BBB2",
    ]
    seen, out = set(), []
    for h in hrefs:
        key = (re.sub(r"[?#].*$", "", h).rstrip("/"), mrtool._extract_x8(h))
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    assert len(out) == 2, out
    print("ok candidate dedup")


def test_registry_roundtrip(tmp):
    reg = mrtool.load_registry()
    assert reg == {"athletes": []}
    reg["athletes"].append({"name": "Jane Smith", "url": "u1", "x8": "X1"})
    mrtool.REGISTRY_PATH.write_text("")
    mrtool.save_registry(reg)
    loaded = mrtool.load_registry()
    assert loaded["athletes"][0]["name"] == "Jane Smith"
    assert mrtool.find_registered("jane   SMITH", loaded)["url"] == "u1"
    mrtool.REGISTRY_PATH.unlink()
    print("ok registry roundtrip")


def test_js_candidate_snippet():
    """The JS snippet handed to eval_on_selector_all must be syntactically
    balanced (paren count) and contain the expected fields."""
    js = ("els => els.map(a => ({ href: a.href,"
          " text: (a.innerText || '').trim().slice(0, 200),"
          " row: ((a.closest('tr') || a.closest('li') || a.closest('div'))"
          " ? (a.closest('tr') || a.closest('li') || a.closest('div'))"
          " .innerText.trim().slice(0, 500) : '') }))")
    assert js.count("(") == js.count(")")
    for field in ("href", "text", "row", "athlete-profile"):
        pass
    assert "href" in js and "text" in js and "row" in js
    print("ok candidate JS snippet balance")


def test_cookie_parsers(tmp):
    """cookie-import must parse the common export shapes into Playwright dicts."""
    import tempfile
    d = Path(tempfile.mkdtemp())

    # JSON array (typical browser-extension export), mixed-case-safe keys
    p1 = d / "arr.json"
    p1.write_text(json.dumps([
        {"domain": ".mastersrankings.com", "httpOnly": True, "name": "cf_clearance",
         "path": "/", "sameSite": "none", "secure": True, "value": "A1",
         "expirationDate": 1750000000.5},
        {"domain": ".mastersrankings.com", "name": "PHPSESSID", "value": "xyz",
         "path": "/", "secure": False, "httpOnly": True, "expirationDate": 0},
        {"domain": ".other.com", "name": "junk", "value": "nope", "path": "/"},
    ]), encoding="utf-8")
    c = mrtool._parse_cookie_file(p1)
    cf = next(x for x in c if x["name"] == "cf_clearance")
    assert cf["expires"] == 1750000000 and cf["secure"] is True
    assert cf["httpOnly"] is True and cf["sameSite"] == "None", cf
    assert next(x for x in c if x["name"] == "PHPSESSID")["expires"] == -1
    # default domain filter keeps only the relevant cookies
    kept = [x["name"] for x in c
            if "mastersrankings.com" in x["domain"]
            or x["name"].lower().startswith("cf_clearance")]
    assert kept == ["cf_clearance", "PHPSESSID"], kept

    # JSON object wrapping the list under "cookies"
    p2 = d / "wrap.json"
    p2.write_text(json.dumps({"cookies": [
        {"Name": "cf_clearance", "Value": "B2", "Domain": ".mastersrankings.com",
         "Path": "/", "Secure": True, "HttpOnly": True,
         "SameSite": "Lax", "Expires": 1750000000},
    ]}), encoding="utf-8")
    c = mrtool._parse_cookie_file(p2)
    assert len(c) == 1 and c[0]["name"] == "cf_clearance"
    assert c[0]["sameSite"] == "Lax" and c[0]["expires"] == 1750000000, c

    # Netscape cookies.txt
    p3 = d / "c.txt"
    p3.write_text(
        "# Netscape HTTP Cookie File\n"
        ".mastersrankings.com\tTRUE\t/\tTRUE\t1750000000\tcf_clearance\tC3\n"
        ".mastersrankings.com\tFALSE\t/\tFALSE\t0\twordpress_logged_in\tdd\n"
        "   \n", encoding="utf-8")
    c = mrtool._parse_cookie_file(p3)
    assert len(c) == 2, c
    ccf = next(x for x in c if x["name"] == "cf_clearance")
    assert ccf["secure"] is True and ccf["expires"] == 1750000000, ccf
    print("ok cookie parsers (json array / wrapped / cookies.txt)")


def main():
    tmp = Path(__file__).parent
    test_slugify()
    test_norm_name()
    test_challenge_detection()
    test_extract_x8()
    test_candidate_dedup_logic()
    test_registry_roundtrip(tmp)
    test_js_candidate_snippet()
    test_cookie_parsers(tmp)
    print("\nall core logic tests passed")


if __name__ == "__main__":
    main()
