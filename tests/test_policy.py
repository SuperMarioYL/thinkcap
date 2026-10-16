"""Tests for the policy model + the governor's decision logic.

These cover the pure, testable half of the m2 governor (the injection
*mechanism* ships in m2's proxy; this module owns the *decision*):

* :func:`percentile` — the small-sample estimator;
* :class:`ReasoningProfile.recompute` — p50/p90 from observations;
* :func:`build_policy` — p90 budgets + a guaranteed default row;
* :func:`save_policy` / :func:`load_policy` — round-trip;
* :func:`should_inject` — the governor's gate: over budget, in-think,
  not-yet-injected.
"""
from __future__ import annotations

import json

import pytest

from thinkcap.config import (
    DEFAULT_FALLBACK_BUDGET,
    DEFAULT_INCEPTION_TOKEN,
    TaskClass,
)
from thinkcap.policy import (
    BudgetPolicy,
    ReasoningProfile,
    build_policy,
    load_policy,
    percentile,
    policy_index,
    save_policy,
    should_inject,
)
from thinkcap.trace_parser import LiveTraceState


# --- percentile ------------------------------------------------------------


def test_percentile_empty_is_zero() -> None:
    assert percentile([], 90) == 0


def test_percentile_single_observation_returned_verbatim() -> None:
    assert percentile([620], 90) == 620
    assert percentile([620], 50) == 620


def test_percentile_p50_is_median_for_even_sample() -> None:
    # [100, 200, 300, 400] -> median of 200/300 -> 250
    assert percentile([400, 100, 300, 200], 50) == 250


def test_percentile_p90_never_exceeds_max() -> None:
    assert percentile([100, 200, 300], 90) <= 300


def test_percentile_p90_at_least_max_for_two_point_sample() -> None:
    # [100, 200] -> p90 interpolates near the top, <= 200
    val = percentile([100, 200], 90)
    assert 100 <= val <= 200


# --- ReasoningProfile ------------------------------------------------------


def test_profile_recompute_sets_stats() -> None:
    p = ReasoningProfile(
        task_class=TaskClass.RENAME,
        observed_tokens=[600, 620, 640],
    )
    p.recompute()
    assert p.n == 3
    assert p.p50 == 620
    assert p.p90 >= 620
    assert p.p90 <= 640


def test_profile_empty_observations() -> None:
    p = ReasoningProfile(task_class=TaskClass.DEFAULT)
    p.recompute()
    assert p.n == 0
    assert p.p50 == 0
    assert p.p90 == 0


# --- build_policy ----------------------------------------------------------


def test_build_policy_uses_p90_for_budget() -> None:
    prof = ReasoningProfile(
        task_class=TaskClass.RENAME,
        observed_tokens=[600, 620, 640],
    )
    prof.recompute()
    policies = build_policy([prof])
    rename = next(p for p in policies if p.task_class == TaskClass.RENAME)
    assert rename.budget_tokens == prof.p90
    assert rename.source == "p90_observed"
    assert rename.inception_token == DEFAULT_INCEPTION_TOKEN


def test_build_policy_always_includes_default() -> None:
    # even with only rename observations, a default row must ship so the
    # proxy has a class to fall back to.
    prof = ReasoningProfile(task_class=TaskClass.RENAME, observed_tokens=[620])
    prof.recompute()
    policies = build_policy([prof])
    classes = [p.task_class for p in policies]
    assert TaskClass.DEFAULT in classes
    default = next(p for p in policies if p.task_class == TaskClass.DEFAULT)
    assert default.budget_tokens == DEFAULT_FALLBACK_BUDGET
    assert default.source == "manual"


def test_build_policy_p90_zero_falls_back_to_p50() -> None:
    prof = ReasoningProfile(
        task_class=TaskClass.BUGFIX, observed_tokens=[1800]
    )
    prof.recompute()
    # single observation: p90 == p50 == 1800
    policies = build_policy([prof])
    bugfix = next(p for p in policies if p.task_class == TaskClass.BUGFIX)
    assert bugfix.budget_tokens >= 1


