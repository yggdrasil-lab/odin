"""Bridge image-baked base venv site-packages into Hermes PM child environments."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)
PTH_FILENAME = "odin-base-site.pth"


def link_base_site(selected_site: Path, base_site: Path) -> Path | None:
    """Write `odin-base-site.pth` inside `selected_site` pointing at `base_site`."""
    if not selected_site.is_dir() or not base_site.is_dir():
        return None
    if selected_site.resolve() == base_site.resolve():
        return None
    pth_file = selected_site / PTH_FILENAME
    expected = f"{base_site}\n"
    try:
        if pth_file.is_file() and pth_file.read_text(encoding="utf-8") == expected:
            return pth_file
        pth_file.write_text(expected, encoding="utf-8")
        return pth_file
    except OSError as exc:
        logger.warning("Failed to write base site-packages .pth %s: %s", pth_file, exc)
        return None


def merged_pythonpath(
    project_root: Path,
    selected_site: Path | None,
    sys_path: Sequence[str],
    *,
    base_site: Path | None = None,
) -> tuple[list[str], str]:
    """Compute ordered `sys.path` and `PYTHONPATH` keeping base site-packages visible."""
    resolved_project = str(Path(project_root).resolve())
    ordered: list[str] = [resolved_project]
    exclude: set[Path] = set()
    if selected_site is not None:
        ordered.append(str(selected_site))
        exclude.add(Path(selected_site).resolve())
        if _should_include_base_site(selected_site, base_site):
            assert base_site is not None
            ordered.append(str(base_site))
            exclude.add(Path(base_site).resolve())
    retained = [entry for entry in sys_path if _safe_resolve(entry) not in exclude]
    return [*ordered, *retained], os.pathsep.join(ordered)


def bridge_stamp_digest(project_root: Path) -> bytes:
    """Return digest of PM bridge inputs so edits invalidate stale PM environments."""
    hasher = hashlib.sha256()
    inputs = (
        Path(__file__),
        Path(project_root) / "pm" / "environments.py",
    )
    for path in inputs:
        if path.is_file():
            hasher.update(path.read_bytes())
        else:
            hasher.update(b"missing")
    return hasher.digest()


def _should_include_base_site(selected_site: Path, base_site: Path | None) -> bool:
    if base_site is None or not Path(base_site).is_dir():
        return False
    return Path(base_site).resolve() != Path(selected_site).resolve()


def _safe_resolve(entry: str) -> Path:
    return Path(entry).resolve()
