"""The release workflows must actually run.

A shell step that looks right but is mangled by quoting breaks a release
channel silently: the Home Assistant add-on job failed once because the shell
built a different regular expression than the one the file appeared to hold.
These tests run the steps for real.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "homeassistant.yml"
VERSION = (ROOT / "VERSION").read_text().strip()


def _step_script(name: str) -> str:
    """The shell body of the workflow step called ``name``."""
    lines = WORKFLOW.read_text().splitlines()
    for index, line in enumerate(lines):  # noqa: B007 (index used after the loop)
        if line.strip() == f"- name: {name}":
            break
    else:
        raise AssertionError(f"no step named {name!r} in {WORKFLOW}")
    for offset, candidate in enumerate(lines[index:], start=index):  # noqa: B007
        if candidate.strip() == "run: |":
            break
    else:
        raise AssertionError(f"step {name!r} has no run block")
    body: list[str] = []
    for candidate in lines[offset + 1:]:
        if candidate.strip() and not candidate.startswith("          "):
            break
        body.append(candidate[10:])
    script = "\n".join(body)
    assert script.strip(), f"step {name!r} has an empty run block"
    return script


def _run(script: str, version: str, prerelease: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        {
            "INPUT_VERSION": version,
            "INPUT_PRERELEASE": prerelease,
            "GITHUB_OUTPUT": str(ROOT / ".test-github-output"),
        }
    )
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def output_file() -> Iterator[Path]:
    path = ROOT / ".test-github-output"
    yield path
    path.unlink(missing_ok=True)


def test_the_add_on_version_step_runs_and_agrees_with_the_manifest(output_file: Path) -> None:
    result = _run(_step_script("Resolve image version"), VERSION, "true")
    assert result.returncode == 0, result.stderr
    written = output_file.read_text()
    assert f"value={VERSION}" in written


def test_the_add_on_version_step_rejects_a_manifest_mismatch() -> None:
    result = _run(_step_script("Resolve image version"), "9.9.9", "true")
    assert result.returncode != 0
    assert "must match release version" in result.stderr


def test_a_pre_release_does_not_move_the_latest_tag(output_file: Path) -> None:
    result = _run(_step_script("Resolve image version"), VERSION, "true")
    assert result.returncode == 0, result.stderr
    written = output_file.read_text()
    assert f"tags<<OSPREY_TAGS_EOF\n{VERSION}\nOSPREY_TAGS_EOF" in written
    assert "latest" not in written


def test_a_final_release_moves_the_latest_tag(output_file: Path) -> None:
    result = _run(_step_script("Resolve image version"), VERSION, "false")
    assert result.returncode == 0, result.stderr
    written = output_file.read_text()
    assert f"tags<<OSPREY_TAGS_EOF\n{VERSION}\nlatest\nOSPREY_TAGS_EOF" in written
