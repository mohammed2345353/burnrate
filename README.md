# burnrate 🔥

**Find out when you'll hit your Claude Code and Codex CLI usage limits *before* you hit them.**

[![CI](https://github.com/mohammed2345353/burnrate/actions/workflows/ci.yml/badge.svg)](https://github.com/mohammed2345353/burnrate/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

```text
$ burnrate
Claude Code
  5h window   started Sat 15:00, resets Sat 20:00 (in 1h 20m)
  used        ████████████░░░░░░░░░░░░  50.8% of 11.1M units (your busiest past 5h block)
  tokens      38.2M across 137 responses
  burn rate   24.8K units/min (last 30 min)
  forecast    ✓ at this pace the window resets before you reach the budget
  last 7 days 16.8M units, 129.1M tokens, 580 responses

Codex CLI  (plan: plus)
  5h window   █░░░░░░░░░░░░░░░░░░░░░░░   3.0%  resets Sat 22:53 (in 4h 14m)
  weekly      ██░░░░░░░░░░░░░░░░░░░░░░   9.0%  resets Fri 21:54 (in 6d 3h)
  last 7 days 245.4M tokens, 1423 turns
```

## The problem

"Usage limit reached" is the most common complaint in the Claude Code and Codex
issue trackers. People get blocked partway through a task with no warning, often
when the usage meter still looked fine. Weekly limits that seem to disappear in
hours and credits that drain faster than expected come up over and over.

The data you need to see it coming is already on your disk. Both tools write every
response, and its token usage, to local JSONL logs. `burnrate` reads those logs
and tells you:

- **where you are** in the current 5-hour window and when it resets (in *your* timezone),
- **how fast you're burning** right now,
- **whether you'll run out before the reset**, so you can switch to a lighter model, `/compact`, or hand off to another tool in time.

It reads files and nothing else. It makes no network calls, needs no API keys, and has no dependencies.

## Install

```bash
pipx install git+https://github.com/mohammed2345353/burnrate
# or
pip install git+https://github.com/mohammed2345353/burnrate
```

## Usage

```bash
burnrate                 # status for Claude Code + Codex
burnrate claude          # only Claude Code
burnrate codex           # only Codex CLI
burnrate daily --days 14 # per-day token breakdown
burnrate --watch 60      # live dashboard, refresh every 60 s
burnrate --json          # for scripts, status bars, tmux, Raycast...
burnrate --limit 9e6     # use your own 5h budget (in units) for the forecast
```

### Status line idea

```bash
burnrate codex --json | jq -r '.codex.five_hour.used'
```

## How it works

| Source | Where it reads | What it gets |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` (or `$CLAUDE_CONFIG_DIR`) | usage for every assistant response: input, output, cache read, cache write |
| Codex CLI | `~/.codex/sessions/**/*.jsonl` (or `$CODEX_HOME`) | `token_count` events, including the **real** `used_percent` and `resets_at` for the 5h and weekly windows |

**Claude Code** doesn't log your remaining quota, so burnrate rebuilds the 5-hour
windows itself. A window starts with your first message after the previous one
expired, floored to the hour. Tokens are weighted into **units** using the same
ratios as Anthropic's API prices (output 5×, cache read 0.1×, cache write 1.25×).
This matters because a session that's 90% cache reads costs far less than the raw
token count suggests. The forecast compares your current window against your
**busiest past window**, a practical stand-in for "how much you can do before
being blocked". You can also pass your own `--limit`.

**Codex CLI** logs the server-reported percentages, so burnrate shows them as-is
and fits a linear trend over the last hour to predict when you'll reach 100%.

Duplicate log lines (streamed responses are written more than once) are
de-duplicated by message id and request id.

## Limitations

- Anthropic doesn't publish exact subscription limits, and they change. The Claude forecast is relative to *your own history*, not an official number. Your source of truth is still `/usage` or claude.ai/settings/usage.
- Only local CLI usage is visible. Usage from claude.ai, the desktop chat or the ChatGPT app shares the same limits but isn't in these logs.
- Codex percentages are only as fresh as the last Codex turn.

## Roadmap

- [ ] Gemini CLI support (`~/.gemini/tmp/**` telemetry)
- [ ] Desktop notification when a forecast crosses the reset
- [ ] Per-model breakdown (Opus vs Sonnet vs Haiku)

## Related tools in this suite

Small, dependency-free tools for people fighting AI usage limits:

| Tool | What it does |
|---|---|
| **burnrate** | Forecast Claude Code / Codex limits from local logs |
| [context-diet](https://github.com/mohammed2345353/context-diet) | Find the CLAUDE.md / AGENTS.md / GEMINI.md bloat you pay for on every message |
| [ai-handoff](https://github.com/mohammed2345353/ai-handoff) | Hit a limit? Move the task to another AI with a handoff file |
| [ai-error-decoder](https://github.com/mohammed2345353/ai-error-decoder) | Explain 429 / 529 / quota errors from Anthropic, OpenAI and Gemini |
| [ratelimit-retry](https://github.com/mohammed2345353/ratelimit-retry) | Retry that respects provider rate-limit headers (JS) |
| [chat-export-md](https://github.com/mohammed2345353/chat-export-md) | ChatGPT / Claude / Gemini exports → Markdown |
| [mcp-token-audit](https://github.com/mohammed2345353/mcp-token-audit) | Measure how many tokens each MCP server's tools cost |
| [logslim](https://github.com/mohammed2345353/logslim) | Shrink logs and stack traces before pasting them into a chat |
| [chunkwise](https://github.com/mohammed2345353/chunkwise) | Split big files into token-budgeted chunks with continuation prompts |
| [ai-reset-clock](https://github.com/mohammed2345353/ai-reset-clock) | When does my quota reset? A countdown in your timezone |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Log formats change, so redacted samples from new versions are very welcome.

## License

[MIT](LICENSE)
