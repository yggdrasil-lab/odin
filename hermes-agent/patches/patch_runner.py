#!/usr/bin/env python3
"""Declarative build-time patch engine for Odin Hermes and plugin hooks."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile


class PatchStatus(str, Enum):
    """Evaluation or execution state of a declarative PatchSpec."""

    PENDING = "PENDING"
    APPLIED = "APPLIED"
    RETIRED_UPSTREAM = "RETIRED_UPSTREAM"
    DRIFT_ERROR = "DRIFT_ERROR"


@dataclass(frozen=True)
class Hunk:
    """Exact-match replacement or block-indent wrapper inside a target file."""

    label: str
    anchor: str
    replacement: str
    end_anchor: str | None = None
    end_replacement: str = ""
    indent_delta: int = 0


@dataclass(frozen=True)
class PatchSpec:
    """Declarative build-time patch definition targeting a single file."""

    id: str
    root_key: str
    rel_path: str
    marker: str
    hunks: tuple[Hunk, ...]
    retired_probe: str | Callable[[str], bool] | None = None
    verify_cmd: tuple[str, ...] = ()


@dataclass(frozen=True)
class PatchOutcome:
    """Result record produced for each PatchSpec during audit or apply."""

    id: str
    target: str
    status: PatchStatus
    detail: str = ""
    modified: bool = False


class PatchRunnerError(RuntimeError):
    """Raised when a patch fails anchor uniqueness, syntax, retirement, or verify checks."""


def default_roots() -> dict[str, Path]:
    """Resolve standard container roots for Hermes, agy plugin, and site-packages."""
    return {
        "hermes": Path("/opt/hermes"),
        "agy_plugin": Path("/opt/hermes/plugins/model-providers/antigravity-subscription-directsdk"),
        "site_packages": _discover_site_packages(),
    }


def apply_hunk(src: str, hunk: Hunk, spec_id: str) -> str:
    """Apply one Hunk to `src`, enforcing exact count == 1 on every anchor."""
    count = src.count(hunk.anchor)
    if count != 1:
        raise PatchRunnerError(
            f"{spec_id}:{hunk.label}: expected anchor count == 1, got {count}"
        )
    if hunk.end_anchor is None:
        return src.replace(hunk.anchor, hunk.replacement, 1)
    return _apply_wrapped_hunk(src, hunk, spec_id)


def evaluate_spec(
    spec: PatchSpec, target: Path
) -> tuple[PatchOutcome, str | None]:
    """Inspect `target` against `spec` without mutating disk."""
    if not target.is_file():
        return (
            PatchOutcome(spec.id, str(target), PatchStatus.DRIFT_ERROR, "target file missing"),
            None,
        )
    src = target.read_text(encoding="utf-8")
    if spec.marker in src:
        return (
            PatchOutcome(spec.id, str(target), PatchStatus.APPLIED, "marker already present"),
            None,
        )
    if _probe_retired(spec.retired_probe, src):
        return (
            PatchOutcome(
                spec.id, str(target), PatchStatus.RETIRED_UPSTREAM, "retired upstream probe matched"
            ),
            None,
        )
    return _render_pending_patch(spec, target, src)


def audit_all(
    specs: Sequence[PatchSpec], roots: Mapping[str, Path]
) -> list[PatchOutcome]:
    """Evaluate all `specs` read-only and return structured PatchOutcome records."""
    outcomes: list[PatchOutcome] = []
    for spec in specs:
        target = _resolve_target(spec, roots)
        outcome, _ = evaluate_spec(spec, target)
        outcomes.append(outcome)
    return outcomes


def apply_all(
    specs: Sequence[PatchSpec],
    roots: Mapping[str, Path],
    *,
    dry_run: bool = False,
    on_retired: str = "fail",
) -> list[PatchOutcome]:
    """Evaluate, validate, atomically write, and verify all `specs`."""
    if on_retired not in ("fail", "skip"):
        raise ValueError(f"Unsupported on_retired policy: {on_retired!r}")
    outcomes: list[PatchOutcome] = []
    pending_writes: list[tuple[PatchSpec, Path, str]] = []
    for spec in specs:
        target = _resolve_target(spec, roots)
        outcome, rendered = evaluate_spec(spec, target)
        _enforce_outcome(outcome, on_retired=on_retired)
        if outcome.status == PatchStatus.PENDING and rendered is not None:
            pending_writes.append((spec, target, rendered))
        outcomes.append(outcome)
    if dry_run:
        return outcomes
    final_outcomes = _commit_pending_writes(outcomes, pending_writes)
    _run_verifications(specs, roots)
    return final_outcomes


def _discover_site_packages() -> Path:
    candidates = sorted(Path("/opt/hermes/.venv/lib").glob("python*/site-packages"))
    if candidates:
        return candidates[-1]
    return Path("/opt/hermes/.venv/lib/python3.14/site-packages")


def _resolve_target(spec: PatchSpec, roots: Mapping[str, Path]) -> Path:
    if spec.root_key not in roots:
        raise PatchRunnerError(f"{spec.id}: unknown root_key {spec.root_key!r}")
    return Path(roots[spec.root_key]) / spec.rel_path


def _apply_wrapped_hunk(src: str, hunk: Hunk, spec_id: str) -> str:
    assert hunk.end_anchor is not None
    end_count = src.count(hunk.end_anchor)
    if end_count != 1:
        raise PatchRunnerError(
            f"{spec_id}:{hunk.label}: expected end_anchor count == 1, got {end_count}"
        )
    start_idx = src.index(hunk.anchor)
    end_idx = src.index(hunk.end_anchor)
    if start_idx >= end_idx:
        raise PatchRunnerError(f"{spec_id}:{hunk.label}: end_anchor precedes anchor")
    inner = src[start_idx + len(hunk.anchor) : end_idx]
    pad = " " * hunk.indent_delta
    indented = "".join(
        (pad + line if line.strip() else line) for line in inner.splitlines(keepends=True)
    )
    return (
        src[:start_idx]
        + hunk.replacement
        + indented
        + hunk.end_replacement
        + src[end_idx + len(hunk.end_anchor) :]
    )


def _probe_retired(
    probe: str | Callable[[str], bool] | None, src: str
) -> bool:
    if probe is None:
        return False
    if callable(probe):
        return bool(probe(src))
    return probe in src


def _render_pending_patch(
    spec: PatchSpec, target: Path, src: str
) -> tuple[PatchOutcome, str | None]:
    patched = src
    try:
        for hunk in spec.hunks:
            patched = apply_hunk(patched, hunk, spec.id)
        ast.parse(patched, filename=str(target))
    except (PatchRunnerError, SyntaxError) as exc:
        return (
            PatchOutcome(spec.id, str(target), PatchStatus.DRIFT_ERROR, str(exc)),
            None,
        )
    return (
        PatchOutcome(spec.id, str(target), PatchStatus.PENDING, "ready to apply"),
        patched,
    )


def _enforce_outcome(outcome: PatchOutcome, *, on_retired: str) -> None:
    if outcome.status == PatchStatus.DRIFT_ERROR:
        raise PatchRunnerError(f"[{outcome.id}] {outcome.detail}")
    if outcome.status == PatchStatus.RETIRED_UPSTREAM and on_retired == "fail":
        raise PatchRunnerError(f"[{outcome.id}] {outcome.detail}")


def _commit_pending_writes(
    outcomes: list[PatchOutcome],
    pending_writes: Sequence[tuple[PatchSpec, Path, str]],
) -> list[PatchOutcome]:
    written_ids: set[str] = set()
    for spec, target, rendered in pending_writes:
        _atomic_compile_and_replace(target, rendered, spec.id)
        written_ids.add(spec.id)
    updated: list[PatchOutcome] = []
    for outcome in outcomes:
        if outcome.id in written_ids:
            updated.append(
                PatchOutcome(outcome.id, outcome.target, PatchStatus.APPLIED, "patched", modified=True)
            )
        else:
            updated.append(outcome)
    return updated


def _atomic_compile_and_replace(target: Path, content: str, spec_id: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=".odin-patch-", suffix=".py", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    pyc_fd, pyc_name = tempfile.mkstemp(prefix=".odin-pyc-", suffix=".pyc")
    pyc_path = Path(pyc_name)
    os.close(pyc_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        py_compile.compile(str(tmp_path), cfile=str(pyc_path), doraise=True)
        os.chmod(tmp_path, target.stat().st_mode)
        os.replace(tmp_path, target)
        py_compile.compile(str(target), doraise=True)
    except (OSError, py_compile.PyCompileError) as exc:
        raise PatchRunnerError(f"[{spec_id}] atomic compile/write failed: {exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)
        pyc_path.unlink(missing_ok=True)


def _run_verifications(specs: Sequence[PatchSpec], roots: Mapping[str, Path]) -> None:
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for spec in specs:
        if not spec.verify_cmd:
            continue
        key = (spec.root_key, spec.verify_cmd)
        if key in seen:
            continue
        seen.add(key)
        _exec_verify_cmd(spec, roots)


def _exec_verify_cmd(spec: PatchSpec, roots: Mapping[str, Path]) -> None:
    cwd = Path(roots[spec.root_key])
    env = _build_verify_env(cwd, roots)
    proc = subprocess.run(list(spec.verify_cmd), cwd=str(cwd), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PatchRunnerError(
            f"[{spec.id}] verification failed (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )


def _build_verify_env(cwd: Path, roots: Mapping[str, Path]) -> dict[str, str]:
    env = os.environ.copy()
    pp_parts = [str(cwd)]
    tests_dir = cwd / "tests"
    if tests_dir.is_dir():
        pp_parts.append(str(tests_dir))
    for extra_key in ("hermes", "site_packages"):
        if extra_key in roots:
            pp_parts.append(str(roots[extra_key]))
    if env.get("PYTHONPATH"):
        pp_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(pp_parts))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _parse_root_overrides(raw_roots: Sequence[str], base: dict[str, Path]) -> dict[str, Path]:
    roots = dict(base)
    for item in raw_roots:
        if "=" not in item:
            raise ValueError(f"Expected KEY=PATH for --root, got {item!r}")
        key, value = item.split("=", 1)
        roots[key.strip()] = Path(value.strip())
    return roots


def _format_outcomes(outcomes: Sequence[PatchOutcome], as_json: bool) -> str:
    if as_json:
        payload = [
            {**asdict(item), "status": item.status.value}
            for item in outcomes
        ]
        return json.dumps(payload, indent=2)
    lines = [
        f"[{item.status.value}] {item.id}: {item.target} ({item.detail})"
        for item in outcomes
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply or audit Odin declarative build-time patches.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("apply", "audit"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--json", action="store_true", help="Emit JSON summary")
        sub.add_argument("--root", action="append", default=[], help="Override root as KEY=PATH")
        if name == "apply":
            sub.add_argument("--dry-run", action="store_true", help="Validate without modifying files")
            sub.add_argument("--on-retired", choices=("fail", "skip"), default="fail")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint supporting `apply` and `audit`."""
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    from specs import get_specs

    specs = get_specs()
    roots = _parse_root_overrides(args.root, default_roots())
    if args.command == "audit":
        outcomes = audit_all(specs, roots)
        print(_format_outcomes(outcomes, args.json))
        has_err = any(
            o.status in (PatchStatus.DRIFT_ERROR, PatchStatus.RETIRED_UPSTREAM) for o in outcomes
        )
        return 1 if has_err else 0
    try:
        outcomes = apply_all(specs, roots, dry_run=args.dry_run, on_retired=args.on_retired)
    except PatchRunnerError as exc:
        print(f"[odin-patch] ERROR: {exc}", file=sys.stderr)
        return 1
    print(_format_outcomes(outcomes, args.json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
