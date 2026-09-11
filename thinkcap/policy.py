"""Per-task-class reasoning-budget policy + the governor's decision.

The new primitive (mvp_plan.md §2) is a *per-task-class* budget learned from
observed trace profiles and enforced by mid-stream inception injection. This
module owns the data model half:

* :class:`ReasoningProfile` — what the profiler learned (observed token
  counts + p50/p90).
* :class:`BudgetPolicy` — one row of the shipped ``policy.json`` (a budget
  in tokens + the inception token to inject).
* :func:`should_inject` — the governor's gate, run on a live
  :class:`~thinkcap.trace_parser.LiveTraceState`: over budget, inside a think
  block, and not already injected this turn.

The injection *mechanism* (writing the inception token into the SSE stream)
ships in m2's proxy; this module is the pure, testable decision logic m1's
profiler already emits a policy for.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .config import (
    DEFAULT_FALLBACK_BUDGET,
    DEFAULT_INCEPTION_TOKEN,
    DEFAULT_POLICY_PATH,
    TaskClass,
)
from .trace_parser import LiveTraceState, estimate_tokens_from_bytes


@dataclass
class ReasoningProfile:
    """Aggregated observations for one task class."""

    task_class: TaskClass
    observed_tokens: List[int] = field(default_factory=list)
    p50: int = 0
    p90: int = 0
    n: int = 0
    token_source: str = "approx"

    def recompute(self) -> None:
        self.n = len(self.observed_tokens)
        self.p50 = percentile(self.observed_tokens, 50)
        self.p90 = percentile(self.observed_tokens, 90)


@dataclass
class BudgetPolicy:
    """One row of the shipped policy: how many reasoning tokens a class gets."""

    task_class: TaskClass
    budget_tokens: int
    source: str = "p90_observed"
    inception_token: str = DEFAULT_INCEPTION_TOKEN

    def to_dict(self) -> dict:
        d = asdict(self)
        d["task_class"] = self.task_class.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BudgetPolicy":
        return cls(
            task_class=TaskClass.coerce(d["task_class"]),
            budget_tokens=int(d["budget_tokens"]),
            source=str(d.get("source", "p90_observed")),
            inception_token=str(d.get("inception_token", DEFAULT_INCEPTION_TOKEN)),
        )


def percentile(values: Iterable[int], p: float) -> int:
    """Linear-interpolation percentile (numpy-style) over a small sample.

    For the suite sizes m1 ships (1–3 observations per class), this is a
    conservative estimator: p90 never exceeds the max, never below the min,
    and a single observation is reported as-is.
    """
    vals = sorted(v for v in values if v >= 0)
    if not vals:
        return 0
    if len(vals) == 1:
        return int(vals[0])
    rank = (p / 100.0) * (len(vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(vals) - 1)
    frac = rank - lo
    return int(round(vals[lo] + (vals[hi] - vals[lo]) * frac))


def build_policy(
    profiles: Iterable[ReasoningProfile],
    source: str = "p90_observed",
    inception_token: Optional[str] = None,
) -> List[BudgetPolicy]:
    """Turn observed profiles into a shippable per-class budget list.

    The budget for a class is its p90 (the tail of "how much it actually
    needed"); a class with no profile falls back to
    :data:`~thinkcap.config.DEFAULT_FALLBACK_BUDGET`. A ``default`` row is
    always present so the proxy has a class to assign to tasks it cannot
    classify.
    """
    inception = inception_token or DEFAULT_INCEPTION_TOKEN
    policies: List[BudgetPolicy] = []
    seen_default = False
    for prof in profiles:
        budget = prof.p90 if prof.p90 else max(1, prof.p50)
        policies.append(
            BudgetPolicy(
                task_class=prof.task_class,
                budget_tokens=int(budget),
                source=source,
                inception_token=inception,
            )
        )
        if prof.task_class == TaskClass.DEFAULT:
            seen_default = True
    if not seen_default:
        policies.append(
            BudgetPolicy(
                task_class=TaskClass.DEFAULT,
                budget_tokens=DEFAULT_FALLBACK_BUDGET,
                source="manual",
                inception_token=inception,
            )
        )
    return policies


def save_policy(
    policies: List[BudgetPolicy],
    path: Path | str = DEFAULT_POLICY_PATH,
) -> Path:
    """Write the policy as ``policy.json`` (the file the m2 proxy loads)."""
    out = Path(path)
    data = {
        "version": "0.2.0",
        "budgets": [p.to_dict() for p in policies],
    }
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def load_policy(path: Path | str = DEFAULT_POLICY_PATH) -> List[BudgetPolicy]:
    """Load a ``policy.json`` written by :func:`save_policy`."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"policy file not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    rows = data.get("budgets") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"policy {p} has no 'budgets' list")
    return [BudgetPolicy.from_dict(row) for row in rows]


def policy_index(policies: Iterable[BudgetPolicy]) -> Dict[TaskClass, BudgetPolicy]:
    """Index a policy list by task class for O(1) lookup in the proxy hot path."""
    return {p.task_class: p for p in policies}


def should_inject(state: LiveTraceState, policy: BudgetPolicy) -> bool:
    """The governor's gate. True when the live trace is over budget mid-think.

    Three conditions, all required:

    * the parser is currently inside a think block (``in_think``) — there is
      no point injecting into action text;
    * the inception thought has not already been injected this turn
      (``injected`` is sticky);
    * the live token estimate (from ``bytes_seen``) has crossed the class
      budget.
    """
    if state.injected or not state.in_think:
        return False
    if policy.budget_tokens <= 0:
        return False
    est = estimate_tokens_from_bytes(state.bytes_seen)
    return est >= policy.budget_tokens
