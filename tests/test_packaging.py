"""The package must build from a context that carries only the source tree.

This exists because a deploy failed where every local check passed. `pyproject.toml`
declared `license = { file = "LICENSE" }`, and hatchling hard-fails metadata generation
with `OSError: License file does not exist` when that file is absent from the build
context. Railway's Nixpacks upload omits it, so the build died on a file that was
committed, present on the remote, and present in every local clone.

A packaging bug of this shape is invisible to the rest of the suite: nothing else builds
a wheel, so `pip install -e .` succeeding locally proves nothing about a remote build.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent


def _build_wheel(context: Path) -> Path:
    out = context / "dist"
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(out), "."],
        cwd=str(context),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        "wheel build failed:\n" + completed.stdout[-2500:] + completed.stderr[-2500:]
    )
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    return wheels[0]


@pytest.fixture(scope="module")
def minimal_context(tmp_path_factory) -> Path:
    """Just the source tree and pyproject — no LICENSE, no .git, no README."""
    context = tmp_path_factory.mktemp("minimal")
    shutil.copytree(REPO / "src", context / "src")
    shutil.copy(REPO / "pyproject.toml", context / "pyproject.toml")
    return context


def test_the_wheel_builds_without_a_license_file(minimal_context: Path):
    """The regression test proper. This is the exact Railway failure.

    Asserting on the build rather than on pyproject's text: any license declaration that
    can satisfy this is acceptable, and the test does not care which one is used.
    """
    assert _build_wheel(minimal_context).exists()


def test_the_wheel_builds_without_a_readme(minimal_context: Path):
    """Same failure shape, different file. `readme = ` would fail identically, so this
    pins that the metadata does not acquire a second context dependency later."""
    assert (minimal_context / "README.md").exists() is False
    assert _build_wheel(minimal_context).exists()


def test_the_license_is_still_declared_as_mit(minimal_context: Path):
    """Building without the file must not mean shipping without the licence.

    The point of the fix was to stop the *build* depending on the file, not to drop the
    licence from the metadata — an MIT portfolio repo that publishes no licence is a
    different and worse problem.
    """
    wheel = _build_wheel(minimal_context)
    with zipfile.ZipFile(wheel) as archive:
        name = next(n for n in archive.namelist() if n.endswith("METADATA"))
        metadata = archive.read(name).decode()

    assert "License-Expression: MIT" in metadata or "License: MIT" in metadata


def test_the_license_text_ships_when_the_file_is_present(tmp_path: Path):
    """With the file in the context it must still be packaged, so an installed copy
    carries the licence text."""
    shutil.copytree(REPO / "src", tmp_path / "src")
    shutil.copy(REPO / "pyproject.toml", tmp_path / "pyproject.toml")
    shutil.copy(REPO / "LICENSE", tmp_path / "LICENSE")

    with zipfile.ZipFile(_build_wheel(tmp_path)) as archive:
        licences = [n for n in archive.namelist() if "LICENSE" in n.upper()]
    assert licences, "LICENSE was not packaged even though it was in the context"


def test_the_console_script_and_package_are_in_the_wheel(minimal_context: Path):
    """A wheel that builds but ships no package would pass every test above."""
    with zipfile.ZipFile(_build_wheel(minimal_context)) as archive:
        names = archive.namelist()
    assert "dbt_sentinel/webhook.py" in names
    assert any(n.endswith("entry_points.txt") for n in names)
