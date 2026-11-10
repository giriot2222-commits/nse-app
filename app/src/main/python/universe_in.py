#!/usr/bin/env python3
"""NSE stock list for the Day Trader pack: trader/universe-in.txt, one SYMBOL|Name a line.

Reads NSE's own equity master file (EQUITY_L.csv) and keeps the plain "EQ" series: the shares
that can be bought and sold within the same day. Standard library only, Python 3.8 or newer.
"""

import argparse
import csv
import io
import os
import re
import sys
import time
import urllib.error
import urllib.request

URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
# NSE blocks clients that do not look like a browser, so a browser-like User-Agent is sent.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.nseindia.com/",
}
SYMBOL_OK = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")  # M&M, BAJAJ-AUTO and GMRP&UI are real NSE symbols
KEEP_SERIES = "EQ"


class UniverseError(Exception):
    pass


def fetch(url=URL, tries=2, pause=3.0, timeout=25):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8-sig", errors="replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            if attempt + 1 < tries:
                time.sleep(pause)
    raise UniverseError("could not download NSE's EQUITY_L.csv (%s). Save it from nseindia.com and "
                        "use --offline PATH" % last)


def parse(text):
    """Return (kept, stats). kept is a sorted list of (symbol, name)."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise UniverseError("the file is empty")
    head = [h.strip().upper() for h in rows[0]]
    try:
        i_sym = head.index("SYMBOL")
        i_name = head.index("NAME OF COMPANY")
        i_ser = head.index("SERIES")
    except ValueError:
        raise UniverseError("this does not look like NSE's EQUITY_L.csv (columns: %s)" % ", ".join(head[:6]))
    kept, seen = [], set()
    stats = {"rows": 0, "other_series": {}, "bad_symbol": 0, "duplicate": 0}
    for row in rows[1:]:
        if len(row) <= max(i_sym, i_name, i_ser):
            continue
        stats["rows"] += 1
        sym, name, ser = row[i_sym].strip().upper(), " ".join(row[i_name].split()), row[i_ser].strip().upper()
        if ser != KEEP_SERIES:
            stats["other_series"][ser] = stats["other_series"].get(ser, 0) + 1
            continue
        if not SYMBOL_OK.match(sym):
            stats["bad_symbol"] += 1
            continue
        if sym in seen:
            stats["duplicate"] += 1
            continue
        seen.add(sym)
        kept.append((sym, name.replace("|", " ")))
    kept.sort()
    return kept, stats


def yahoo_symbol(sym):
    """Yahoo Finance's name for an NSE share: RELIANCE -> RELIANCE.NS (the & in M&M is encoded in URLs)."""
    return sym + ".NS"


def write(kept, path, yahoo=False):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for sym, name in kept:
            fh.write("%s|%s\n" % (yahoo_symbol(sym) if yahoo else sym, name))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build trader/universe-in.txt from NSE's equity list.")
    ap.add_argument("command", nargs="?", default="build", choices=["build"])
    ap.add_argument("--offline", metavar="PATH", help="read a saved EQUITY_L.csv instead of downloading")
    ap.add_argument("--out", default=os.path.join("trader", "universe-in.txt"))
    ap.add_argument("--yahoo", action="store_true", help="write Yahoo tickers (RELIANCE.NS) instead of NSE symbols")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.offline:
            with open(args.offline, encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        else:
            text = fetch()
        kept, st = parse(text)
    except (UniverseError, OSError) as exc:
        print("No list built: %s" % exc)
        return 1
    if not kept:
        print("No list built: no EQ-series rows found")
        return 1
    other = sum(st["other_series"].values())
    detail = ", ".join("%s %d" % (k, v) for k, v in sorted(st["other_series"].items(), key=lambda kv: -kv[1])[:5])
    print("NSE equity list: %d rows read, %d kept (series EQ)." % (st["rows"], len(kept)))
    print("Left out: %d in other series%s, %d with an odd symbol, %d duplicates."
          % (other, " (%s)" % detail if detail else "", st["bad_symbol"], st["duplicate"]))
    if not args.no_write:
        write(kept, args.out, args.yahoo)
        print("Wrote %s (%s). Liquidity is not filtered here: the scanner filters by price and volume."
              % (args.out, "Yahoo tickers, .NS" if args.yahoo else "NSE symbols"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
