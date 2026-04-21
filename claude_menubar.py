#!/usr/bin/env python3
"""
Claude Code Usage Monitor v2 — macOS Menu Bar App
Shows Claude Code usage with visual progress bars, just like /usage.
Uses the Anthropic OAuth API for accurate rate-limit data,
and reads local JSONL session files for detailed token/cost breakdowns.
"""

import json
import os
import sys
import glob
import time
import tempfile
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
from urllib.request import Request, urlopen
from urllib.error import URLError

import rumps
try:
    import Quartz  # noqa: F401 — loads CGColorRef type info into PyObjC bridge, suppresses ObjCPointerWarning
except ImportError:
    pass


# ─── Configuration ───────────────────────────────────────────────────────────

APP_VERSION = "2.1.2"
GITHUB_REPO = "hmyanghm/claude-usage-menubar"
GITHUB_API_RELEASES = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
PLATFORM_TAG_SUFFIX = "-mac"  # only check releases tagged like v1.0.9-mac
UPDATE_CHECK_INTERVAL = 3600  # check every 1 hour

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
REFRESH_INTERVAL_SEC = 300

CONFIG_PATH = CLAUDE_DIR / "menubar_config.json"
LAST_TITLE_CACHE_PATH = CLAUDE_DIR / "menubar_last_title.json"
DEFAULT_CONFIG = {
    "title_display": "session",   # "session" | "week" | "both"
    "show_session": True,
    "show_week": True,
    "show_sonnet": True,
    "alert_enabled": True,        # usage threshold alerts on/off
    "animation_enabled": True,    # RunCat-style pulse animation on/off
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


def _load_last_title_snapshot(path=None):
    """Load the last known title percentages for fast startup display."""
    path = path or LAST_TITLE_CACHE_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "title_mode": data.get("title_mode", "session"),
            "sess_pct": float(data.get("sess_pct", 0)),
            "week_pct": float(data.get("week_pct", 0)),
            "api_stale": bool(data.get("api_stale", False)),
        }
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError, PermissionError):
        return None


def _save_last_title_snapshot(data, path=None):
    """Persist the last known title percentages for the next app launch."""
    path = path or LAST_TITLE_CACHE_PATH
    snapshot = {
        "title_mode": data.get("title_mode", "session"),
        "sess_pct": round(float(data.get("sess_pct", 0))),
        "week_pct": round(float(data.get("week_pct", 0))),
        "api_stale": bool(data.get("api_stale", False)),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
    except Exception as e:
        print(f"[CACHE] Save last title error: {e}", flush=True)


def _format_title_from_data(data):
    """Format the menu bar title from usage percentages."""
    stale_mark = "~" if data.get("api_stale") else ""
    title_mode = data.get("title_mode", "session")
    sess_pct = data.get("sess_pct", 0)
    week_pct = data.get("week_pct", 0)
    if title_mode == "week":
        return f" {stale_mark}{week_pct:.0f}%"
    if title_mode == "both":
        return f" {stale_mark}{sess_pct:.0f}% | {week_pct:.0f}%"
    return f" {stale_mark}{sess_pct:.0f}%"


def _configure_popover_scroll_view(scroll):
    """Configure the popover scroll view for stable layout during rebuilds."""
    scroll.setDrawsBackground_(False)
    scroll.setHasVerticalScroller_(True)
    scroll.setHasHorizontalScroller_(False)
    scroll.setAutohidesScrollers_(True)
    if hasattr(AppKit, "NSScrollerStyleOverlay"):
        scroll.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)


