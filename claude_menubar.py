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

APP_VERSION = "1.0.7"
GITHUB_REPO = "hmyanghm/claude-usage-menubar"
GITHUB_API_RELEASES = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
UPDATE_CHECK_INTERVAL = 3600  # check every 1 hour

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
REFRESH_INTERVAL_SEC = 300

CONFIG_PATH = CLAUDE_DIR / "menubar_config.json"
DEFAULT_CONFIG = {
    "title_display": "session",   # "session" | "week" | "both"
    "show_session": True,
    "show_week": True,
    "show_sonnet": True,
    "alert_enabled": True,        # usage threshold alerts on/off
}

# Alert thresholds (percent) — triggers macOS notification once per reset cycle
ALERT_THRESHOLDS = [80, 90]


def _load_config():
    """Load menubar config from JSON file, returning defaults if missing."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # Merge with defaults for any missing keys
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        return merged
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return dict(DEFAULT_CONFIG)


def _save_config(config):
    """Save menubar config to JSON file."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[CONFIG] Save error: {e}", flush=True)

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

# Estimated cost limits (USD) per plan tier — rough approximations
# These are used ONLY when the usage API is unavailable
TIER_LIMITS = {
    # tier_keyword:           (5h_session, 7d_week)
    "max_5x":                 (250.0, 2500.0),
    "max_20x":                (1000.0, 10000.0),
    "max":                    (50.0, 500.0),
    "pro":                    (5.0, 50.0),
}

# Cache for API responses — survives transient API failures
_api_cache = {
    "token_prefix": None,   # first 20 chars of token — detect account switch
    "data": None,           # last successful API response
    "fetched_at": 0,        # timestamp of last successful fetch
    "is_stale": False,      # True when using cached data after API failure
    "account_email": None,  # current account email
    "account_name": None,   # current account display name
    "rate_limit_tier": None,  # e.g. "default_claude_max_5x"
    "next_call_at": 0,      # earliest time to call usage API again
    "backoff_sec": 90,      # current backoff interval (grows on 429)
}

API_INTERVAL_OK = 300       # normal: call every 5 min
API_INTERVAL_MAX = 600      # max backoff: 10 min
API_CACHE_TTL = 600         # cache valid for 10 min

PROFILE_API_URL = "https://api.anthropic.com/api/oauth/profile"

# ─── Update checker ──────────────────────────────────────────────────────────

_update_cache = {
    "latest_version": None,
    "release_url": None,
    "checked_at": 0,
}


