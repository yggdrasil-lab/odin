#!/usr/bin/env python3
"""Unit tests for build-time startup API media sweep offload patch."""

from __future__ import annotations

import ast
from contextlib import contextmanager
from contextvars import ContextVar
import importlib.util
import logging
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

PATCHES_DIR = Path(__file__).resolve().parent.parent / "patches"
if str(PATCHES_DIR) not in sys.path:
    sys.path.insert(0, str(PATCHES_DIR))

import fix_startup_api_media_sweep_offload as sweep_patch  # noqa: E402


def _build_session_api_turn_fixture(*, include_anchor: bool = True) -> str:
    """Construct a minimal session_api_turn.py source matching upstream anchor."""
    if include_anchor:
        sweep_block = (
            "    from gateway.session_ingress_media import collect_unheld_api_images\n"
            "    import logging\n"
            "    store = authority_persistence(authority)\n"
            "    collect_unheld_api_images(store)\n"
            "    from gateway.session_api_media import compact_settled_api_payloads\n"
            "    compact_settled_api_payloads(store)\n"
        )
    else:
        sweep_block = (
            "    import logging\n"
            "    store = authority_persistence(authority)\n"
            "    _unexpected_refactored_sweep(store)\n"
        )

    return (
        '"""Fixture module for recover_authority_api_turns."""\n'
        "class AuthorityRefusal(Exception):\n"
        "    pass\n\n"
        "def authority_persistence(authority):\n"
        "    return authority.store\n\n"
        "def restore_api_session(authority, sid):\n"
        "    return ('ref', sid)\n\n"
        "def check_api_turn(authority, ref, payload):\n"
        "    return None\n\n"
        "def _refused_check(exc):\n"
        "    return str(exc)\n\n"
        "def recover_authority_api_turns(adapter, authority):\n"
        f"{sweep_block}"
        "    targets = sorted(store.admissions.pending_targets(statuses=frozenset({'queued'}), principal_id='api'))\n"
        "    for sid in targets:\n"
        "        try:\n"
        "            ref = restore_api_session(authority, sid)\n"
        "            pending = store.admissions.list(session_id=sid)\n"
        "            if any(row.status == 'unknown' for row in pending):\n"
        "                continue\n"
        "            for row in pending:\n"
        "                if row.status == 'queued':\n"
        "                    check_api_turn(authority, ref, row.payload)\n"
        "            authority._schedule(ref)\n"
        "        except AuthorityRefusal as exc:\n"
        "            logging.getLogger(__name__).warning('API session %s paused: %s', sid, _refused_check(exc))\n"
    )


