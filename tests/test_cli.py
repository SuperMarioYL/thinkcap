"""CLI contract tests — error paths must be clean, never tracebacks.

v0.1.0 shipped two crash paths: ``thinkcap probe`` against an unreachable
server dumped a raw httpx traceback, and ``thinkcap serve`` with a malformed
policy.json crashed at startup with ``json.JSONDecodeError`` / ``ValueError``.
These tests pin the fixed contract: a red ``error:`` line, exit code 2, and
no traceback — the same contract the unknown-suite path always had.
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from thinkcap.cli import app

runner = CliRunner()


def test_probe_unreachable_server_is_clean_error(tmp_path) -> None:
    # Nothing listens on 127.0.0.1:1 — loopback connect is refused
    # instantly on macOS/Linux/CI runners. v0.1.0 printed a 40-line
    # httpx traceback and exited 1 here.
    result = runner.invoke(
        app,
        [
            "probe",
            "--server",
            "http://127.0.0.1:1",
            "--policy",
            str(tmp_path / "policy.json"),
            "--budgets",
            str(tmp_path / "budgets.json"),
        ],
    )
    assert result.exit_code == 2
    assert "error:" in result.output
    assert "http://127.0.0.1:1" in result.output
    assert "Traceback" not in result.output


def test_probe_mock_still_succeeds_and_writes_policy(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "probe",
            "--mock",
            "--policy",
            str(tmp_path / "policy.json"),
            "--budgets",
            str(tmp_path / "budgets.json"),
        ],
    )
    assert result.exit_code == 0
    policy = json.loads((tmp_path / "policy.json").read_text(encoding="utf-8"))
    assert any(b["task_class"] == "rename" for b in policy["budgets"])
    store = json.loads((tmp_path / "budgets.json").read_text(encoding="utf-8"))
    assert len(store["records"]) == 5


def test_probe_unknown_suite_is_clean_error() -> None:
    result = runner.invoke(app, ["probe", "--suite", "nope", "--mock"])
    assert result.exit_code == 2
    assert "unknown suite" in result.output
    assert "Traceback" not in result.output