def _parse_version(tag):
    """Parse version string like 'v1.0.5' or '1.0.5' into tuple of ints."""
    tag = tag.lstrip("v")
    try:
        return tuple(int(x) for x in tag.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _check_update():
    """Check GitHub releases for a newer version. Returns (new_version, url) or (None, None)."""
    now = time.time()
    if now - _update_cache["checked_at"] < UPDATE_CHECK_INTERVAL:
        v = _update_cache["latest_version"]
        if v and _parse_version(v) > _parse_version(APP_VERSION):
            return v, _update_cache["release_url"]
        return None, None

    _update_cache["checked_at"] = now
    try:
        req = Request(GITHUB_API_RELEASES, headers={
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": f"claude-usage-menubar/{APP_VERSION}",
        })
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        tag = data.get("tag_name", "")
        url = data.get("html_url", GITHUB_RELEASES_PAGE)
        _update_cache["latest_version"] = tag
        _update_cache["release_url"] = url
        if _parse_version(tag) > _parse_version(APP_VERSION):
            print(f"[UPDATE] New version available: {tag}", flush=True)
            return tag, url
    except Exception as e:
        print(f"[UPDATE] Check failed: {e}", flush=True)
    return None, None


def _send_notification(title, message):
    """Send macOS notification via osascript."""
    try:
        script = f'display notification "{message}" with title "{title}" sound name "default"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception as e:
        print(f"[ALERT] Notification failed: {e}", flush=True)


def _open_url(url):
    """Open a URL in the default browser."""
    try:
        subprocess.run(["open", url], capture_output=True, timeout=5)
    except Exception as e:
        print(f"[URL] Failed to open: {e}", flush=True)
TOKEN_REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def _get_keychain_account():
    """Return the macOS Keychain account name for Claude Code credentials."""
    return os.environ.get("USER", "claude-code-user")


def _get_oauth_data():
    """Retrieve full OAuth data from macOS Keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", _get_keychain_account(),
             "-w", "-s", "Claude Code-credentials"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout.strip())
    except Exception:
        return None


def _save_oauth_data(data):
    """Save updated OAuth data back to macOS Keychain."""
    account = _get_keychain_account()
    service = "Claude Code-credentials"
    payload = json.dumps(data)
    try:
        # Delete old entry first (update not supported directly)
        subprocess.run(
            ["security", "delete-generic-password", "-a", account, "-s", service],
            capture_output=True, text=True, timeout=5,
        )
        # Add new entry
        subprocess.run(
            ["security", "add-generic-password", "-a", account, "-s", service,
             "-w", payload],
            capture_output=True, text=True, timeout=5,
        )
        print("[OAuth] Keychain updated with refreshed token", flush=True)
    except Exception as e:
        print(f"[OAuth] Keychain save error: {e}", flush=True)


def _is_token_expired(oauth_data):
    """Check if access token is expired or about to expire (within 5 min)."""
    expires_at = oauth_data.get("expiresAt")
    if not expires_at:
        return False
    # expiresAt is in milliseconds
    now_ms = int(time.time() * 1000)
    margin_ms = 5 * 60 * 1000  # 5 minutes margin
    return now_ms >= (expires_at - margin_ms)


def _refresh_oauth_token(keychain_data, oauth_data):
    """Refresh the OAuth access token using the refresh token.
    Returns new access token on success, None on failure.
    Updates Keychain with new tokens.
    """
    refresh_token = oauth_data.get("refreshToken")
    if not refresh_token:
        print("[OAuth] No refresh token available", flush=True)
        return None

    try:
        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }).encode()
        req = Request(TOKEN_REFRESH_URL, data=body, headers={
            "Content-Type": "application/json",
            "User-Agent": "claude-code/2.1.80",
        })
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())

        new_access = result.get("access_token")
        new_refresh = result.get("refresh_token")
        expires_in = result.get("expires_in", 28800)  # default 8h

        if not new_access:
            print("[OAuth] Refresh response missing access_token", flush=True)
            return None

        # Update oauth data
        updated_oauth = {
            **oauth_data,
            "accessToken": new_access,
            "expiresAt": int(time.time() * 1000) + (expires_in * 1000),
        }
        if new_refresh:
            updated_oauth["refreshToken"] = new_refresh

        # Update account info from response if available
        account_info = result.get("account", {})
        org_info = result.get("organization", {})
        if org_info.get("rateLimitTier"):
            updated_oauth["rateLimitTier"] = org_info["rateLimitTier"]
        if account_info.get("subscriptionType"):
            updated_oauth["subscriptionType"] = account_info["subscriptionType"]

        updated_data = {**keychain_data, "claudeAiOauth": updated_oauth}
        _save_oauth_data(updated_data)
        print(f"[OAuth] Token refreshed, expires in {expires_in}s", flush=True)
        return new_access

    except Exception as e:
        print(f"[OAuth] Token refresh failed: {e}", flush=True)
        return None


def _get_oauth_token():
    """Retrieve Claude Code OAuth access token from macOS Keychain.
    Automatically refreshes if expired.
    """
    data = _get_oauth_data()
    if not data:
        return None
    oauth = data.get("claudeAiOauth", {})
    # Cache the rate limit tier
    tier = oauth.get("rateLimitTier")
    if tier:
        _api_cache["rate_limit_tier"] = tier

    token = oauth.get("accessToken")
    if not token:
        return None

    # Auto-refresh if expired
    if _is_token_expired(oauth):
        print("[OAuth] Token expired, attempting refresh...", flush=True)
        new_token = _refresh_oauth_token(data, oauth)
        if new_token:
            return new_token
        # Refresh failed — return old token anyway (might still work)

    return token


def _estimate_limits():
    """Return (session_limit_usd, week_limit_usd) based on plan tier."""
    tier = (_api_cache.get("rate_limit_tier") or "").lower()
    for keyword, limits in TIER_LIMITS.items():
        if keyword in tier:
            return limits
    # Unknown tier — use pro as conservative default
    return TIER_LIMITS["pro"]


def _fetch_profile(token):
    """Fetch account profile (email, name) from Anthropic API."""
    try:
        req = Request(PROFILE_API_URL, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-beta": ANTHROPIC_BETA,
            "User-Agent": "claude-code/2.1.59",
        })
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            acct = data.get("account", {})
            return acct.get("email"), acct.get("display_name") or acct.get("full_name")
    except Exception:
        return None, None


def fetch_usage_api(force=False):
    """Call Anthropic usage API with caching, backoff, and account-switch detection.
    Returns (data_dict | None, is_stale: bool).
    If force=True, ignore backoff and call API immediately.
    """
    token = _get_oauth_token()
    if not token:
        return None, False

    # Detect account switch → clear cache and refresh profile
    token_prefix = token[:20]
    if _api_cache["token_prefix"] != token_prefix:
        _api_cache["data"] = None
        _api_cache["fetched_at"] = 0
        _api_cache["is_stale"] = False
        _api_cache["next_call_at"] = 0
        _api_cache["backoff_sec"] = API_INTERVAL_OK
        # Fetch new account profile
        email, name = _fetch_profile(token)
        _api_cache["account_email"] = email
        _api_cache["account_name"] = name
    _api_cache["token_prefix"] = token_prefix

    now = time.time()

    # Respect backoff — skip API call if too soon
    if not force and now < _api_cache["next_call_at"]:
        if _api_cache["data"] and (now - _api_cache["fetched_at"]) < API_CACHE_TTL:
            return _api_cache["data"], _api_cache["is_stale"]
        return None, False

    # Make the API call
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "anthropic-beta": ANTHROPIC_BETA,
        "User-Agent": "claude-code/2.1.59",
    }
    try:
        req = Request(USAGE_API_URL, headers=headers)
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            _api_cache["data"] = data
            _api_cache["fetched_at"] = time.time()
            _api_cache["is_stale"] = False
            # Success → reset to normal interval
            _api_cache["backoff_sec"] = API_INTERVAL_OK
            _api_cache["next_call_at"] = time.time() + API_INTERVAL_OK
            return data, False
    except URLError as e:
        print(f"[API] URLError: {e}", flush=True)
        status = getattr(e, "code", None) or getattr(getattr(e, "reason", None), "status", None)
        if status == 401 and not force:
            # Token expired during runtime — force refresh and retry once
            print("[API] 401 Unauthorized — attempting token refresh", flush=True)
            kc_data = _get_oauth_data()
            if kc_data:
                oauth = kc_data.get("claudeAiOauth", {})
                new_token = _refresh_oauth_token(kc_data, oauth)
                if new_token:
                    _api_cache["token_prefix"] = new_token[:20]
                    _api_cache["next_call_at"] = 0  # allow immediate retry
                    # Retry with the new token (one retry only via force=True)
                    return fetch_usage_api(force=True)
            _api_cache["next_call_at"] = time.time() + API_INTERVAL_OK
        elif status == 401 and force:
            # Already retried once with refreshed token, don't loop
            print("[API] 401 persists after token refresh, backing off", flush=True)
            _api_cache["next_call_at"] = time.time() + API_INTERVAL_OK
        elif status == 429:
            # Use retry-after header if available, otherwise exponential backoff
            retry_after = None
            if hasattr(e, "headers"):
                retry_after = e.headers.get("retry-after")
            if retry_after:
                try:
                    wait = max(int(retry_after), API_INTERVAL_OK)
                    print(f"[API] retry-after: {retry_after}s → waiting {wait}s", flush=True)
                    _api_cache["next_call_at"] = time.time() + wait
                    _api_cache["backoff_sec"] = wait
                except ValueError:
                    _api_cache["backoff_sec"] = min(_api_cache["backoff_sec"] * 2, API_INTERVAL_MAX)
                    _api_cache["next_call_at"] = time.time() + _api_cache["backoff_sec"]
            else:
                _api_cache["backoff_sec"] = min(_api_cache["backoff_sec"] * 2, API_INTERVAL_MAX)
                _api_cache["next_call_at"] = time.time() + _api_cache["backoff_sec"]
        else:
            _api_cache["next_call_at"] = time.time() + API_INTERVAL_OK
    except Exception as e:
        print(f"[API] Error: {e}", flush=True)
        _api_cache["next_call_at"] = time.time() + API_INTERVAL_OK

    # Return cached data if still fresh
    if _api_cache["data"] and (time.time() - _api_cache["fetched_at"]) < API_CACHE_TTL:
        _api_cache["is_stale"] = True
        return _api_cache["data"], True
    return None, False


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


SPARK_CHARS = "▁▂▃▄▅▆▇█"

def make_sparkline(values):
    """Create a sparkline string from a list of numbers."""
    if not values or max(values) == 0:
        return "▁" * len(values)
    hi = max(values)
    return "".join(
        SPARK_CHARS[min(int(v / hi * (len(SPARK_CHARS) - 1)), len(SPARK_CHARS) - 1)]
        for v in values
    )


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

    def daily_costs(self, days=7):
        """Return list of (date_str, cost_usd) for the last N days."""
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        # Collect per-day cost buckets
        day_costs = defaultdict(float)
        for fp in self._jsonl_files():
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
                        if not dt or dt < since:
                            continue
                        rec = self._extract_usage(entry)
                        if rec is None:
                            continue
                        rec.pop("msg_id", None)
                        day_costs[dt.strftime("%m/%d")] += self._cost_of(rec)
            except (PermissionError, FileNotFoundError):
                continue

        # Build ordered list for the last N days
        result = []
        for i in range(days):
            d = since + timedelta(days=i)
            key = d.strftime("%m/%d")
            result.append((key, day_costs.get(key, 0.0)))
        return result


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
        # Track which alert thresholds have already fired per reset cycle
        # Keys: "session", "week" — Values: set of thresholds already notified
        self._alerted = {"session": set(), "week": set()}
        # Store last known reset times to detect new cycles
        self._last_reset = {"session": None, "week": None}
        # Defer heavy work (API calls, file I/O) to after the run loop starts
        self.menu.add(rumps.MenuItem("로딩 중...", callback=None))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("종료", callback=rumps.quit_application))

    @rumps.timer(1)
    def _initial_load(self, timer):
        """First load after run loop starts, then switch to normal refresh interval."""
        timer.stop()
        # Hide Dock icon (must be after run loop starts)
        try:
            import AppKit
            AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        except Exception:
            pass
        # Register wake-from-sleep notification
        self._register_wake_observer()
        try:
            self._rebuild()
        except Exception as e:
            print(f"[DEBUG] _initial_load error: {e}", flush=True)
        self._refresh_timer = rumps.Timer(self._on_tick, REFRESH_INTERVAL_SEC)
        self._refresh_timer.start()

    def _register_wake_observer(self):
        """Listen for macOS wake-from-sleep to trigger immediate refresh."""
        try:
            import AppKit
            import objc

            self._wake_retry_count = 0
            self._wake_retry_timer = None
            self._last_active_time = time.time()

            # Store callback as instance attribute to prevent GC
            def on_wake(_notification):
                print("[WAKE] System woke from sleep, scheduling refresh...", flush=True)
                self._wake_retry_count = 0
                self._start_wake_retry()

            self._on_wake_callback = on_wake  # prevent garbage collection

            nc = AppKit.NSWorkspace.sharedWorkspace().notificationCenter()
            nc.addObserverForName_object_queue_usingBlock_(
                "NSWorkspaceDidWakeNotification",
                None,
                None,
                self._on_wake_callback,
            )
            print("[WAKE] Registered wake observer", flush=True)
        except Exception as e:
            print(f"[WAKE] Failed to register wake observer: {e}", flush=True)

    def _start_wake_retry(self):
        """Start retry cycle after wake. Delays: 5s, 10s, 20s, 40s, 60s (max 5 retries)."""
        if self._wake_retry_timer is not None:
            self._wake_retry_timer.stop()
        delays = [5, 10, 20, 40, 60]
        delay = delays[min(self._wake_retry_count, len(delays) - 1)]
        print(f"[WAKE] Retry #{self._wake_retry_count + 1} in {delay}s...", flush=True)
        self._wake_retry_timer = rumps.Timer(self._wake_refresh, delay)
        self._wake_retry_timer.start()

    def _wake_refresh(self, timer):
        """Called after wake-from-sleep. Retries on failure until network is back."""
        timer.stop()
        self._wake_retry_timer = None
        max_retries = 5
        try:
            # Skip network pre-check, just rebuild directly
            print("[WAKE] Attempting refresh...", flush=True)
            self._rebuild(force_api=True)
            self._wake_retry_count = 0
            print("[WAKE] Refresh successful", flush=True)
        except Exception as e:
            self._wake_retry_count += 1
            if self._wake_retry_count < max_retries:
                print(f"[WAKE] Refresh failed ({e}), will retry...", flush=True)
                self._start_wake_retry()
            else:
                print(f"[WAKE] Max retries reached, giving up. Next regular tick will retry.", flush=True)
                self._wake_retry_count = 0

    # ── menu builders ───────────────────────────────────────────────────

    def _rebuild(self, force_api=False):
        self._last_active_time = time.time()
        self.menu.clear()
        try:
            self._build_dashboard(force_api=force_api)
        except Exception as e:
            self.menu.add(rumps.MenuItem(f"⚠️ 오류: {e}", callback=None))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("🔄 새로고침", callback=self._refresh))
        self._add_settings_menu()
        # Update check
        new_ver, release_url = _check_update()
        if new_ver:
            self.menu.add(rumps.MenuItem(
                f"🔔 업데이트 {new_ver} 사용 가능",
                callback=lambda _: _open_url(release_url),
            ))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem(f"v{APP_VERSION}", callback=None))
        self.menu.add(rumps.MenuItem("종료", callback=rumps.quit_application))

    def _build_dashboard(self, force_api=False):
        config = _load_config()

        sess_totals, sess_models, sess_costs = self.tracker.session_usage()
        week_totals, week_models, week_costs = self.tracker.week_usage()
        today_totals, _, _ = self.tracker.today_usage()

        # ── fetch real usage from API ──
        api, api_stale = fetch_usage_api(force=force_api)

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
            # API unavailable — estimate from local cost data
            sess_limit, week_limit = _estimate_limits()
            sess_cost = sess_totals.get("cost", 0)
            week_cost = week_totals.get("cost", 0)
            sess_pct = min(sess_cost / sess_limit * 100, 100) if sess_limit else 0
            week_pct = min(week_cost / week_limit * 100, 100) if week_limit else 0
            sess_reset = None
            week_reset = None
            sonnet_pct = None
            sonnet_reset = None

        api_ok = api and "five_hour" in api

        # ── check threshold alerts ──
        self._check_alerts(sess_pct, sess_reset, week_pct, week_reset)

        # ── update title bar based on config ──
        stale_mark = "~" if (api_stale or not api_ok) else ""
        title_mode = config.get("title_display", "session")
        if title_mode == "week":
            self.title = f"⚡ {stale_mark}{week_pct:.0f}%"
        elif title_mode == "both":
            self.title = f"⚡ {stale_mark}{sess_pct:.0f}% | {week_pct:.0f}%"
        else:  # "session" (default)
            self.title = f"⚡ {stale_mark}{sess_pct:.0f}%"

        # ── Account info ──
        acct_email = _api_cache.get("account_email")
        acct_name = _api_cache.get("account_name")
        if acct_email:
            label = f"👤 {acct_name} ({acct_email})" if acct_name else f"👤 {acct_email}"
            self.menu.add(rumps.MenuItem(label, callback=None))
            self.menu.add(rumps.separator)

        # ── API status indicator ──
        if not api:
            self.menu.add(rumps.MenuItem("⚠️ Usage API 일시 장애", callback=None))
            self.menu.add(rumps.separator)
        elif api_stale:
            ago = int(time.time() - _api_cache["fetched_at"])
            self.menu.add(rumps.MenuItem(f"⏳ 캐시 데이터 ({ago}초 전)", callback=None))
            self.menu.add(rumps.separator)

        # ── Current session ──
        est = "" if api_ok else " (예상)"
        if config.get("show_session", True):
            self.menu.add(rumps.MenuItem(f"Current session{est}", callback=None))
            self.menu.add(rumps.MenuItem(f"  {make_bar(sess_pct)}", callback=None))
            if sess_reset:
                self.menu.add(rumps.MenuItem(
                    f"  Resets {_fmt_reset(sess_reset)}",
                    callback=None,
                ))
            self.menu.add(rumps.separator)

        # ── Current week (all models) ──
        if config.get("show_week", True):
            self.menu.add(rumps.MenuItem(f"Current week (all models){est}", callback=None))
            self.menu.add(rumps.MenuItem(f"  {make_bar(week_pct)}", callback=None))
            if week_reset:
                self.menu.add(rumps.MenuItem(
                    f"  Resets {_fmt_reset(week_reset)}",
                    callback=None,
                ))
            self.menu.add(rumps.separator)

        # ── Current week (Sonnet only) ──
        if config.get("show_sonnet", True) and sonnet_pct is not None:
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

        # ── Usage history sparkline (submenu) ──
        daily = self.tracker.daily_costs(7)
        costs = [c for _, c in daily]
        spark = make_sparkline(costs)
        total_7d = sum(costs)
        history_menu = rumps.MenuItem(f"📈 7일  {spark}  {fmt_cost(total_7d)}")
        for date_str, cost in daily:
            bar_len = 0
            if max(costs) > 0:
                bar_len = round(cost / max(costs) * 10)
            bar = "█" * bar_len + "░" * (10 - bar_len)
            history_menu.add(rumps.MenuItem(
                f"  {date_str}  {bar}  {fmt_cost(cost)}",
                callback=None,
            ))
        self.menu.add(history_menu)

    # ── threshold alerts ─────────────────────────────────────────────────

    def _check_alerts(self, sess_pct, sess_reset, week_pct, week_reset):
        """Send macOS notification when usage crosses a threshold.
        Each threshold fires only once per reset cycle.
        """
        config = _load_config()
        if not config.get("alert_enabled", True):
            return

        checks = [
            ("session", "세션(5h)", sess_pct, sess_reset),
            ("week", "주간(7d)", week_pct, week_reset),
        ]
        for key, label, pct, reset_time in checks:
            # Detect reset cycle change → clear previous alerts
            if reset_time != self._last_reset[key]:
                self._alerted[key] = set()
                self._last_reset[key] = reset_time

            for threshold in ALERT_THRESHOLDS:
                if pct >= threshold and threshold not in self._alerted[key]:
                    self._alerted[key].add(threshold)
                    reset_msg = f" | 리셋: {_fmt_reset(reset_time)}" if reset_time else ""
                    _send_notification(
                        title=f"⚡ Claude 사용량 {threshold}% 도달",
                        message=f"{label} 사용률: {pct:.0f}%{reset_msg}",
                    )
                    print(f"[ALERT] {label} {pct:.0f}% >= {threshold}%", flush=True)

    # ── settings menu ────────────────────────────────────────────────────

    def _add_settings_menu(self):
        """Build the ⚙️ 표시 설정 submenu."""
        config = _load_config()
        settings = rumps.MenuItem("⚙️ 표시 설정")

        title_mode = config.get("title_display", "session")

        # Title display options (radio-style with check marks)
        opt_session = rumps.MenuItem(
            f"{'✓ ' if title_mode == 'session' else '   '}타이틀: 세션",
            callback=lambda _: self._set_title_display("session"),
        )
        opt_week = rumps.MenuItem(
            f"{'✓ ' if title_mode == 'week' else '   '}타이틀: 주간",
            callback=lambda _: self._set_title_display("week"),
        )
        opt_both = rumps.MenuItem(
            f"{'✓ ' if title_mode == 'both' else '   '}타이틀: 둘 다",
            callback=lambda _: self._set_title_display("both"),
        )
        settings.add(opt_session)
        settings.add(opt_week)
        settings.add(opt_both)
        settings.add(rumps.separator)

        # Dropdown section toggles
        show_sess = config.get("show_session", True)
        show_week = config.get("show_week", True)
        show_sonnet = config.get("show_sonnet", True)

        settings.add(rumps.MenuItem(
            f"{'✓ ' if show_sess else '   '}세션 표시",
            callback=lambda _: self._toggle_config("show_session"),
        ))
        settings.add(rumps.MenuItem(
            f"{'✓ ' if show_week else '   '}주간 표시",
            callback=lambda _: self._toggle_config("show_week"),
        ))
        settings.add(rumps.MenuItem(
            f"{'✓ ' if show_sonnet else '   '}소넷 표시",
            callback=lambda _: self._toggle_config("show_sonnet"),
        ))
        settings.add(rumps.separator)

        # Alert toggle
        alert_on = config.get("alert_enabled", True)
        settings.add(rumps.MenuItem(
            f"{'✓ ' if alert_on else '   '}사용량 알림 (80%/90%)",
            callback=lambda _: self._toggle_config("alert_enabled"),
        ))

        self.menu.add(settings)

    def _set_title_display(self, mode):
        config = _load_config()
        config["title_display"] = mode
        _save_config(config)
        self._rebuild()

    def _toggle_config(self, key):
        config = _load_config()
        config[key] = not config.get(key, True)
        _save_config(config)
        self._rebuild()

    # ── callbacks ───────────────────────────────────────────────────────

    def _refresh(self, _=None):
        self._rebuild(force_api=True)

    def _on_tick(self, _=None):
        try:
            # Detect wake-from-sleep via elapsed time gap
            # If more time passed than 2x the refresh interval, we likely slept
            now = time.time()
            last = getattr(self, "_last_active_time", now)
            self._last_active_time = now
            was_sleeping = (now - last) > REFRESH_INTERVAL_SEC * 2
            if was_sleeping:
                print(f"[WAKE] Detected sleep gap ({now - last:.0f}s), forcing API refresh...", flush=True)
            self._rebuild(force_api=was_sleeping)
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
