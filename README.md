# Claude Code Usage Monitor

macOS menu bar app that shows your [Claude Code](https://docs.anthropic.com/en/docs/claude-code) usage in real time — just like `/usage`, but always visible.

![menubar](https://img.shields.io/badge/macOS-menu%20bar-blue)

## Features

- **Real-time usage** from the Anthropic API (same data as `/usage`)
- Session (5h), Weekly (all models), Weekly (Sonnet only) progress bars
- Detailed token & cost breakdown (from local JSONL logs)
- Per-model usage stats
- Auto-refreshes every 30 seconds
- Starts automatically on login (optional)

## Requirements

- macOS
- Python 3
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and logged in

## Install

```bash
git clone https://github.com/hmyanghm/claude-usage-menubar.git
cd claude-usage-menubar
./setup.sh
```

## Run

```bash
~/.claude-menubar/launch.sh
```

## Auto-start on login

```bash
launchctl load ~/Library/LaunchAgents/com.claude.usage-monitor.plist
```

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.claude.usage-monitor.plist 2>/dev/null
rm -rf ~/.claude-menubar
rm -f ~/Library/LaunchAgents/com.claude.usage-monitor.plist
```

## How it works

The app reads your Claude Code OAuth token from the macOS Keychain and calls the Anthropic usage API to get accurate rate-limit data. Token/cost breakdowns are calculated from local JSONL session files in `~/.claude/`. If the API is unavailable, it falls back to local estimates.
