#!/usr/bin/env python3
"""NSE day-trading plan tool for the Day Trader pack. Standard library only, Python 3.8 or newer.

  nse.py clock                       what the NSE is doing right now (IST) and what kind of plan fits
  nse.py scan daily                  20-day breakouts and oversold bounces on daily closes
  nse.py scan orb                    opening-range breakouts on 5-minute bars (15-minute range, 9:15 to 9:30)
  nse.py plan SYMBOL                 entry, stop, target, shares, history replay and a rules check, in rupees
  nse.py init                        write trader/config-in.json from the example

It plans. It never places an order and has no broker code. Data: Yahoo Finance chart data for
NSE tickers (SYMBOL.NS), free and unofficial, so it can lag or rate-limit.
"""

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))  # India has no daylight saving
OPEN = dt.time(9, 15)
CLOSE = dt.time(15, 30)
BAR_MIN = 5
RANGE_BARS = 3  # 9:15, 9:20, 9:25 -> the 15-minute opening range
FULL_SESSION_BARS = 75  # 9:15 to 15:30 in 5-minute bars

# NSE equity trading holidays that fall on weekdays, 2026 (from NSE's circular, as published by
# brokers). Check nseindia.com for changes: special sessions and one-off closures are announced late.
HOLIDAYS = {
    2026: ["2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
           "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
           "2026-11-10", "2026-11-24", "2026-12-25"],
}