def _load_module_from_path(path: Path, module_name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _EventLogHandler(logging.Handler):
    def __init__(self, event: threading.Event) -> None:
        super().__init__(level=logging.WARNING)
        self._event = event

    def emit(self, record: logging.LogRecord) -> None:
        self._event.set()


class TestStartupApiMediaSweepOffload(unittest.TestCase):
    """Tests for patch application, idempotency, fail-loud, and thread offload behavior."""

    def test_patches_upstream_snippet_and_validates_ast(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "session_api_turn.py"
            target.write_text(_build_session_api_turn_fixture(include_anchor=True), encoding="utf-8")

            modified = sweep_patch.patch_file(target)
            self.assertTrue(modified)

            patched = target.read_text(encoding="utf-8")
            ast.parse(patched, filename=str(target))
            self.assertIn(sweep_patch.MARKER, patched)
            self.assertIn("from gateway.session_authorities import owner_scope", patched)
            self.assertIn("with owner_scope(authority):", patched)
            self.assertIn("daemon=True", patched)
            self.assertIn(
                "targets = sorted(store.admissions.pending_targets(statuses=frozenset({'queued'}), principal_id='api'))",
                patched,
            )

    def test_idempotent_reapply_does_not_duplicate_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "session_api_turn.py"
            target.write_text(_build_session_api_turn_fixture(include_anchor=True), encoding="utf-8")

            self.assertEqual(sweep_patch.main(["--target", str(target)]), 0)
            after_first = target.read_text(encoding="utf-8")

            self.assertEqual(sweep_patch.main(["--target", str(target)]), 0)
            after_second = target.read_text(encoding="utf-8")

            self.assertEqual(after_second, after_first)
            self.assertEqual(after_second.count(sweep_patch.MARKER), 1)
            ast.parse(after_second, filename=str(target))

    def test_fails_loudly_when_anchor_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "session_api_turn.py"
            original = _build_session_api_turn_fixture(include_anchor=False)
            target.write_text(original, encoding="utf-8")

            with self.assertRaises(RuntimeError):
                sweep_patch.patch_file(target)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

            rc = sweep_patch.main(["--target", str(target)])
            self.assertNotEqual(rc, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_offload_runs_nonblocking_with_owner_scope_and_logs_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "session_api_turn.py"
            target.write_text(_build_session_api_turn_fixture(include_anchor=True), encoding="utf-8")
            self.assertTrue(sweep_patch.patch_file(target))

            mod_name = "test_patched_session_api_turn"
            try:
                mod = _load_module_from_path(target, mod_name)
                scope_var: ContextVar[object | None] = ContextVar("test_owner_scope", default=None)

                @contextmanager
                def fake_owner_scope(auth: object):
                    token = scope_var.set(auth)
                    try:
                        yield
                    finally:
                        scope_var.reset(token)

                started = threading.Event()
                release = threading.Event()
                finished = threading.Event()
                calls: list[tuple[str, object, object | None, threading.Thread]] = []

                def fake_collect(store_arg: object) -> None:
                    calls.append(("collect", store_arg, scope_var.get(), threading.current_thread()))
                    started.set()
                    if not release.wait(timeout=5.0):
                        raise TimeoutError("release event was not set")

                def fake_compact(store_arg: object) -> None:
                    calls.append(("compact", store_arg, scope_var.get(), threading.current_thread()))
                    finished.set()

                gw_pkg = types.ModuleType("gateway")
                gw_auth = types.ModuleType("gateway.session_authorities")
                gw_auth.owner_scope = fake_owner_scope  # type: ignore[attr-defined]
                gw_ingress = types.ModuleType("gateway.session_ingress_media")
                gw_ingress.collect_unheld_api_images = fake_collect  # type: ignore[attr-defined]
                gw_media = types.ModuleType("gateway.session_api_media")
                gw_media.compact_settled_api_payloads = fake_compact  # type: ignore[attr-defined]

                store = MagicMock()
                store.admissions.pending_targets.return_value = ["sess-1"]
                pending_row = MagicMock(status="queued", payload={"text": "hello"})
                store.admissions.list.return_value = [pending_row]

                authority = MagicMock()
                authority.name = "odin-test"
                authority.store = store

                with patch.dict(
                    sys.modules,
                    {
                        "gateway": gw_pkg,
                        "gateway.session_authorities": gw_auth,
                        "gateway.session_ingress_media": gw_ingress,
                        "gateway.session_api_media": gw_media,
                    },
                ):
                    # Must return immediately before release.set() is called
                    mod.recover_authority_api_turns(adapter=MagicMock(), authority=authority)

                    store.admissions.pending_targets.assert_called_once_with(
                        statuses=frozenset({"queued"}), principal_id="api"
                    )
                    authority._schedule.assert_called_once_with(("ref", "sess-1"))
                    self.assertTrue(started.wait(timeout=5.0), "sweep thread did not start")
                    self.assertFalse(finished.is_set(), "compact ran before collect was released")

                    release.set()
                    self.assertTrue(finished.wait(timeout=5.0), "sweep thread did not finish")

                self.assertEqual(len(calls), 2)
                caller_thread = threading.current_thread()
                for op_name, seen_store, seen_scope, seen_thread in calls:
                    with self.subTest(op=op_name):
                        self.assertIs(seen_store, store)
                        self.assertIs(seen_scope, authority)
                        self.assertIsNot(seen_thread, caller_thread)
                        self.assertTrue(seen_thread.daemon)
                        self.assertEqual(seen_thread.name, "api-media-sweep-odin-test")
                self.assertIsNone(scope_var.get())

                # Verify exception inside sweep thread logs a warning without crashing
                warned = threading.Event()

                def failing_collect(store_arg: object) -> None:
                    raise RuntimeError("SessionDB is closed")

                gw_ingress.collect_unheld_api_images = failing_collect  # type: ignore[attr-defined]
                gw_media.compact_settled_api_payloads = MagicMock()  # type: ignore[attr-defined]
                authority_err = MagicMock()
                authority_err.name = "err-auth"
                authority_err.store = MagicMock()
                authority_err.store.admissions.pending_targets.return_value = []

                with (
                    patch.dict(
                        sys.modules,
                        {
                            "gateway": gw_pkg,
                            "gateway.session_authorities": gw_auth,
                            "gateway.session_ingress_media": gw_ingress,
                            "gateway.session_api_media": gw_media,
                        },
                    ),
                    self.assertLogs(mod_name, level="WARNING") as log_cm,
                ):
                    logger = logging.getLogger(mod_name)
                    handler = _EventLogHandler(warned)
                    logger.addHandler(handler)
                    try:
                        mod.recover_authority_api_turns(adapter=MagicMock(), authority=authority_err)
                        self.assertTrue(warned.wait(timeout=5.0), "warning log was not emitted")
                    finally:
                        logger.removeHandler(handler)

                joined = "\n".join(log_cm.output)
                self.assertIn("Startup API media sweep failed", joined)
                self.assertIn("err-auth", joined)
                self.assertIn("SessionDB is closed", joined)
                gw_media.compact_settled_api_payloads.assert_not_called()
            finally:
                sys.modules.pop(mod_name, None)


if __name__ == "__main__":
    unittest.main()
