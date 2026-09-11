"""Version lockstep — every live surface must report the same version.

Surfaces: the VERSION file, pyproject.toml [project] version, the installed
package metadata (thinkcap.config.VERSION), the version save_policy writes
into policy.json, web/site.json meta.content_version, the console script
(``thinkcap version``, only when the binary is on PATH — GH runners without
it stay green), and the CHANGELOG sections.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from thinkcap.config import VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED = "0.2.0"


def test_version_file() -> None:
    assert (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip() == EXPECTED


def test_pyproject_version() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None
    assert match.group(1) == EXPECTED


def test_installed_metadata_version() -> None:
    # importlib.metadata for the editable/installed build; equals the
    # fallback constant when running from a bare source tree.
    assert VERSION == EXPECTED


def test_save_policy_version(tmp_path) -> None:
    from thinkcap.config import TaskClass
    from thinkcap.policy import BudgetPolicy, save_policy

    path = save_policy(
        [BudgetPolicy(task_class=TaskClass.DEFAULT, budget_tokens=100)],
        tmp_path / "policy.json",
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == EXPECTED


def test_site_json_content_version() -> None:
    data = json.loads((REPO_ROOT / "web" / "site.json").read_text(encoding="utf-8"))
    assert data["meta"]["content_version"] == EXPECTED


def test_changelog_has_both_sections() -> None:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.1.0] - 2026-08-17" in text
    assert "## [0.2.0] - 2026-09-11" in text


@pytest.mark.skipif(not shutil.which("thinkcap"), reason="thinkcap not on PATH")
def test_console_script_version() -> None:
    out = subprocess.run(
        ["thinkcap", "version"], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == f"thinkcap {EXPECTED}"
