#!/usr/bin/env python3
"""test_forseti_visual_gatekeeper.py - Comprehensive Unit Tests for Forseti Visual CI Gatekeeper."""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add scripts directory to path
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(TESTS_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from forseti_visual_gatekeeper import (
    VIEWPORT_IPHONE_14,
    VIEWPORT_PIXEL_7,
    capture_browserless_screenshot,
    discover_screenshots,
    generate_side_by_side_markdown,
    probe_browserless_health,
    resolve_browserless_url,
    resolve_browserless_ws,
    resolve_repo_path,
    resolve_vault_path,
    sync_snapshots_to_obsidian,
    transition_kanban_verification,
)


class TestForsetiVisualGatekeeper(unittest.TestCase):
    """Test suite for Forseti Visual CI Gatekeeper (Pillar 3)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))

    def test_viewports_specification(self) -> None:
        """Verify standardized iPhone 14 and Pixel 7 viewport dimensions."""
        self.assertEqual(VIEWPORT_IPHONE_14["width"], 390)
        self.assertEqual(VIEWPORT_IPHONE_14["height"], 844)
        self.assertEqual(VIEWPORT_IPHONE_14["name"], "ios")

        self.assertEqual(VIEWPORT_PIXEL_7["width"], 412)
        self.assertEqual(VIEWPORT_PIXEL_7["height"], 915)
        self.assertEqual(VIEWPORT_PIXEL_7["name"], "android")

    def test_resolve_browserless_endpoints(self) -> None:
        """Verify URL and WebSocket endpoint conversions."""
        with patch.dict(os.environ, {"BROWSERLESS_URL": "http://poseidon_browserless:3000"}):
            self.assertEqual(resolve_browserless_url(), "http://poseidon_browserless:3000")
            self.assertEqual(resolve_browserless_ws(), "ws://poseidon_browserless:3000")

        with patch.dict(os.environ, {"BROWSERLESS_URL": "https://custom.browserless.io", "BROWSERLESS_WS_ENDPOINT": ""}):
            self.assertEqual(resolve_browserless_url(), "https://custom.browserless.io")
            self.assertEqual(resolve_browserless_ws(), "wss://custom.browserless.io")

    def test_resolve_repo_path(self) -> None:
        """Verify repository path resolution against vault."""
        vault = Path(self.temp_dir)
        forge = vault / "Forge"
        ui_repo = forge / "ui"
        ui_repo.mkdir(parents=True)

        res = resolve_repo_path("ui", vault_path=vault)
        self.assertEqual(res, ui_repo.resolve())

        res2 = resolve_repo_path("Forge/ui", vault_path=vault)
        self.assertEqual(res2, ui_repo.resolve())

        with self.assertRaises(ValueError):
            resolve_repo_path("non_existent_stack", vault_path=vault)

    def test_probe_browserless_health_success(self) -> None:
        """Verify health check returns reachable when pressure JSON is returned."""
        fake_response = io.BytesIO(b'{"pressure": {"isAvailable": true, "running": 1, "cpu": 5}}')
        with patch("urllib.request.urlopen", return_value=fake_response):
            health = probe_browserless_health("http://poseidon_browserless:3000")
            self.assertTrue(health["reachable"])
            self.assertTrue(health["is_available"])
            self.assertEqual(health["running"], 1)

    def test_probe_browserless_health_failure(self) -> None:
        """Verify graceful error reporting when endpoint is unreachable."""
        with patch("urllib.request.urlopen", side_effect=OSError("Connection refused")):
            health = probe_browserless_health("http://invalid-endpoint:3000")
            self.assertFalse(health["reachable"])
            self.assertIn("Connection refused", health.get("error", ""))

    def test_capture_browserless_screenshot_success(self) -> None:
        """Verify successful remote snapshot capture and PNG header validation."""
        valid_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 50
        fake_response = io.BytesIO(valid_png)
        target_out = Path(self.temp_dir) / "test_snapshot.png"

        with patch("urllib.request.urlopen", return_value=fake_response):
            success = capture_browserless_screenshot(
                target_url_or_html="http://ui:8080",
                output_path=target_out,
                viewport=VIEWPORT_IPHONE_14,
                browserless_url="http://poseidon_browserless:3000",
            )
            self.assertTrue(success)
            self.assertTrue(target_out.is_file())
            self.assertEqual(target_out.read_bytes(), valid_png)

    def test_capture_browserless_screenshot_invalid_magic(self) -> None:
        """Verify rejection when response is not a valid PNG."""
        invalid_data = b"<html>Error 500</html>"
        fake_response = io.BytesIO(invalid_data)
        target_out = Path(self.temp_dir) / "invalid_snapshot.png"

        with patch("urllib.request.urlopen", return_value=fake_response):
            success = capture_browserless_screenshot(
                target_url_or_html="http://ui:8080",
                output_path=target_out,
                viewport=VIEWPORT_IPHONE_14,
                browserless_url="http://poseidon_browserless:3000",
            )
            self.assertFalse(success)
            self.assertFalse(target_out.is_file())

    def test_discover_screenshots(self) -> None:
        """Verify discovery of screenshots across iOS and Android directories."""
        repo = Path(self.temp_dir)
        screenshots = repo / "screenshots"
        ios_dir = screenshots / "ios"
        android_dir = screenshots / "android"
        ios_dir.mkdir(parents=True)
        android_dir.mkdir(parents=True)

        (ios_dir / "01-app-shell.png").write_bytes(b"dummy")
        (android_dir / "01-app-shell.png").write_bytes(b"dummy")
        (ios_dir / "02-tactile-keypad.png").write_bytes(b"dummy")

        discovered = discover_screenshots(repo)
        self.assertIn("01-app-shell", discovered)
        self.assertIn("ios", discovered["01-app-shell"])
        self.assertIn("android", discovered["01-app-shell"])

        self.assertIn("02-tactile-keypad", discovered)
        self.assertIn("ios", discovered["02-tactile-keypad"])
        self.assertNotIn("android", discovered["02-tactile-keypad"])

    def test_generate_side_by_side_markdown_passed(self) -> None:
        """Verify generated markdown table formatting and zero em/en dashes."""
        discovered = {
            "01-app-shell": {
                "ios": Path("/tmp/ios/01-app-shell.png"),
                "android": Path("/tmp/android/01-app-shell.png"),
            }
        }
        md = generate_side_by_side_markdown(
            repo_name="ui",
            screenshots_map=discovered,
            pr_number="7",
            test_passed=True,
        )

        self.assertIn("## Forseti Mobile Visual Gate Report (PASSED)", md)
        self.assertIn("**Pull Request**: #7", md)
        self.assertIn("| **01 App Shell** |", md)
        self.assertIn("![iOS 01-app-shell](Screenshots/ios/01-app-shell.png)", md)
        self.assertIn("![Android 01-app-shell](Screenshots/android/01-app-shell.png)", md)

        # Invariant: Zero em/en dashes
        self.assertNotIn("\u2014", md)
        self.assertNotIn("\u2013", md)

    def test_generate_side_by_side_markdown_failed(self) -> None:
        """Verify failed report contains failure telemetry block."""
        discovered = {}
        md = generate_side_by_side_markdown(
            repo_name="kratos",
            screenshots_map=discovered,
            pr_number="25",
            test_passed=False,
            test_output="AssertionError: Expected header Kratos but found None",
        )

        self.assertIn("## Forseti Mobile Visual Gate Report (FAILED)", md)
        self.assertIn("### Failure Telemetry", md)
        self.assertIn("AssertionError: Expected header Kratos", md)

    def test_transition_kanban_verification_pass(self) -> None:
        """Verify kanban-gateway verify --pass execution."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Task kb-001 verification PASSED"

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            res = transition_kanban_verification(
                task_id="kb-001",
                passed=True,
                summary="All visual flows verified.",
                pr_url="https://github.com/yggdrasil-lab/ui/pull/7",
            )
            self.assertTrue(res["success"])
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertEqual(args[:3], ["kanban-gateway", "verify", "kb-001"])
            self.assertIn("--pass", args)
            self.assertIn("--pr-url", args)

    def test_transition_kanban_verification_fail(self) -> None:
        """Verify kanban-gateway verify --fail execution on test regression."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Task kb-002 verification FAILED -> bounced to sindri"

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            res = transition_kanban_verification(
                task_id="kb-002",
                passed=False,
                feedback="Modal bleed-through detected on iPhone 14.",
            )
            self.assertTrue(res["success"])
            args = mock_run.call_args[0][0]
            self.assertIn("--fail", args)
            self.assertIn("--feedback", args)

    def test_sync_snapshots_to_obsidian(self) -> None:
        """Verify syncing snapshots and writing the visual review note in vault."""
        vault = Path(self.temp_dir)
        repo_dir = vault / "Forge" / "ui"
        ios_dir = repo_dir / "screenshots" / "ios"
        android_dir = repo_dir / "screenshots" / "android"
        ios_dir.mkdir(parents=True)
        android_dir.mkdir(parents=True)

        (ios_dir / "01-app-shell.png").write_bytes(b"fake_ios")
        (android_dir / "01-app-shell.png").write_bytes(b"fake_android")

        discovered = {
            "01-app-shell": {
                "ios": ios_dir / "01-app-shell.png",
                "android": android_dir / "01-app-shell.png",
            }
        }

        report = "## Test Report\nEverything nominal."
        copied, note_path = sync_snapshots_to_obsidian(
            repo_name="ui",
            screenshots_map=discovered,
            markdown_report=report,
            vault_path=vault,
        )

        self.assertEqual(copied, 2)
        self.assertTrue(note_path.is_file())
        self.assertEqual(note_path.read_text(encoding="utf-8"), report)

        target_ios = vault / "second-brain" / "Areas" / "90-Infrastructure" / "Ui" / "Screenshots" / "ios" / "01-app-shell.png"
        self.assertTrue(target_ios.is_file())


if __name__ == "__main__":
    unittest.main()