def _compute_anchor_scroll_origin(header_y, viewport_offset, document_height, viewport_height):
    """Return a clamped scroll origin that keeps the header near its previous viewport offset."""
    max_origin = max(0, document_height - viewport_height)
    origin_y = header_y - viewport_offset
    return min(max(0, origin_y), max_origin)

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
    """Parse version string like 'v1.0.5-mac' or '1.0.5' into tuple of ints."""
    tag = tag.lstrip("v")
    # Strip platform suffix (e.g. '-mac', '-win')
    for suffix in ("-mac", "-win"):
        if tag.endswith(suffix):
            tag = tag[:-len(suffix)]
            break
    try:
        return tuple(int(x) for x in tag.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _check_update(force=False):
    """Check GitHub releases for a newer version matching this platform.
    Only considers releases tagged with PLATFORM_TAG_SUFFIX (e.g. v1.0.9-mac).
    Returns (new_version, url) or (None, None).
    """
    now = time.time()
    if not force and now - _update_cache["checked_at"] < UPDATE_CHECK_INTERVAL:
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

        # Find the latest release matching our platform suffix
        for release in releases:
            tag = release.get("tag_name", "")
            if tag.endswith(PLATFORM_TAG_SUFFIX):
                url = release.get("html_url", GITHUB_RELEASES_PAGE)
                _update_cache["latest_version"] = tag
                _update_cache["release_url"] = url
                if _parse_version(tag) > _parse_version(APP_VERSION):
                    print(f"[UPDATE] New version available: {tag}", flush=True)
                    return tag, url
                break  # first match is latest (API returns newest first)
    except Exception as e:
        print(f"[UPDATE] Check failed: {e}", flush=True)
    return None, None


def _get_running_script_path():
    """Detect the actual path of the currently running claude_menubar.py."""
    # __file__ gives us the real path of the running script
    current = Path(__file__).resolve()
    if current.name == "claude_menubar.py":
        return current
    # Fallback: setup.sh installed location
    fallback = Path.home() / ".claude-menubar" / "claude_menubar.py"
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


def _download_source_tarball(tag):
    """Download and extract the GitHub source tarball for a given tag.
    Returns the extracted directory path, or None on failure.
    """
    tarball_url = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{tag}.tar.gz"
    tmp_dir = tempfile.mkdtemp(prefix="claude-update-")
    tar_path = os.path.join(tmp_dir, "source.tar.gz")

    try:
        print(f"[UPDATE] Downloading source {tag}...", flush=True)
        req = Request(tarball_url, headers={
            "User-Agent": f"claude-usage-menubar/{APP_VERSION}",
        })
        with urlopen(req, timeout=30) as resp:
            with open(tar_path, "wb") as f:
                f.write(resp.read())

        # Extract
        subprocess.run(
            ["tar", "xzf", tar_path, "-C", tmp_dir],
            capture_output=True, timeout=15, check=True,
        )

        # Find extracted directory (e.g. claude-usage-menubar-v1.0.8/)
        for item in os.listdir(tmp_dir):
            item_path = os.path.join(tmp_dir, item)
            if os.path.isdir(item_path) and item != "__MACOSX":
                setup_sh = os.path.join(item_path, "setup.sh")
                if os.path.exists(setup_sh):
                    return item_path
        print("[UPDATE] setup.sh not found in tarball", flush=True)
        return None
    except Exception as e:
        print(f"[UPDATE] Tarball download failed: {e}", flush=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None


def _auto_update(new_ver):
    """Auto-update the app regardless of installation method."""
    script_path = _get_running_script_path()

    # git clone → git pull
    if script_path and _is_git_repo(script_path):
        return _auto_update_git(new_ver, script_path)

    # setup.sh install or .app bundle → download source & run setup.sh
    return _auto_update_setup(new_ver)


def _auto_update_git(new_ver, script_path):
    """Update via git pull when running from a cloned repo."""
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
        _send_notification("업데이트 완료", f"{new_ver} git pull 완료, 앱을 재시작합니다")
        _restart_app()
        return True
    except Exception as e:
        print(f"[UPDATE] Git update failed: {e}", flush=True)
        return False


def _auto_update_setup(new_ver):
    """Download source tarball and run setup.sh to update."""
    tag = _update_cache.get("latest_version")
    if not tag:
        print("[UPDATE] No version tag available", flush=True)
        return False

    source_dir = _download_source_tarball(tag)
    if not source_dir:
        return False

    try:
        setup_sh = os.path.join(source_dir, "setup.sh")
        print(f"[UPDATE] Running setup.sh from {source_dir}...", flush=True)
        env = os.environ.copy()
        env["CLAUDE_AUTO_UPDATE"] = "1"
        result = subprocess.run(
            ["bash", setup_sh],
            capture_output=True, text=True, timeout=60, env=env,
        )
        if result.returncode != 0:
            print(f"[UPDATE] setup.sh failed: {result.stderr}", flush=True)
            return False

        print(f"[UPDATE] Updated to {new_ver} via setup.sh", flush=True)
        _send_notification("업데이트 완료", f"{new_ver} 설치 완료, 앱을 재시작합니다")

        # Cleanup
        shutil.rmtree(os.path.dirname(source_dir), ignore_errors=True)

        _restart_app()
        return True
    except Exception as e:
        print(f"[UPDATE] setup.sh update failed: {e}", flush=True)
        shutil.rmtree(os.path.dirname(source_dir), ignore_errors=True)
        return False


def _restart_app():
    """Quit current process and relaunch after a short delay."""
    install_dir = Path.home() / ".claude-menubar"
    launch_sh = install_dir / "launch.sh"

    if launch_sh.exists():
        relaunch_cmd = str(launch_sh)
    else:
        script_path = _get_running_script_path()
        if script_path:
            relaunch_cmd = f"{sys.executable} {script_path}"
        else:
            rumps.quit_application()
            return

    # Use shell to wait 2 seconds then relaunch, so current process exits first
    subprocess.Popen(
        f"sleep 2 && {relaunch_cmd}",
        shell=True, start_new_session=True,
    )
    rumps.quit_application()


def _make_label(text):
    """Create a non-clickable menu item with normal (white) text.
    Uses a custom NSView so macOS cannot override the text color.
    """
    try:
        import AppKit
        item = rumps.MenuItem(text, callback=None)
        ns_item = item._menuitem

        font = AppKit.NSFont.menuFontOfSize_(14)
        color = AppKit.NSColor.secondaryLabelColor()
        attrs = {
            AppKit.NSFontAttributeName: font,
            AppKit.NSForegroundColorAttributeName: color,
        }
        attr_str = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        size = attr_str.size()

        # Create a text field inside a view
        text_field = AppKit.NSTextField.labelWithAttributedString_(attr_str)
        text_field.setSelectable_(False)
        text_field.setBezeled_(False)
        text_field.setDrawsBackground_(False)

        view = AppKit.NSView.alloc().initWithFrame_(
            ((0, 0), (max(size.width + 28, 300), size.height + 8))
        )
        text_field.setFrame_(((14, 4), (size.width + 14, size.height)))
        view.addSubview_(text_field)

        ns_item.setView_(view)
        return item
    except Exception:
        return rumps.MenuItem(text, callback=None)



# ─── NSPopover UI Components ───────────────────────────────────────────────

try:
    import AppKit
    import objc
    from Foundation import NSObject, NSRect, NSSize, NSPoint, NSMakeRect
except ImportError:
    print("❌ pyobjc가 설치되지 않았습니다. setup.sh를 다시 실행하거나:", flush=True)
    print("   pip install pyobjc-framework-Cocoa pyobjc-core", flush=True)
    sys.exit(1)


# ─── RunCat-style Running Animation ──────────────────────────────────────

# 8 frames of a walking stick figure (SVG viewBox 0 0 16 22)
# Cycle: contact → recoil → passing → high → contact (mirror) → recoil → passing → high
# Body bobs down at contact/recoil, up at passing/high. Arms swing contralateral to legs.
_RUNNER_FRAMES_DATA = [
    {  # Frame 0 - Contact A: far foot heel strike forward, near foot back extended
        "head": (7.5, 4.0, 2.5),
        "body": (8, 6.5, 7.5, 13),
        "arm_near": (8, 9, 11.5, 10.5),
        "arm_far": (8, 9, 4.5, 11),
        "leg_near": [(7.5, 13), (5.5, 17), (3, 21)],
        "leg_far": [(7.5, 13), (10, 16.5), (12, 20)],
    },
    {  # Frame 1 - Recoil A: body lowest, far knee absorbs, near toe pushes off
        "head": (7.5, 4.3, 2.5),
        "body": (8, 6.8, 7.8, 13.3),
        "arm_near": (8, 9.3, 10, 11),
        "arm_far": (8, 9.3, 6, 11),
        "leg_near": [(7.8, 13.3), (5.5, 17.5), (4, 20.5)],
        "leg_far": [(7.8, 13.3), (9, 17), (11, 21)],
    },
    {  # Frame 2 - Passing A→B: body rising, near leg knee high, foot tucked under
        "head": (7.5, 3.5, 2.5),
        "body": (8, 6, 8, 12.5),
        "arm_near": (8, 8.5, 8.5, 11.5),
        "arm_far": (8, 8.5, 7.5, 11),
        "leg_near": [(8, 12.5), (9, 15.5), (7, 18.5)],
        "leg_far": [(8, 12.5), (8.5, 17), (9, 21)],
    },
    {  # Frame 3 - High A→B: body apex, near leg extending forward, foot descending
        "head": (7.5, 3.2, 2.5),
        "body": (8, 5.7, 8.3, 12.2),
        "arm_near": (8, 8.2, 5.5, 11),
        "arm_far": (8, 8.2, 10.5, 11),
        "leg_near": [(8.3, 12.2), (10.8, 16.5), (11.5, 19.5)],
        "leg_far": [(8.3, 12.2), (7, 17), (6, 21)],
    },
    {  # Frame 4 - Contact B: near foot heel strike forward, far foot back extended
        "head": (7.5, 4.0, 2.5),
        "body": (8, 6.5, 8.5, 13),
        "arm_near": (8, 9, 4.5, 10.5),
        "arm_far": (8, 9, 11.5, 11),
        "leg_near": [(8.5, 13), (11, 16.5), (12.5, 20)],
        "leg_far": [(8.5, 13), (6, 17), (3.5, 21)],
    },
    {  # Frame 5 - Recoil B: body lowest, near knee absorbs, far toe pushes off
        "head": (7.5, 4.3, 2.5),
        "body": (8, 6.8, 8.2, 13.3),
        "arm_near": (8, 9.3, 6, 11),
        "arm_far": (8, 9.3, 10, 11),
        "leg_near": [(8.2, 13.3), (10, 17.5), (11, 21)],
        "leg_far": [(8.2, 13.3), (6.5, 17), (4.5, 20.5)],
    },
    {  # Frame 6 - Passing B→A: body rising, far leg knee high, foot tucked under
        "head": (7.5, 3.5, 2.5),
        "body": (8, 6, 8, 12.5),
        "arm_near": (8, 8.5, 7.5, 11.5),
        "arm_far": (8, 8.5, 8.5, 11),
        "leg_near": [(8, 12.5), (8.5, 17), (9, 21)],
        "leg_far": [(8, 12.5), (9, 15.5), (7, 18.5)],
    },
    {  # Frame 7 - High B→A: body apex, far leg extending forward, foot descending
        "head": (7.5, 3.2, 2.5),
        "body": (8, 5.7, 7.7, 12.2),
        "arm_near": (8, 8.2, 10.5, 11),
        "arm_far": (8, 8.2, 5.5, 11),
        "leg_near": [(7.7, 12.2), (7, 17), (6, 21)],
        "leg_far": [(7.7, 12.2), (10.8, 16.5), (11.5, 19.5)],
    },
]


def _draw_runner_frame(fd, w, h):
    """Draw a single runner frame into the current NSImage focus."""
    fg = AppKit.NSColor.blackColor()
    fg_dim = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0, 0, 0, 0.38)

    # Head
    cx, cy, r = fd["head"]
    cy = h - cy
    head_path = AppKit.NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(cx - r, cy - r, r * 2, r * 2)
    )
    fg.setFill()
    head_path.fill()

    # Body
    x1, y1, x2, y2 = fd["body"]
    body = AppKit.NSBezierPath.bezierPath()
    body.moveToPoint_(NSPoint(x1, h - y1))
    body.lineToPoint_(NSPoint(x2, h - y2))
    body.setLineWidth_(1.8)
    body.setLineCapStyle_(1)  # NSLineCapStyleRound
    fg.setStroke()
    body.stroke()

    # Arms
    for key, color in [("arm_near", fg), ("arm_far", fg_dim)]:
        x1, y1, x2, y2 = fd[key]
        arm = AppKit.NSBezierPath.bezierPath()
        arm.moveToPoint_(NSPoint(x1, h - y1))
        arm.lineToPoint_(NSPoint(x2, h - y2))
        arm.setLineWidth_(1.5)
        arm.setLineCapStyle_(1)
        color.setStroke()
        arm.stroke()

    # Legs
    for key, color in [("leg_near", fg), ("leg_far", fg_dim)]:
        pts = fd[key]
        leg = AppKit.NSBezierPath.bezierPath()
        leg.moveToPoint_(NSPoint(pts[0][0], h - pts[0][1]))
        for px, py in pts[1:]:
            leg.lineToPoint_(NSPoint(px, h - py))
        leg.setLineWidth_(1.9)
        leg.setLineCapStyle_(1)
        leg.setLineJoinStyle_(1)  # NSLineJoinStyleRound
        color.setStroke()
        leg.stroke()


def _create_runner_frames():
    """Create NSImage template frames of a walking stick figure."""
    w, h = 16.0, 22.0
    frames = []
    for fd in _RUNNER_FRAMES_DATA:
        img = AppKit.NSImage.alloc().initWithSize_(NSSize(w, h))
        img.lockFocus()
        _draw_runner_frame(fd, w, h)
        img.unlockFocus()
        img.setTemplate_(True)
        frames.append(img)
    return frames


def _create_static_runner():
    """Create a single static runner icon (standing pose, frame 1)."""
    return _create_runner_frames()[1]


def _anim_interval_for_pct(pct):
    """Map usage percentage to animation frame interval (seconds). 8-frame walk cycle.
    0%: 615ms, 80%: 130ms, 90%: 68ms, 100%: 60ms (fastest).
    """
    if pct <= 90:
        return max(0.068, 0.615 - (pct / 90.0) * 0.547)
    return 0.068 - (pct - 90) / 10.0 * 0.008

# Color constants
_BG_COLOR = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.12, 0.12, 0.14, 1.0)
_CARD_BG_COLOR = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.06)
_CARD_BORDER_COLOR = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.10)
_TEXT_PRIMARY = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.90)
_TEXT_SECONDARY = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.60)
_PROGRESS_BG = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.10)
_GREEN = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.204, 0.78, 0.349, 1.0)   # #34C759
_YELLOW = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.84, 0.039, 1.0)    # #FFD60A
_RED = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.271, 0.227, 1.0)       # #FF453A
_SEPARATOR_COLOR = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.08)

POPOVER_WIDTH = 320
CARD_INSET = 12       # horizontal padding inside popover
CARD_PADDING = 10     # padding inside cards
CARD_SPACING = 8      # vertical spacing between cards
INNER_SPACING = 4     # spacing between elements inside a card


