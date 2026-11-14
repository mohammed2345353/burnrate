"""burnrate command-line interface."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

from . import __version__
from .core import (DEFAULT_WEIGHTS, WEEK, burn_rate, claude_events, codex_events, daily_totals,
                   five_hour_blocks, percent_forecast)


def human(n: float) -> str:
    n = float(n)
    for unit in ("", "K", "M", "B"):
        if abs(n) < 1000:
            return f"{n:.0f}{unit}" if unit == "" else f"{n:.1f}{unit}"
        n /= 1000
    return f"{n:.1f}T"


def dur(td: timedelta) -> str:
    secs = int(td.total_seconds())
    sign = "-" if secs < 0 else ""
    secs = abs(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{sign}{d}d {h}h"
    if h:
        return f"{sign}{h}h {m:02d}m"
    return f"{sign}{m}m"


def local(dt):
    return dt.astimezone().strftime("%a %H:%M") if dt else "?"


def bar(frac: float, width: int = 24) -> str:
    frac = max(0.0, min(frac, 1.0))
    full = int(round(frac * width))
    return "█" * full + "░" * (width - full)


# --------------------------------------------------------------------------- claude

def claude_report(now, limit_units=None, rate_minutes=30):
    events = claude_events(since=now - WEEK - timedelta(hours=5))
    blocks = five_hour_blocks(events)
    week = [e for e in events if e.ts >= now - WEEK]
    report = {"source": "claude", "messages_7d": len(week),
              "units_7d": sum(e.units() for e in week), "tokens_7d": sum(e.total for e in week)}
    past = [b for b in blocks if b.end <= now]
    report["max_block_units"] = max((b.units() for b in past), default=0.0)
    current = blocks[-1] if blocks and blocks[-1].end > now else None
    if current is None:
        report["active"] = False
        return report
    rate = burn_rate(events, now, rate_minutes)
    used = current.units()
    budget = limit_units or report["max_block_units"] or None
    report.update({
        "active": True, "block_start": current.start.isoformat(), "block_end": current.end.isoformat(),
        "resets_in_s": (current.end - now).total_seconds(), "units": used,
        "tokens": current.tokens(), "messages": len(current.events),
        "rate_units_per_min": rate, "budget_units": budget,
        "budget_source": "--limit" if limit_units else ("your busiest past 5h block" if budget else None),
    })
    if budget and rate > 0:
        remaining = max(budget - used, 0)
        hits = now + timedelta(minutes=remaining / rate)
        report["hits_budget_at"] = hits.isoformat()
        report["hits_before_reset"] = hits < current.end
    return report


def print_claude(r, now):
    print("Claude Code")
    if r.get("messages_7d", 0) == 0:
        print("  no usage found in ~/.claude/projects (set CLAUDE_CONFIG_DIR if it lives elsewhere)\n")
        return
    if not r["active"]:
        print("  no active 5-hour window — your next message starts a fresh one")
    else:
        end = datetime.fromisoformat(r["block_end"])
        print(f"  5h window   started {local(datetime.fromisoformat(r['block_start']))}, "
              f"resets {local(end)} (in {dur(end - now)})")
        budget = r.get("budget_units")
        if budget:
            frac = r["units"] / budget
            print(f"  used        {bar(frac)} {frac:6.1%} of {human(budget)} units ({r['budget_source']})")
        else:
            print(f"  used        {human(r['units'])} units")
        print(f"  tokens      {human(r['tokens'])} across {r['messages']} responses")
        print(f"  burn rate   {human(r['rate_units_per_min'])} units/min (last 30 min)")
        if "hits_budget_at" in r:
            hits = datetime.fromisoformat(r["hits_budget_at"])
            if r["hits_before_reset"]:
                print(f"  forecast    ⚠ at this pace you reach the budget at {local(hits)} "
                      f"(in {dur(hits - now)}) — before the reset")
            else:
                print("  forecast    ✓ at this pace the window resets before you reach the budget")
    print(f"  last 7 days {human(r['units_7d'])} units, {human(r['tokens_7d'])} tokens, "
          f"{r['messages_7d']} responses\n")


# --------------------------------------------------------------------------- codex

def codex_report(now):
    events, limits = codex_events(since=now - WEEK)
    r = {"source": "codex", "messages_7d": len(events), "tokens_7d": sum(e.total for e in events)}
    if not limits:
        return r
    last = limits[-1]
    r["plan"] = last.plan
    r["credits_balance"] = last.credits_balance
    r["snapshot_at"] = last.ts.isoformat()
    for key, attr, res, win in (("five_hour", "primary_used", "primary_resets", last.primary_window_min),
                                ("weekly", "secondary_used", "secondary_resets", last.secondary_window_min)):
        fc = percent_forecast(limits, attr, res, now)
        if fc:
            fc["window_minutes"] = win
            fc = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in fc.items()}
            r[key] = fc
    return r


def print_codex(r, now):
    print("Codex CLI" + (f"  (plan: {r['plan']})" if r.get("plan") else ""))
    if r.get("messages_7d", 0) == 0 and "five_hour" not in r:
        print("  no usage found in ~/.codex/sessions (set CODEX_HOME if it lives elsewhere)\n")
        return
    for key, label in (("five_hour", "5h window"), ("weekly", "weekly")):
        fc = r.get(key)
        if not fc:
            continue
        resets = datetime.fromisoformat(fc["resets_at"]) if fc.get("resets_at") else None
        if fc.get("stale"):
            print(f"  {label:<11} reset at {local(resets)} — 0% used since")
            continue
        used = fc["used"]
        line = f"  {label:<11} {bar(used / 100)} {used:5.1f}%"
        if resets:
            line += f"  resets {local(resets)} (in {dur(resets - now)})"
        print(line)
        if fc.get("hits_at") and fc["hits_before_reset"]:
            hits = datetime.fromisoformat(fc["hits_at"])
            print(f"  {'':<11} ⚠ +{fc['rate_per_hour']:.1f}%/h → 100% at {local(hits)} (in {dur(hits - now)})")
    if r.get("credits_balance") not in (None, "0", 0):
        print(f"  credits     {r['credits_balance']}")
    print(f"  last 7 days {human(r['tokens_7d'])} tokens, {r['messages_7d']} turns\n")


# --------------------------------------------------------------------------- main

def main(argv=None):
    p = argparse.ArgumentParser(prog="burnrate", description="See how fast you are burning your Claude Code "
                                "and Codex CLI limits, and when you will hit them.")
    p.add_argument("command", nargs="?", default="status", choices=["status", "claude", "codex", "daily"])
    p.add_argument("--limit", type=float, help="your own 5h budget for Claude in units (default: your busiest past window)")
    p.add_argument("--days", type=int, default=7, help="days to show for `daily` (default 7)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--watch", type=int, metavar="SECONDS", help="refresh every N seconds")
    p.add_argument("--version", action="version", version=f"burnrate {__version__}")
    a = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    while True:
        now = datetime.now(timezone.utc)
        if a.watch and not a.json:
            print("\033[2J\033[H", end="")
        if a.command == "daily":
            ev = claude_events(since=now - timedelta(days=a.days))
            cx, _ = codex_events(since=now - timedelta(days=a.days))
            data = {"claude": daily_totals(ev), "codex": daily_totals(cx)}
            if a.json:
                print(json.dumps(data, indent=2))
            else:
                for src, rows in data.items():
                    print(f"{src}:")
                    print(f"  {'day':<11}{'input':>9}{'output':>9}{'cache rd':>10}{'cache wr':>10}{'units':>10}")
                    for day, row in rows.items():
                        print(f"  {day:<11}{human(row['input']):>9}{human(row['output']):>9}"
                              f"{human(row['cache_read']):>10}{human(row['cache_write']):>10}{human(row['units']):>10}")
                    print()
        else:
            out = {}
            if a.command in ("status", "claude"):
                out["claude"] = claude_report(now, a.limit)
            if a.command in ("status", "codex"):
                out["codex"] = codex_report(now)
            if a.json:
                print(json.dumps(out, indent=2, default=str))
            else:
                if "claude" in out:
                    print_claude(out["claude"], now)
                if "codex" in out:
                    print_codex(out["codex"], now)
                print("units = tokens weighted like API prices (output 5x, cache read 0.1x, cache write 1.25x).")
        if not a.watch:
            return 0
        try:
            time.sleep(a.watch)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
