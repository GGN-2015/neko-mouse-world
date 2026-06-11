from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]


PACKAGE_DISTRIBUTION_NAME = "neko-mouse-world"
PACKAGE_IMPORT_NAME = "neko_mouse_world"
_VALID_PROJECT_NAMES = {
    "neko-mouse-world",
}


@lru_cache(maxsize=1)
def get_neko_mouse_world_version() -> str:
    """Return this package version in both source-tree and installed layouts."""

    for pyproject in _candidate_pyprojects():
        version = _version_from_pyproject(pyproject)
        if version:
            return version
    version = _version_from_pip()
    return version or "unknown"


def _candidate_pyprojects() -> list[Path]:
    package_dir = Path(__file__).resolve().parent
    candidates: list[Path] = []
    for directory in (package_dir, *package_dir.parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            candidates.append(candidate)
    return candidates


def _version_from_pyproject(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception:
            return None
        project = data.get("project", {})
        if not isinstance(project, dict):
            return None
        name = str(project.get("name", "")).strip()
        if _normalize_project_name(name) not in _VALID_PROJECT_NAMES:
            return None
        version = str(project.get("version", "")).strip()
        return version or None
    return _version_from_pyproject_text(text)


def _version_from_pyproject_text(text: str) -> str | None:
    in_project = False
    project_name = ""
    project_version = ""
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            in_project = line == "[project]"
            continue
        if not in_project:
            continue
        if line.startswith("name"):
            project_name = _toml_string_value(line)
        elif line.startswith("version"):
            project_version = _toml_string_value(line)
    if _normalize_project_name(project_name) not in _VALID_PROJECT_NAMES:
        return None
    return project_version or None


def _toml_string_value(line: str) -> str:
    match = re.match(r"^[A-Za-z0-9_.-]+\s*=\s*(['\"])(.*?)\1\s*$", line)
    if not match:
        return ""
    return match.group(2).strip()


def _version_from_pip() -> str | None:
    for name in (PACKAGE_DISTRIBUTION_NAME, PACKAGE_IMPORT_NAME):
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pip", "show", name],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        for line in completed.stdout.splitlines():
            if line.lower().startswith("version:"):
                version = line.split(":", 1)[1].strip()
                if version:
                    return version
    return None


def _normalize_project_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name).strip().lower())
