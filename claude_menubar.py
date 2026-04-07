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


# ─── Configuration ───────────────────────────────────────────────────────────

APP_VERSION = "2.0.0"
GITHUB_REPO = "hmyanghm/claude-usage-menubar"
GITHUB_API_RELEASES = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
PLATFORM_TAG_SUFFIX = "-mac"  # only check releases tagged like v1.0.9-mac
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


def _check_update():
    """Check GitHub releases for a newer version matching this platform.
    Only considers releases tagged with PLATFORM_TAG_SUFFIX (e.g. v1.0.9-mac).
    Returns (new_version, url) or (None, None).
    """
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

import AppKit
import objc
from Foundation import NSObject, NSRect, NSSize, NSPoint, NSMakeRect

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
    """Custom view for a single day bar in the 7-day history."""

    def initWithFrame_fillRatio_color_(self, frame, ratio, color):
        self = objc.super(HistoryBarView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._ratio = max(0, min(1.0, ratio))
        self._color = color
        return self

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
        self._app.toggle_popover(sender)


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
            self._callback()


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
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(4.0)
        self._updateTrackingArea()
        return self

    def _updateTrackingArea(self):
        if self._tracking_area:
            self.removeTrackingArea_(self._tracking_area)
        self._tracking_area = AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(),
            AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveAlways,
            self, None
        )
        self.addTrackingArea_(self._tracking_area)

    def updateTrackingAreas(self):
        objc.super(HoverButton, self).updateTrackingAreas()
        self._updateTrackingArea()

    def mouseEntered_(self, event):
        self.layer().setBackgroundColor_(
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1, 1, 1, 0.08).CGColor()
        )

    def mouseExited_(self, event):
        self.layer().setBackgroundColor_(
            AppKit.NSColor.clearColor().CGColor()
        )


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
        self.view_mode = "dashboard"
        self._alerted = {"session": set(), "week": set()}
        self._last_reset = {"session": None, "week": None}
        # Popover state
        self._popover = None
        self._toggle_target = None
        self._settings_visible = False
        self._action_refs = []
        self._collapse_state = {
            "detail": False,
            "model": False,
            "history": False,
            "plan": False,
        }
        self._cached_data = None
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
            button.setAction_(objc.selector(self._toggle_target.togglePopover_, signature=b'v@:@'))
            print("[POPOVER] NSPopover set up successfully", flush=True)
        except Exception as e:
            print(f"[POPOVER] Setup failed: {e}", flush=True)

        self._register_wake_observer()
        try:
            self._rebuild()
        except Exception as e:
            print(f"[DEBUG] _initial_load error: {e}", flush=True)
        self._refresh_timer = rumps.Timer(self._on_tick, REFRESH_INTERVAL_SEC)
        self._refresh_timer.start()

    def toggle_popover(self, sender):
        """Toggle the NSPopover open/closed."""
        if not self._popover:
            return
        if self._popover.isShown():
            self._popover.close()
        else:
            # Refresh data when opening popover
            try:
                self._cached_data = self._gather_data()
            except Exception as e:
                print(f"[POPOVER] Data gather error: {e}", flush=True)
            self._rebuild_popover_content()
            button = self._nsapp.nsstatusitem.button()
            self._popover.showRelativeToRect_ofView_preferredEdge_(
                button.bounds(), button, AppKit.NSMinYEdge
            )

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
        """Called after wake-from-sleep."""
        timer.stop()
        self._wake_retry_timer = None
        max_retries = 5
        try:
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
                print("[WAKE] Max retries reached, giving up.", flush=True)
                self._wake_retry_count = 0

    # ── popover content builder ──────────────────────────────────────────

    def _rebuild(self, force_api=False):
        """Gather data and update title bar. Popover content is built on open."""
        self._last_active_time = time.time()
        try:
            self._cached_data = self._gather_data(force_api=force_api)
        except Exception as e:
            print(f"[REBUILD] Error: {e}", flush=True)
            self._cached_data = None

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
        self._check_alerts(sess_pct, sess_reset, week_pct, week_reset)

        # Update title bar
        stale_mark = "~" if (api_stale or not api_ok) else ""
        title_mode = config.get("title_display", "session")
        if title_mode == "week":
            self.title = f"⚡ {stale_mark}{week_pct:.0f}%"
        elif title_mode == "both":
            self.title = f"⚡ {stale_mark}{sess_pct:.0f}% | {week_pct:.0f}%"
        else:
            self.title = f"⚡ {stale_mark}{sess_pct:.0f}%"

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
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setDocumentView_(container)

        # Scroll to top
        container.scrollPoint_(NSPoint(0, total_h))

        vc = PopoverViewController.alloc().initWithView_(scroll)
        self._popover.setContentSize_(NSSize(POPOVER_WIDTH, max_h))
        self._popover.setContentViewController_(vc)

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

        def toggle():
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

        lines = [
            ("── 오늘 ──", True),
            (f"  토큰 {fmt_tokens(today.get('total', 0))}  (입력 {fmt_tokens(today.get('input', 0))} / 출력 {fmt_tokens(today.get('output', 0))})", False),
            (f"  비용 {fmt_cost(today.get('cost', 0))}  |  요청 {today.get('requests', 0)}회", False),
            ("── 세션 (5시간) ──", True),
            (f"  토큰 {fmt_tokens(sess.get('total', 0))}  (입력 {fmt_tokens(sess.get('input', 0))} / 출력 {fmt_tokens(sess.get('output', 0))})", False),
        ]
        if sess.get("cache_write", 0) or sess.get("cache_read", 0):
            lines.append((f"  캐시 생성 {fmt_tokens(sess.get('cache_write', 0))} / 읽기 {fmt_tokens(sess.get('cache_read', 0))}", False))
        lines.append((f"  비용 {fmt_cost(sess.get('cost', 0))}", False))
        lines.extend([
            ("── 이번 주 (7일) ──", True),
            (f"  토큰 {fmt_tokens(week.get('total', 0))}  (입력 {fmt_tokens(week.get('input', 0))} / 출력 {fmt_tokens(week.get('output', 0))})", False),
            (f"  비용 {fmt_cost(week.get('cost', 0))}  |  요청 {week.get('requests', 0)}회", False),
        ])

        card = self._build_text_card(lines, card_w, card_inner_w)
        views.append((card, card.frame().size.height))
        return views

    def _build_model_section(self, data, card_w, card_inner_w):
        """Build model breakdown collapsible section."""
        views = []
        week_models = data.get("week_models", {})
        week_costs = data.get("week_costs", {})
        if not week_models:
            return views

        header = self._make_section_header("🤖 모델별", "model", card_w)
        views.append((header, 24))

        if not self._collapse_state.get("model", False):
            return views

        lines = []
        for m in sorted(week_costs, key=week_costs.get, reverse=True):
            d = week_models[m]
            total_t = sum(d.values())
            short = m.split("/")[-1] if "/" in m else m
            lines.append((f"  {short}: {fmt_tokens(total_t)}  {fmt_cost(week_costs[m])}", False))

        card = self._build_text_card(lines, card_w, card_inner_w)
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

                bar.setToolTip_(f"{date_str}: {fmt_cost(cost)}")

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

        lines = [
            (f"현재 플랜: {rec['current_plan']}", True),
            (f"30일 비용: {fmt_cost(rec['total_30d'])} ({rec['active_days']}일 활성)", False),
            (f"피크 주간: {fmt_cost(rec['peak_week_cost'])}", False),
        ]
        if rec['opus_ratio'] > 0.01:
            lines.append((f"Opus 비중: {rec['opus_ratio']:.0%}", False))
        lines.append(("", False))

        for pf in rec['plan_fits']:
            usage_pct = pf['usage_pct']
            if usage_pct > 100:
                status = "🔴"
            elif usage_pct > 80:
                status = "🟡"
            else:
                status = "🟢"
            is_rec = " ⭐" if pf['name'] == rec['recommended'] else ""
            is_cur = " ←" if pf['name'] == rec['current_plan'] else ""
            lines.append((f"{status} {pf['name']} ${pf['price']}/월 — {usage_pct:.0f}%{is_rec}{is_cur}", False))

        lines.append(("", False))
        star = "⭐ " if rec['recommended'] != rec['current_plan'] else "✓ "
        lines.append((f"{star}추천: {rec['recommended']} (${rec['rec_price']}/월)", True))
        for r in rec['reasons']:
            lines.append((f"  → {r}", False))
        if rec['savings']:
            lines.append((f"💰 월 ${rec['savings']} 절약 가능", False))
        elif rec['recommended'] == rec['current_plan']:
            lines.append(("✅ 현재 플랜이 적합합니다", False))

        card = self._build_text_card(lines, card_w, card_inner_w)
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
                self.title = f"⚡ {stale_mark}{week_pct:.0f}%"
            elif mode == "both":
                self.title = f"⚡ {stale_mark}{sess_pct:.0f}% | {week_pct:.0f}%"
            else:
                self.title = f"⚡ {stale_mark}{sess_pct:.0f}%"
        self._rebuild_popover_content()

    def _toggle_config(self, key):
        config = _load_config()
        config[key] = not config.get(key, True)
        _save_config(config)
        # Update cached config without re-fetching data
        if self._cached_data:
            self._cached_data["config"] = config
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
