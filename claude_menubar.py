#!/usr/bin/env python3
"""
Claude Code Usage Monitor v2 — macOS Menu Bar App
Shows Claude Code usage with visual progress bars, just like /usage.
Uses the Anthropic OAuth API for accurate rate-limit data,
and reads local JSONL session files for detailed token/cost breakdowns.
"""

import json
import os
import glob
import time
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
from urllib.request import Request, urlopen
from urllib.error import URLError

import rumps


# ─── Configuration ───────────────────────────────────────────────────────────

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
REFRESH_INTERVAL_SEC = 30

USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_BETA = "oauth-2025-04-20"

# Session window = 5 hours, Week window = 7 days
SESSION_WINDOW_HOURS = 5
WEEK_WINDOW_DAYS = 7

# Anthropic pricing (USD per 1M tokens)
PRICING = {
    "claude-sonnet-4-6":          {"input": 3.0,  "output": 15.0,  "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-20250514":   {"input": 3.0,  "output": 15.0,  "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4":            {"input": 3.0,  "output": 15.0,  "cache_read": 0.30, "cache_write": 3.75},
    "claude-opus-4-6":            {"input": 15.0, "output": 75.0,  "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus-4-20250514":     {"input": 15.0, "output": 75.0,  "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus-4":              {"input": 15.0, "output": 75.0,  "cache_read": 1.50, "cache_write": 18.75},
    "claude-sonnet-4-5-20250514": {"input": 3.0,  "output": 15.0,  "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5":          {"input": 3.0,  "output": 15.0,  "cache_read": 0.30, "cache_write": 3.75},
    "default":                    {"input": 3.0,  "output": 15.0,  "cache_read": 0.30, "cache_write": 3.75},
}

# ─── OAuth / Usage API ───────────────────────────────────────────────────────

def _get_oauth_token():
    """Retrieve Claude Code OAuth access token from macOS Keychain."""
    account = os.environ.get("USER", "claude-code-user")
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-w", "-s", "Claude Code-credentials"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout.strip())
        return data.get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def fetch_usage_api():
    """Call Anthropic usage API → dict with five_hour, seven_day, etc. or None."""
    token = _get_oauth_token()
    if not token:
        return None
    try:
        req = Request(USAGE_API_URL, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-beta": ANTHROPIC_BETA,
            "User-Agent": "claude-code-menubar/1.0",
        })
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _parse_reset_time(iso_str):
    """Parse ISO timestamp from API → naive local datetime."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.astimezone(None).replace(tzinfo=None)
    except Exception:
        return None


# ─── Progress Bar ────────────────────────────────────────────────────────────

def make_bar(pct, width=20):
    """Create a Unicode progress bar.  ████████░░░░  42%"""
    pct = max(0, min(100, pct))
    filled = round(width * pct / 100)
    empty = width - filled
    bar = "█" * filled + "░" * empty
    return f"{bar}  {pct:.0f}%"


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def fmt_cost(usd):
    if usd >= 1.0:
        return f"${usd:.2f}"
    if usd >= 0.01:
        return f"${usd:.3f}"
    return f"${usd:.4f}"


# ─── JSONL Parser ────────────────────────────────────────────────────────────

class UsageTracker:
    """Parse ~/.claude/ JSONL files to aggregate token usage."""

    def __init__(self):
        self.claude_dir = CLAUDE_DIR
        self.projects_dir = PROJECTS_DIR

    # ── file discovery ──────────────────────────────────────────────────

    def _jsonl_files(self):
        files = []
        if self.projects_dir.exists():
            for p in self.projects_dir.rglob("*.jsonl"):
                files.append(p)
        g = self.claude_dir / "history.jsonl"
        if g.exists():
            files.append(g)
        return files

    # ── parsing ─────────────────────────────────────────────────────────

    def _parse_ts(self, raw):
        """Best-effort timestamp parse → naive LOCAL datetime."""
        if raw is None:
            return None
        try:
            if isinstance(raw, (int, float)):
                ts = raw / 1000 if raw > 1e12 else raw
                return datetime.fromtimestamp(ts)  # already local
            # ISO format like "2026-02-26T08:15:35.652Z" — UTC
            s = str(raw)
            if s.endswith("Z"):
                dt_utc = datetime.fromisoformat(s.replace("Z", "+00:00"))
                # Convert UTC → local time
                return dt_utc.astimezone(None).replace(tzinfo=None)
            elif "+" in s or (s.count("-") > 2):
                dt_aware = datetime.fromisoformat(s)
                return dt_aware.astimezone(None).replace(tzinfo=None)
            else:
                return datetime.fromisoformat(s)
        except Exception:
            return None

    def _extract_usage(self, entry):
        """Return dict with token fields or None.
        Only count entries that have stop_reason (= final message),
        to avoid double-counting streaming intermediate entries.
        """
        msg = entry.get("message", {}) if isinstance(entry.get("message"), dict) else {}

        # Skip intermediate streaming entries (stop_reason is null)
        # Only count the FINAL entry per assistant message
        if msg.get("role") == "assistant" and msg.get("stop_reason") is None:
            return None

        # Get the unique message ID for deduplication
        msg_id = msg.get("id") or entry.get("requestId") or entry.get("uuid")

        # direct usage field
        for container in (entry, msg):
            u = container.get("usage") if isinstance(container, dict) else None
            if isinstance(u, dict) and ("input_tokens" in u or "output_tokens" in u):
                return {
                    "input":        u.get("input_tokens", 0),
                    "output":       u.get("output_tokens", 0),
                    "cache_write":  u.get("cache_creation_input_tokens", 0),
                    "cache_read":   u.get("cache_read_input_tokens", 0),
                    "model":        container.get("model") or entry.get("model", "default"),
                    "msg_id":       msg_id,
                }
        # costUSD / camelCase fields
        if "costUSD" in entry or "inputTokens" in entry:
            return {
                "input":        entry.get("inputTokens", 0),
                "output":       entry.get("outputTokens", 0),
                "cache_write":  entry.get("cacheCreationInputTokens", 0),
                "cache_read":   entry.get("cacheReadInputTokens", 0),
                "model":        entry.get("model", "default"),
                "cost_override": entry.get("costUSD"),
                "msg_id":       msg_id,
            }
        return None

    def _cost_of(self, rec):
        if rec.get("cost_override") is not None:
            return rec["cost_override"]
        model = rec.get("model", "default")
        pr = PRICING.get(model)
        if pr is None:
            for k, v in PRICING.items():
                if k in model or model in k:
                    pr = v
                    break
        pr = pr or PRICING["default"]
        return (
            rec["input"]       / 1e6 * pr["input"]
            + rec["output"]    / 1e6 * pr["output"]
            + rec["cache_write"] / 1e6 * pr["cache_write"]
            + rec["cache_read"]  / 1e6 * pr["cache_read"]
        )

    # ── aggregation ─────────────────────────────────────────────────────

    def query(self, since=None):
        """Return aggregated usage since *since* (datetime, naive local).
        Deduplicates by message ID — only the LAST entry per msg counts.
        """
        # First pass: collect records, dedup by msg_id (keep last seen)
        seen = {}       # msg_id → rec
        no_id_recs = [] # records without a msg_id

        for fp in self._jsonl_files():
            if since:
                try:
                    if datetime.fromtimestamp(fp.stat().st_mtime) < since:
                        continue
                except OSError:
                    continue
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts_raw = entry.get("timestamp") or entry.get("createdAt") or entry.get("time")
                        dt = self._parse_ts(ts_raw)
                        if since and dt and dt < since:
                            continue
                        rec = self._extract_usage(entry)
                        if rec is None:
                            continue
                        mid = rec.pop("msg_id", None)
                        if mid:
                            seen[mid] = rec  # overwrite → keeps last
                        else:
                            no_id_recs.append(rec)
            except (PermissionError, FileNotFoundError):
                continue

        # Second pass: aggregate
        totals = defaultdict(int)
        totals["cost"] = 0.0
        model_map = defaultdict(lambda: defaultdict(int))
        model_map_cost = defaultdict(float)

        for rec in list(seen.values()) + no_id_recs:
            for k in ("input", "output", "cache_write", "cache_read"):
                totals[k] += rec[k]
            c = self._cost_of(rec)
            totals["cost"] += c
            totals["requests"] += 1
            m = rec["model"]
            for k in ("input", "output", "cache_write", "cache_read"):
                model_map[m][k] += rec[k]
            model_map_cost[m] += c

        totals["total"] = totals["input"] + totals["output"] + totals["cache_write"] + totals["cache_read"]
        return dict(totals), dict(model_map), dict(model_map_cost)

    # ── convenience ─────────────────────────────────────────────────────

    def session_usage(self):
        """Usage within the current 5-hour session window."""
        since = datetime.now() - timedelta(hours=SESSION_WINDOW_HOURS)
        return self.query(since)

    def week_usage(self):
        """Usage within the current 7-day window."""
        since = datetime.now() - timedelta(days=WEEK_WINDOW_DAYS)
        return self.query(since)

    def today_usage(self):
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.query(since)

    def month_usage(self):
        since = datetime.now() - timedelta(days=30)
        return self.query(since)


# ─── Reset-time helpers ──────────────────────────────────────────────────────

def _next_session_reset():
    """Rough estimate: session resets 5 h from now (rolling window)."""
    return datetime.now() + timedelta(hours=SESSION_WINDOW_HOURS)

def _next_week_reset():
    """Weekly limit resets 7 days from the oldest request in the window."""
    return datetime.now() + timedelta(days=WEEK_WINDOW_DAYS)

def _fmt_reset(dt):
    """Format reset time like '8pm (Asia/Seoul)' or 'Mar 5, 10am'."""
    now = datetime.now()
    tz_name = time.tzname[time.daylight] if time.daylight else time.tzname[0]
    if dt.date() == now.date():
        return dt.strftime("%-I%p").lower() + f" ({tz_name})"
    return dt.strftime("%b %-d, %-I%p").lower().replace("am", "am").replace("pm", "pm") + f" ({tz_name})"


# ─── Menu Bar App ────────────────────────────────────────────────────────────

class ClaudeUsageApp(rumps.App):
    def __init__(self):
        super().__init__(name="Claude Usage", title="⚡ Claude", quit_button=None)
        self.tracker = UsageTracker()
        self.view_mode = "dashboard"  # dashboard | detail
        # Defer heavy work (API calls, file I/O) to after the run loop starts
        self.menu.add(rumps.MenuItem("로딩 중...", callback=None))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("종료", callback=rumps.quit_application))

    @rumps.timer(1)
    def _initial_load(self, timer):
        """First load after run loop starts, then switch to normal refresh interval."""
        timer.stop()
        try:
            self._rebuild()
        except Exception:
            pass
        self._refresh_timer = rumps.Timer(self._on_tick, REFRESH_INTERVAL_SEC)
        self._refresh_timer.start()

    # ── menu builders ───────────────────────────────────────────────────

    def _rebuild(self):
        self.menu.clear()
        try:
            self._build_dashboard()
        except Exception as e:
            self.menu.add(rumps.MenuItem(f"⚠️ 오류: {e}", callback=None))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("🔄 새로고침", callback=self._refresh))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("종료", callback=rumps.quit_application))

    def _build_dashboard(self):
        sess_totals, sess_models, sess_costs = self.tracker.session_usage()
        week_totals, week_models, week_costs = self.tracker.week_usage()
        today_totals, _, _ = self.tracker.today_usage()

        # ── fetch real usage from API ──
        api = fetch_usage_api()

        sess_tokens = sess_totals.get("total", 0)
        week_tokens = week_totals.get("total", 0)

        if api and "five_hour" in api:
            sess_pct = api["five_hour"].get("utilization", 0)
            sess_reset = _parse_reset_time(api["five_hour"].get("resets_at"))
            week_pct = api.get("seven_day", {}).get("utilization", 0) if api.get("seven_day") else 0
            week_reset = _parse_reset_time((api.get("seven_day") or {}).get("resets_at"))
            sonnet_data = api.get("seven_day_sonnet")
            sonnet_pct = sonnet_data.get("utilization", 0) if sonnet_data else None
            sonnet_reset = _parse_reset_time(sonnet_data.get("resets_at")) if sonnet_data else None
        else:
            # fallback: rough local estimates
            sess_pct = min(sess_tokens / 5_000_000 * 100, 100)
            week_pct = min(week_tokens / 50_000_000 * 100, 100)
            sess_reset = _next_session_reset()
            week_reset = _next_week_reset()
            sonnet_pct = None
            sonnet_reset = None

        # ── update title bar (weekly) ──
        self.title = f"⚡ {week_pct:.0f}%"

        # ── Current session ──
        self.menu.add(rumps.MenuItem("Current session", callback=None))
        self.menu.add(rumps.MenuItem(f"  {make_bar(sess_pct)}", callback=None))
        if sess_reset:
            self.menu.add(rumps.MenuItem(
                f"  Resets {_fmt_reset(sess_reset)}",
                callback=None,
            ))
        self.menu.add(rumps.separator)

        # ── Current week (all models) ──
        self.menu.add(rumps.MenuItem("Current week (all models)", callback=None))
        self.menu.add(rumps.MenuItem(f"  {make_bar(week_pct)}", callback=None))
        if week_reset:
            self.menu.add(rumps.MenuItem(
                f"  Resets {_fmt_reset(week_reset)}",
                callback=None,
            ))
        self.menu.add(rumps.separator)

        # ── Current week (Sonnet only) ──
        if sonnet_pct is not None:
            self.menu.add(rumps.MenuItem("Current week (Sonnet only)", callback=None))
            self.menu.add(rumps.MenuItem(f"  {make_bar(sonnet_pct)}", callback=None))
            if sonnet_reset:
                self.menu.add(rumps.MenuItem(
                    f"  Resets {_fmt_reset(sonnet_reset)}",
                    callback=None,
                ))
            self.menu.add(rumps.separator)

        # ── Detail breakdown (submenu) ──
        detail = rumps.MenuItem("📊 세부 사용량")

        # Today
        detail.add(rumps.MenuItem("── 오늘 ──", callback=None))
        detail.add(rumps.MenuItem(
            f"  토큰  {fmt_tokens(today_totals.get('total', 0))}  "
            f"(입력 {fmt_tokens(today_totals.get('input', 0))} / 출력 {fmt_tokens(today_totals.get('output', 0))})",
            callback=None,
        ))
        detail.add(rumps.MenuItem(f"  비용  {fmt_cost(today_totals.get('cost', 0))}", callback=None))
        detail.add(rumps.MenuItem(f"  요청  {today_totals.get('requests', 0)}회", callback=None))

        # Session
        detail.add(rumps.separator)
        detail.add(rumps.MenuItem("── 세션 (5시간) ──", callback=None))
        detail.add(rumps.MenuItem(
            f"  토큰  {fmt_tokens(sess_tokens)}  "
            f"(입력 {fmt_tokens(sess_totals.get('input', 0))} / 출력 {fmt_tokens(sess_totals.get('output', 0))})",
            callback=None,
        ))
        if sess_totals.get("cache_write", 0) or sess_totals.get("cache_read", 0):
            detail.add(rumps.MenuItem(
                f"  캐시  생성 {fmt_tokens(sess_totals.get('cache_write', 0))} / 읽기 {fmt_tokens(sess_totals.get('cache_read', 0))}",
                callback=None,
            ))
        detail.add(rumps.MenuItem(f"  비용  {fmt_cost(sess_totals.get('cost', 0))}", callback=None))

        # Week
        detail.add(rumps.separator)
        detail.add(rumps.MenuItem("── 이번 주 (7일) ──", callback=None))
        detail.add(rumps.MenuItem(
            f"  토큰  {fmt_tokens(week_tokens)}  "
            f"(입력 {fmt_tokens(week_totals.get('input', 0))} / 출력 {fmt_tokens(week_totals.get('output', 0))})",
            callback=None,
        ))
        detail.add(rumps.MenuItem(f"  비용  {fmt_cost(week_totals.get('cost', 0))}", callback=None))
        detail.add(rumps.MenuItem(f"  요청  {week_totals.get('requests', 0)}회", callback=None))

        self.menu.add(detail)

        # ── Model breakdown (submenu) ──
        if week_models:
            model_menu = rumps.MenuItem("🤖 모델별")
            for m in sorted(week_costs, key=week_costs.get, reverse=True):
                d = week_models[m]
                total_t = sum(d.values())
                short = m.split("/")[-1] if "/" in m else m
                model_menu.add(rumps.MenuItem(
                    f"  {short}: {fmt_tokens(total_t)}  {fmt_cost(week_costs[m])}",
                    callback=None,
                ))
            self.menu.add(model_menu)

    # ── callbacks ───────────────────────────────────────────────────────

    def _refresh(self, _=None):
        self._rebuild()

    def _on_tick(self, _=None):
        try:
            self._rebuild()
        except Exception:
            pass


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    import sys
    print(f"[DEBUG] Python: {sys.version}", flush=True)
    print(f"[DEBUG] Executable: {sys.executable}", flush=True)
    print(f"[DEBUG] CLAUDE_DIR: {CLAUDE_DIR} exists={CLAUDE_DIR.exists()}", flush=True)
    if not CLAUDE_DIR.exists():
        rumps.alert(
            title="Claude Code를 찾을 수 없습니다",
            message="~/.claude/ 디렉토리가 없습니다.\nClaude Code가 설치되어 있는지 확인해주세요.",
        )
        return
    print("[DEBUG] Creating ClaudeUsageApp...", flush=True)
    app = ClaudeUsageApp()
    print(f"[DEBUG] App created, title={app.title}", flush=True)
    print("[DEBUG] Calling app.run()...", flush=True)
    app.run()


if __name__ == "__main__":
    main()
