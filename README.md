# claude-usage

A live, terminal usage tracker for [Claude Code](https://claude.com/claude-code). Parses the local session logs and shows your token burn and estimated API-equivalent cost in real time.

![screenshot](docs/screenshot.png)

## What it shows

- **5-hour window** — token/cost burn in the current rolling 5-hour block, with a reset countdown. The bar is calibrated against your *median* historical block ("vs typical"); set `CLAUDE_USAGE_TOKEN_LIMIT` to gauge against a real plan limit instead.
- **Today** — tokens and estimated cost, broken down by model.
- **Live session** — the session you're working in right now, with tokens/min.
- **All-time** — per-project breakdown across every session.

Costs are *estimated API-equivalent* (subscription users don't pay per token) using the `PRICING` table at the top of `tracker.py`.

## How it works

Reads `~/.claude/projects/*/*.jsonl`, pulling `usage` (input / output / cache-create / cache-read tokens) and `model` from every assistant message. Events are deduped by `uuid` and files are re-parsed incrementally via an mtime+size cache, so refreshes stay cheap. The 5-hour window uses `ccusage`-style blocks (hour-floored, new block on a >5h gap).

## Usage

```sh
bin/claude-usage          # run in the current terminal
bin/claude-usage --once   # print one frame and exit
bin/claude-usage-float    # floating kitty window (class: claude-usage-tui)
```

`q` quits. Refreshes every 2s.

## Install

```sh
ln -s "$PWD/bin/claude-usage" ~/.local/bin/claude-usage
ln -s "$PWD/bin/claude-usage-float" ~/.local/bin/claude-usage-float
```

Stdlib Python only — no dependencies.

## Theming

If `~/.local/state/quickshell/user/generated/colors.json` (a [matugen](https://github.com/InioX/matugen) Material 3 palette) exists, colors are read from it every frame, so the UI tracks your wallpaper. Otherwise it falls back to sensible defaults.
