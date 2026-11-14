"""Read local Claude Code / Codex CLI logs and compute usage, burn rate and forecasts."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, List, Optional

FIVE_HOURS = timedelta(hours=5)
WEEK = timedelta(days=7)

# Relative cost of each token type versus a plain input token, matching the
# ratios Anthropic publishes for API pricing (cache read 0.1x, 5-minute cache
# write 1.25x, 1-hour cache write 2x, output 5x). Subscription limits are not
# published, so these "units" are only a consistent way to compare sessions.
DEFAULT_WEIGHTS = {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25}


def parse_ts(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Event:
    ts: datetime
    source: str
    model: str = ""
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def units(self, weights=DEFAULT_WEIGHTS) -> float:
        return (self.input * weights["input"] + self.output * weights["output"]
                + self.cache_read * weights["cache_read"] + self.cache_write * weights["cache_write"])


@dataclass
class CodexLimits:
    ts: datetime
    plan: str = ""
    primary_used: Optional[float] = None
    primary_window_min: Optional[int] = None
    primary_resets: Optional[datetime] = None
    secondary_used: Optional[float] = None
    secondary_window_min: Optional[int] = None
    secondary_resets: Optional[datetime] = None
    credits_balance: Optional[str] = None


# --------------------------------------------------------------------------- paths

def claude_dirs() -> List[Path]:
    dirs = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    for base in ([Path(p) for p in env.split(",")] if env else []) + [
        Path.home() / ".claude", Path.home() / ".config" / "claude"]:
        p = base / "projects"
        if p.is_dir() and p not in dirs:
            dirs.append(p)
    return dirs


def codex_dirs() -> List[Path]:
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return [d for d in (home / "sessions", home / "archived_sessions") if d.is_dir()]


def _jsonl(paths: Iterable[Path]) -> Iterator[dict]:
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue


def _files(dirs: Iterable[Path], since: Optional[datetime]) -> List[Path]:
    out = []
    cutoff = since.timestamp() - 86400 if since else None
    for d in dirs:
        for p in d.rglob("*.jsonl"):
            try:
                if cutoff is None or p.stat().st_mtime >= cutoff:
                    out.append(p)
            except OSError:
                pass
    return out


# --------------------------------------------------------------------------- claude

def claude_events(dirs=None, since: Optional[datetime] = None) -> List[Event]:
    """Assistant messages with usage, de-duplicated by message id + request id."""
    seen = set()
    events = []
    for rec in _jsonl(_files(dirs if dirs is not None else claude_dirs(), since)):
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        key = (msg.get("id"), rec.get("requestId"))
        if key[0] and key in seen:
            continue
        seen.add(key)
        ts = parse_ts(rec.get("timestamp"))
        if ts is None or (since and ts < since):
            continue
        events.append(Event(
            ts=ts, source="claude", model=str(msg.get("model", "")),
            input=int(usage.get("input_tokens") or 0),
            output=int(usage.get("output_tokens") or 0),
            cache_read=int(usage.get("cache_read_input_tokens") or 0),
            cache_write=int(usage.get("cache_creation_input_tokens") or 0),
        ))
    events.sort(key=lambda e: e.ts)
    return events


@dataclass
class Block:
    start: datetime
    events: List[Event] = field(default_factory=list)

    @property
    def end(self) -> datetime:
        return self.start + FIVE_HOURS

    def tokens(self) -> int:
        return sum(e.total for e in self.events)

    def units(self, weights=DEFAULT_WEIGHTS) -> float:
        return sum(e.units(weights) for e in self.events)


def five_hour_blocks(events: List[Event]) -> List[Block]:
    """Group events into 5-hour windows that start at the first message after
    the previous window expired (floored to the hour, like Claude's session)."""
    blocks: List[Block] = []
    for ev in events:
        if not blocks or ev.ts >= blocks[-1].end:
            start = ev.ts.replace(minute=0, second=0, microsecond=0)
            blocks.append(Block(start=start))
        blocks[-1].events.append(ev)
    return blocks


def burn_rate(events: List[Event], now: datetime, minutes: int = 30, weights=DEFAULT_WEIGHTS) -> float:
    """Units per minute over the trailing window."""
    since = now - timedelta(minutes=minutes)
    recent = [e for e in events if since <= e.ts <= now]
    if not recent:
        return 0.0
    return sum(e.units(weights) for e in recent) / minutes


# --------------------------------------------------------------------------- codex

def codex_events(dirs=None, since: Optional[datetime] = None):
    """Return (token events, list of CodexLimits snapshots) from Codex rollouts."""
    events: List[Event] = []
    limits: List[CodexLimits] = []
    for path in _files(dirs if dirs is not None else codex_dirs(), since):
        model = ""
        for rec in _jsonl([path]):
            payload = rec.get("payload") or {}
            if rec.get("type") == "turn_context" and isinstance(payload, dict):
                model = str(payload.get("model", model))
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            ts = parse_ts(rec.get("timestamp"))
            if ts is None or (since and ts < since):
                continue
            info = payload.get("info") or {}
            last = info.get("last_token_usage") or {}
            if last:
                cached = int(last.get("cached_input_tokens") or 0)
                events.append(Event(
                    ts=ts, source="codex", model=model,
                    input=max(int(last.get("input_tokens") or 0) - cached, 0),
                    output=int(last.get("output_tokens") or 0),
                    cache_read=cached,
                    cache_write=int(last.get("cache_write_input_tokens") or 0),
                ))
            rl = payload.get("rate_limits") or info.get("rate_limits")
            if isinstance(rl, dict):
                p, s = rl.get("primary") or {}, rl.get("secondary") or {}
                credits = rl.get("credits") or {}
                limits.append(CodexLimits(
                    ts=ts, plan=str(rl.get("plan_type") or ""),
                    primary_used=p.get("used_percent"), primary_window_min=p.get("window_minutes"),
                    primary_resets=parse_ts(p.get("resets_at")),
                    secondary_used=s.get("used_percent"), secondary_window_min=s.get("window_minutes"),
                    secondary_resets=parse_ts(s.get("resets_at")),
                    credits_balance=credits.get("balance") if isinstance(credits, dict) else None,
                ))
    events.sort(key=lambda e: e.ts)
    limits.sort(key=lambda l: l.ts)
    return events, limits


def percent_forecast(snapshots: List[CodexLimits], attr: str, resets_attr: str, now: datetime,
                     lookback=timedelta(hours=1)):
    """Linear forecast of when a used_percent series reaches 100%.

    Returns dict(used, resets_at, rate_per_hour, hits_at, hits_before_reset)."""
    series = [(s.ts, getattr(s, attr), getattr(s, resets_attr)) for s in snapshots
              if getattr(s, attr) is not None]
    if not series:
        return None
    ts_last, used, resets = series[-1]
    if resets and resets <= now:
        return {"used": 0.0, "resets_at": resets, "rate_per_hour": 0.0, "hits_at": None,
                "hits_before_reset": False, "stale": True}
    window = [(t, u) for t, u, r in series if t >= ts_last - lookback and r == resets]
    rate = 0.0
    if len(window) >= 2 and window[-1][0] > window[0][0]:
        hours = (window[-1][0] - window[0][0]).total_seconds() / 3600
        rate = max((window[-1][1] - window[0][1]) / hours, 0.0)
    hits_at = None
    if rate > 0 and used < 100:
        hits_at = ts_last + timedelta(hours=(100 - used) / rate)
    elif used >= 100:
        hits_at = ts_last
    return {"used": float(used), "resets_at": resets, "rate_per_hour": rate, "hits_at": hits_at,
            "hits_before_reset": bool(hits_at and (resets is None or hits_at < resets)), "stale": False}


def daily_totals(events: List[Event], tz=None, weights=DEFAULT_WEIGHTS):
    out = {}
    for e in events:
        day = e.ts.astimezone(tz).date().isoformat()
        row = out.setdefault(day, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                                   "total": 0, "units": 0.0, "messages": 0})
        row["input"] += e.input
        row["output"] += e.output
        row["cache_read"] += e.cache_read
        row["cache_write"] += e.cache_write
        row["total"] += e.total
        row["units"] += e.units(weights)
        row["messages"] += 1
    return dict(sorted(out.items()))
