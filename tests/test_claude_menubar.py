import unittest
import subprocess
import base64
import json
from unittest.mock import mock_open, patch
from pathlib import Path
from tempfile import TemporaryDirectory

from claude_menubar import (
    AppKit,
    ClaudeUsageApp,
    CodexUsageTracker,
    LAST_TITLE_CACHE_PATH,
    _ButtonAction,
    _account_label_for_config,
    _api_status_notice,
    _compute_anchor_scroll_origin,
    _configure_popover_scroll_view,
    _load_codex_account_label,
    _current_plan_price_text,
    _format_title_from_data,
    _fmt_time_remaining,
    _primary_progress_sections,
    _title_slots_from_config,
    _resolve_title_slots,
    _robot_color_components,
    _robot_duration_for_pct,
    _codex_section_summary,
    _combined_robot_usage_pct,
    _codex_subprocess_env,
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
                {"slots": [
                    {"pct": 61, "tag": "5h", "provider": "claude"},
                    {"pct": 24, "tag": "5h", "provider": "openai"},
                ], "api_stale": False},
                path=cache_path,
            )

            loaded = _load_last_title_snapshot(path=cache_path)

        self.assertEqual(loaded["slots"][0], {"pct": 61, "tag": "5h", "provider": "claude"})
        self.assertEqual(loaded["slots"][1], {"pct": 24, "tag": "5h", "provider": "openai"})

    def test_last_title_snapshot_loads_legacy_shape(self):
        with TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "last_title.json"
            cache_path.write_text(
                '{"title_mode": "both", "title_source": "claude", '
                '"sess_pct": 61, "week_pct": 24, "api_stale": false}',
                encoding="utf-8",
            )
            loaded = _load_last_title_snapshot(path=cache_path)

        # Legacy snapshot still renders through the slot formatter.
        self.assertEqual(_format_title_from_data(loaded), " 5h 61% | 7d 24%")

    def test_format_title_from_data_uses_stale_prefix(self):
        title = _format_title_from_data(
            {"slots": [{"pct": 58, "tag": "5h", "provider": "claude"}], "api_stale": True}
        )

        self.assertEqual(title, " ~5h 58%")

    def test_format_title_from_data_two_slots(self):
        title = _format_title_from_data({"slots": [
            {"pct": 18, "tag": "5h", "provider": "openai"},
            {"pct": 7, "tag": "7d", "provider": "openai"},
        ]})

        self.assertEqual(title, " 5h 18% | 7d 7%")

    def test_title_slots_from_config_prefers_explicit_slots(self):
        self.assertEqual(
            _title_slots_from_config({"title_slots": ["claude_session", "codex_session"]}),
            ["claude_session", "codex_session"])
        # Unknown ids dropped, capped at 2.
        self.assertEqual(
            _title_slots_from_config({"title_slots": ["bogus", "codex_week", "claude_week", "claude_session"]}),
            ["codex_week", "claude_week"])

    def test_title_slots_from_config_migrates_legacy_keys(self):
        self.assertEqual(
            _title_slots_from_config({"title_source": "codex", "title_display": "both"}),
            ["codex_session", "codex_week"])
        self.assertEqual(
            _title_slots_from_config({"title_source": "claude", "title_display": "week"}),
            ["claude_week"])
        self.assertEqual(_title_slots_from_config({}), ["claude_session"])

    def test_resolve_title_slots_skips_unavailable_provider(self):
        data = {
            "config": {"title_slots": ["claude_session", "codex_session"]},
            "sess_pct": 40, "week_pct": 10, "sonnet_pct": None,
            "codex_usage": {"available": False},
        }
        slots = _resolve_title_slots(data)
        self.assertEqual(slots, [{"pct": 40.0, "tag": "5h", "provider": "claude"}])

    def test_resolve_title_slots_mixes_providers(self):
        data = {
            "config": {"title_slots": ["claude_session", "codex_week"]},
            "sess_pct": 95, "week_pct": 92, "sonnet_pct": None,
            "codex_usage": {"available": True, "session_pct": 4, "week_pct": 8},
        }
        slots = _resolve_title_slots(data)
        self.assertEqual(slots, [
            {"pct": 95.0, "tag": "5h", "provider": "claude"},
            {"pct": 8.0, "tag": "7d", "provider": "openai"},
        ])

    def test_resolve_codex_session_slot_uses_week_when_only_week_is_available(self):
        data = {
            "config": {"title_slots": ["codex_session"]},
            "sess_pct": 91,
            "week_pct": 20,
            "sonnet_pct": None,
            "codex_usage": {
                "available": True,
                "session_available": False,
                "session_pct": 0,
                "week_available": True,
                "week_pct": 4,
            },
        }

        self.assertEqual(_resolve_title_slots(data), [{
            "pct": 4.0,
            "tag": "7d",
            "provider": "openai",
        }])

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
                {"slots": [{"pct": 77, "tag": "5h", "provider": "claude"}], "api_stale": False},
                path=cache_path,
            )

            with patch("claude_menubar.LAST_TITLE_CACHE_PATH", cache_path):
                app = ClaudeUsageApp()

        self.assertEqual(app.title, " 5h 77%")

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

    def test_primary_progress_sections_follow_codex_title_source_when_available(self):
        sections = _primary_progress_sections({
            "config": {"title_source": "codex", "show_session": True, "show_week": True},
            "api_ok": True,
            "sess_pct": 12,
            "sess_reset": "claude-session-reset",
            "week_pct": 34,
            "week_reset": "claude-week-reset",
            "sonnet_pct": None,
            "sonnet_reset": None,
            "codex_usage": {
                "available": True,
                "session_pct": 56,
                "session_reset": "codex-session-reset",
                "week_pct": 78,
                "week_reset": "codex-week-reset",
            },
        })

        self.assertEqual(
            sections[:2],
            [
                {
                    "label": "Codex 세션",
                    "pct": 56,
                    "reset": "codex-session-reset",
                    "is_estimate": False,
                    "provider": "openai",
                },
                {
                    "label": "Codex 주간",
                    "pct": 78,
                    "reset": "codex-week-reset",
                    "is_estimate": False,
                    "provider": "openai",
                },
            ],
        )

    def test_primary_progress_sections_fall_back_to_claude_when_codex_unavailable(self):
        sections = _primary_progress_sections({
            "config": {"title_source": "codex", "show_session": True, "show_week": True},
            "api_ok": False,
            "sess_pct": 12,
            "sess_reset": "claude-session-reset",
            "week_pct": 34,
            "week_reset": "claude-week-reset",
            "sonnet_pct": None,
            "sonnet_reset": None,
            "codex_usage": {"available": False},
        })

        self.assertEqual(sections[0]["label"], "Claude 세션")
        self.assertEqual(sections[0]["pct"], 12)
        self.assertEqual(sections[0]["reset"], "claude-session-reset")
        self.assertEqual(sections[0]["provider"], "claude")
        self.assertTrue(all(s["provider"] == "claude" for s in sections))

    def test_primary_progress_sections_show_both_providers_at_once(self):
        sections = _primary_progress_sections({
            "config": {"title_source": "claude", "show_session": True,
                       "show_week": True, "show_sonnet": False},
            "api_ok": True,
            "sess_pct": 12,
            "sess_reset": "claude-session-reset",
            "week_pct": 34,
            "week_reset": "claude-week-reset",
            "sonnet_pct": None,
            "sonnet_reset": None,
            "codex_usage": {
                "available": True,
                "session_pct": 56,
                "session_reset": "codex-session-reset",
                "week_pct": 78,
                "week_reset": "codex-week-reset",
            },
        })

        providers = [s["provider"] for s in sections]
        # Claude first (title source), then Codex — both visible together.
        self.assertEqual(providers, ["claude", "claude", "openai", "openai"])

    def test_fmt_time_remaining_formats_by_magnitude(self):
        from datetime import datetime, timedelta
        now = datetime(2026, 6, 29, 12, 0, 0)
        self.assertEqual(
            _fmt_time_remaining(now + timedelta(hours=4, minutes=32), now=now),
            "4시간 32분 남음")
        self.assertEqual(
            _fmt_time_remaining(now + timedelta(minutes=45), now=now),
            "45분 남음")
        self.assertEqual(
            _fmt_time_remaining(now + timedelta(days=6, hours=18), now=now),
            "6일 18시간 남음")
        self.assertEqual(
            _fmt_time_remaining(now - timedelta(minutes=1), now=now),
            "곧 갱신")
        self.assertIsNone(_fmt_time_remaining(None))

    def test_fmt_time_remaining_compact_form(self):
        from datetime import datetime, timedelta
        now = datetime(2026, 6, 29, 12, 0, 0)
        self.assertEqual(
            _fmt_time_remaining(now + timedelta(hours=4, minutes=32), now=now, compact=True),
            "4h32m")
        self.assertEqual(
            _fmt_time_remaining(now + timedelta(minutes=45), now=now, compact=True),
            "45m")
        # Day counts round up (time remaining), so 6d exactly stays 6일.
        self.assertEqual(
            _fmt_time_remaining(now + timedelta(days=6), now=now, compact=True),
            "6일")
        self.assertEqual(
            _fmt_time_remaining(now + timedelta(days=6, hours=23), now=now, compact=True),
            "7일")
        self.assertEqual(
            _fmt_time_remaining(now - timedelta(minutes=1), now=now, compact=True),
            "곧")

    def test_account_label_uses_claude_profile_for_claude_title_source(self):
        label = _account_label_for_config(
            {"config": {"title_source": "claude"}},
            {"account_name": "Claude User", "account_email": "claude@example.com"},
        )

        self.assertEqual(label, "Claude User (claude@example.com)")

    def test_account_label_uses_codex_profile_for_codex_title_source(self):
        with TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            payload = base64.urlsafe_b64encode(json.dumps({
                "name": "Codex User",
                "email": "codex@example.com",
            }).encode("utf-8")).decode("ascii").rstrip("=")
            (codex_dir / "auth.json").write_text(json.dumps({
                "tokens": {"id_token": f"header.{payload}.signature"},
            }), encoding="utf-8")

            label = _account_label_for_config(
                {"config": {"title_source": "codex"}},
                {"account_name": "Claude User", "account_email": "claude@example.com"},
                codex_dir=codex_dir,
            )

        self.assertEqual(label, "Codex User (codex@example.com)")

    def test_load_codex_account_label_falls_back_to_account_id(self):
        with TemporaryDirectory() as tmpdir:
            codex_dir = Path(tmpdir)
            (codex_dir / "auth.json").write_text(json.dumps({
                "tokens": {"account_id": "acct_123"},
            }), encoding="utf-8")

            label = _load_codex_account_label(codex_dir=codex_dir)

        self.assertEqual(label, "acct_123")

    def test_api_status_notice_shows_local_estimate_when_claude_api_unavailable(self):
        notice = _api_status_notice({
            "config": {"title_source": "claude"},
            "api_ok": False,
            "api_stale": False,
            "sess_totals": {"total": 100},
            "week_totals": {"total": 200},
        })

        self.assertEqual(notice, ("Claude 로컬 로그 기반 추정치", "secondary"))

    def test_api_status_notice_hides_claude_api_state_for_codex_source(self):
        notice = _api_status_notice({
            "config": {"title_source": "codex"},
            "api_ok": False,
            "api_stale": False,
            "sess_totals": {"total": 100},
            "week_totals": {"total": 200},
        })

        self.assertIsNone(notice)

    def test_api_status_notice_shows_disconnected_when_no_api_or_local_usage(self):
        notice = _api_status_notice({
            "config": {"title_source": "claude"},
            "api_ok": False,
            "api_stale": False,
            "sess_totals": {"total": 0},
            "week_totals": {"total": 0},
        })

        self.assertEqual(notice, ("Claude API 미연결", "warning"))

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
    def test_codex_subprocess_env_adds_common_node_locations_for_launchagent(self):
        env = _codex_subprocess_env({"PATH": "/usr/bin:/bin"})

        self.assertIn("/usr/local/bin", env["PATH"].split(":"))
        self.assertIn("/opt/homebrew/bin", env["PATH"].split(":"))

    def test_latest_usage_prefers_live_app_server_and_classifies_weekly_window(self):
        live_response = {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 4,
                    "windowDurationMins": 10080,
                    "resetsAt": 1785124803,
                },
                "secondary": None,
                "planType": "pro",
            }
        }

        with TemporaryDirectory() as tmpdir:
            usage = CodexUsageTracker(
                codex_dir=Path(tmpdir),
                live_fetcher=lambda: live_response,
            ).latest_usage()

        self.assertTrue(usage["available"])
        self.assertFalse(usage["session_available"])
        self.assertTrue(usage["week_available"])
        self.assertEqual(usage["week_pct"], 4)
        self.assertEqual(usage["plan_type"], "pro")
        self.assertEqual(usage["source"], "app-server")

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

            usage = CodexUsageTracker(
                codex_dir=codex_dir,
                live_fetcher=lambda: None,
            ).latest_usage()

        self.assertTrue(usage["available"])
        self.assertEqual(usage["session_pct"], 4)
        self.assertEqual(usage["week_pct"], 6)
        self.assertEqual(usage["plan_type"], "pro")
        self.assertEqual(usage["total_tokens"], 25)

    def test_latest_usage_reports_unavailable_without_token_count_logs(self):
        with TemporaryDirectory() as tmpdir:
            usage = CodexUsageTracker(
                codex_dir=Path(tmpdir),
                live_fetcher=lambda: None,
            ).latest_usage()

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

    def test_codex_summary_marks_missing_session_window_as_unavailable(self):
        summary = _codex_section_summary({
            "available": True,
            "session_available": False,
            "session_pct": 0,
            "week_available": True,
            "week_pct": 4,
        })

        self.assertEqual(summary["title"], "GPT/Codex  — / 4%")

if __name__ == "__main__":
    unittest.main()