def _progress_color(pct):
    """Return green/yellow/red based on percentage."""
    if pct >= 80:
        return _RED
    elif pct >= 60:
        return _YELLOW
    return _GREEN


def _make_text_field(text, font_size=12, color=None, bold=False, align=None):
    """Create an NSTextField label."""
    if color is None:
        color = _TEXT_PRIMARY
    if bold:
        font = AppKit.NSFont.boldSystemFontOfSize_(font_size)
    else:
        font = AppKit.NSFont.systemFontOfSize_(font_size)
    tf = AppKit.NSTextField.labelWithString_(text)
    tf.setFont_(font)
    tf.setTextColor_(color)
    tf.setDrawsBackground_(False)
    tf.setBezeled_(False)
    tf.setEditable_(False)
    tf.setSelectable_(False)
    tf.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    if align is not None:
        tf.setAlignment_(align)
    return tf


class ProgressBarView(AppKit.NSView):
    """Custom NSView that draws a rounded progress bar."""

    def initWithFrame_percentage_color_(self, frame, pct, color):
        self = objc.super(ProgressBarView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._pct = max(0, min(100, pct))
        self._color = color
        return self

    def drawRect_(self, dirty_rect):
        bounds = self.bounds()
        h = bounds.size.height
        w = bounds.size.width
        radius = h / 2.0

        # Background
        bg_path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0, 0, w, h), radius, radius
        )
        _PROGRESS_BG.setFill()
        bg_path.fill()

        # Filled portion
        if self._pct > 0:
            fill_w = max(h, w * self._pct / 100.0)  # at least pill-sized
            fill_path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(0, 0, fill_w, h), radius, radius
            )
            self._color.setFill()
            fill_path.fill()


class CardView(AppKit.NSView):
    """A rounded card container view with dark background and border."""

    def initWithFrame_(self, frame):
        self = objc.super(CardView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(8.0)
        self.layer().setBackgroundColor_(_CARD_BG_COLOR.CGColor())
        self.layer().setBorderColor_(_CARD_BORDER_COLOR.CGColor())
        self.layer().setBorderWidth_(0.5)
        return self


class HistoryBarView(AppKit.NSView):
    """Custom view for a single day bar in the 7-day history with hover label."""

    def initWithFrame_fillRatio_color_(self, frame, ratio, color):
        self = objc.super(HistoryBarView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._ratio = max(0, min(1.0, ratio))
        self._color = color
        self._hover_label = None
        self._cost_text = None
        # Set up mouse tracking
        tracking = AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            (AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveInActiveApp),
            self,
            None,
        )
        self.addTrackingArea_(tracking)
        return self

    def setCostText_(self, text):
        self._cost_text = text

    def mouseEntered_(self, event):
        if not self._cost_text:
            return
        if self._hover_label is None:
            bounds = self.bounds()
            fill_h = max(2, bounds.size.height * self._ratio)
            label = AppKit.NSTextField.alloc().initWithFrame_(
                NSMakeRect(-8, fill_h + 2, bounds.size.width + 16, 14)
            )
            label.setStringValue_(self._cost_text)
            label.setBezeled_(False)
            label.setDrawsBackground_(True)
            label.setBackgroundColor_(
                AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.15, 0.15, 0.18, 0.95)
            )
            label.setTextColor_(_TEXT_PRIMARY)
            label.setFont_(AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(9, 0.0))
            label.setAlignment_(AppKit.NSTextAlignmentCenter)
            label.setEditable_(False)
            label.setSelectable_(False)
            self._hover_label = label
        self.addSubview_(self._hover_label)
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        if self._hover_label:
            self._hover_label.removeFromSuperview()
        self.setNeedsDisplay_(True)

    def drawRect_(self, dirty_rect):
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        fill_h = max(2, h * self._ratio)
        radius = min(2.0, w / 2.0)

        bar_rect = NSMakeRect(0, 0, w, fill_h)
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bar_rect, radius, radius
        )
        self._color.setFill()
        path.fill()


def _build_card(content_height):
    """Create a CardView with given content height, full width minus insets."""
    card_w = POPOVER_WIDTH - 2 * CARD_INSET
    card = CardView.alloc().initWithFrame_(NSMakeRect(0, 0, card_w, content_height + 2 * CARD_PADDING))
    return card


def _add_to_card(card, subview, x, y):
    """Add subview at (x + CARD_PADDING, y + CARD_PADDING) inside card."""
    frame = subview.frame()
    subview.setFrame_(NSMakeRect(x + CARD_PADDING, y + CARD_PADDING, frame.size.width, frame.size.height))
    card.addSubview_(subview)


def _build_progress_section(label_text, pct, reset_text=None, is_estimate=False):
    """Build a card with label, progress bar, percentage, and optional reset time."""
    card_inner_w = POPOVER_WIDTH - 2 * CARD_INSET - 2 * CARD_PADDING
    bar_height = 8
    elements_height = 16 + INNER_SPACING + bar_height + INNER_SPACING + 14
    if reset_text:
        elements_height += INNER_SPACING + 12
    card = _build_card(elements_height)

    y = elements_height  # Build top-down, place bottom-up

    # Label
    suffix = " (예상)" if is_estimate else ""
    lbl = _make_text_field(label_text + suffix, font_size=11, color=_TEXT_SECONDARY)
    lbl.sizeToFit()
    y -= 14
    _add_to_card(card, lbl, 0, y)

    # Progress bar
    color = _progress_color(pct)
    y -= (INNER_SPACING + bar_height)
    bar = ProgressBarView.alloc().initWithFrame_percentage_color_(
        NSMakeRect(0, 0, card_inner_w, bar_height), pct, color
    )
    _add_to_card(card, bar, 0, y)

    # Percentage text (right-aligned)
    pct_text = f"{pct:.0f}%"
    pct_tf = _make_text_field(pct_text, font_size=13, color=color, bold=True)
    pct_tf.sizeToFit()
    pct_w = pct_tf.frame().size.width
    y -= (INNER_SPACING + 14)
    pct_tf.setFrame_(NSMakeRect(
        CARD_PADDING + card_inner_w - pct_w,
        y + CARD_PADDING,
        pct_w, 14
    ))
    card.addSubview_(pct_tf)

    # Reset time (if available)
    if reset_text:
        reset_tf = _make_text_field(f"Resets {reset_text}", font_size=10, color=_TEXT_SECONDARY)
        reset_tf.sizeToFit()
        _add_to_card(card, reset_tf, 0, y - INNER_SPACING - 12)

    return card


class PopoverDelegate(NSObject):
    """NSPopover delegate."""

    def initWithApp_(self, app):
        self = objc.super(PopoverDelegate, self).init()
        if self is None:
            return None
        self._app = app
        return self

    @objc.typedSelector(b"v@:@")
    def popoverDidClose_(self, notification):
        pass


class PopoverToggleTarget(NSObject):
    """NSObject subclass to serve as the action target for the status bar button."""

    def initWithApp_(self, app):
        self = objc.super(PopoverToggleTarget, self).init()
        if self is None:
            return None
        self._app = app
        return self

    @objc.typedSelector(b"v@:@")
    def togglePopover_(self, sender):
        print("[TARGET] togglePopover_ called!", flush=True)
        try:
            self._app.toggle_popover(sender)
        except Exception as e:
            import traceback
            print(f"[TARGET] toggle_popover raised: {e}\n{traceback.format_exc()}", flush=True)


class _ButtonAction(NSObject):
    """NSObject subclass to serve as action target for buttons with a Python callback."""

    def initWithCallback_(self, callback):
        self = objc.super(_ButtonAction, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    @objc.typedSelector(b"v@:@")
    def perform_(self, sender):
        if self._callback:
            try:
                self._callback()
            except Exception as e:
                import traceback
                print(f"[ACTION] callback raised: {e}\n{traceback.format_exc()}", flush=True)


class PopoverViewController(AppKit.NSViewController):
    """Minimal NSViewController to host popover content."""

    def initWithView_(self, view):
        self = objc.super(PopoverViewController, self).init()
        if self is None:
            return None
        self._contentView = view
        return self

    def loadView(self):
        self.setView_(self._contentView)


class HoverButton(AppKit.NSButton):
    """NSButton subclass with hover highlight effect."""

    def initWithFrame_(self, frame):
        self = objc.super(HoverButton, self).initWithFrame_(frame)
        if self is None:
            return None
        self._tracking_area = None
        self._hovered = False
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(4.0)
        self._updateTrackingArea()
        return self

    def _updateTrackingArea(self):
        if self._tracking_area:
            self.removeTrackingArea_(self._tracking_area)
        self._tracking_area = AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            AppKit.NSTrackingMouseEnteredAndExited
            | AppKit.NSTrackingActiveAlways
            | AppKit.NSTrackingInVisibleRect
            | AppKit.NSTrackingEnabledDuringMouseDrag,
            self, None
        )
        self.addTrackingArea_(self._tracking_area)

    def _setHoverState_(self, hovered):
        self._hovered = bool(hovered)
        color = (
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1, 1, 1, 0.08)
            if self._hovered
            else AppKit.NSColor.clearColor()
        )
        self.layer().setBackgroundColor_(color.CGColor())

    def updateTrackingAreas(self):
        objc.super(HoverButton, self).updateTrackingAreas()
        self._updateTrackingArea()

    def mouseEntered_(self, event):
        self._setHoverState_(True)

    def mouseExited_(self, event):
        self._setHoverState_(False)

    def mouseDown_(self, event):
        self._setHoverState_(False)
        objc.super(HoverButton, self).mouseDown_(event)

    def viewDidMoveToWindow(self):
        objc.super(HoverButton, self).viewDidMoveToWindow()
        if self.window() is None:
            self._setHoverState_(False)


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


