#!/usr/bin/env python3
"""
Claude Code Usage Monitor — Windows System Tray App
Shows Claude Code usage with visual progress bars, just like /usage.
Uses the Anthropic OAuth API for accurate rate-limit data,
and reads local JSONL session files for detailed token/cost breakdowns.

Requirements: pip install pystray Pillow
"""

import json
import os
import sys
import time
import ctypes
import ctypes.wintypes
import struct
import threading
import webbrowser
import subprocess
import tempfile
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
from urllib.request import Request, urlopen
from urllib.error import URLError

import tkinter as tk

import pystray
from PIL import Image, ImageDraw, ImageFont


# ─── Configuration ───────────────────────────────────────────────────────────

APP_VERSION = "2.1.0"
GITHUB_REPO = "hmyanghm/claude-usage-menubar"
GITHUB_API_RELEASES = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
PLATFORM_TAG_SUFFIX = "-win"  # only check releases tagged like v1.0.9-win
UPDATE_CHECK_INTERVAL = 3600

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
REFRESH_INTERVAL_SEC = 300

CONFIG_PATH = CLAUDE_DIR / "menubar_config.json"
DEFAULT_CONFIG = {
    "title_display": "session",
    "show_session": True,
    "show_week": True,
    "show_sonnet": True,
    "alert_enabled": True,
    "animation_enabled": True,
    "show_widget": True,
    "widget_x": None,
    "widget_y": None,
    "widget_theme": "light",
}

ALERT_THRESHOLDS = [80, 90]


def _load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        return merged
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return dict(DEFAULT_CONFIG)


def _save_config(config):
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[CONFIG] Save error: {e}", flush=True)


USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_BETA = "oauth-2025-04-20"

SESSION_WINDOW_HOURS = 5
WEEK_WINDOW_DAYS = 7

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

TIER_LIMITS = {
    "max_5x":  (250.0, 2500.0),
    "max_20x": (1000.0, 10000.0),
    "max":     (50.0, 500.0),
    "pro":     (5.0, 50.0),
}

_api_cache = {
    "token_prefix": None,
    "data": None,
    "fetched_at": 0,
    "is_stale": False,
    "account_email": None,
    "account_name": None,
    "rate_limit_tier": None,
    "next_call_at": 0,
    "backoff_sec": 90,
}

API_INTERVAL_OK = 300
API_INTERVAL_MAX = 600
API_CACHE_TTL = 600

PROFILE_API_URL = "https://api.anthropic.com/api/oauth/profile"
TOKEN_REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


# ─── Windows Credential Manager ─────────────────────────────────────────────

def _get_oauth_data():
    """Retrieve OAuth data from credentials file or Windows Credential Manager."""
    # 1) Try reading from ~/.claude/.credentials.json (Claude Code's default on Windows)
    creds_file = CLAUDE_DIR / ".credentials.json"
    try:
        with open(creds_file, "r", encoding="utf-8") as f:
            file_data = json.load(f)
        oauth = file_data.get("claudeAiOauth")
        if oauth and oauth.get("accessToken"):
            print("[OAuth] Loaded from .credentials.json", flush=True)
            return file_data
    except (FileNotFoundError, json.JSONDecodeError, PermissionError, KeyError) as e:
        print(f"[OAuth] .credentials.json read failed: {e}", flush=True)

    # 2) Fallback: Windows Credential Manager
    try:
        return _cred_read_win32("Claude Code-credentials")
    except Exception as e:
        print(f"[OAuth] Credential read error: {e}", flush=True)
        return None


def _cred_read_win32(target_name):
    """Read a credential from Windows Credential Manager using Win32 API."""
    advapi32 = ctypes.windll.advapi32

    CRED_TYPE_GENERIC = 1

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.wintypes.DWORD),
            ("Type", ctypes.wintypes.DWORD),
            ("TargetName", ctypes.wintypes.LPWSTR),
            ("Comment", ctypes.wintypes.LPWSTR),
            ("LastWritten", ctypes.wintypes.FILETIME),
            ("CredentialBlobSize", ctypes.wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", ctypes.wintypes.DWORD),
            ("AttributeCount", ctypes.wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", ctypes.wintypes.LPWSTR),
            ("UserName", ctypes.wintypes.LPWSTR),
        ]

    pcred = ctypes.POINTER(CREDENTIAL)()
    ok = advapi32.CredReadW(target_name, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred))
    if not ok:
        print("[OAuth] CredReadW failed - no credential found", flush=True)
        return None

    try:
        cred = pcred.contents
        blob_size = cred.CredentialBlobSize
        if blob_size == 0:
            return None
        blob_bytes = bytes(ctypes.cast(
            cred.CredentialBlob,
            ctypes.POINTER(ctypes.c_byte * blob_size)
        ).contents)
        # Credential blob is UTF-16LE encoded on Windows
        try:
            password = blob_bytes.decode("utf-16-le")
        except UnicodeDecodeError:
            password = blob_bytes.decode("utf-8", errors="replace")
        return json.loads(password)
    finally:
        advapi32.CredFree(pcred)