DEFAULTS = {
    "budget_inr": 100000, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500,
    "max_trades_per_day": 3, "no_entries_before": "09:30", "no_entries_after": "14:45",
    "flat_by": "15:15", "min_reward_risk": 1.0, "target_r": 1.0, "blocked": [],
    "min_price_inr": 50, "min_avg_turnover_inr": 100000000,
}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36", "Accept": "application/json,*/*"}


class NseError(Exception):
    pass


# ---------------------------------------------------------------- clock
def is_trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS.get(d.year, [])


def next_session(d):
    n = d + dt.timedelta(days=1)
    for _ in range(14):
        if is_trading_day(n):
            return n
        n += dt.timedelta(days=1)
    return n


def parse_hm(text, what="time"):
    try:
        h, m = text.strip().split(":")
        t = dt.time(int(h), int(m))
    except (ValueError, AttributeError):
        raise NseError("%s wants an IST time like 14:45, not %r" % (what, text))
    return t


def parse_now(text):
    if not text:
        return dt.datetime.now(IST)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    raise NseError("--now wants 'YYYY-MM-DD HH:MM' in IST, not %r" % text)


def clock(now, cfg):
    d, t = now.date(), now.time()
    cutoff = parse_hm(cfg["no_entries_after"], "no_entries_after")
    out = {"now_ist": now.strftime("%Y-%m-%d %H:%M"), "weekday": now.strftime("%A"), "date": d.isoformat(),
           "calendar_covers_year": d.year in HOLIDAYS}
    if is_trading_day(d):
        if t < OPEN:
            mode, note = "pre-market", "NSE opens at 9:15 AM IST today"
        elif t < dt.time(9, 30):
            mode, note = "range-forming", "the 15-minute opening range runs 9:15 to 9:30"
        elif t < cutoff:
            mode, note = "live", "entries are allowed until %s" % cfg["no_entries_after"]
        elif t < CLOSE:
            mode, note = "too-late", "past the entry cutoff of %s: no new entries today" % cfg["no_entries_after"]
        else:
            mode, note = "next-session", "the NSE closed at 3:30 PM IST"
    else:
        why = "weekend" if d.weekday() >= 5 else "NSE holiday"
        mode, note = "next-session", "the NSE is closed today (%s)" % why
    out.update(mode=mode, note=note, open=(mode in ("range-forming", "live", "too-late")))
    out["next_session"] = (d if (is_trading_day(d) and t < OPEN) else next_session(d)).isoformat()
    if not out["calendar_covers_year"]:
        out["warning"] = "this tool has no NSE holiday list for %d: check nseindia.com" % d.year
    return out


# ---------------------------------------------------------------- data
def fetch_chart(sym, rng, interval, tries=2, timeout=20):
    q = urllib.parse.quote(sym + ".NS", safe="")
    last = None
    for attempt in range(tries):
        for host in ("query1", "query2"):
            url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s" % (host, q, rng, interval)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = exc
        time.sleep(1.5)
    raise NseError("no chart data for %s (%s)" % (sym, last))


def bars_of(payload):
    """Yahoo chart payload -> list of bar dicts in IST order. Bars with no close are skipped."""
    try:
        res = payload["chart"]["result"][0]
        ts, q = res.get("timestamp") or [], res["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        raise NseError("the chart data has an unexpected shape")
    out = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        except (KeyError, IndexError):
            continue
        if c is None or h is None or l is None:
            continue
        out.append({"t": dt.datetime.fromtimestamp(t, IST), "o": o, "h": h, "l": l, "c": c, "v": v or 0})
    return out


def load_offline(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- indicators
def rsi14(closes):
    if len(closes) < 15:
        return None
    gains = losses = 0.0
    for i in range(1, 15):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    ag, al = gains / 14, losses / 14
    for i in range(15, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * 13 + max(ch, 0)) / 14
        al = (al * 13 + max(-ch, 0)) / 14
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def daily_setups(bars, cfg, today_partial_date=None):
    """Return a list of setup dicts for one symbol from daily bars, or [] (also why it was skipped)."""
    if today_partial_date:
        bars = [b for b in bars if b["t"].date() != today_partial_date]
    if len(bars) < 25:
        return [], "too little history"
    closes = [b["c"] for b in bars]
    last = bars[-1]
    avg_turn = sum(b["c"] * b["v"] for b in bars[-20:]) / 20
    if last["c"] < cfg["min_price_inr"]:
        return [], "price under the minimum"
    if avg_turn < cfg["min_avg_turnover_inr"]:
        return [], "too thinly traded"
    hits = []
    level = max(closes[-21:-1])
    if last["c"] > level:
        hits.append({"setup": "breakout20", "level": round(level, 2)})
    r = rsi14(closes)
    if r is not None and r < 30 and closes[-1] > closes[-2]:
        hits.append({"setup": "oversold-bounce", "level": round(closes[-2], 2)})
    for h in hits:
        h.update(close=round(last["c"], 2), date=last["t"].date().isoformat(),
                 rsi=round(r, 1) if r is not None else None, avg_turnover_inr=round(avg_turn))
    return hits, None


# ---------------------------------------------------------------- opening range
def session_bars(bars, day):
    return [b for b in bars if b["t"].date() == day and OPEN <= b["t"].time() < CLOSE]


def opening_range(day_bars):
    first = day_bars[:RANGE_BARS]
    starts = [b["t"].time() for b in first]
    want = [(dt.datetime.combine(dt.date(2000, 1, 1), OPEN) + dt.timedelta(minutes=BAR_MIN * i)).time()
            for i in range(RANGE_BARS)]
    if starts != want:
        return None
    return {"high": max(b["h"] for b in first), "low": min(b["l"] for b in first)}


def find_trigger(day_bars, rng, earliest, cutoff, now=None):
    """First completed 5-minute bar (starting at or after `earliest`, before `cutoff`) that closes above the range."""
    for b in day_bars[RANGE_BARS:]:
        t = b["t"]
        if t.time() < earliest or t.time() >= cutoff:
            continue
        if now is not None and t + dt.timedelta(minutes=BAR_MIN) > now:
            break  # bar still forming
        if b["c"] > rng["high"]:
            return b
    return None


def orb_state(payload, day, now, cfg):
    bars = session_bars(bars_of(payload), day)
    if len(bars) < RANGE_BARS:
        return {"status": "no-data", "reason": "fewer than %d bars so far" % RANGE_BARS}
    rng = opening_range(bars)
    if rng is None:
        return {"status": "no-data", "reason": "a bar is missing in 9:15 to 9:30 (a halt or a stock that did not trade)"}
    done = now is None or now >= dt.datetime.combine(day, dt.time(9, 30), tzinfo=IST)
    if not done:
        return {"status": "forming", "range": rng}
    earliest = max(dt.time(9, 30), parse_hm(cfg["no_entries_before"], "no_entries_before"))
    cutoff = parse_hm(cfg["no_entries_after"], "no_entries_after")
    trig = find_trigger(bars, rng, earliest, cutoff, now)
    last_close = bars[-1]["c"]
    if trig:
        return {"status": "triggered", "range": rng, "entry": trig["c"], "time": trig["t"].strftime("%H:%M"),
                "last_close": last_close}
    if now is not None and now.time() >= cutoff:
        return {"status": "expired", "range": rng, "last_close": last_close}
    return {"status": "waiting", "range": rng, "last_close": last_close}


# ---------------------------------------------------------------- history replay
def replay_history(bars, cfg, target_r):
    flat = parse_hm(cfg["flat_by"], "flat_by")
    earliest = max(dt.time(9, 30), parse_hm(cfg["no_entries_before"], "no_entries_before"))
    cutoff = parse_hm(cfg["no_entries_after"], "no_entries_after")
    by_day = {}
    for b in bars:
        by_day.setdefault(b["t"].date(), []).append(b)
    res = {"sessions": 0, "fired": 0, "target": 0, "stop": 0, "neither": 0}
    for day in sorted(by_day):
        day_bars = session_bars(by_day[day], day)
        if len(day_bars) < FULL_SESSION_BARS - 3:
            continue  # half days, gaps and partial days are left out
        rng = opening_range(day_bars)
        if rng is None:
            continue
        res["sessions"] += 1
        trig = find_trigger(day_bars, rng, earliest, cutoff)
        if trig is None:
            continue
        stop = (rng["high"] + rng["low"]) / 2
        entry = trig["c"]
        if entry <= stop:
            continue
        target = entry + target_r * (entry - stop)
        res["fired"] += 1
        outcome = "neither"
        for b in day_bars:
            if b["t"] <= trig["t"]:
                continue
            if b["t"].time() >= flat:
                break
            if b["l"] <= stop:  # a bar that touches both counts as the stop: the cautious reading
                outcome = "stop"
                break
            if b["h"] >= target:
                outcome = "target"
                break
        res[outcome] += 1
    return res


def verdict(res):
    if res["fired"] < 20:
        return "too few past sessions (%d) to say anything" % res["fired"]
    decided = res["target"] + res["stop"]
    if decided == 0:
        return "it mostly ended without hitting either level"
    share = res["target"] / decided
    if share >= 0.6:
        return "leaned to the target in the past sessions"
    if share <= 0.4:
        return "leaned to the stop in the past sessions"
    return "close to a coin flip"


# ---------------------------------------------------------------- config + plan
def load_config(path):
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                user = json.load(fh)
        except (OSError, ValueError) as exc:
            raise NseError("could not read %s (%s)" % (path, exc))
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    for k in ("budget_inr", "max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day"):
        if not isinstance(cfg[k], (int, float)) or cfg[k] <= 0:
            raise NseError("%s must be a number above zero in %s" % (k, path))
    return cfg


def rupees(x):
    return "Rs %s" % format(int(round(x)), ",") if abs(x - round(x)) < 0.005 or abs(x) >= 1000 else "Rs %.2f" % x


def build_plan(sym, state, history, cfg, now, trades_today, loss_today, target_r):
    plan = {"symbol": sym, "now_ist": now.strftime("%Y-%m-%d %H:%M"), "status": state["status"],
            "checks": [], "blocks": []}
    if state["status"] in ("no-data", "expired"):
        plan["blocks"].append(state.get("reason") or "the entry window closed with no breakout")
        plan["verdict"] = "BLOCK"
        return plan
    rng = state["range"]
    entry = state["entry"] if state["status"] == "triggered" else rng["high"]
    stop = (rng["high"] + rng["low"]) / 2
    risk = entry - stop
    plan.update(range_high=round(rng["high"], 2), range_low=round(rng["low"], 2), entry=round(entry, 2),
                entry_is="trigger bar close at %s" % state["time"] if state["status"] == "triggered"
                else "the range high: the plan if it breaks out (not triggered yet)",
                stop=round(stop, 2), stop_is="midpoint of the opening range")
    if risk <= 0.0:
        plan["blocks"].append("the range is too narrow to put a stop under the entry")
        plan["verdict"] = "BLOCK"
        return plan
    target = entry + target_r * risk
    shares = int(math.floor(min(cfg["max_loss_per_trade_inr"] / risk, cfg["budget_inr"] / entry)))
    decided_by = "max loss per trade" if cfg["max_loss_per_trade_inr"] / risk <= cfg["budget_inr"] / entry else "budget"
    plan.update(target=round(target, 2), target_r=target_r, risk_per_share=round(risk, 2), shares=shares,
                sized_by=decided_by, cost=round(shares * entry, 2), loss_at_stop=round(shares * risk, 2),
                reward_at_target=round(shares * (target - entry), 2), flat_by=cfg["flat_by"])
    if history is not None:
        plan["history"] = dict(history, verdict=verdict(history))
    # rules check
    blocks, passed = plan["blocks"], plan["checks"]
    if shares < 1:
        blocks.append("not even one share fits: risk per share %s against a max loss of %s"
                      % (rupees(risk), rupees(cfg["max_loss_per_trade_inr"])))
    else:
        passed.append("max loss per trade: %s at the stop, limit %s" % (rupees(shares * risk), rupees(cfg["max_loss_per_trade_inr"])))
    if shares * entry > cfg["budget_inr"]:
        blocks.append("cost %s is above the budget %s" % (rupees(shares * entry), rupees(cfg["budget_inr"])))
    if sym.upper() in [s.upper() for s in cfg["blocked"]]:
        blocks.append("%s is on your blocked list" % sym)
    if trades_today >= cfg["max_trades_per_day"]:
        blocks.append("already %d trades today, the limit is %d" % (trades_today, cfg["max_trades_per_day"]))
    else:
        passed.append("trades today: %d of %d" % (trades_today, cfg["max_trades_per_day"]))
    if loss_today + shares * risk > cfg["max_daily_loss_inr"]:
        blocks.append("loss so far %s plus this stop %s would pass the daily limit %s"
                      % (rupees(loss_today), rupees(shares * risk), rupees(cfg["max_daily_loss_inr"])))
    else:
        passed.append("daily loss limit: room for this stop")
    rr = (target - entry) / risk
    if rr + 1e-9 < cfg["min_reward_risk"]:
        blocks.append("reward to risk %.2f is under your minimum %.2f" % (rr, cfg["min_reward_risk"]))
    else:
        passed.append("reward to risk %.2f" % rr)
    live = is_trading_day(now.date()) and OPEN <= now.time() < CLOSE
    t = now.time()
    if live:
        if t < parse_hm(cfg["no_entries_before"]) or t >= parse_hm(cfg["no_entries_after"]):
            blocks.append("%s is outside your entry window %s to %s" % (t.strftime("%H:%M"), cfg["no_entries_before"], cfg["no_entries_after"]))
        else:
            passed.append("inside the entry window")
    else:
        passed.append("market closed: the entry window is checked again at the open")
    plan["verdict"] = "BLOCK" if blocks else "PASS"
    return plan


def print_plan(p, cfg):
    print("%s  %s IST  %s" % (p["symbol"], p["now_ist"], p["status"].upper()))
    if "entry" in p:
        print("  Entry %s (%s)" % (rupees(p["entry"]), p["entry_is"]))
        if "stop" in p:
            print("  Opening range %s to %s" % (rupees(p["range_low"]), rupees(p["range_high"])))
        if "target" in p:
            print("  Stop %s (%s)   Target %s (%.1fR)   Risk %s a share"
                  % (rupees(p["stop"]), p["stop_is"], rupees(p["target"]), p["target_r"], rupees(p["risk_per_share"])))
            print("  Size %d shares, cost %s, loss at the stop %s (decided by %s), flat by %s IST"
                  % (p["shares"], rupees(p["cost"]), rupees(p["loss_at_stop"]), p["sized_by"], p["flat_by"]))
    if "history" in p:
        h = p["history"]
        print("  Past sessions: %d usable, rule fired %d times: %d hit the target, %d hit the stop, %d neither"
              % (h["sessions"], h["fired"], h["target"], h["stop"], h["neither"]))
        print("  Verdict: %s. A replay of recent days, not a prediction." % h["verdict"])
    else:
        print("  Past-session replay: not run.")
    for c in p["checks"]:
        print("  ok   " + c)
    for b in p["blocks"]:
        print("  STOP " + b)
    print("  RULES CHECK: %s" % p["verdict"])


# ---------------------------------------------------------------- commands
def cmd_clock(a, cfg):
    c = clock(parse_now(a.now), cfg)
    if a.json:
        print(json.dumps(c, indent=2))
    else:
        print("%s %s IST: %s. Mode: %s. Next session: %s." % (c["weekday"], c["now_ist"], c["note"], c["mode"], c["next_session"]))
        if "warning" in c:
            print("Warning: " + c["warning"])
    return 0


def read_universe(path):
    if not os.path.exists(path):
        raise NseError("no %s yet: run /trader-universe-india first" % path)
    syms = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            s = line.split("|")[0].strip()
            if s:
                syms.append(s[:-3] if s.endswith(".NS") else s)
    return syms


def cmd_scan(a, cfg):
    now = parse_now(a.now)
    c = clock(now, cfg)
    if a.kind == "daily":
        syms = [s.upper() for s in a.symbols.split(",")] if a.symbols else read_universe(a.universe)
        if a.limit:
            syms = syms[:a.limit]
        partial = now.date() if c["open"] else None

        def one(s):
            try:
                return s, daily_setups(bars_of(fetch_chart(s, "3mo", "1d")), cfg, partial), None
            except NseError as exc:
                return s, ([], None), str(exc)
        hits, failed, skipped = [], 0, 0
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            for s, (h, why), err in ex.map(one, syms):
                if err:
                    failed += 1
                elif why:
                    skipped += 1
                for x in h:
                    x["symbol"] = s
                    hits.append(x)
        hits.sort(key=lambda x: -x["avg_turnover_inr"])
        out = {"generated_ist": c["now_ist"], "mode": "daily", "scanned": len(syms), "failed": failed,
               "filtered_out": skipped, "hits": hits}
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2)
        print("Daily scan, %s IST: %d symbols, %d no data, %d filtered (price or turnover), %d setups."
              % (c["now_ist"], len(syms), failed, skipped, len(hits)))
        for x in hits[:a.top]:
            print("  %-12s %-16s close %s  level %s  RSI %s  avg turnover %.1f Cr"
                  % (x["symbol"], x["setup"], rupees(x["close"]), rupees(x["level"]), x["rsi"], x["avg_turnover_inr"] / 1e7))
        print("Wrote %s. A setup is a chart rule that matched, not a recommendation." % a.out)
        return 0
    # orb
    if not c["open"] and not a.now:
        raise NseError("the NSE is not in session (%s). The opening-range scan needs today's 5-minute bars" % c["note"])
    if a.symbols:
        syms = [s.upper() for s in a.symbols.split(",")]
    elif os.path.exists(a.watchlist):
        with open(a.watchlist, encoding="utf-8") as fh:
            syms = [h["symbol"] for h in json.load(fh).get("hits", [])]
        syms = list(dict.fromkeys(syms))
    else:
        raise NseError("pass --symbols, or run 'scan daily' first so there is a watchlist to check")
    day = now.date()

    def one(s):
        try:
            return s, orb_state(fetch_chart(s, "1d", "5m"), day, now, cfg), None
        except NseError as exc:
            return s, None, str(exc)
    rows, failed = [], 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for s, st, err in ex.map(one, syms):
            if err:
                failed += 1
            elif st["status"] in ("triggered", "waiting"):
                rows.append((s, st))
    print("Opening-range scan, %s IST: %d symbols checked, %d no data. Range is 9:15 to 9:30, 5-minute bars."
          % (c["now_ist"], len(syms), failed))
    trig = [r for r in rows if r[1]["status"] == "triggered"]
    for s, st in trig[:a.top]:
        print("  %-12s TRIGGERED %s at %s (range %s to %s)" % (s, st["time"], rupees(st["entry"]), rupees(st["range"]["low"]), rupees(st["range"]["high"])))
    print("%d triggered, %d still inside the range. A later hit is closer to the price now, so check the plan." % (len(trig), len(rows) - len(trig)))
    return 0


def cmd_plan(a, cfg):
    now = parse_now(a.now)
    sym = a.symbol.upper()
    day = now.date() if is_trading_day(now.date()) and now.time() >= OPEN else None
    if day is None:
        # before the open, or closed: plan from the most recent session's range as a stand-in is misleading,
        # so say what is needed instead of inventing a range
        raise NseError("%s: no opening range yet. The plan needs today's 9:15 to 9:30 bars, so run it from 9:30 IST on a "
                       "trading day. For earlier, use 'scan daily' to build a watchlist" % sym)
    today = load_offline(a.offline) if a.offline else fetch_chart(sym, "1d", "5m")
    state = orb_state(today, day, now, cfg)
    if state["status"] == "forming":
        raise NseError("%s: the opening range is still forming (it completes at 9:30 IST). Run again then" % sym)
    history = None
    if state["status"] in ("triggered", "waiting"):
        try:
            hist = load_offline(a.history) if a.history else fetch_chart(sym, "60d", "5m")
            hb = [b for b in bars_of(hist) if b["t"].date() < day]
            history = replay_history(hb, cfg, a.target_r)
        except NseError as exc:
            print("(past-session replay skipped: %s)" % exc, file=sys.stderr)
    plan = build_plan(sym, state, history, cfg, now, a.trades_today, a.loss_today, a.target_r)
    if a.json:
        print(json.dumps(plan, indent=2))
    else:
        print_plan(plan, cfg)
    return 0


def cmd_init(a, cfg):
    path = a.config
    if os.path.exists(path) and not a.force:
        raise NseError("%s already exists (use --force to overwrite)" % path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({k: DEFAULTS[k] for k in ("budget_inr", "max_loss_per_trade_inr", "max_daily_loss_inr",
                   "max_trades_per_day", "no_entries_before", "no_entries_after", "flat_by", "min_reward_risk",
                   "target_r", "blocked")}, fh, indent=2)
        fh.write("\n")
    print("Wrote %s with the example limits. Edit them to your own before you rely on a plan." % path)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="NSE day-trading plan (IST, rupees). Plans only, never orders.")
    ap.add_argument("--config", default=os.path.join("trader", "config-in.json"))
    ap.add_argument("--now", help="pretend it is this IST time: 'YYYY-MM-DD HH:MM' (for testing)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("clock"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("scan"); s.add_argument("kind", choices=["daily", "orb"])
    s.add_argument("--symbols"); s.add_argument("--universe", default=os.path.join("trader", "universe-in.txt"))
    s.add_argument("--watchlist", default=os.path.join("trader", "watchlist-in.json"))
    s.add_argument("--out", default=os.path.join("trader", "watchlist-in.json"))
    s.add_argument("--limit", type=int); s.add_argument("--top", type=int, default=10)
    s.add_argument("--workers", type=int, default=6)
    s = sub.add_parser("plan"); s.add_argument("symbol")
    s.add_argument("--offline", help="a saved Yahoo 1d/5m chart for today (testing)")
    s.add_argument("--history", help="a saved Yahoo 60d/5m chart (testing)")
    s.add_argument("--target-r", type=float, default=None); s.add_argument("--trades-today", type=int, default=0)
    s.add_argument("--loss-today", type=float, default=0.0); s.add_argument("--json", action="store_true")
    s = sub.add_parser("init"); s.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        if a.cmd == "plan" and a.target_r is None:
            a.target_r = float(cfg["target_r"])
        if a.cmd == "plan" and a.target_r <= 0:
            raise NseError("--target-r must be above zero")
        return {"clock": cmd_clock, "scan": cmd_scan, "plan": cmd_plan, "init": cmd_init}[a.cmd](a, cfg)
    except NseError as exc:
        print("No result: %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