# ─── Plan recommendation ────────────────────────────────────────────────────

# Claude subscription plans: (name, monthly_price_usd, session_limit, week_limit, description)
CLAUDE_PLANS = [
    ("Pro",     20,   5.0,   50.0,   "기본 사용량"),
    ("Max 5x",  100,  250.0, 2500.0, "5배 사용량"),
    ("Max 20x", 200,  1000.0, 10000.0, "20배 사용량"),
]


def _recommend_plan(tracker, api_cache):
    """Analyze usage and recommend the best subscription plan.
    Returns dict with recommendation details.
    """
    # Gather 30-day cost data
    daily = tracker.daily_costs(30)
    daily_costs_list = [c for _, c in daily]
    active_days = sum(1 for c in daily_costs_list if c > 0.001)
    total_30d = sum(daily_costs_list)

    # Monthly projection: use actual 30-day total as-is
    # (extrapolating active-day averages over-estimates for irregular usage)
    projected_monthly = total_30d

    # Current plan detection
    tier = (api_cache.get("rate_limit_tier") or "").lower()
    current_plan = "알 수 없음"
    for keyword, label in [("max_20x", "Max 20x"), ("max_5x", "Max 5x"), ("max", "Max 5x"), ("pro", "Pro")]:
        if keyword in tier:
            current_plan = label
            break

    # 7-day usage data for utilization analysis
    week_totals, week_models, week_costs = tracker.week_usage()
    week_cost = week_totals.get("cost", 0)

    # Opus ratio (higher Opus usage = needs higher tier)
    opus_cost = sum(v for k, v in week_costs.items() if "opus" in k.lower())
    opus_ratio = opus_cost / week_cost if week_cost > 0 else 0

    # Find the cheapest plan whose weekly limit covers peak usage
    # Calculate peak 7-day rolling cost from daily data
    peak_week_cost = 0
    for i in range(max(len(daily_costs_list) - 6, 0)):
        week_sum = sum(daily_costs_list[i:i+7])
        peak_week_cost = max(peak_week_cost, week_sum)

    recommended = None
    for plan_name, price, sess_lim, week_lim, _desc in CLAUDE_PLANS:
        # Plan is suitable if its weekly limit covers peak usage with 20% headroom
        if peak_week_cost <= week_lim * 0.8:
            recommended = (plan_name, price)
            break

    if recommended is None:
        recommended = ("Max 20x", 200)

    rec_name, rec_price = recommended

    # Build reason
    reasons = []
    if rec_name == "Pro":
        reasons.append(f"피크 주간 ${peak_week_cost:.0f} → Pro 한도 내")
    elif rec_name == "Max 5x":
        reasons.append(f"피크 주간 ${peak_week_cost:.0f} → Max 5x 한도 내")
    else:
        reasons.append(f"피크 주간 ${peak_week_cost:.0f} → Max 20x 필요")

    if opus_ratio > 0.5:
        reasons.append(f"Opus 비중 높음 ({opus_ratio:.0%})")

    # Savings calculation
    savings = None
    if current_plan != "알 수 없음" and current_plan != rec_name:
        current_price = next((p for n, p, _, _, _ in CLAUDE_PLANS if n == current_plan), None)
        if current_price and rec_price < current_price:
            savings = current_price - rec_price

    # Per-plan fit info
    plan_fits = []
    for plan_name, price, sess_lim, week_lim, _desc in CLAUDE_PLANS:
        usage_pct = (peak_week_cost / week_lim * 100) if week_lim else 0
        plan_fits.append({
            "name": plan_name,
            "price": price,
            "week_limit": week_lim,
            "usage_pct": usage_pct,
            "fits": peak_week_cost <= week_lim * 0.8,
        })

    return {
        "current_plan": current_plan,
        "recommended": rec_name,
        "rec_price": rec_price,
        "projected_monthly": projected_monthly,
        "total_30d": total_30d,
        "active_days": active_days,
        "peak_week_cost": peak_week_cost,
        "opus_ratio": opus_ratio,
        "reasons": reasons,
        "savings": savings,
        "plan_fits": plan_fits,
    }


