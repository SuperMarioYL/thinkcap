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


def _flat(output: str) -> str:
    """Collapse rich's terminal line-wrapping for substring assertions.

    A non-tty rich Console wraps at 80 columns, so multi-word fragments can
    span lines in CI; asserting on the whitespace-normalized string keeps
    the tests width-independent.
    """
    return " ".join(output.split())


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
    flat = _flat(result.output)
    assert "error:" in flat
    assert "http://127.0.0.1:1" in flat
    assert "Traceback" not in flat


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
    assert "unknown suite" in _flat(result.output)
    assert "Traceback" not in result.output


# --- serve: malformed policy must fail fast, not crash --------------------


def test_serve_invalid_json_policy_is_clean_error(tmp_path) -> None:
    # v0.1.0 crashed here: create_app only caught FileNotFoundError, so a
    # truncated/hand-edited policy.json raised json.JSONDecodeError through
    # cli.serve -> proxy.run -> create_app.
    bad = tmp_path / "bad.json"
    bad.write_text("{broken json", encoding="utf-8")
    result = runner.invoke(app, ["serve", "--policy", str(bad), "--port", "9917"])
    assert result.exit_code == 2
    flat = _flat(result.output)
    assert "error:" in flat
    assert "is not a valid policy.json" in flat
    assert "Traceback" not in flat


def test_serve_wrong_shape_policy_is_clean_error(tmp_path) -> None:
    # budgets must be a list; a string here raised ValueError at startup.
    bad = tmp_path / "badshape.json"
    bad.write_text('{"version": "0.1.0", "budgets": "NOT-A-LIST"}', encoding="utf-8")
    result = runner.invoke(app, ["serve", "--policy", str(bad), "--port", "9918"])
    assert result.exit_code == 2
    assert "is not a valid policy.json" in _flat(result.output)
    assert "Traceback" not in result.output


def test_serve_missing_policy_warns_and_runs_passthrough(
    tmp_path, monkeypatch
) -> None:
    # The documented first-run contract is unchanged: a missing file warns
    # and serve continues passthrough-only (policy_path=None to the app).
    import thinkcap.proxy as proxy_mod

    calls = {}
    monkeypatch.setattr(proxy_mod, "run", lambda **kw: calls.update(kw))
    result = runner.invoke(
        app,
        ["serve", "--policy", str(tmp_path / "missing.json"), "--port", "9919"],
    )
    assert result.exit_code == 0
    flat = _flat(result.output)
    assert "warn" in flat
    assert "passthrough-only" in flat
    assert calls["policy_path"] is None


def test_serve_valid_policy_reaches_proxy_run(tmp_path, monkeypatch) -> None:
    import thinkcap.proxy as proxy_mod

    calls = {}
    monkeypatch.setattr(proxy_mod, "run", lambda **kw: calls.update(kw))
    good = tmp_path / "good.json"
    good.write_text(
        '{"version": "0.1.0", "budgets": '
        '[{"task_class": "default", "budget_tokens": 100}]}',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["serve", "--policy", str(good), "--port", "9920"])
    assert result.exit_code == 0
    assert "warn" not in result.output
    assert calls["policy_path"] == str(good)


def test_create_app_with_malformed_policy_degrades_to_passthrough(tmp_path) -> None:
    # Programmatic contract: create_app always returns an app object; an
    # unusable policy means passthrough-only (no classes, not enforcing).
    from fastapi.testclient import TestClient

    from thinkcap.proxy import create_app

    bad = tmp_path / "bad.json"
    bad.write_text("{broken json", encoding="utf-8")
    app = create_app(policy_path=str(bad))
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enforce"] is False
    assert body["classes"] == []
