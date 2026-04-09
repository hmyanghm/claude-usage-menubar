import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from claude_menubar import (
    AppKit,
    ClaudeUsageApp,
    LAST_TITLE_CACHE_PATH,
    _ButtonAction,
    _compute_anchor_scroll_origin,
    _configure_popover_scroll_view,
    _current_plan_price_text,
    _format_title_from_data,
    _load_last_title_snapshot,
    _save_last_title_snapshot,
)


class _FakePopover:
    def __init__(self, shown):
        self._shown = shown
        self.close_calls = 0

    def isShown(self):
        return self._shown

    def close(self):
        self.close_calls += 1
        self._shown = False


class _FakeScrollView:
    def __init__(self):
        self.calls = []

    def setDrawsBackground_(self, value):
        self.calls.append(("draws_background", value))

    def setHasVerticalScroller_(self, value):
        self.calls.append(("vertical_scroller", value))

    def setHasHorizontalScroller_(self, value):
        self.calls.append(("horizontal_scroller", value))

    def setAutohidesScrollers_(self, value):
        self.calls.append(("autohides_scrollers", value))

    def setScrollerStyle_(self, value):
        self.calls.append(("scroller_style", value))


class ClaudeUsageAppTests(unittest.TestCase):
    def test_close_popover_closes_visible_popover_without_event_monitor(self):
        app = ClaudeUsageApp()
        app._popover = _FakePopover(shown=True)

        app._close_popover()

        self.assertEqual(app._popover.close_calls, 1)

    def test_close_popover_ignores_hidden_popover(self):
        app = ClaudeUsageApp()
        app._popover = _FakePopover(shown=False)

        app._close_popover()

        self.assertEqual(app._popover.close_calls, 0)

    def test_unknown_current_plan_uses_placeholder_price(self):
        rec = {
            "current_plan": "알 수 없음",
            "recommended": "Pro",
            "rec_price": 20,
        }

        price = _current_plan_price_text(rec)

        self.assertEqual(price, "?")

    def test_button_action_logs_callback_exceptions(self):
        action = _ButtonAction.alloc().initWithCallback_(
            lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        with patch("builtins.print") as print_mock:
            action.perform_(None)

        print_mock.assert_called()
        self.assertIn("[ACTION]", print_mock.call_args[0][0])

    def test_last_title_snapshot_round_trip(self):
        with TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "last_title.json"
            _save_last_title_snapshot(
                {"title_mode": "both", "sess_pct": 61, "week_pct": 24, "api_stale": False},
                path=cache_path,
            )

            loaded = _load_last_title_snapshot(path=cache_path)

        self.assertEqual(loaded["title_mode"], "both")
        self.assertEqual(loaded["sess_pct"], 61)
        self.assertEqual(loaded["week_pct"], 24)

    def test_format_title_from_data_uses_stale_prefix(self):
        title = _format_title_from_data(
            {"title_mode": "session", "sess_pct": 58, "week_pct": 12, "api_stale": True}
        )

        self.assertEqual(title, " ~58%")

    def test_configure_popover_scroll_view_enables_overlay_scroller(self):
        scroll = _FakeScrollView()

        _configure_popover_scroll_view(scroll)

        self.assertIn(("vertical_scroller", True), scroll.calls)
        self.assertIn(("horizontal_scroller", False), scroll.calls)
        self.assertIn(("autohides_scrollers", True), scroll.calls)
        if hasattr(AppKit, "NSScrollerStyleOverlay"):
            self.assertIn(("scroller_style", AppKit.NSScrollerStyleOverlay), scroll.calls)

    def test_compute_anchor_scroll_origin_preserves_viewport_offset(self):
        origin_y = _compute_anchor_scroll_origin(
            header_y=120,
            viewport_offset=20,
            document_height=500,
            viewport_height=200,
        )

        self.assertEqual(origin_y, 100)

    def test_compute_anchor_scroll_origin_clamps_to_document_bounds(self):
        origin_y = _compute_anchor_scroll_origin(
            header_y=40,
            viewport_offset=80,
            document_height=300,
            viewport_height=200,
        )

        self.assertEqual(origin_y, 0)

    def test_app_uses_cached_title_on_init(self):
        with TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "last_title.json"
            _save_last_title_snapshot(
                {"title_mode": "session", "sess_pct": 77, "week_pct": 12, "api_stale": False},
                path=cache_path,
            )

            with patch("claude_menubar.LAST_TITLE_CACHE_PATH", cache_path):
                app = ClaudeUsageApp()

        self.assertEqual(app.title, " 77%")

if __name__ == "__main__":
    unittest.main()
