import unittest
import subprocess
from unittest.mock import mock_open, patch
from pathlib import Path
from tempfile import TemporaryDirectory

from claude_menubar import (
    AppKit,
    ClaudeUsageApp,
    CodexUsageTracker,
    LAST_TITLE_CACHE_PATH,
    _ButtonAction,
    _compute_anchor_scroll_origin,
    _configure_popover_scroll_view,
    _current_plan_price_text,
    _format_title_from_data,
    _robot_color_components,
    _robot_duration_for_pct,
    _codex_section_summary,
    _combined_robot_usage_pct,
    _title_badge_provider_for_config,
    _provider_label,
    _load_last_title_snapshot,
    _save_last_title_snapshot,
    _auto_update_setup,
    _update_cache,
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

    def test_format_title_from_data_can_show_codex_prefix(self):
        title = _format_title_from_data(
            {"title_mode": "both", "title_source": "codex", "sess_pct": 18, "week_pct": 7}
        )

        self.assertEqual(title, " 18% | 7%")

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

    def test_robot_color_matches_preview_gradient(self):
        self.assertEqual(_robot_color_components(0), (1.0, 1.0, 1.0))
        self.assertEqual(_robot_color_components(33), (1.0, 191 / 255, 36 / 255))
        self.assertEqual(_robot_color_components(66), (1.0, 115 / 255, 22 / 255))
        self.assertEqual(_robot_color_components(100), (220 / 255, 38 / 255, 38 / 255))

    def test_robot_duration_matches_preview_range(self):
        self.assertAlmostEqual(_robot_duration_for_pct(0), 2.0)
        self.assertAlmostEqual(_robot_duration_for_pct(100), 0.35)

    def test_combined_robot_usage_uses_higher_claude_or_codex_session_pct(self):
        data = {
            "sess_pct": 12,
            "codex_usage": {"available": True, "session_pct": 84},
        }

        self.assertEqual(_combined_robot_usage_pct(data), 84)

    def test_title_badge_provider_follows_title_source(self):
        self.assertEqual(_title_badge_provider_for_config({"title_source": "codex"}), "openai")
        self.assertEqual(_title_badge_provider_for_config({"title_source": "claude"}), "claude")

    def test_provider_label_prefixes_product_names(self):
        self.assertEqual(_provider_label("claude", "세션"), "Claude 세션")
        self.assertEqual(_provider_label("openai", "Codex"), "GPT/Codex")

    def test_auto_update_setup_writes_setup_output_to_update_log(self):
        old_cache = _update_cache.copy()
        try:
            with TemporaryDirectory() as tmpdir:
                source_dir = Path(tmpdir) / "source"
                source_dir.mkdir()
                (source_dir / "setup.sh").write_text("#!/bin/bash\n", encoding="utf-8")
                _update_cache["latest_version"] = "v9.9.9-mac"

                result = subprocess.CompletedProcess(["bash"], 0)
                open_mock = mock_open()
                with patch("claude_menubar._download_source_tarball", return_value=str(source_dir)), \
                     patch("claude_menubar._send_notification"), \
                     patch("claude_menubar._restart_app"), \
                     patch("claude_menubar.shutil.rmtree"), \
                     patch("builtins.open", open_mock), \
                     patch("claude_menubar.subprocess.run", return_value=result) as run_mock:
                    updated = _auto_update_setup("v9.9.9-mac")

                self.assertTrue(updated)
                _, kwargs = run_mock.call_args
                self.assertNotIn("capture_output", kwargs)
                self.assertIs(kwargs["stdout"], open_mock())
                self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        finally:
            _update_cache.clear()
            _update_cache.update(old_cache)


class CodexUsageTrackerTests(unittest.TestCase):
    def test_latest_usage_reads_rate_limits_from_session_logs(self):
        with TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            session_dir = codex_dir / "sessions" / "2026" / "05" / "19"
            session_dir.mkdir(parents=True)
            log_path = session_dir / "rollout.jsonl"
            log_path.write_text(
                "\n".join([
                    '{"timestamp":"2026-05-19T01:00:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"total_tokens":100},"last_token_usage":{"total_tokens":10}},"rate_limits":{"primary":{"used_percent":2,"window_minutes":300,"resets_at":1779182909},"secondary":{"used_percent":3,"window_minutes":10080,"resets_at":1779665079},"plan_type":"pro"}}}',
                    '{"timestamp":"2026-05-19T02:00:00Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":20,"output_tokens":5,"total_tokens":25},"last_token_usage":{"total_tokens":15}},"rate_limits":{"primary":{"used_percent":4,"window_minutes":300,"resets_at":1779186509},"secondary":{"used_percent":6,"window_minutes":10080,"resets_at":1779668679},"plan_type":"pro"}}}',
                ]),
                encoding="utf-8",
            )

            usage = CodexUsageTracker(codex_dir=codex_dir).latest_usage()

        self.assertTrue(usage["available"])
        self.assertEqual(usage["session_pct"], 4)
        self.assertEqual(usage["week_pct"], 6)
        self.assertEqual(usage["plan_type"], "pro")
        self.assertEqual(usage["total_tokens"], 25)

    def test_latest_usage_reports_unavailable_without_token_count_logs(self):
        with TemporaryDirectory() as tmpdir:
            usage = CodexUsageTracker(codex_dir=Path(tmpdir)).latest_usage()

        self.assertFalse(usage["available"])

    def test_codex_section_summary_uses_only_available_local_fields(self):
        summary = _codex_section_summary({
            "available": True,
            "session_pct": 4.2,
            "week_pct": 6.8,
            "plan_type": "pro",
            "total_tokens": 12345,
            "last_tokens": 678,
        })

        self.assertEqual(summary["title"], "GPT/Codex  4% / 7%")
        self.assertIn(("플랜", "pro"), summary["rows"])
        self.assertIn(("총 토큰", "12.3K"), summary["rows"])
        self.assertIn(("마지막 요청", "678"), summary["rows"])

if __name__ == "__main__":
    unittest.main()