def _current_plan_price_text(rec):
    """Return the current plan price text, or '?' when the plan is unknown."""
    if rec["current_plan"] == rec["recommended"]:
        return str(rec["rec_price"])

    current_price = next(
        (p for n, p, _, _, _ in CLAUDE_PLANS if n == rec["current_plan"]),
        None,
    )
    return str(current_price) if current_price is not None else "?"


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
        snapshot = _load_last_title_snapshot()
        initial_title = _format_title_from_data(snapshot) if snapshot else "⚡ Claude"
        super().__init__(name="Claude Usage", title=initial_title, quit_button=None)
        self.tracker = UsageTracker()
        self.view_mode = "dashboard"
        self._alerted = {"session": set(), "week": set()}
        self._last_reset = {"session": None, "week": None}
        # Popover state
        self._popover = None
        self._popover_scroll = None
        self._popover_view_controller = None
        self._toggle_target = None
        self._popover_delegate = None
        self._settings_visible = False
        self._action_refs = []
        self._section_headers = {}
        self._pending_section_anchor = None
        self._collapse_state = {
            "detail": False,
            "model": False,
            "history": False,
            "plan": False,
        }
        self._cached_data = None
        # Animation state
        self._anim_frames = None
        self._anim_static = None
        self._anim_index = 0
        self._anim_timer = None
        self._anim_interval = 2.0
        self._anim_pct = 0
        self._last_title_snapshot = snapshot
        # Minimal fallback menu (quit only)
        self.menu.add(rumps.MenuItem("종료", callback=rumps.quit_application))

    @rumps.timer(1)
    def _initial_load(self, timer):
        """First load after run loop starts — set up NSPopover."""
        timer.stop()
        # Hide Dock icon
        try:
            AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        except Exception:
            pass

        if self._last_title_snapshot:
            self.title = _format_title_from_data(self._last_title_snapshot)

        # Set up NSPopover
        try:
            button = self._nsapp.nsstatusitem.button()
            self._nsapp.nsstatusitem.setMenu_(None)

            self._popover = AppKit.NSPopover.alloc().init()
            self._popover.setBehavior_(1)  # NSPopoverBehaviorTransient
            self._popover.setAppearance_(
                AppKit.NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")
            )

            self._toggle_target = PopoverToggleTarget.alloc().initWithApp_(self)
            button.setTarget_(self._toggle_target)
            sel = objc.selector(self._toggle_target.togglePopover_, signature=b'v@:@')
            button.setAction_(sel)
            print(f"[POPOVER] target={button.target()}, action={button.action()}", flush=True)
            self._popover_delegate = PopoverDelegate.alloc().initWithApp_(self)
            self._popover.setDelegate_(self._popover_delegate)
            print("[POPOVER] NSPopover set up successfully", flush=True)
        except Exception as e:
            print(f"[POPOVER] Setup failed: {e}", flush=True)

        self._register_wake_observer()
        # Initialize animation frames
        try:
            self._anim_frames = _create_runner_frames()
            self._anim_static = _create_static_runner()
            print("[ANIM] Lightning frames created", flush=True)
        except Exception as e:
            print(f"[ANIM] Frame creation failed: {e}", flush=True)

        # Initial data fetch in background to avoid blocking main thread at startup
        import threading
        def _initial_bg():
            try:
                new_data = self._gather_data()
                if new_data:
                    from PyObjCTools import AppHelper
                    def _update():
                        self._cached_data = new_data
                        self._apply_main_thread_updates(new_data)
                    AppHelper.callAfter(_update)
            except Exception as e:
                print(f"[DEBUG] _initial_load error: {e}", flush=True)
        threading.Thread(target=_initial_bg, daemon=True).start()

        # Start animation if enabled
        self._start_animation()

        self._refresh_timer = rumps.Timer(self._on_tick, REFRESH_INTERVAL_SEC)
        self._refresh_timer.start()

    # ── animation ───────────────────────────────────────────────────────

    def _start_animation(self):
        """Start or restart the pulse animation timer."""
        self._stop_animation()
        config = _load_config()
        if not config.get("animation_enabled", True) or not self._anim_frames:
            # Set static icon
            if self._anim_static:
                try:
                    button = self._nsapp.nsstatusitem.button()
                    button.setImage_(self._anim_static)
                except Exception as e:
                    print(f"[ANIM] static icon error: {e}", flush=True)
            return
        # Set initial frame immediately
        try:
            button = self._nsapp.nsstatusitem.button()
            button.setImage_(self._anim_frames[0])
            print(f"[ANIM] Started animation, interval={self._anim_interval:.2f}s", flush=True)
        except Exception as e:
            print(f"[ANIM] initial frame error: {e}", flush=True)
        self._anim_timer = rumps.Timer(self._on_anim_tick, self._anim_interval)
        self._anim_timer.start()

    def _stop_animation(self):
        """Stop the animation timer."""
        if self._anim_timer:
            self._anim_timer.stop()
            self._anim_timer = None

    def _on_anim_tick(self, _=None):
        """Advance to the next animation frame."""
        if not self._anim_frames:
            return
        try:
            self._anim_index = (self._anim_index + 1) % len(self._anim_frames)
            button = self._nsapp.nsstatusitem.button()
            button.setImage_(self._anim_frames[self._anim_index])
        except Exception as e:
            print(f"[ANIM] tick error: {e}", flush=True)

    def _update_anim_speed(self, pct):
        """Update animation speed based on usage percentage."""
        new_interval = _anim_interval_for_pct(pct)
        # Only restart if interval changed significantly (>10%)
        if abs(new_interval - self._anim_interval) / max(self._anim_interval, 0.01) > 0.1:
            old = self._anim_interval
            self._anim_interval = new_interval
            self._anim_pct = pct
            config = _load_config()
            if config.get("animation_enabled", True) and self._anim_frames:
                self._stop_animation()
                self._anim_timer = rumps.Timer(self._on_anim_tick, self._anim_interval)
                self._anim_timer.start()
                print(f"[ANIM] Speed updated: {old:.2f}s -> {new_interval:.2f}s (pct={pct:.0f}%)", flush=True)

    def _close_popover(self):
        """Close popover if visible."""
        if self._popover and self._popover.isShown():
            self._popover.close()

    def toggle_popover(self, sender):
        """Toggle the NSPopover open/closed."""
        print(
            f"[TOGGLE] called, popover={self._popover is not None}, "
            f"isShown={self._popover.isShown() if self._popover else 'N/A'}",
            flush=True,
        )
        if not self._popover:
            return
        if self._popover.isShown():
            print("[TOGGLE] closing popover", flush=True)
            self._close_popover()
        else:
            print("[TOGGLE] opening popover", flush=True)
            # Reset collapse/settings state on every open
            self._settings_visible = False
            self._collapse_state = {k: False for k in self._collapse_state}
            # Show immediately with cached data, then refresh in background
            try:
                self._rebuild_popover_content()
            except Exception as e:
                print(f"[TOGGLE] _rebuild_popover_content error: {e}", flush=True)
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            button = self._nsapp.nsstatusitem.button()
            print(f"[TOGGLE] calling showRelativeToRect, button={button}", flush=True)
            self._popover.showRelativeToRect_ofView_preferredEdge_(
                button.bounds(), button, AppKit.NSMinYEdge
            )
            print(f"[TOGGLE] after show, isShown={self._popover.isShown()}", flush=True)
            # Background data refresh
            import threading
            def _bg_refresh():
                try:
                    new_data = self._gather_data()
                    # Update on main thread (title, animation, popover)
                    from PyObjCTools import AppHelper
                    def _update():
                        self._cached_data = new_data
                        self._apply_main_thread_updates(new_data)
                        if self._popover and self._popover.isShown():
                            self._rebuild_popover_content()
                    AppHelper.callAfter(_update)
                except Exception as e:
                    print(f"[POPOVER] Background refresh error: {e}", flush=True)
            threading.Thread(target=_bg_refresh, daemon=True).start()

    def _register_wake_observer(self):
        """Listen for macOS wake-from-sleep to trigger immediate refresh."""
        try:
            self._wake_retry_count = 0
            self._wake_retry_timer = None
            self._last_active_time = time.time()

            def on_wake(_notification):
                print("[WAKE] System woke from sleep, scheduling refresh...", flush=True)
                self._wake_retry_count = 0
                self._start_wake_retry()

            self._on_wake_callback = on_wake

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
        """Start retry cycle after wake."""
        if self._wake_retry_timer is not None:
            self._wake_retry_timer.stop()
        delays = [5, 10, 20, 40, 60]
        delay = delays[min(self._wake_retry_count, len(delays) - 1)]
        print(f"[WAKE] Retry #{self._wake_retry_count + 1} in {delay}s...", flush=True)
        self._wake_retry_timer = rumps.Timer(self._wake_refresh, delay)
        self._wake_retry_timer.start()

    def _wake_refresh(self, timer):
        """Called after wake-from-sleep — runs data fetch in background to avoid blocking main thread."""
        timer.stop()
        self._wake_retry_timer = None
        max_retries = 5
        import threading
        def _bg():
            try:
                print("[WAKE] Attempting refresh...", flush=True)
                new_data = self._gather_data(force_api=True)
                if new_data:
                    from PyObjCTools import AppHelper
                    def _update():
                        self._cached_data = new_data
                        self._apply_main_thread_updates(new_data)
                        self._wake_retry_count = 0
                        print("[WAKE] Refresh successful", flush=True)
                    AppHelper.callAfter(_update)
                else:
                    raise Exception("no data returned")
            except Exception as e:
                self._wake_retry_count += 1
                if self._wake_retry_count < max_retries:
                    print(f"[WAKE] Refresh failed ({e}), will retry...", flush=True)
                    from PyObjCTools import AppHelper
                    AppHelper.callAfter(self._start_wake_retry)
                else:
                    print("[WAKE] Max retries reached, giving up.", flush=True)
                    self._wake_retry_count = 0
        threading.Thread(target=_bg, daemon=True).start()

    # ── popover content builder ──────────────────────────────────────────

    def _rebuild(self, force_api=False):
        """Gather data and update title bar. Popover content is built on open.
        MUST run on the main thread (updates NSTimer, title bar, alerts).
        """
        self._last_active_time = time.time()
        try:
            self._cached_data = self._gather_data(force_api=force_api)
            if self._cached_data:
                self._apply_main_thread_updates(self._cached_data)
        except Exception as e:
            print(f"[REBUILD] Error: {e}", flush=True)
            self._cached_data = None

    def _apply_main_thread_updates(self, data):
        """Apply updates that MUST run on the main thread (title, animation, alerts)."""
        config = data["config"]
        sess_pct = data["sess_pct"]
        week_pct = data["week_pct"]
        api_ok = data["api_ok"]
        api_stale = data["api_stale"]

        # Update title bar
        title_data = {
            "title_mode": config.get("title_display", "session"),
            "sess_pct": sess_pct,
            "week_pct": week_pct,
            "api_stale": (api_stale or not api_ok),
        }
        self.title = _format_title_from_data(title_data)
        _save_last_title_snapshot(title_data)

        # Update animation speed (NSTimer — main thread only)
        self._update_anim_speed(sess_pct)

        # Alerts
        self._check_alerts(sess_pct, data.get("sess_reset"),
                           week_pct, data.get("week_reset"))

    def _gather_data(self, force_api=False):
        """Gather all usage data into a dict for popover rendering."""
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

        daily = self.tracker.daily_costs(7)
        try:
            rec = _recommend_plan(self.tracker, _api_cache)
        except Exception:
            rec = None

        return {
            "config": config,
            "api_ok": api_ok,
            "api_stale": api_stale,
            "sess_pct": sess_pct, "sess_reset": sess_reset,
            "week_pct": week_pct, "week_reset": week_reset,
            "sonnet_pct": sonnet_pct, "sonnet_reset": sonnet_reset,
            "sess_totals": sess_totals, "week_totals": week_totals,
            "today_totals": today_totals,
            "week_models": week_models, "week_costs": week_costs,
            "daily": daily,
            "rec": rec,
        }

    def _rebuild_popover_content(self):
        """Build NSViews for the popover content using cached data."""
        if not self._popover:
            return
        self._action_refs = []
        self._section_headers = {}

        data = self._cached_data
        if not data:
            return

        config = data["config"]
        card_w = POPOVER_WIDTH - 2 * CARD_INSET
        card_inner_w = card_w - 2 * CARD_PADDING
        views = []  # list of (view, height)

        # 1. Account info
        acct_email = _api_cache.get("account_email")
        acct_name = _api_cache.get("account_name")
        if acct_email:
            label = f"{acct_name} ({acct_email})" if acct_name else acct_email
            tf = _make_text_field(label, font_size=11, color=_TEXT_SECONDARY)
            tf.setFrame_(NSMakeRect(CARD_INSET, 0, card_w, 16))
            views.append((tf, 16))

        # 2. API status
        if not data["api_ok"]:
            tf = _make_text_field("⚠️ Usage API 일시 장애", font_size=11, color=_YELLOW)
            tf.setFrame_(NSMakeRect(CARD_INSET, 0, card_w, 16))
            views.append((tf, 16))
        elif data["api_stale"]:
            ago = int(time.time() - _api_cache["fetched_at"])
            tf = _make_text_field(f"⏳ 캐시 데이터 ({ago}초 전)", font_size=11, color=_TEXT_SECONDARY)
            tf.setFrame_(NSMakeRect(CARD_INSET, 0, card_w, 16))
            views.append((tf, 16))

        # 3. Session progress card
        if config.get("show_session", True):
            est = "" if data["api_ok"] else " (예상)"
            reset_text = _fmt_reset(data["sess_reset"]) if data["sess_reset"] else None
            card = _build_progress_section(f"Current session{est}", data["sess_pct"],
                                           reset_text=reset_text, is_estimate=not data["api_ok"])
            views.append((card, card.frame().size.height))

        # 4. Week progress card
        if config.get("show_week", True):
            est = "" if data["api_ok"] else " (예상)"
            reset_text = _fmt_reset(data["week_reset"]) if data["week_reset"] else None
            card = _build_progress_section(f"Current week (all models){est}", data["week_pct"],
                                           reset_text=reset_text, is_estimate=not data["api_ok"])
            views.append((card, card.frame().size.height))

        # 5. Sonnet progress card
        if config.get("show_sonnet", True) and data["sonnet_pct"] is not None:
            reset_text = _fmt_reset(data["sonnet_reset"]) if data["sonnet_reset"] else None
            card = _build_progress_section("Current week (Sonnet only)", data["sonnet_pct"],
                                           reset_text=reset_text)
            views.append((card, card.frame().size.height))

        # 6. Separator
        sep = AppKit.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, card_w, 1))
        sep.setWantsLayer_(True)
        sep.layer().setBackgroundColor_(_SEPARATOR_COLOR.CGColor())
        views.append((sep, 1))

        # 7. Detail usage (collapsible)
        views.extend(self._build_detail_section(data, card_w, card_inner_w))
        # 8. Model breakdown (collapsible)
        views.extend(self._build_model_section(data, card_w, card_inner_w))
        # 9. 7-day history (collapsible)
        views.extend(self._build_history_section(data, card_w, card_inner_w))
        # 10. Plan recommendation (collapsible)
        views.extend(self._build_plan_section(data, card_w, card_inner_w))

        # 11. Separator
        sep2 = AppKit.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, card_w, 1))
        sep2.setWantsLayer_(True)
        sep2.layer().setBackgroundColor_(_SEPARATOR_COLOR.CGColor())
        views.append((sep2, 1))

        # 12. Settings (inline, toggled)
        if self._settings_visible:
            views.extend(self._build_settings_views(data, card_w, card_inner_w))

        # 13. Update notice
        new_ver, release_url = _check_update()
        if new_ver:
            update_card = self._build_update_card(new_ver, release_url, card_w, card_inner_w)
            views.append((update_card, update_card.frame().size.height))

        # 14. Footer
        footer = self._build_footer(card_w)
        views.append((footer, footer.frame().size.height))

        # Assemble into scroll view
        total_h = sum(h for _, h in views) + CARD_SPACING * (len(views) + 1)
        max_h = min(total_h, 500)

        container = AppKit.NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, POPOVER_WIDTH, total_h)
        )
        container.setWantsLayer_(True)
        container.layer().setBackgroundColor_(_BG_COLOR.CGColor())

        y = total_h - CARD_SPACING
        for view, h in views:
            y -= h
            frame = view.frame()
            x = (POPOVER_WIDTH - frame.size.width) / 2.0
            view.setFrame_(NSMakeRect(x, y, frame.size.width, h))
            container.addSubview_(view)
            y -= CARD_SPACING

        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, 0, POPOVER_WIDTH, max_h)
        )
        _configure_popover_scroll_view(scroll)
        scroll.setDocumentView_(container)
        self._popover.setContentSize_(NSSize(POPOVER_WIDTH, max_h))
        self._popover_scroll = scroll
        if self._popover_view_controller is None:
            self._popover_view_controller = PopoverViewController.alloc().initWithView_(scroll)
            self._popover.setContentViewController_(self._popover_view_controller)
        else:
            self._popover_view_controller.setView_(scroll)
        self._restore_pending_section_anchor()

    def _capture_section_anchor(self, key):
        """Remember the clicked section header's current position inside the viewport."""
        if not self._popover_scroll:
            self._pending_section_anchor = None
            return

        header = self._section_headers.get(key)
        clip = self._popover_scroll.contentView() if self._popover_scroll else None
        if not header or not clip:
            self._pending_section_anchor = None
            return

        clip_bounds = clip.bounds()
        header_y = header.frame().origin.y
        viewport_offset = header_y - clip_bounds.origin.y
        self._pending_section_anchor = {
            "key": key,
            "viewport_offset": viewport_offset,
        }

    def _restore_pending_section_anchor(self):
        """Restore the clicked section header near its previous viewport position."""
        anchor = self._pending_section_anchor
        self._pending_section_anchor = None
        if not anchor or not self._popover_scroll:
            return

        header = self._section_headers.get(anchor["key"])
        clip = self._popover_scroll.contentView()
        doc = self._popover_scroll.documentView()
        if not header or not clip or not doc:
            return

        origin_y = _compute_anchor_scroll_origin(
            header.frame().origin.y,
            anchor["viewport_offset"],
            doc.frame().size.height,
            clip.bounds().size.height,
        )
        clip.scrollToPoint_(NSPoint(0, origin_y))
        self._popover_scroll.reflectScrolledClipView_(clip)

    # ── collapsible section helpers ──

    def _make_section_header(self, title, key, card_w):
        """Create a clickable header for a collapsible section."""
        expanded = self._collapse_state.get(key, False)
        arrow = "▼" if expanded else "▶"
        btn = HoverButton.alloc().initWithFrame_(NSMakeRect(0, 0, card_w, 24))
        btn.setTitle_(f" {arrow}  {title}")
        btn.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        btn.setAlignment_(AppKit.NSTextAlignmentLeft)
        btn.setBordered_(False)
        btn.setContentTintColor_(_TEXT_SECONDARY)
        self._section_headers[key] = btn

        def toggle():
            self._capture_section_anchor(key)
            self._collapse_state[key] = not self._collapse_state.get(key, False)
            self._rebuild_popover_content()

        toggler = _ButtonAction.alloc().initWithCallback_(toggle)
        btn.setTarget_(toggler)
        btn.setAction_(objc.selector(toggler.perform_, signature=b'v@:@'))
        self._action_refs.append(toggler)
        return btn

    def _build_detail_section(self, data, card_w, card_inner_w):
        """Build the detail usage collapsible section."""
        views = []
        header = self._make_section_header("📊 세부 사용량", "detail", card_w)
        views.append((header, 24))

        if not self._collapse_state.get("detail", False):
            return views

        today = data["today_totals"]
        sess = data["sess_totals"]
        week = data["week_totals"]

        # Each period: title row, then key-value pairs
        periods = [
            ("오늘", today, False),
            ("세션 (5h)", sess, True),
            ("주간 (7d)", week, False),
        ]

        row_h = 14
        section_gap = 8
        # Calculate height: each period has title + 2~3 data rows
        num_rows = 0
        for label, d, show_cache in periods:
            num_rows += 1  # title
            num_rows += 2  # tokens, cost
            if show_cache and (d.get("cache_write", 0) or d.get("cache_read", 0)):
                num_rows += 1
        total_h = num_rows * row_h + (len(periods) - 1) * section_gap

        card = _build_card(total_h)
        y = total_h

        for idx, (label, d, show_cache) in enumerate(periods):
            # Section title
            y -= row_h
            tf = _make_text_field(label, font_size=10, color=_TEXT_PRIMARY, bold=True)
            tf.setFrame_(NSMakeRect(CARD_PADDING, y + CARD_PADDING, card_inner_w, row_h))
            card.addSubview_(tf)

            # Tokens row: left "입력/출력", right "총 토큰"
            y -= row_h
            in_t = fmt_tokens(d.get('input', 0))
            out_t = fmt_tokens(d.get('output', 0))
            tf_left = _make_text_field(f"입력 {in_t} / 출력 {out_t}", font_size=9,
                                        color=_TEXT_SECONDARY, bold=False)
            tf_left.setFrame_(NSMakeRect(CARD_PADDING + 8, y + CARD_PADDING, card_inner_w * 0.65, row_h))
            card.addSubview_(tf_left)
            tf_right = _make_text_field(fmt_tokens(d.get('total', 0)), font_size=9,
                                         color=_TEXT_PRIMARY, bold=False)
            tf_right.setAlignment_(AppKit.NSTextAlignmentRight)
            tf_right.setFrame_(NSMakeRect(CARD_PADDING + card_inner_w * 0.65, y + CARD_PADDING,
                                           card_inner_w * 0.35 - 8, row_h))
            card.addSubview_(tf_right)

            # Cache row (session only)
            if show_cache and (d.get("cache_write", 0) or d.get("cache_read", 0)):
                y -= row_h
                cache_w = fmt_tokens(d.get('cache_write', 0))
                cache_r = fmt_tokens(d.get('cache_read', 0))
                tf = _make_text_field(f"캐시 쓰기 {cache_w} / 읽기 {cache_r}", font_size=9,
                                       color=_TEXT_SECONDARY, bold=False)
                tf.setFrame_(NSMakeRect(CARD_PADDING + 8, y + CARD_PADDING, card_inner_w, row_h))
                card.addSubview_(tf)

            # Cost + requests row
            y -= row_h
            cost_text = fmt_cost(d.get('cost', 0))
            req_text = f"{d.get('requests', 0)}회"
            tf_left = _make_text_field(f"{req_text} 요청", font_size=9,
                                        color=_TEXT_SECONDARY, bold=False)
            tf_left.setFrame_(NSMakeRect(CARD_PADDING + 8, y + CARD_PADDING, card_inner_w * 0.5, row_h))
            card.addSubview_(tf_left)
            tf_right = _make_text_field(cost_text, font_size=10,
                                         color=_TEXT_PRIMARY, bold=True)
            tf_right.setAlignment_(AppKit.NSTextAlignmentRight)
            tf_right.setFrame_(NSMakeRect(CARD_PADDING + card_inner_w * 0.5, y + CARD_PADDING,
                                           card_inner_w * 0.5 - 8, row_h))
            card.addSubview_(tf_right)

            # Gap between periods
            if idx < len(periods) - 1:
                y -= section_gap
                sep_y = y + CARD_PADDING + section_gap // 2
                sep = AppKit.NSView.alloc().initWithFrame_(
                    NSMakeRect(CARD_PADDING + 8, sep_y, card_inner_w - 16, 1)
                )
                sep.setWantsLayer_(True)
                sep.layer().setBackgroundColor_(_SEPARATOR_COLOR.CGColor())
                card.addSubview_(sep)

        views.append((card, card.frame().size.height))
        return views

    def _build_model_section(self, data, card_w, card_inner_w):
        """Build model breakdown collapsible section with proportion bars."""
        views = []
        week_models = data.get("week_models", {})
        week_costs = data.get("week_costs", {})
        if not week_models:
            return views

        header = self._make_section_header("🤖 모델별", "model", card_w)
        views.append((header, 24))

        if not self._collapse_state.get("model", False):
            return views

        sorted_models = sorted(week_costs, key=week_costs.get, reverse=True)
        total_cost = sum(week_costs.values())
        max_cost = max(week_costs.values()) if week_costs else 1

        row_h = 14
        bar_h = 4
        model_row_h = row_h + bar_h + 6  # label + bar + gap
        total_h = len(sorted_models) * model_row_h - 6  # no gap after last

        card = _build_card(total_h)
        y = total_h

        # Color palette for models
        model_colors = [
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.55, 0.47, 0.96, 1.0),  # purple
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.20, 0.78, 0.35, 1.0),  # green
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.30, 0.65, 0.95, 1.0),  # blue
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.00, 0.72, 0.25, 1.0),  # orange
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.95, 0.40, 0.40, 1.0),  # red
        ]

        for i, m in enumerate(sorted_models):
            d = week_models[m]
            cost = week_costs[m]
            total_t = sum(d.values())
            short = m.split("/")[-1] if "/" in m else m
            pct = (cost / total_cost * 100) if total_cost > 0 else 0
            bar_pct = (cost / max_cost * 100) if max_cost > 0 else 0
            color = model_colors[i % len(model_colors)]

            # Label row: model name left, cost + percentage right
            y -= row_h
            tf_left = _make_text_field(short, font_size=10,
                                        color=_TEXT_PRIMARY, bold=(i == 0))
            tf_left.setFrame_(NSMakeRect(CARD_PADDING, y + CARD_PADDING, card_inner_w * 0.5, row_h))
            card.addSubview_(tf_left)

            tf_right = _make_text_field(f"{fmt_cost(cost)}  {pct:.0f}%", font_size=9,
                                         color=_TEXT_SECONDARY, bold=False)
            tf_right.setAlignment_(AppKit.NSTextAlignmentRight)
            tf_right.setFrame_(NSMakeRect(CARD_PADDING + card_inner_w * 0.5, y + CARD_PADDING,
                                           card_inner_w * 0.5, row_h))
            card.addSubview_(tf_right)

            # Proportion bar
            y -= (bar_h + 2)
            bar = ProgressBarView.alloc().initWithFrame_percentage_color_(
                NSMakeRect(CARD_PADDING, y + CARD_PADDING, card_inner_w, bar_h),
                bar_pct, color
            )
            card.addSubview_(bar)

            y -= 4  # gap

        views.append((card, card.frame().size.height))
        return views

    def _build_history_section(self, data, card_w, card_inner_w):
        """Build 7-day history with bar chart."""
        views = []
        daily = data.get("daily", [])
        costs = [c for _, c in daily]
        total_7d = sum(costs)
        spark = make_sparkline(costs)

        header = self._make_section_header(f"📈 7일  {spark}  {fmt_cost(total_7d)}", "history", card_w)
        views.append((header, 24))

        if not self._collapse_state.get("history", False):
            return views

        max_cost = max(costs) if costs else 1
        bar_area_h = 60
        label_h = 14
        chart_h = bar_area_h + label_h + CARD_PADDING * 2
        card = _build_card(chart_h)

        num_bars = len(daily)
        if num_bars > 0:
            gap = 4
            bar_w = (card_inner_w - gap * (num_bars - 1)) / num_bars
            for i, (date_str, cost) in enumerate(daily):
                ratio = cost / max_cost if max_cost > 0 else 0
                color = _progress_color(ratio * 100)
                x = CARD_PADDING + i * (bar_w + gap)

                bar = HistoryBarView.alloc().initWithFrame_fillRatio_color_(
                    NSMakeRect(x, CARD_PADDING + label_h, bar_w, bar_area_h),
                    ratio, color
                )
                card.addSubview_(bar)

                day_lbl = _make_text_field(date_str.split("/")[-1], font_size=8,
                                            color=_TEXT_SECONDARY)
                day_lbl.setAlignment_(AppKit.NSTextAlignmentCenter)
                day_lbl.setFrame_(NSMakeRect(x, CARD_PADDING, bar_w, label_h))
                card.addSubview_(day_lbl)

                bar.setCostText_(fmt_cost(cost))

        views.append((card, card.frame().size.height))
        return views

    def _build_plan_section(self, data, card_w, card_inner_w):
        """Build plan recommendation collapsible section."""
        views = []
        rec = data.get("rec")
        if not rec:
            return views

        header = self._make_section_header("💡 플랜 추천", "plan", card_w)
        views.append((header, 24))

        if not self._collapse_state.get("plan", False):
            return views

        line_h = 15
        bar_h = 5
        plan_fits = rec['plan_fits']

        # Card height calculation
        # Row 1: title + result line
        # Row 2: peak week info
        # Separator
        # Plan rows: label line + bar + spacing
        plan_section_h = len(plan_fits) * (14 + 2 + bar_h + 8) - 8  # last row no bottom margin
        result_h = line_h  # result message
        total_h = line_h + line_h + 6 + plan_section_h + 4 + result_h

        card = _build_card(total_h)
        y = total_h

        # Title: current plan
        y -= line_h
        tf = _make_text_field(f"{rec['current_plan']} (${_current_plan_price_text(rec)}/월)",
                               font_size=11, color=_TEXT_PRIMARY, bold=True)
        tf.setFrame_(NSMakeRect(CARD_PADDING, y + CARD_PADDING, card_inner_w, line_h))
        card.addSubview_(tf)

        # Peak week cost vs limit
        y -= line_h
        cur_plan = next((pf for pf in plan_fits if pf['name'] == rec['current_plan']), None)
        limit_text = f"${cur_plan['week_limit']:.0f}" if cur_plan else "?"
        tf = _make_text_field(f"피크 주간: {fmt_cost(rec['peak_week_cost'])} / {limit_text} 한도",
                               font_size=10, color=_TEXT_SECONDARY, bold=False)
        tf.setFrame_(NSMakeRect(CARD_PADDING, y + CARD_PADDING, card_inner_w, line_h))
        card.addSubview_(tf)

        y -= 6  # separator spacing

        # Plan comparison bars
        for idx, pf in enumerate(plan_fits):
            usage_pct = pf['usage_pct']
            display_pct = min(usage_pct, 100)
            if usage_pct > 100:
                bar_color = _RED
            elif usage_pct > 80:
                bar_color = _YELLOW
            else:
                bar_color = _GREEN

            is_cur = pf['name'] == rec['current_plan']
            is_rec = pf['name'] == rec['recommended']

            # Label: "Pro $20  ━━━ 150% 초과" or "Max 5x $100  ━━ 36% ←"
            pct_text = f"{usage_pct:.0f}%"
            if usage_pct > 100:
                pct_text += " 초과"
            suffix = ""
            if is_cur:
                suffix = " ←"
            elif is_rec and not is_cur:
                suffix = " ⭐"

            y -= 14
            left_label = f"{pf['name']} ${pf['price']}"
            right_label = f"{pct_text}{suffix}"
            # Left-aligned plan name
            tf_left = _make_text_field(left_label, font_size=10,
                                        color=_TEXT_PRIMARY if (is_cur or is_rec) else _TEXT_SECONDARY,
                                        bold=(is_cur or is_rec))
            tf_left.setFrame_(NSMakeRect(CARD_PADDING, y + CARD_PADDING, card_inner_w * 0.5, 14))
            card.addSubview_(tf_left)
            # Right-aligned percentage
            tf_right = _make_text_field(right_label, font_size=10,
                                         color=bar_color,
                                         bold=False)
            tf_right.setFrame_(NSMakeRect(CARD_PADDING + card_inner_w * 0.5, y + CARD_PADDING, card_inner_w * 0.5, 14))
            tf_right.setAlignment_(AppKit.NSTextAlignmentRight)
            card.addSubview_(tf_right)

            # Progress bar (tight to its label, 2px gap)
            y -= (bar_h + 2)
            bar = ProgressBarView.alloc().initWithFrame_percentage_color_(
                NSMakeRect(CARD_PADDING, y + CARD_PADDING, card_inner_w, bar_h),
                display_pct, bar_color
            )
            card.addSubview_(bar)

            # Space before next plan row (8px)
            if idx < len(plan_fits) - 1:
                y -= 8

        y -= 4

        # Result message
        y -= line_h
        if rec['recommended'] != rec['current_plan']:
            if rec['savings']:
                msg = f"⭐ {rec['recommended']}으로 변경 시 월 ${rec['savings']} 절약"
                msg_color = _YELLOW
            else:
                msg = f"⭐ {rec['recommended']} 추천"
                msg_color = _YELLOW
        else:
            msg = "✓ 현재 플랜이 적합합니다"
            msg_color = _GREEN
        tf = _make_text_field(msg, font_size=11, color=msg_color, bold=True)
        tf.setFrame_(NSMakeRect(CARD_PADDING, y + CARD_PADDING, card_inner_w, line_h))
        card.addSubview_(tf)

        views.append((card, card.frame().size.height))
        return views

    def _build_text_card(self, lines, card_w, card_inner_w):
        """Build a card with multiple text lines. lines = [(text, is_bold), ...]"""
        line_h = 15
        total_h = len(lines) * line_h
        card = _build_card(total_h)

        y = total_h - line_h
        for text, is_bold in lines:
            if text:
                tf = _make_text_field(text, font_size=11,
                                       color=_TEXT_PRIMARY if is_bold else _TEXT_SECONDARY,
                                       bold=is_bold)
                tf.setFrame_(NSMakeRect(CARD_PADDING, y + CARD_PADDING, card_inner_w, line_h))
                card.addSubview_(tf)
            y -= line_h

        return card

    def _build_update_card(self, new_ver, release_url, card_w, card_inner_w):
        """Build update notification card."""
        card_h = 44
        card = _build_card(card_h)

        lbl = _make_text_field(f"⬆️ {new_ver} 업데이트 가능", font_size=11, color=_GREEN, bold=True)
        lbl.setFrame_(NSMakeRect(CARD_PADDING, CARD_PADDING + 22, card_inner_w, 16))
        card.addSubview_(lbl)

        btn = HoverButton.alloc().initWithFrame_(NSMakeRect(CARD_PADDING, CARD_PADDING + 2, 80, 18))
        btn.setTitle_("업데이트")
        btn.setFont_(AppKit.NSFont.systemFontOfSize_(10))
        btn.setBordered_(False)
        btn.setContentTintColor_(_GREEN)
        updater = _ButtonAction.alloc().initWithCallback_(lambda v=new_ver: _auto_update(v))
        btn.setTarget_(updater)
        btn.setAction_(objc.selector(updater.perform_, signature=b'v@:@'))
        self._action_refs.append(updater)
        card.addSubview_(btn)

        notes_btn = HoverButton.alloc().initWithFrame_(NSMakeRect(CARD_PADDING + 90, CARD_PADDING + 2, 80, 18))
        notes_btn.setTitle_("릴리즈 노트")
        notes_btn.setFont_(AppKit.NSFont.systemFontOfSize_(10))
        notes_btn.setBordered_(False)
        notes_btn.setContentTintColor_(_TEXT_SECONDARY)
        opener = _ButtonAction.alloc().initWithCallback_(lambda: _open_url(release_url))
        notes_btn.setTarget_(opener)
        notes_btn.setAction_(objc.selector(opener.perform_, signature=b'v@:@'))
        self._action_refs.append(opener)
        card.addSubview_(notes_btn)

        return card

    def _build_settings_views(self, data, card_w, card_inner_w):
        """Build inline settings panel views."""
        views = []
        config = data["config"]
        line_h = 22

        settings_lines = []
        title_mode = config.get("title_display", "session")

        for mode, label in [("session", "타이틀: 세션"), ("week", "타이틀: 주간"), ("both", "타이틀: 둘 다")]:
            check = "✓ " if title_mode == mode else "   "
            settings_lines.append((f"{check}{label}", mode, "title"))

        settings_lines.append(None)

        for key, label in [("show_session", "세션 표시"), ("show_week", "주간 표시"),
                           ("show_sonnet", "소넷 표시")]:
            check = "✓ " if config.get(key, True) else "   "
            settings_lines.append((f"{check}{label}", key, "toggle"))

        settings_lines.append(None)
        alert_on = config.get("alert_enabled", True)
        settings_lines.append((f"{'✓ ' if alert_on else '   '}사용량 알림 (80%/90%)", "alert_enabled", "toggle"))

        anim_on = config.get("animation_enabled", True)
        settings_lines.append((f"{'✓ ' if anim_on else '   '}🏃 달리기 애니메이션", "animation_enabled", "toggle"))

        total_h = len(settings_lines) * line_h
        card = _build_card(total_h)

        y = total_h - line_h
        for item in settings_lines:
            if item is None:
                sep = AppKit.NSView.alloc().initWithFrame_(
                    NSMakeRect(CARD_PADDING, y + CARD_PADDING + line_h // 2, card_inner_w, 1)
                )
                sep.setWantsLayer_(True)
                sep.layer().setBackgroundColor_(_SEPARATOR_COLOR.CGColor())
                card.addSubview_(sep)
                y -= line_h
                continue

            text, key, kind = item
            btn = HoverButton.alloc().initWithFrame_(
                NSMakeRect(CARD_PADDING, y + CARD_PADDING, card_inner_w, line_h)
            )
            btn.setTitle_(text)
            btn.setFont_(AppKit.NSFont.systemFontOfSize_(11))
            btn.setAlignment_(AppKit.NSTextAlignmentLeft)
            btn.setBordered_(False)
            btn.setContentTintColor_(_TEXT_PRIMARY)

            if kind == "title":
                action = _ButtonAction.alloc().initWithCallback_(
                    lambda m=key: self._set_title_display(m)
                )
            else:
                action = _ButtonAction.alloc().initWithCallback_(
                    lambda k=key: self._toggle_config(k)
                )
            btn.setTarget_(action)
            btn.setAction_(objc.selector(action.perform_, signature=b'v@:@'))
            self._action_refs.append(action)
            card.addSubview_(btn)
            y -= line_h

        views.append((card, card.frame().size.height))
        return views

    def _build_footer(self, card_w):
        """Build footer with refresh, settings, version, quit buttons."""
        footer_h = 28
        footer = AppKit.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, card_w, footer_h))

        btn_w = card_w / 4.0
        buttons = [
            ("🔄", self._on_refresh_click),
            ("⚙️", self._on_settings_click),
            (f"v{APP_VERSION}", None),
            ("종료", self._on_quit_click),
        ]

        for i, (title, callback) in enumerate(buttons):
            btn = HoverButton.alloc().initWithFrame_(
                NSMakeRect(i * btn_w, 0, btn_w, footer_h)
            )
            btn.setTitle_(title)
            btn.setFont_(AppKit.NSFont.systemFontOfSize_(11))
            btn.setBordered_(False)
            btn.setContentTintColor_(_TEXT_SECONDARY)

            if callback:
                action = _ButtonAction.alloc().initWithCallback_(callback)
                btn.setTarget_(action)
                btn.setAction_(objc.selector(action.perform_, signature=b'v@:@'))
                self._action_refs.append(action)

            footer.addSubview_(btn)

        return footer

    def _on_refresh_click(self):
        self._rebuild(force_api=True)
        _check_update(force=True)
        self._rebuild_popover_content()

    def _on_settings_click(self):
        self._settings_visible = not self._settings_visible
        self._rebuild_popover_content()

    def _on_quit_click(self):
        if self._popover and self._popover.isShown():
            self._popover.close()
        rumps.quit_application()

    # ── threshold alerts ─────────────────────────────────────────────────

    def _check_alerts(self, sess_pct, sess_reset, week_pct, week_reset):
        """Send macOS notification when usage crosses a threshold."""
        config = _load_config()
        if not config.get("alert_enabled", True):
            return

        checks = [
            ("session", "세션(5h)", sess_pct, sess_reset),
            ("week", "주간(7d)", week_pct, week_reset),
        ]
        for key, label, pct, reset_time in checks:
            if reset_time != self._last_reset[key]:
                self._alerted[key] = set()
                self._last_reset[key] = reset_time

            fired = None
            for threshold in ALERT_THRESHOLDS:
                if pct >= threshold and threshold not in self._alerted[key]:
                    fired = threshold
            if fired is not None:
                for threshold in ALERT_THRESHOLDS:
                    if threshold <= fired:
                        self._alerted[key].add(threshold)
                reset_msg = f" | 리셋: {_fmt_reset(reset_time)}" if reset_time else ""
                _send_notification(
                    title=f"⚡ Claude 사용량 {fired}% 도달",
                    message=f"{label} 사용률: {pct:.0f}%{reset_msg}",
                )
                print(f"[ALERT] {label} {pct:.0f}% >= {fired}%", flush=True)

    # ── settings callbacks ───────────────────────────────────────────────

    def _set_title_display(self, mode):
        config = _load_config()
        config["title_display"] = mode
        _save_config(config)
        # Update cached config and title bar without re-fetching data
        if self._cached_data:
            self._cached_data["config"] = config
            stale_mark = "~" if (self._cached_data.get("api_stale") or not self._cached_data.get("api_ok")) else ""
            sess_pct = self._cached_data.get("sess_pct", 0)
            week_pct = self._cached_data.get("week_pct", 0)
            if mode == "week":
                self.title = f" {stale_mark}{week_pct:.0f}%"
            elif mode == "both":
                self.title = f" {stale_mark}{sess_pct:.0f}% | {week_pct:.0f}%"
            else:
                self.title = f" {stale_mark}{sess_pct:.0f}%"
        self._rebuild_popover_content()

    def _toggle_config(self, key):
        config = _load_config()
        config[key] = not config.get(key, True)
        _save_config(config)
        # Update cached config without re-fetching data
        if self._cached_data:
            self._cached_data["config"] = config
        # Restart or stop animation when toggled
        if key == "animation_enabled":
            self._start_animation()
        self._rebuild_popover_content()

    # ── callbacks ───────────────────────────────────────────────────────

    def _refresh(self, _=None):
        self._rebuild(force_api=True)

    def _on_tick(self, _=None):
        try:
            now = time.time()
            last = getattr(self, "_last_active_time", now)
            self._last_active_time = now
            was_sleeping = (now - last) > REFRESH_INTERVAL_SEC * 2
            if was_sleeping:
                print(f"[WAKE] Detected sleep gap ({now - last:.0f}s), forcing API refresh...", flush=True)
            # Run data gathering in background thread to avoid blocking main thread
            import threading
            def _bg_rebuild():
                try:
                    new_data = self._gather_data(force_api=was_sleeping)
                    if new_data:
                        from PyObjCTools import AppHelper
                        def _update():
                            self._cached_data = new_data
                            self._apply_main_thread_updates(new_data)
                        AppHelper.callAfter(_update)
                except Exception as e:
                    print(f"[REBUILD] Background error: {e}", flush=True)
            threading.Thread(target=_bg_rebuild, daemon=True).start()
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
