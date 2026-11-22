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
    resolve_visual_gate_script,
    run_visual_test_harness,
    sync_snapshots_to_obsidian,
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

    def test_resolve_visual_gate_script_container_vault_root(self) -> None:
        """Verify container /app/vault resolution to Court of Odin skills."""
        expected_vault = Path("/app/vault")
        expected_script = Path(
            "/app/vault/.agents/plugins/court-of-odin/skills/forge-pr-workflow/scripts/run_frontend_visual_gate.py"
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(Path, "is_dir", autospec=True, side_effect=lambda p: p == expected_vault),
            patch.object(Path, "is_file", autospec=True, side_effect=lambda p: p == expected_script),
        ):
            self.assertEqual(resolve_vault_path(), expected_vault)
            self.assertEqual(resolve_visual_gate_script(), expected_script)

    def test_resolve_visual_gate_script_host_vault_root(self) -> None:
        """Verify host /opt/atlas/vault resolution to Court of Odin skills."""
        expected_vault = Path("/opt/atlas/vault")
        expected_script = Path(
            "/opt/atlas/vault/.agents/plugins/court-of-odin/skills/forge-pr-workflow/scripts/run_frontend_visual_gate.py"
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(Path, "is_dir", autospec=True, side_effect=lambda p: p == expected_vault),
            patch.object(Path, "is_file", autospec=True, side_effect=lambda p: p == expected_script),
        ):
            self.assertEqual(resolve_vault_path(), expected_vault)
            self.assertEqual(resolve_visual_gate_script(), expected_script)

    def test_resolve_visual_gate_script_env_and_explicit_vault(self) -> None:
        """Verify VAULT, OBSIDIAN_VAULT_PATH, and explicit vault_path resolution."""
        vault = Path(self.temp_dir) / "vault"
        script = vault / ".agents/plugins/court-of-odin/skills/forge-pr-workflow/scripts/run_frontend_visual_gate.py"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        self.assertEqual(resolve_visual_gate_script(vault_path=vault), script.resolve())
        with patch.dict(os.environ, {"VAULT": str(vault)}, clear=True):
            self.assertEqual(resolve_visual_gate_script(), script.resolve())
        with patch.dict(os.environ, {"OBSIDIAN_VAULT_PATH": str(vault)}, clear=True):
            self.assertEqual(resolve_visual_gate_script(), script.resolve())

    def test_resolve_visual_gate_script_fails_loud_and_ignores_legacy_paths(self) -> None:
        """Verify missing gate script raises FileNotFoundError and ignores legacy paths."""
        vault = Path(self.temp_dir) / "legacy_only_vault"
        legacy_vault_script = vault / ".agents/skills/forge-pr-workflow/scripts/run_frontend_visual_gate.py"
        legacy_vault_script.parent.mkdir(parents=True)
        legacy_vault_script.write_text("# legacy\n", encoding="utf-8")
        legacy_domain_script = (
            vault
            / ".agents/plugins/court-of-odin/domain-skills/forge-pr-workflow/scripts/run_frontend_visual_gate.py"
        )
        legacy_domain_script.parent.mkdir(parents=True)
        legacy_domain_script.write_text("# legacy domain-skills\n", encoding="utf-8")

        with self.assertRaises(FileNotFoundError):
            resolve_visual_gate_script(vault_path=vault)

        legacy_paths = {
            Path("/opt/odin/hermes/skills/devops/forge-pr-workflow/scripts/run_frontend_visual_gate.py"),
            Path("/opt/data/skills/devops/forge-pr-workflow/scripts/run_frontend_visual_gate.py"),
            Path("/app/vault/.agents/skills/forge-pr-workflow/scripts/run_frontend_visual_gate.py"),
            Path("/opt/atlas/vault/.agents/skills/forge-pr-workflow/scripts/run_frontend_visual_gate.py"),
            Path(
                "/app/vault/.agents/plugins/court-of-odin/domain-skills/forge-pr-workflow/scripts/run_frontend_visual_gate.py"
            ),
            Path(
                "/opt/atlas/vault/.agents/plugins/court-of-odin/domain-skills/forge-pr-workflow/scripts/run_frontend_visual_gate.py"
            ),
        }
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(Path, "is_dir", autospec=True, side_effect=lambda p: p == Path("/app/vault")),
            patch.object(Path, "is_file", autospec=True, side_effect=lambda p: p in legacy_paths),
        ):
            with self.assertRaises(FileNotFoundError):
                resolve_visual_gate_script()

        with patch.dict(os.environ, {"VAULT": str(Path(self.temp_dir) / "missing_vault")}, clear=True):
            with self.assertRaises(FileNotFoundError):
                resolve_visual_gate_script()

    def test_run_visual_test_harness_executes_skill_script(self) -> None:
        """Verify harness runs resolved Court of Odin skills visual gate script."""
        vault = Path(self.temp_dir) / "vault"
        repo = vault / "Forge" / "ui"
        repo.mkdir(parents=True)
        script = vault / ".agents/plugins/court-of-odin/skills/forge-pr-workflow/scripts/run_frontend_visual_gate.py"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        fake_proc = MagicMock(returncode=0, stdout="Visual checks passed\n")
        with patch("subprocess.run", return_value=fake_proc) as mock_run:
            passed, output = run_visual_test_harness(
                repo_path=repo,
                browserless_ws="ws://poseidon_browserless:3000",
                sync_vault=True,
                vault_path=vault,
            )

        self.assertTrue(passed)
        self.assertEqual(output, "Visual checks passed")
        mock_run.assert_called_once()
        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd, ["python3", str(script.resolve()), str(repo), "--sync-vault"])
        self.assertEqual(mock_run.call_args.kwargs["env"]["BROWSERLESS_WS_ENDPOINT"], "ws://poseidon_browserless:3000")

    def test_run_visual_test_harness_fails_loud_without_docker_fallback(self) -> None:
        """Verify harness raises FileNotFoundError and skips docker fallback when missing."""
        vault = Path(self.temp_dir) / "empty_vault"
        repo = vault / "Forge" / "ui"
        repo.mkdir(parents=True)

        with patch("subprocess.run") as mock_run:
            with self.assertRaises(FileNotFoundError):
                run_visual_test_harness(repo_path=repo, vault_path=vault)
            mock_run.assert_not_called()

    def test_resolve_vault_path_fails_loud_when_missing(self) -> None:
        """Verify resolve_vault_path raises FileNotFoundError when no vault root exists."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(Path, "is_dir", autospec=True, return_value=False),
        ):
            with self.assertRaises(FileNotFoundError):
                resolve_vault_path()


if __name__ == "__main__":
    unittest.main()