def test_build_policy_custom_inception_token() -> None:
    custom = " </think>\nActing now."
    prof = ReasoningProfile(task_class=TaskClass.RENAME, observed_tokens=[620])
    prof.recompute()
    policies = build_policy([prof], inception_token=custom)
    assert all(p.inception_token == custom for p in policies)


# --- save / load round-trip -----------------------------------------------


def test_policy_round_trip(tmp_path) -> None:
    profs = [
        ReasoningProfile(
            task_class=TaskClass.RENAME, observed_tokens=[600, 620, 640]
        ),
        ReasoningProfile(task_class=TaskClass.BUGFIX, observed_tokens=[1800]),
    ]
    for p in profs:
        p.recompute()
    policies = build_policy(profs)
    path = tmp_path / "policy.json"
    save_policy(policies, path)

    loaded = load_policy(path)
    assert len(loaded) == len(policies)
    by = policy_index(loaded)
    assert by[TaskClass.RENAME].budget_tokens == next(
        p for p in policies if p.task_class == TaskClass.RENAME
    ).budget_tokens
    assert by[TaskClass.DEFAULT].budget_tokens == DEFAULT_FALLBACK_BUDGET


def test_policy_file_is_valid_json_with_budgets_key(tmp_path) -> None:
    policies = build_policy(
        [ReasoningProfile(task_class=TaskClass.RENAME, observed_tokens=[620])]
    )
    path = tmp_path / "policy.json"
    save_policy(policies, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == "0.3.0"
    assert isinstance(data["budgets"], list)
    assert any(b["task_class"] == "rename" for b in data["budgets"])


def test_load_policy_missing_file_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_policy(tmp_path / "nope.json")


# --- should_inject (the governor gate) ------------------------------------


def test_should_inject_true_when_over_budget_in_think() -> None:
    policy = BudgetPolicy(task_class=TaskClass.RENAME, budget_tokens=100)
    # bytes_seen=500 -> est ~125 tokens >= 100 budget
    state = LiveTraceState(bytes_seen=500, in_think=True, budget=100, injected=False)
    assert should_inject(state, policy) is True


def test_should_inject_false_when_under_budget() -> None:
    policy = BudgetPolicy(task_class=TaskClass.RENAME, budget_tokens=1000)
    state = LiveTraceState(bytes_seen=100, in_think=True, budget=1000, injected=False)
    assert should_inject(state, policy) is False


def test_should_inject_false_when_not_in_think() -> None:
    # action text is never governed
    policy = BudgetPolicy(task_class=TaskClass.RENAME, budget_tokens=1)
    state = LiveTraceState(bytes_seen=10000, in_think=False, budget=1, injected=False)
    assert should_inject(state, policy) is False


def test_should_inject_false_after_already_injected() -> None:
    # the inception thought is sticky: one injection per turn, never twice
    policy = BudgetPolicy(task_class=TaskClass.RENAME, budget_tokens=1)
    state = LiveTraceState(bytes_seen=10000, in_think=True, budget=1, injected=True)
    assert should_inject(state, policy) is False


def test_should_inject_false_for_zero_budget() -> None:
    policy = BudgetPolicy(task_class=TaskClass.DEFAULT, budget_tokens=0)
    state = LiveTraceState(bytes_seen=10000, in_think=True, budget=0, injected=False)
    assert should_inject(state, policy) is False


# --- BudgetPolicy serialization -------------------------------------------


def test_budget_policy_round_trip_through_dict() -> None:
    p = BudgetPolicy(
        task_class=TaskClass.REFACTOR,
        budget_tokens=2400,
        source="manual",
        inception_token=" custom inception",
    )
    d = p.to_dict()
    assert d["task_class"] == "refactor"
    assert d["budget_tokens"] == 2400
    restored = BudgetPolicy.from_dict(d)
    assert restored.task_class == TaskClass.REFACTOR
    assert restored.budget_tokens == 2400
    assert restored.inception_token == " custom inception"


def test_budget_policy_from_dict_coerces_unknown_class_to_default() -> None:
    # an unknown class string falls back to DEFAULT, never raises
    p = BudgetPolicy.from_dict(
        {"task_class": "nope", "budget_tokens": 10}
    )
    assert p.task_class == TaskClass.DEFAULT
    assert p.budget_tokens == 10