def _save_oauth_data(data):
    """Save updated OAuth data to ~/.claude/.credentials.json."""
    creds_file = CLAUDE_DIR / ".credentials.json"
    try:
        # Read existing file to preserve other keys
        existing = {}
        if creds_file.exists():
            with open(creds_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        existing["claudeAiOauth"] = data
        with open(creds_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        print("[OAuth] .credentials.json updated", flush=True)
    except Exception as e:
        print(f"[OAuth] Credential save error: {e}", flush=True)


def _is_token_expired(oauth_data):
    expires_at = oauth_data.get("expiresAt")
    if not expires_at:
        return False
    now_ms = int(time.time() * 1000)
    margin_ms = 5 * 60 * 1000
    return now_ms >= (expires_at - margin_ms)


def _refresh_oauth_token(keychain_data, oauth_data):
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
        expires_in = result.get("expires_in", 28800)

        if not new_access:
            return None

        updated_oauth = {
            **oauth_data,
            "accessToken": new_access,
            "expiresAt": int(time.time() * 1000) + (expires_in * 1000),
        }
        if new_refresh:
            updated_oauth["refreshToken"] = new_refresh

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
    data = _get_oauth_data()
    if not data:
        return None
    oauth = data.get("claudeAiOauth", {})
    tier = oauth.get("rateLimitTier")
    if tier:
        _api_cache["rate_limit_tier"] = tier

    token = oauth.get("accessToken")
    if not token:
        return None

    if _is_token_expired(oauth):
        print("[OAuth] Token expired, attempting refresh...", flush=True)
        new_token = _refresh_oauth_token(data, oauth)
        if new_token:
            return new_token

    return token


def _estimate_limits():
    tier = (_api_cache.get("rate_limit_tier") or "").lower()
    for keyword, limits in TIER_LIMITS.items():
        if keyword in tier:
            return limits
    return TIER_LIMITS["pro"]


def _fetch_profile(token):
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
    token = _get_oauth_token()
    if not token:
        return None, False

    token_prefix = token[:20]
    if _api_cache["token_prefix"] != token_prefix:
        _api_cache["data"] = None
        _api_cache["fetched_at"] = 0
        _api_cache["is_stale"] = False
        _api_cache["next_call_at"] = 0
        _api_cache["backoff_sec"] = API_INTERVAL_OK
        email, name = _fetch_profile(token)
        _api_cache["account_email"] = email
        _api_cache["account_name"] = name
    _api_cache["token_prefix"] = token_prefix

    now = time.time()

    if not force and now < _api_cache["next_call_at"]:
        if _api_cache["data"] and (now - _api_cache["fetched_at"]) < API_CACHE_TTL:
            return _api_cache["data"], _api_cache["is_stale"]
        return None, False

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
            _api_cache["backoff_sec"] = API_INTERVAL_OK
            _api_cache["next_call_at"] = time.time() + API_INTERVAL_OK
            return data, False
    except URLError as e:
        print(f"[API] URLError: {e}", flush=True)
        status = getattr(e, "code", None) or getattr(getattr(e, "reason", None), "status", None)
        if status == 401 and not force:
            print("[API] 401 Unauthorized — attempting token refresh", flush=True)
            kc_data = _get_oauth_data()
            if kc_data:
                oauth = kc_data.get("claudeAiOauth", {})
                new_token = _refresh_oauth_token(kc_data, oauth)
                if new_token:
                    _api_cache["token_prefix"] = new_token[:20]
                    _api_cache["next_call_at"] = 0
                    return fetch_usage_api(force=True)
            _api_cache["next_call_at"] = time.time() + API_INTERVAL_OK
        elif status == 429:
            retry_after = None
            if hasattr(e, "headers"):
                retry_after = e.headers.get("retry-after")
            if retry_after:
                try:
                    wait = max(int(retry_after), API_INTERVAL_OK)
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

    if _api_cache["data"] and (time.time() - _api_cache["fetched_at"]) < API_CACHE_TTL:
        _api_cache["is_stale"] = True
        return _api_cache["data"], True
    return None, False


def _parse_reset_time(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.astimezone(None).replace(tzinfo=None)
    except Exception:
        return None


# ─── Progress Bar ────────────────────────────────────────────────────────────

def _noop(*args, **kwargs):
    """No-op callback for read-only menu items (keeps text non-greyed)."""
    pass


def _pct_emoji(pct):
    """Return a colored circle emoji based on usage percentage."""
    if pct >= 90:
        return "\U0001f534"   # 🔴
    if pct >= 70:
        return "\U0001f7e0"   # 🟠
    if pct >= 50:
        return "\U0001f7e1"   # 🟡
    return "\U0001f7e2"       # 🟢


def make_bar(pct, width=20):
    pct = max(0, min(100, pct))
    filled = round(width * pct / 100)
    empty = width - filled
    bar = "\u2588" * filled + "\u2591" * empty
    return f"{_pct_emoji(pct)} {bar}  {pct:.0f}%"


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


SPARK_CHARS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"

def make_sparkline(values):
    if not values or max(values) == 0:
        return "\u2581" * len(values)
    hi = max(values)
    return "".join(
        SPARK_CHARS[min(int(v / hi * (len(SPARK_CHARS) - 1)), len(SPARK_CHARS) - 1)]
        for v in values
    )


# ─── JSONL Parser ────────────────────────────────────────────────────────────

class UsageTracker:
    def __init__(self):
        self.claude_dir = CLAUDE_DIR
        self.projects_dir = PROJECTS_DIR

    def _jsonl_files(self):
        files = []
        if self.projects_dir.exists():
            for p in self.projects_dir.rglob("*.jsonl"):
                files.append(p)
        g = self.claude_dir / "history.jsonl"
        if g.exists():
            files.append(g)
        return files

    def _parse_ts(self, raw):
        if raw is None:
            return None
        try:
            if isinstance(raw, (int, float)):
                ts = raw / 1000 if raw > 1e12 else raw
                return datetime.fromtimestamp(ts)
            s = str(raw)
            if s.endswith("Z"):
                dt_utc = datetime.fromisoformat(s.replace("Z", "+00:00"))
                return dt_utc.astimezone(None).replace(tzinfo=None)
            elif "+" in s or (s.count("-") > 2):
                dt_aware = datetime.fromisoformat(s)
                return dt_aware.astimezone(None).replace(tzinfo=None)
            else:
                return datetime.fromisoformat(s)
        except Exception:
            return None

    def _extract_usage(self, entry):
        msg = entry.get("message", {}) if isinstance(entry.get("message"), dict) else {}
        if msg.get("role") == "assistant" and msg.get("stop_reason") is None:
            return None
        msg_id = msg.get("id") or entry.get("requestId") or entry.get("uuid")
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

    def query(self, since=None):
        seen = {}
        no_id_recs = []
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
                            seen[mid] = rec
                        else:
                            no_id_recs.append(rec)
            except (PermissionError, FileNotFoundError):
                continue

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

    def session_usage(self):
        since = datetime.now() - timedelta(hours=SESSION_WINDOW_HOURS)
        return self.query(since)

    def week_usage(self):
        since = datetime.now() - timedelta(days=WEEK_WINDOW_DAYS)
        return self.query(since)

    def today_usage(self):
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.query(since)

    def daily_costs(self, days=7):
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
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

        result = []
        for i in range(days):
            d = since + timedelta(days=i)
            key = d.strftime("%m/%d")
            result.append((key, day_costs.get(key, 0.0)))
        return result


# ─── Plan recommendation ────────────────────────────────────────────────────

CLAUDE_PLANS = [
    ("Pro",     20,   5.0,   50.0,   "기본 사용량"),
    ("Max 5x",  100,  250.0, 2500.0, "5배 사용량"),
    ("Max 20x", 200,  1000.0, 10000.0, "20배 사용량"),
]


def _recommend_plan(tracker, api_cache):
    """Analyze usage and recommend the best subscription plan."""
    daily = tracker.daily_costs(30)
    daily_costs_list = [c for _, c in daily]
    active_days = sum(1 for c in daily_costs_list if c > 0.001)
    total_30d = sum(daily_costs_list)
    projected_monthly = total_30d

    tier = (api_cache.get("rate_limit_tier") or "").lower()
    current_plan = "Unknown"
    for keyword, label in [("max_20x", "Max 20x"), ("max_5x", "Max 5x"), ("max", "Max 5x"), ("pro", "Pro")]:
        if keyword in tier:
            current_plan = label
            break

    week_totals, week_models, week_costs = tracker.week_usage()
    week_cost = week_totals.get("cost", 0)
    opus_cost = sum(v for k, v in week_costs.items() if "opus" in k.lower())
    opus_ratio = opus_cost / week_cost if week_cost > 0 else 0

    peak_week_cost = 0
    for i in range(max(len(daily_costs_list) - 6, 0)):
        week_sum = sum(daily_costs_list[i:i+7])
        peak_week_cost = max(peak_week_cost, week_sum)

    recommended = None
    for plan_name, price, sess_lim, week_lim, _desc in CLAUDE_PLANS:
        if peak_week_cost <= week_lim * 0.8:
            recommended = (plan_name, price)
            break
    if recommended is None:
        recommended = ("Max 20x", 200)

    rec_name, rec_price = recommended

    reasons = []
    if rec_name == "Pro":
        reasons.append(f"Peak weekly ${peak_week_cost:.0f} within Pro limit")
    elif rec_name == "Max 5x":
        reasons.append(f"Peak weekly ${peak_week_cost:.0f} within Max 5x limit")
    else:
        reasons.append(f"Peak weekly ${peak_week_cost:.0f} needs Max 20x")
    if opus_ratio > 0.5:
        reasons.append(f"High Opus usage ({opus_ratio:.0%})")

    savings = None
    if current_plan != "Unknown" and current_plan != rec_name:
        current_price = next((p for n, p, _, _, _ in CLAUDE_PLANS if n == current_plan), None)
        if current_price and rec_price < current_price:
            savings = current_price - rec_price

    plan_fits = []
    for plan_name, price, sess_lim, week_lim, _desc in CLAUDE_PLANS:
        usage_pct = (peak_week_cost / week_lim * 100) if week_lim else 0
        plan_fits.append({
            "name": plan_name, "price": price, "week_limit": week_lim,
            "usage_pct": usage_pct, "fits": peak_week_cost <= week_lim * 0.8,
        })

    return {
        "current_plan": current_plan, "recommended": rec_name, "rec_price": rec_price,
        "projected_monthly": projected_monthly, "total_30d": total_30d,
        "active_days": active_days, "peak_week_cost": peak_week_cost,
        "opus_ratio": opus_ratio, "reasons": reasons, "savings": savings,
        "plan_fits": plan_fits,
    }


# ─── Reset-time helpers ──────────────────────────────────────────────────────

def _fmt_reset(dt):
    if not dt:
        return ""
    now = datetime.now()
    tz_name = time.tzname[time.daylight] if time.daylight else time.tzname[0]
    if dt.date() == now.date():
        return dt.strftime("%I%p").lstrip("0").lower() + f" ({tz_name})"
    return dt.strftime("%b %d, %I%p").lstrip("0").lower() + f" ({tz_name})"


# ─── Update checker ─────────────────────────────────────────────────────────

_update_cache = {
    "latest_version": None,
    "release_url": None,
    "checked_at": 0,
}


def _parse_version(tag):
    tag = tag.lstrip("v")
    for suffix in ("-mac", "-win"):
        if tag.endswith(suffix):
            tag = tag[:-len(suffix)]
            break
    try:
        return tuple(int(x) for x in tag.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _check_update():
    """Check GitHub releases for a newer version matching this platform."""
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
            releases = json.loads(resp.read())

        for release in releases:
            tag = release.get("tag_name", "")
            if tag.endswith(PLATFORM_TAG_SUFFIX):
                url = release.get("html_url", GITHUB_RELEASES_PAGE)
                _update_cache["latest_version"] = tag
                _update_cache["release_url"] = url
                if _parse_version(tag) > _parse_version(APP_VERSION):
                    print(f"[UPDATE] New version available: {tag}", flush=True)
                    return tag, url
                break
    except Exception as e:
        print(f"[UPDATE] Check failed: {e}", flush=True)
    return None, None


def _get_running_script_path():
    """Detect the actual path of the currently running claude_menubar_windows.py."""
    current = Path(__file__).resolve()
    if current.name == "claude_menubar_windows.py":
        return current
    fallback = Path.home() / ".claude-menubar" / "claude_menubar_windows.py"
    if fallback.exists():
        return fallback
    return None


def _is_git_repo(script_path):
    """Check if the script lives inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=script_path.parent, capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _auto_update(new_ver):
    """Auto-update the app via git pull if running from a cloned repo."""
    script_path = _get_running_script_path()
    if not script_path or not _is_git_repo(script_path):
        return False

    try:
        repo_dir = script_path.parent
        print(f"[UPDATE] Git pull in {repo_dir}...", flush=True)
        result = subprocess.run(
            ["git", "pull", "--ff-only", "origin", "main"],
            cwd=repo_dir, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"[UPDATE] git pull failed: {result.stderr}", flush=True)
            return False

        print(f"[UPDATE] Git updated to {new_ver}, restarting...", flush=True)
        _send_notification("Update Complete", f"{new_ver} updated via git pull, restarting...")
        _restart_app()
        return True
    except Exception as e:
        print(f"[UPDATE] Git update failed: {e}", flush=True)
        return False


def _restart_app():
    """Quit current process and relaunch after a short delay."""
    install_dir = Path.home() / ".claude-menubar"
    launch_bat = install_dir / "launch.bat"

    if launch_bat.exists():
        relaunch_cmd = str(launch_bat)
    else:
        script_path = _get_running_script_path()
        if script_path:
            relaunch_cmd = f'pythonw "{script_path}"'
        else:
            sys.exit(0)

    # Use cmd to wait 2 seconds then relaunch, so current process exits first
    subprocess.Popen(
        f'cmd /c "timeout /t 2 /nobreak >nul & {relaunch_cmd}"',
        shell=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    sys.exit(0)


# ─── Windows Notifications ───────────────────────────────────────────────────

def _send_notification(title, message):
    """Send Windows toast notification via PowerShell."""
    try:
        ps_cmd = f'''
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
        $template = @"
        <toast>
            <visual>
                <binding template="ToastGeneric">
                    <text>{title}</text>
                    <text>{message}</text>
                </binding>
            </visual>
            <audio silent="false"/>
        </toast>
"@
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml($template)
        $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Claude Usage Monitor")
        $notifier.Show($toast)
        '''
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, timeout=10,
        )
    except Exception as e:
        print(f"[ALERT] Notification failed: {e}", flush=True)


# ─── Floating Widget ─────────────────────────────────────────────────────────

def _create_runner_tk_frames(height=20, fg=(26, 26, 46, 255), fg_dim=(26, 26, 46, 97)):
    """Create runner animation frames as PhotoImage-compatible PIL images for tk Canvas."""
    # Scale from SVG 16x22 to widget icon size
    aspect = 16.0 / 22.0
    w = int(height * aspect)
    h = height
    sx = w / 16.0
    sy = h / 22.0
    frames = []
    for fd in _RUNNER_FRAMES_DATA_WIN:
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Head
        cx, cy, r = fd["head"]
        draw.ellipse([
            (cx - r) * sx, cy * sy - r * sy,
            (cx + r) * sx, cy * sy + r * sy
        ], fill=fg)

        # Body
        x1, y1, x2, y2 = fd["body"]
        draw.line([(x1 * sx, y1 * sy), (x2 * sx, y2 * sy)],
                  fill=fg, width=max(1, int(1.8 * sx)))

        # Arms
        for key, color in [("arm_near", fg), ("arm_far", fg_dim)]:
            x1, y1, x2, y2 = fd[key]
            draw.line([(x1 * sx, y1 * sy), (x2 * sx, y2 * sy)],
                      fill=color, width=max(1, int(1.5 * sx)))

        # Legs
        for key, color in [("leg_near", fg), ("leg_far", fg_dim)]:
            pts = fd[key]
            scaled = [(p[0] * sx, p[1] * sy) for p in pts]
            for j in range(len(scaled) - 1):
                draw.line([scaled[j], scaled[j + 1]],
                          fill=color, width=max(1, int(1.9 * sx)))

        frames.append(img)
    return frames


class FloatingWidget:
    """Always-on-top slim bar showing usage at a glance, like a macOS menubar."""

    THEMES = {
        "light": {
            "BG": "#F0F0F0", "BG_INNER": "#FFFFFF", "BORDER": "#C0C0C0",
            "FG": "#1A1A2E", "GREEN": "#16A34A", "YELLOW": "#CA8A04",
            "ORANGE": "#EA580C", "RED": "#DC2626", "ACCENT": "#2563EB",
            "DIM": "#6B7280", "SEP": "#D1D5DB", "BAR_BG": "#E5E7EB",
            "RUNNER_FG": (26, 26, 46, 255), "RUNNER_FG_DIM": (26, 26, 46, 97),
        },
        "dark": {
            "BG": "#1A1B26", "BG_INNER": "#24283B", "BORDER": "#414868",
            "FG": "#E8ECF5", "GREEN": "#9ECE6A", "YELLOW": "#E0AF68",
            "ORANGE": "#FF9E64", "RED": "#F7768E", "ACCENT": "#7AA2F7",
            "DIM": "#9AA5CE", "SEP": "#414868", "BAR_BG": "#1A1B26",
            "RUNNER_FG": (232, 236, 245, 255), "RUNNER_FG_DIM": (232, 236, 245, 97),
        },
    }

    BAR_W = 50
    BAR_H = 6

    def __init__(self):
        self._root = tk.Tk()
        self._root.withdraw()
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.attributes("-alpha", 0.94)

        self._apply_theme(_load_config().get("widget_theme", "light"))

        self._root.configure(bg=self.BG)

        # Canvas for rounded rectangle background
        self._canvas = tk.Canvas(self._root, highlightthickness=0, bg=self.BG, bd=0)
        self._canvas.pack(fill="both", expand=True)

        # We'll draw everything on the canvas after first update
        self._items = {}
        self._bar_items = {}
        self._sep_items = {}
        self._built = False

        # Runner animation
        self._runner_pil_frames = _create_runner_tk_frames(
            height=20, fg=self.RUNNER_FG, fg_dim=self.RUNNER_FG_DIM)
        self._runner_tk_frames = []  # PhotoImage refs (created after mainloop)
        self._runner_index = 0
        self._runner_icon_id = None
        self._runner_anim_interval = 820  # ms
        self._runner_after_id = None

        # Drag support
        self._drag_data = {"x": 0, "y": 0}
        self._canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self._canvas.bind("<B1-Motion>", self._on_drag_move)
        self._canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self._canvas.bind("<Button-3>", self._on_right_click)

        # Restore saved position
        config = _load_config()
        wx = config.get("widget_x")
        wy = config.get("widget_y")
        if wx is not None and wy is not None:
            self._root.geometry(f"+{wx}+{wy}")
        else:
            self._root.geometry("+8+4")

        self._visible = config.get("show_widget", True)

    def _apply_theme(self, theme_name):
        """Apply color theme to widget instance attributes."""
        t = self.THEMES.get(theme_name, self.THEMES["light"])
        self.BG = t["BG"]
        self.BG_INNER = t["BG_INNER"]
        self.BORDER = t["BORDER"]
        self.FG = t["FG"]
        self.GREEN = t["GREEN"]
        self.YELLOW = t["YELLOW"]
        self.ORANGE = t["ORANGE"]
        self.RED = t["RED"]
        self.ACCENT = t["ACCENT"]
        self.DIM = t["DIM"]
        self.SEP = t["SEP"]
        self.BAR_BG = t["BAR_BG"]
        self.RUNNER_FG = t["RUNNER_FG"]
        self.RUNNER_FG_DIM = t["RUNNER_FG_DIM"]
        self._theme = theme_name

    def rebuild_theme(self, theme_name, data=None):
        """Switch theme and rebuild the widget."""
        self._apply_theme(theme_name)
        self._root.configure(bg=self.BG)
        self._canvas.configure(bg=self.BG)
        self._runner_pil_frames = _create_runner_tk_frames(
            height=20, fg=self.RUNNER_FG, fg_dim=self.RUNNER_FG_DIM)
        self._canvas.delete("all")
        self._items = {}
        self._bar_items = {}
        self._sep_items = {}
        self._runner_tk_frames = []
        self._runner_icon_id = None
        self._built = False
        if data:
            self.update(data)

    def _color_for_pct(self, pct):
        if pct >= 90:
            return self.RED
        if pct >= 70:
            return self.ORANGE
        if pct >= 50:
            return self.YELLOW
        return self.GREEN

    def _bar_bg_color(self):
        return self.BAR_BG

    def _round_rect(self, canvas, x1, y1, x2, y2, r, **kwargs):
        """Draw a rounded rectangle on canvas."""
        points = [
            x1 + r, y1, x2 - r, y1,
            x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r,
            x1, y1 + r, x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, **kwargs)

    def _build(self, data):
        """Build canvas items for the first time."""
        c = self._canvas
        config = data.get("config", {})

        show_sess = config.get("show_session", True)
        show_week = config.get("show_week", True)
        show_sonnet = config.get("show_sonnet", True) and data.get("sonnet_pct") is not None

        # Calculate width
        sections = []
        if show_sess:
            sections.append("session")
        if show_week:
            sections.append("week")
        if show_sonnet:
            sections.append("sonnet")

        # Layout: icon(16) + pad(8) + [label(28) + bar(50) + pct(36) + sep(12)] * n
        section_w = 28 + self.BAR_W + 38
        pad_left = 12
        icon_w = 14
        gap = 8
        sep_w = 16
        total_w = pad_left + icon_w + gap + len(sections) * section_w + (len(sections) - 1) * sep_w + 12
        total_h = 28

        c.config(width=total_w, height=total_h)
        self._root.geometry(f"{total_w}x{total_h}")

        # Background rounded rect
        self._round_rect(c, 0, 0, total_w, total_h, 6,
                         fill=self.BG_INNER, outline=self.BORDER, width=1)

        # Runner animation icon
        from PIL import ImageTk
        self._runner_tk_frames = [ImageTk.PhotoImage(f) for f in self._runner_pil_frames]
        self._runner_icon_id = c.create_image(
            pad_left + 7, total_h // 2,
            image=self._runner_tk_frames[0], anchor="center"
        )
        self._start_runner_anim()

        cx = pad_left + icon_w + gap

        label_map = {"session": "5h", "week": "7d", "sonnet": "So"}

        for i, key in enumerate(sections):
            # Separator before (except first)
            if i > 0:
                sep_x = cx + 2
                self._sep_items[key] = c.create_text(
                    sep_x, total_h // 2, text="|", font=("Segoe UI", 8),
                    fill=self.SEP, anchor="center")
                cx += sep_w

            # Label
            self._items[f"{key}_label"] = c.create_text(
                cx + 14, total_h // 2, text=label_map[key],
                font=("Segoe UI Semibold", 9, "bold"), fill=self.DIM, anchor="center")
            cx += 28

            # Progress bar background (rounded)
            bar_y = (total_h - self.BAR_H) // 2
            self._round_rect(c, cx, bar_y, cx + self.BAR_W, bar_y + self.BAR_H, 3,
                             fill=self._bar_bg_color(), outline="")
            # Progress bar fill
            self._bar_items[key] = self._round_rect(
                c, cx, bar_y, cx + 1, bar_y + self.BAR_H, 3,
                fill=self.GREEN, outline="")
            cx += self.BAR_W + 4

            # Percentage text
            self._items[f"{key}_pct"] = c.create_text(
                cx + 16, total_h // 2, text="0%",
                font=("Segoe UI Semibold", 9, "bold"), fill=self.GREEN, anchor="center")
            cx += 34

        self._sections = sections
        self._total_h = total_h
        self._built = True

    def _update_bars(self, data):
        """Update bar fills and percentage text."""
        c = self._canvas
        config = data.get("config", {})
        api_ok = data.get("api_ok", False)
        est = "~" if not api_ok else ""

        pct_map = {
            "session": data.get("sess_pct", 0),
            "week": data.get("week_pct", 0),
            "sonnet": data.get("sonnet_pct", 0) or 0,
        }

        for key in self._sections:
            pct = pct_map.get(key, 0)
            color = self._color_for_pct(pct)

            # Update bar fill width
            coords = c.coords(self._bar_items[key])
            if len(coords) >= 12:
                x1 = coords[0] - 3  # account for rounded rect offset
                bar_y = (self._total_h - self.BAR_H) // 2
                fill_w = max(1, round(self.BAR_W * min(pct, 100) / 100))
                c.delete(self._bar_items[key])
                self._bar_items[key] = self._round_rect(
                    c, x1, bar_y, x1 + fill_w, bar_y + self.BAR_H, 3,
                    fill=color, outline="")

            # Update percentage text
            c.itemconfigure(self._items[f"{key}_pct"],
                           text=f"{est}{pct:.0f}%", fill=color)

    def _on_drag_start(self, event):
        self._drag_data["x"] = event.x
        self._drag_data["y"] = event.y

    def _on_drag_move(self, event):
        x = self._root.winfo_x() + event.x - self._drag_data["x"]
        y = self._root.winfo_y() + event.y - self._drag_data["y"]
        self._root.geometry(f"+{x}+{y}")

    def _on_drag_end(self, event):
        config = _load_config()
        config["widget_x"] = self._root.winfo_x()
        config["widget_y"] = self._root.winfo_y()
        _save_config(config)

    def _on_right_click(self, event):
        self.hide()
        config = _load_config()
        config["show_widget"] = False
        _save_config(config)

    def _start_runner_anim(self):
        """Start the runner animation loop on the widget."""
        if self._runner_after_id:
            self._root.after_cancel(self._runner_after_id)
        config = _load_config()
        if not config.get("animation_enabled", True):
            return
        self._runner_after_id = self._root.after(
            self._runner_anim_interval, self._runner_tick)

    def _runner_tick(self):
        """Advance to next runner frame."""
        if not self._runner_tk_frames or not self._runner_icon_id:
            return
        self._runner_index = (self._runner_index + 1) % len(self._runner_tk_frames)
        self._canvas.itemconfigure(
            self._runner_icon_id, image=self._runner_tk_frames[self._runner_index])
        config = _load_config()
        if config.get("animation_enabled", True):
            self._runner_after_id = self._root.after(
                self._runner_anim_interval, self._runner_tick)

    def update_anim_speed(self, pct):
        """Update runner animation speed from usage percentage."""
        self._runner_anim_interval = int(_anim_interval_for_pct(pct) * 1000)

    def update(self, data):
        """Update widget from usage data. Must be called from tk thread."""
        if not data:
            return

        if not self._built:
            self._build(data)
        self._update_bars(data)
        # Update animation speed
        sess_pct = data.get("sess_pct", 0)
        self.update_anim_speed(sess_pct)

    def show(self):
        self._visible = True
        self._root.deiconify()

    def hide(self):
        self._visible = False
        self._root.withdraw()

    @property
    def visible(self):
        return self._visible

    def schedule(self, func, *args):
        """Thread-safe way to run something on the tk main loop."""
        try:
            self._root.after(0, func, *args)
        except Exception:
            pass

    def mainloop(self):
        """Start the tk event loop (blocks)."""
        if self._visible:
            self._root.deiconify()
        self._root.mainloop()

    def quit(self):
        try:
            self._root.quit()
            self._root.destroy()
        except Exception:
            pass


# ─── Tray Icon ───────────────────────────────────────────────────────────────

_RUNNER_FRAMES_DATA_WIN = [
    {
        "head": (7.5, 4.0, 2.5),
        "body": (8, 6.5, 7.5, 13),
        "arm_near": (8, 9, 4.5, 10.5),
        "arm_far": (8, 9, 12, 11),
        "leg_near": [(7.5, 13), (5, 17.5), (3, 21)],
        "leg_far": [(7.5, 13), (10.5, 16), (12, 20)],
    },
    {
        "head": (7.5, 3.5, 2.5),
        "body": (8, 6, 8, 12.5),
        "arm_near": (8, 8.5, 5.5, 11.5),
        "arm_far": (8, 8.5, 10.5, 11),
        "leg_near": [(8, 12.5), (7, 17), (6, 21)],
        "leg_far": [(8, 12.5), (9.5, 16.5), (10.5, 21)],
    },
    {
        "head": (7.5, 4.0, 2.5),
        "body": (8, 6.5, 8.5, 13),
        "arm_near": (8, 9, 12, 10.5),
        "arm_far": (8, 9, 4, 11),
        "leg_near": [(8.5, 13), (5.5, 17), (3.5, 21)],
        "leg_far": [(8.5, 13), (11, 16), (12.5, 20)],
    },
    {
        "head": (7.5, 3.5, 2.5),
        "body": (8, 6, 8, 12.5),
        "arm_near": (8, 8.5, 10.5, 10.5),
        "arm_far": (8, 8.5, 5, 11.5),
        "leg_near": [(8, 12.5), (9.5, 17), (10.5, 21)],
        "leg_far": [(8, 12.5), (7, 16.5), (6, 21)],
    },
]



def _anim_interval_for_pct(pct):
    """Map usage percentage to animation frame interval (seconds).
    820ms at 0%, 80ms at 100%.
    """
    return max(0.08, 0.82 - (pct / 100.0) * 0.74)


def _create_icon_image(text="--", color="#4A90D9"):
    """Create a small tray icon with percentage text."""
    size = 64
    img = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background circle (fill entire square first, then draw circle)
    draw.rectangle([0, 0, size, size], fill=(0, 0, 0))
    draw.ellipse([2, 2, size - 2, size - 2], fill=color)

    # Text — try multiple common Windows fonts
    font = None
    for font_name in ["arial.ttf", "segoeui.ttf", "tahoma.ttf", "calibri.ttf"]:
        try:
            font = ImageFont.truetype(font_name, 24)
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.truetype("C:\\Windows\\Fonts\\arial.ttf", 24)
        except OSError:
            font = ImageFont.load_default(size=20)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) / 2
    ty = (size - th) / 2 - 2
    draw.text((tx, ty), text, fill="white", font=font)

    return img


class ClaudeUsageTray:
    def __init__(self):
        self.tracker = UsageTracker()
        self._alerted = {"session": set(), "week": set()}
        self._last_reset = {"session": None, "week": None}
        self._running = True
        self._icon = None
        self._last_data = {}
        self._widget = None

    def _get_usage_data(self, force_api=False):
        """Gather all usage data for menu building."""
        config = _load_config()
        sess_totals, sess_models, sess_costs = self.tracker.session_usage()
        week_totals, week_models, week_costs = self.tracker.week_usage()
        today_totals, _, _ = self.tracker.today_usage()

        api, api_stale = fetch_usage_api(force=force_api)

        if api and "five_hour" in api:
            sess_pct = api["five_hour"].get("utilization", 0)
            sess_reset = _parse_reset_time(api["five_hour"].get("resets_at"))
            week_pct = api.get("seven_day", {}).get("utilization", 0) if api.get("seven_day") else 0
            week_reset = _parse_reset_time((api.get("seven_day") or {}).get("resets_at"))
            sonnet_data = api.get("seven_day_sonnet")
            sonnet_pct = sonnet_data.get("utilization", 0) if sonnet_data else None
            sonnet_reset = _parse_reset_time(sonnet_data.get("resets_at")) if sonnet_data else None
        else:
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

        # Check alerts
        self._check_alerts(sess_pct, sess_reset, week_pct, week_reset)

        return {
            "config": config,
            "sess_totals": sess_totals, "sess_models": sess_models, "sess_costs": sess_costs,
            "week_totals": week_totals, "week_models": week_models, "week_costs": week_costs,
            "today_totals": today_totals,
            "sess_pct": sess_pct, "sess_reset": sess_reset,
            "week_pct": week_pct, "week_reset": week_reset,
            "sonnet_pct": sonnet_pct, "sonnet_reset": sonnet_reset,
            "api_ok": api_ok, "api_stale": api_stale, "api": api,
        }

    def _check_alerts(self, sess_pct, sess_reset, week_pct, week_reset):
        config = _load_config()
        if not config.get("alert_enabled", True):
            return

        checks = [
            ("session", "Session(5h)", sess_pct, sess_reset),
            ("week", "Week(7d)", week_pct, week_reset),
        ]
        for key, label, pct, reset_time in checks:
            if reset_time != self._last_reset[key]:
                self._alerted[key] = set()
                self._last_reset[key] = reset_time

            # Find the highest un-notified threshold that has been crossed
            fired = None
            for threshold in ALERT_THRESHOLDS:
                if pct >= threshold and threshold not in self._alerted[key]:
                    fired = threshold
            if fired is not None:
                # Mark all thresholds up to the fired one as alerted
                for threshold in ALERT_THRESHOLDS:
                    if threshold <= fired:
                        self._alerted[key].add(threshold)
                reset_msg = f" | Resets: {_fmt_reset(reset_time)}" if reset_time else ""
                _send_notification(
                    title=f"Claude Usage {fired}%",
                    message=f"{label}: {pct:.0f}%{reset_msg}",
                )

    def _build_menu(self, data=None):
        """Build pystray menu from usage data."""
        if data is None:
            try:
                data = self._get_usage_data()
            except Exception as e:
                return pystray.Menu(
                    pystray.MenuItem(f"Error: {e}", _noop),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Refresh", self._on_refresh),
                    pystray.MenuItem("Quit", self._on_quit),
                )

        self._last_data = data
        config = data["config"]
        items = []

        # Account info
        acct_email = _api_cache.get("account_email")
        acct_name = _api_cache.get("account_name")
        if acct_email:
            label = f"\U0001f464 {acct_name} ({acct_email})" if acct_name else f"\U0001f464 {acct_email}"
            items.append(pystray.MenuItem(label, _noop))
            items.append(pystray.Menu.SEPARATOR)

        # API status
        api_ok = data["api_ok"]
        api_stale = data["api_stale"]
        if not data["api"]:
            items.append(pystray.MenuItem("\u26a0\ufe0f Usage API unavailable", _noop))
            items.append(pystray.Menu.SEPARATOR)
        elif api_stale:
            ago = int(time.time() - _api_cache["fetched_at"])
            items.append(pystray.MenuItem(f"\U0001f4e6 Cached data ({ago}s ago)", _noop))
            items.append(pystray.Menu.SEPARATOR)

        est = "" if api_ok else " (est.)"

        # Session
        if config.get("show_session", True):
            sess_pct = data["sess_pct"]
            items.append(pystray.MenuItem(f"\u23f1\ufe0f Session{est}: {make_bar(sess_pct)}", _noop))
            if data["sess_reset"]:
                items.append(pystray.MenuItem(f"  \u23f3 Resets {_fmt_reset(data['sess_reset'])}", _noop))
            items.append(pystray.Menu.SEPARATOR)

        # Week (all models)
        if config.get("show_week", True):
            week_pct = data["week_pct"]
            items.append(pystray.MenuItem(f"\U0001f4c5 Week (all){est}: {make_bar(week_pct)}", _noop))
            if data["week_reset"]:
                items.append(pystray.MenuItem(f"  \u23f3 Resets {_fmt_reset(data['week_reset'])}", _noop))
            items.append(pystray.Menu.SEPARATOR)

        # Week (Sonnet only)
        if config.get("show_sonnet", True) and data["sonnet_pct"] is not None:
            items.append(pystray.MenuItem(f"\U0001f4a0 Week (Sonnet): {make_bar(data['sonnet_pct'])}", _noop))
            if data["sonnet_reset"]:
                items.append(pystray.MenuItem(f"  \u23f3 Resets {_fmt_reset(data['sonnet_reset'])}", _noop))
            items.append(pystray.Menu.SEPARATOR)

        # Detail submenu
        sess_totals = data["sess_totals"]
        week_totals = data["week_totals"]
        today_totals = data["today_totals"]
        sess_tokens = sess_totals.get("total", 0)
        week_tokens = week_totals.get("total", 0)

        detail_items = [
            pystray.MenuItem("\U0001f4c6 Today", _noop),
            pystray.MenuItem(
                f"  \U0001f4ac Tokens {fmt_tokens(today_totals.get('total', 0))}  "
                f"(in {fmt_tokens(today_totals.get('input', 0))} / out {fmt_tokens(today_totals.get('output', 0))})",
                _noop),
            pystray.MenuItem(f"  \U0001f4b0 Cost {fmt_cost(today_totals.get('cost', 0))}", _noop),
            pystray.MenuItem(f"  \U0001f4e8 Requests {today_totals.get('requests', 0)}", _noop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("\u23f1\ufe0f Session (5h)", _noop),
            pystray.MenuItem(
                f"  \U0001f4ac Tokens {fmt_tokens(sess_tokens)}  "
                f"(in {fmt_tokens(sess_totals.get('input', 0))} / out {fmt_tokens(sess_totals.get('output', 0))})",
                _noop),
            pystray.MenuItem(f"  \U0001f4b0 Cost {fmt_cost(sess_totals.get('cost', 0))}", _noop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("\U0001f4c5 This Week (7d)", _noop),
            pystray.MenuItem(
                f"  \U0001f4ac Tokens {fmt_tokens(week_tokens)}  "
                f"(in {fmt_tokens(week_totals.get('input', 0))} / out {fmt_tokens(week_totals.get('output', 0))})",
                _noop),
            pystray.MenuItem(f"  \U0001f4b0 Cost {fmt_cost(week_totals.get('cost', 0))}", _noop),
            pystray.MenuItem(f"  \U0001f4e8 Requests {week_totals.get('requests', 0)}", _noop),
        ]
        items.append(pystray.MenuItem("\U0001f4ca Detail", pystray.Menu(*detail_items)))

        # Model breakdown submenu
        week_models = data["week_models"]
        week_costs = data["week_costs"]
        if week_models:
            model_items = []
            for m in sorted(week_costs, key=week_costs.get, reverse=True):
                d = week_models[m]
                total_t = sum(d.values())
                short = m.split("/")[-1] if "/" in m else m
                model_items.append(pystray.MenuItem(
                    f"{short}: {fmt_tokens(total_t)}  {fmt_cost(week_costs[m])}",
                    _noop))
            items.append(pystray.MenuItem("\U0001f916 Models", pystray.Menu(*model_items)))

        # 7-day history submenu
        daily = self.tracker.daily_costs(7)
        costs = [c for _, c in daily]
        spark = make_sparkline(costs)
        total_7d = sum(costs)
        history_items = []
        for date_str, cost in daily:
            bar_len = 0
            if max(costs) > 0:
                bar_len = round(cost / max(costs) * 10)
            bar = "\u2588" * bar_len + "\u2591" * (10 - bar_len)
            history_items.append(pystray.MenuItem(
                f"{date_str}  {bar}  {fmt_cost(cost)}",
                _noop))
        items.append(pystray.MenuItem(f"\U0001f4c8 7d {spark} {fmt_cost(total_7d)}", pystray.Menu(*history_items)))

        # Plan recommendation submenu
        try:
            rec = _recommend_plan(self.tracker, _api_cache)
            plan_items = []
            plan_items.append(pystray.MenuItem(f"\U0001f4cc Current: {rec['current_plan']}", _noop))
            plan_items.append(pystray.Menu.SEPARATOR)
            plan_items.append(pystray.MenuItem(
                f"\U0001f4b0 30d cost: {fmt_cost(rec['total_30d'])} ({rec['active_days']} active days)",
                _noop))
            plan_items.append(pystray.MenuItem(
                f"\U0001f4c8 Peak weekly: {fmt_cost(rec['peak_week_cost'])}", _noop))
            if rec['opus_ratio'] > 0.01:
                plan_items.append(pystray.MenuItem(
                    f"Opus ratio: {rec['opus_ratio']:.0%}", _noop))
            plan_items.append(pystray.Menu.SEPARATOR)
            for pf in rec['plan_fits']:
                usage_pct = pf['usage_pct']
                if usage_pct > 100:
                    status = "\U0001f534"  # red circle
                elif usage_pct > 80:
                    status = "\U0001f7e1"  # yellow circle
                else:
                    status = "\U0001f7e2"  # green circle
                is_rec = " *" if pf['name'] == rec['recommended'] else ""
                is_cur = " (current)" if pf['name'] == rec['current_plan'] else ""
                plan_items.append(pystray.MenuItem(
                    f"{status} {pf['name']} ${pf['price']}/mo — {usage_pct:.0f}% of limit{is_rec}{is_cur}",
                    _noop))
            plan_items.append(pystray.Menu.SEPARATOR)
            star = "\u2b50 " if rec['recommended'] != rec['current_plan'] else "\u2705 "
            plan_items.append(pystray.MenuItem(
                f"{star}Recommended: {rec['recommended']} (${rec['rec_price']}/mo)",
                _noop))
            for r in rec['reasons']:
                plan_items.append(pystray.MenuItem(f"  \u2192 {r}", _noop))
            if rec['savings']:
                plan_items.append(pystray.MenuItem(
                    f"\U0001f4b5 Save ${rec['savings']}/mo", _noop))
            elif rec['recommended'] == rec['current_plan']:
                plan_items.append(pystray.MenuItem(
                    "\u2705 Current plan is optimal", _noop))
            items.append(pystray.MenuItem("\U0001f4cb Plan Recommendation", pystray.Menu(*plan_items)))
        except Exception as e:
            print(f"[PLAN] Recommendation error: {e}", flush=True)

        items.append(pystray.Menu.SEPARATOR)

        # Settings submenu
        title_mode = config.get("title_display", "session")
        show_sess = config.get("show_session", True)
        show_week = config.get("show_week", True)
        show_sonnet = config.get("show_sonnet", True)
        alert_on = config.get("alert_enabled", True)

        settings_items = [
            pystray.MenuItem("Title: Session",
                             lambda: self._set_title_display("session"),
                             checked=lambda _: _load_config().get("title_display", "session") == "session"),
            pystray.MenuItem("Title: Week",
                             lambda: self._set_title_display("week"),
                             checked=lambda _: _load_config().get("title_display", "session") == "week"),
            pystray.MenuItem("Title: Both",
                             lambda: self._set_title_display("both"),
                             checked=lambda _: _load_config().get("title_display", "session") == "both"),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Show Session",
                             lambda: self._toggle_config("show_session"),
                             checked=lambda _: _load_config().get("show_session", True)),
            pystray.MenuItem("Show Week",
                             lambda: self._toggle_config("show_week"),
                             checked=lambda _: _load_config().get("show_week", True)),
            pystray.MenuItem("Show Sonnet",
                             lambda: self._toggle_config("show_sonnet"),
                             checked=lambda _: _load_config().get("show_sonnet", True)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Alerts (80%/90%)",
                             lambda: self._toggle_config("alert_enabled"),
                             checked=lambda _: _load_config().get("alert_enabled", True)),
            pystray.MenuItem("🏃 Running Animation",
                             lambda: self._toggle_config("animation_enabled"),
                             checked=lambda _: _load_config().get("animation_enabled", True)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Floating Widget",
                             lambda: self._toggle_widget(),
                             checked=lambda _: _load_config().get("show_widget", True)),
            pystray.MenuItem("Widget Theme", pystray.Menu(
                pystray.MenuItem("Light",
                                 lambda: self._set_widget_theme("light"),
                                 checked=lambda _: _load_config().get("widget_theme", "light") == "light"),
                pystray.MenuItem("Dark",
                                 lambda: self._set_widget_theme("dark"),
                                 checked=lambda _: _load_config().get("widget_theme", "light") == "dark"),
            )),
        ]
        items.append(pystray.MenuItem("Settings", pystray.Menu(*settings_items)))

        # Update check
        new_ver, release_url = _check_update()
        if new_ver:
            def _on_update_click(ver=new_ver, url=release_url):
                if not _auto_update(ver):
                    webbrowser.open(url)
            items.append(pystray.MenuItem(f"Update: {new_ver}", _on_update_click))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("\U0001f504 Refresh", self._on_refresh))
        items.append(pystray.MenuItem(f"v{APP_VERSION}", _noop))
        items.append(pystray.MenuItem("\u274c Quit", self._on_quit))

        return pystray.Menu(*items)

    def _get_icon_color(self, pct):
        if pct >= 90:
            return "#E74C3C"  # red
        elif pct >= 70:
            return "#F39C12"  # orange
        else:
            return "#4A90D9"  # blue

    def _update_icon(self, data=None):
        """Update tray icon image and menu."""
        if data is None:
            data = self._last_data
        if not data:
            return

        config = data.get("config", {})
        title_mode = config.get("title_display", "session")
        sess_pct = data.get("sess_pct", 0)
        week_pct = data.get("week_pct", 0)
        api_ok = data.get("api_ok", False)
        api_stale = data.get("api_stale", False)

        stale_mark = "~" if (api_stale or not api_ok) else ""

        if title_mode == "week":
            display_pct = week_pct
        else:
            display_pct = sess_pct

        text = f"{int(display_pct)}"
        color = self._get_icon_color(display_pct)
        new_image = _create_icon_image(text, color)

        if title_mode == "both":
            tooltip = f"Claude: {stale_mark}{sess_pct:.0f}% | {week_pct:.0f}%"
        elif title_mode == "week":
            tooltip = f"Claude: {stale_mark}{week_pct:.0f}%"
        else:
            tooltip = f"Claude: {stale_mark}{sess_pct:.0f}%"

        if self._icon:
            self._icon.icon = new_image
            self._icon.title = tooltip
            self._icon.menu = self._build_menu(data)

        # Update floating widget
        if self._widget:
            self._widget.schedule(self._widget.update, data)

    def _on_refresh(self, *args):
        """Manual refresh callback."""
        threading.Thread(target=self._refresh_thread, args=(True,), daemon=True).start()

    def _refresh_thread(self, force_api=False):
        try:
            data = self._get_usage_data(force_api=force_api)
            self._update_icon(data)
        except Exception as e:
            print(f"[REFRESH] Error: {e}", flush=True)

    def _on_quit(self, *args):
        self._running = False
        if self._widget:
            self._widget.schedule(self._widget.quit)
        if self._icon:
            self._icon.stop()

    def _set_title_display(self, mode):
        config = _load_config()
        config["title_display"] = mode
        _save_config(config)
        self._refresh_thread()

    def _toggle_config(self, key):
        config = _load_config()
        config[key] = not config.get(key, True)
        _save_config(config)
        if key == "animation_enabled" and self._widget:
            # Restart or stop widget runner animation
            if config[key]:
                self._widget.schedule(self._widget._start_runner_anim)
            else:
                if self._widget._runner_after_id:
                    self._widget.schedule(
                        lambda: self._widget._root.after_cancel(self._widget._runner_after_id))
        self._refresh_thread()

    def _set_widget_theme(self, theme_name):
        config = _load_config()
        config["widget_theme"] = theme_name
        _save_config(config)
        if self._widget:
            self._widget.schedule(
                self._widget.rebuild_theme, theme_name, self._last_data)

    def _toggle_widget(self):
        config = _load_config()
        new_val = not config.get("show_widget", True)
        config["show_widget"] = new_val
        _save_config(config)
        if self._widget:
            if new_val:
                self._widget.schedule(self._widget.show)
                self._widget.schedule(self._widget.update, self._last_data)
            else:
                self._widget.schedule(self._widget.hide)

    def _auto_refresh_loop(self):
        """Background thread that refreshes data periodically."""
        while self._running:
            time.sleep(REFRESH_INTERVAL_SEC)
            if not self._running:
                break
            try:
                data = self._get_usage_data()
                self._update_icon(data)
            except Exception as e:
                print(f"[AUTO-REFRESH] Error: {e}", flush=True)

            # Check for updates and auto-update if possible
            try:
                new_ver, release_url = _check_update()
                if new_ver:
                    if not _auto_update(new_ver):
                        _send_notification("Update Available",
                                           f"New version {new_ver} is available.\nCheck the tray menu to download.")
            except Exception as e:
                print(f"[AUTO-REFRESH] Update check error: {e}", flush=True)

    def run(self):
        print(f"[DEBUG] Python: {sys.version}", flush=True)
        print(f"[DEBUG] CLAUDE_DIR: {CLAUDE_DIR} exists={CLAUDE_DIR.exists()}", flush=True)

        if not CLAUDE_DIR.exists():
            ctypes.windll.user32.MessageBoxW(
                0,
                "~/.claude/ directory not found.\nMake sure Claude Code is installed.",
                "Claude Usage Monitor",
                0x10,  # MB_ICONERROR
            )
            return

        # Initial data load
        try:
            data = self._get_usage_data()
        except Exception as e:
            print(f"[DEBUG] Initial load error: {e}", flush=True)
            data = {"config": _load_config(), "sess_pct": 0, "week_pct": 0,
                    "sess_totals": {}, "week_totals": {}, "today_totals": {},
                    "sess_models": {}, "sess_costs": {}, "week_models": {}, "week_costs": {},
                    "sess_reset": None, "week_reset": None,
                    "sonnet_pct": None, "sonnet_reset": None,
                    "api_ok": False, "api_stale": False, "api": None}

        self._last_data = data
        pct = data.get("sess_pct", 0)
        icon_image = _create_icon_image(f"{int(pct)}", self._get_icon_color(pct))

        self._icon = pystray.Icon(
            name="Claude Usage Monitor",
            icon=icon_image,
            title=f"Claude: {pct:.0f}%",
            menu=self._build_menu(data),
        )

        # Create floating widget (runner animation runs on widget)
        self._widget = FloatingWidget()
        self._widget.update(data)

        # Start auto-refresh thread
        refresh_thread = threading.Thread(target=self._auto_refresh_loop, daemon=True)
        refresh_thread.start()

        # Run tray icon in background thread (pystray supports this)
        print("[DEBUG] Starting tray icon...", flush=True)
        tray_thread = threading.Thread(target=self._icon.run, daemon=True)
        tray_thread.start()

        # Run tkinter mainloop on main thread (required by Windows)
        self._widget.mainloop()


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    app = ClaudeUsageTray()
    app.run()


if __name__ == "__main__":
    main()
