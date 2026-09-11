"""Profiler tests — the policy must reflect the accumulated distribution.

v0.1.0 built policy.json from the latest probe run only, so the appended
budgets.json observations never widened it: re-probing a class after a
flaky low-token run silently reset its budget to near zero, contradicting
the documented accumulate-and-widen behavior (budget_store.py docstring,
README --budgets row). These tests pin the fixed semantics.
"""
from __future__ import annotations

import json

from thinkcap.config import THINK_END, THINK_START, TaskClass, get_suite
from thinkcap.policy import percentile
from thinkcap.profiler import probe


def _resp(body: str) -> str:
    return THINK_START + body + THINK_END + "ACTION"


def _mock(bodies: dict) -> dict:
    return {task_id: _resp(body) for task_id, body in bodies.items()}


def _stub_tokenize(text: str):
    return (len(text.split()), "stub")


def test_policy_widens_with_accumulated_observations(tmp_path) -> None:
    # The v0.1.0 repro: run 1 observes rename ≈ 2000 tokens, run 2 ≈ 5.
    # The shipped policy must reflect the accumulated store (p90 = 2000),
    # not the latest run (5).
    suite = get_suite("coding-mini")
    policy_path = str(tmp_path / "policy.json")
    budgets_path = str(tmp_path / "budgets.json")

    hi = _mock(
        {
            "rename-01": "x " * 2000,
            "rename-02": "x " * 2000,
            "bugfix-01": "y " * 100,
            "refactor-01": "y " * 100,
            "newfile-01": "y " * 100,
        }
    )
    probe(
        "http://mock",
        suite,
        mock_responses=hi,
        tokenize_fn=_stub_tokenize,
        policy_path=policy_path,
        budget_store_path=budgets_path,
    )

    lo = _mock(
        {
            "rename-01": "x " * 5,
            "rename-02": "x " * 5,
            "bugfix-01": "y " * 2,
            "refactor-01": "y " * 2,
            "newfile-01": "y " * 2,
        }
    )
    report = probe(
        "http://mock",
        suite,
        mock_responses=lo,
        tokenize_fn=_stub_tokenize,
        policy_path=policy_path,
        budget_store_path=budgets_path,
    )

    policy = json.loads((tmp_path / "policy.json").read_text(encoding="utf-8"))
    rename = next(b for b in policy["budgets"] if b["task_class"] == "rename")
    assert rename["budget_tokens"] == percentile([2000, 2000, 5, 5], 90)
    assert rename["budget_tokens"] == 2000

    store = json.loads((tmp_path / "budgets.json").read_text(encoding="utf-8"))
    rename_observed = [
        r["tokens"] for r in store["records"] if r["task_class"] == "rename"
    ]
    assert rename_observed == [2000, 2000, 5, 5]

    # The printed table shows the same accumulated distribution.
    rename_prof = next(p for p in report.profiles if p.task_class == TaskClass.RENAME)
    assert rename_prof.n == 4
    assert rename_prof.p90 == 2000


def test_report_without_artifacts_keeps_current_run_profiles(tmp_path) -> None:
    suite = get_suite("coding-mini")
    hi = _mock(
        {
            "rename-01": "x " * 2000,
            "rename-02": "x " * 2000,
            "bugfix-01": "y " * 100,
            "refactor-01": "y " * 100,
            "newfile-01": "y " * 100,
        }
    )
    report = probe(
        "http://mock",
        suite,
        mock_responses=hi,
        tokenize_fn=_stub_tokenize,
        policy_path=str(tmp_path / "unused-policy.json"),
        budget_store_path=str(tmp_path / "unused-budgets.json"),
        write_artifacts=False,
    )
    rename_prof = next(p for p in report.profiles if p.task_class == TaskClass.RENAME)
    assert rename_prof.n == 2
    assert not (tmp_path / "unused-budgets.json").exists()


def test_token_source_provenance_survives_store_round_trip(tmp_path) -> None:
    # A class whose history includes API-counted observations must keep the
    # tokenize_api label when re-aggregated from the store.
    suite = get_suite("coding-mini")
    bodies = {
        "rename-01": "x " * 10,
        "rename-02": "x " * 10,
        "bugfix-01": "y " * 10,
        "refactor-01": "y " * 10,
        "newfile-01": "y " * 10,
    }
    probe(
        "http://mock",
        suite,
        mock_responses=_mock(bodies),
        tokenize_fn=lambda t: (len(t.split()), "tokenize_api"),
        policy_path=str(tmp_path / "policy.json"),
        budget_store_path=str(tmp_path / "budgets.json"),
    )
    report = probe(
        "http://mock",
        suite,
        mock_responses=_mock(bodies),
        tokenize_fn=_stub_tokenize,
        policy_path=str(tmp_path / "policy.json"),
        budget_store_path=str(tmp_path / "budgets.json"),
    )
    rename_prof = next(p for p in report.profiles if p.task_class == TaskClass.RENAME)
    assert rename_prof.token_source == "tokenize_api"
    assert rename_prof.n == 4


def test_v010_store_files_load_without_token_source(tmp_path) -> None:
    # Backward compat: budgets.json written by v0.1.0 has no token_source
    # field; it must load and re-aggregate as approx.
    from thinkcap.budget_store import BudgetStore

    legacy = {
        "version": "0.1.0",
        "records": [
            {"task_class": "rename", "tokens": 620, "outcome": "observed", "ts": 1.0},
        ],
    }
    path = tmp_path / "budgets.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    store = BudgetStore(path)
    profiles = store.profiles()
    assert profiles[0].task_class == TaskClass.RENAME
    assert profiles[0].observed_tokens == [620]
    assert profiles[0].token_source == "approx"
