import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from burnrate.core import (Event, burn_rate, claude_events, codex_events, daily_totals,
                           five_hour_blocks, percent_forecast)

T0 = datetime(2026, 11, 14, 10, 20, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")


def claude_row(ts, mid, req="r1", inp=10, out=100, cr=1000, cw=200):
    return {"timestamp": iso(ts), "requestId": req, "type": "assistant",
            "message": {"id": mid, "model": "claude-test", "usage": {
                "input_tokens": inp, "output_tokens": out,
                "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}}


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dedupes_streamed_duplicates(self):
        write_jsonl(self.dir / "p" / "s.jsonl", [
            claude_row(T0, "m1"), claude_row(T0, "m1"),  # same message written twice
            claude_row(T0 + timedelta(minutes=1), "m2"),
            {"timestamp": iso(T0), "type": "user", "message": {"role": "user", "content": "hi"}},
        ])
        ev = claude_events([self.dir])
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[0].total, 1310)

    def test_since_filter(self):
        write_jsonl(self.dir / "s.jsonl", [claude_row(T0, "a"), claude_row(T0 + timedelta(days=2), "b")])
        ev = claude_events([self.dir], since=T0 + timedelta(days=1))
        self.assertEqual([e.ts for e in ev], [T0 + timedelta(days=2)])

    def test_blocks(self):
        evs = [Event(ts=T0, source="c", input=1), Event(ts=T0 + timedelta(hours=4), source="c", input=1),
               Event(ts=T0 + timedelta(hours=5, minutes=30), source="c", input=1)]
        blocks = five_hour_blocks(evs)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].start, T0.replace(minute=0))
        self.assertEqual(len(blocks[0].events), 2)
        self.assertEqual(blocks[1].start, (T0 + timedelta(hours=5)).replace(minute=0))

    def test_units_and_burn_rate(self):
        e = Event(ts=T0, source="c", input=100, output=100, cache_read=1000, cache_write=100)
        self.assertAlmostEqual(e.units(), 100 + 500 + 100 + 125)
        self.assertAlmostEqual(burn_rate([e], T0 + timedelta(minutes=5), minutes=10), e.units() / 10)
        self.assertEqual(burn_rate([e], T0 + timedelta(hours=1), minutes=10), 0.0)

    def test_daily(self):
        evs = [Event(ts=T0, source="c", input=5), Event(ts=T0 + timedelta(days=1), source="c", output=2)]
        rows = daily_totals(evs, tz=timezone.utc)
        self.assertEqual(list(rows), ["2026-11-14", "2026-11-15"])
        self.assertEqual(rows["2026-11-15"]["units"], 10)


def codex_row(ts, used_p, used_s, resets_p, resets_s, inp=1000, cached=600, out=50):
    return {"timestamp": iso(ts), "type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}},
        "rate_limits": {"plan_type": "plus", "credits": {"balance": "0"},
                        "primary": {"used_percent": used_p, "window_minutes": 300, "resets_at": resets_p},
                        "secondary": {"used_percent": used_s, "window_minutes": 10080, "resets_at": resets_s}}}}


class CodexTests(unittest.TestCase):
    def test_parse_and_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset_p = int((T0 + timedelta(hours=4)).timestamp())
            reset_s = int((T0 + timedelta(days=3)).timestamp())
            rows = [{"timestamp": iso(T0), "type": "turn_context", "payload": {"model": "gpt-test"}}]
            rows += [codex_row(T0 + timedelta(minutes=10 * i), 10 + 5 * i, 20, reset_p, reset_s) for i in range(7)]
            write_jsonl(Path(tmp) / "2026" / "11" / "14" / "rollout.jsonl", rows)
            events, limits = codex_events([Path(tmp)])
        self.assertEqual(len(events), 7)
        self.assertEqual(events[0].model, "gpt-test")
        self.assertEqual(events[0].input, 400)
        self.assertEqual(events[0].cache_read, 600)
        now = T0 + timedelta(hours=1)
        fc = percent_forecast(limits, "primary_used", "primary_resets", now)
        self.assertAlmostEqual(fc["used"], 40.0)
        self.assertAlmostEqual(fc["rate_per_hour"], 30.0)  # 5% every 10 minutes
        self.assertTrue(fc["hits_before_reset"])           # 60% left at 30%/h -> T0+3h, reset at T0+4h
        weekly = percent_forecast(limits, "secondary_used", "secondary_resets", now)
        self.assertEqual(weekly["rate_per_hour"], 0.0)
        self.assertFalse(weekly["hits_before_reset"])

    def test_stale_after_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset = int((T0 + timedelta(minutes=30)).timestamp())
            write_jsonl(Path(tmp) / "r.jsonl", [codex_row(T0, 90, 10, reset, reset)])
            _, limits = codex_events([Path(tmp)])
        fc = percent_forecast(limits, "primary_used", "primary_resets", T0 + timedelta(hours=1))
        self.assertTrue(fc["stale"])
        self.assertEqual(fc["used"], 0.0)


class CliSmokeTest(unittest.TestCase):
    def test_json_with_empty_dirs(self):
        from burnrate import cli
        with tempfile.TemporaryDirectory() as tmp:
            old = dict(os.environ)
            os.environ["CLAUDE_CONFIG_DIR"] = tmp
            os.environ["CODEX_HOME"] = tmp
            os.environ["HOME"] = tmp
            os.environ["USERPROFILE"] = tmp
            try:
                self.assertEqual(cli.main(["status", "--json"]), 0)
            finally:
                os.environ.clear()
                os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
