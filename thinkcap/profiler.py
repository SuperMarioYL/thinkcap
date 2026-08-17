"""``thinkcap probe`` — run a task suite, profile the reasoning trace.

The profiler is the whole m1 product. It sends a fixed mini suite against
the user's local llama.cpp server, streams back, parses every
``<|im_start|>think`` block, counts reasoning tokens via the server's
``/tokenize`` endpoint (with an honest char-based fallback the table labels
``approx``), and emits:

* a ``rich`` per-task-class table (task_class | n | p50 | p90 | source);
* ``policy.json`` — the per-class p90 budget the m2 governor will enforce;
* a ``budgets.json`` log of the raw observations (appended on every probe).

Test surface: :func:`probe` accepts a ``mock_responses`` map (task_id ->
pre-canned full response) and a ``tokenize_fn`` so the suite can be profiled
without a live llama.cpp, which is what the CI suite does.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .budget_store import BudgetStore
from .config import (
    DEFAULT_BUDGET_STORE_PATH,
    DEFAULT_MODEL,
    DEFAULT_POLICY_PATH,
    DEFAULT_UPSTREAM,
    Suite,
    Task,
    TaskClass,
)
from .policy import (
    BudgetPolicy,
    ReasoningProfile,
    build_policy,
    save_policy,
)
from .trace_parser import (
    TokenizeFn,
    extract_think_blocks,
    make_tokenize_fn,
)


@dataclass
class TaskResult:
    """One task's measurement: class, tokens, source, raw response."""

    task_id: str
    task_class: TaskClass
    reasoning_tokens: int
    token_source: str
    raw_response: str = ""


@dataclass
class ProbeReport:
    """The full output of a ``probe`` run."""

    suite_name: str
    server: str
    results: List[TaskResult] = field(default_factory=list)
    profiles: List[ReasoningProfile] = field(default_factory=list)
    policy: List[BudgetPolicy] = field(default_factory=list)
    policy_path: Optional[str] = None
    budget_store_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "suite": self.suite_name,
            "server": self.server,
            "results": [
                {
                    "task_id": r.task_id,
                    "task_class": r.task_class.value,
                    "reasoning_tokens": r.reasoning_tokens,
                    "token_source": r.token_source,
                }
                for r in self.results
            ],
            "profiles": [
                {
                    "task_class": p.task_class.value,
                    "n": p.n,
                    "p50": p.p50,
                    "p90": p.p90,
                    "token_source": p.token_source,
                }
                for p in self.profiles
            ],
        }


# --- HTTP collection (real llama.cpp path) ---------------------------------


def collect_response(
    client,
    server: str,
    task: Task,
    model: str = DEFAULT_MODEL,
) -> str:
    """POST a chat completion (streaming) and accumulate ``content`` deltas.

    Returns the full assembled text — including the literal
    ``<|im_start|>think`` / ``<|im_end|>`` delimiters the Qwen3.8 chat
    template emits into the content stream, which is exactly what the parser
    needs. Non-200 / transport errors propagate so the CLI can surface them.
    """
    url = f"{server.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": task.prompt}],
        "stream": True,
        "max_tokens": 2048,
    }
    parts: List[str] = []
    with client.stream("POST", url, json=payload, timeout=180) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                if data == "[DONE]":
                    break
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                parts.append(piece)
    return "".join(parts)


# --- Profiling (pure, testable) --------------------------------------------


def profile_task(
    task: Task,
    response: str,
    tokenize_fn: TokenizeFn,
) -> TaskResult:
    """Parse one captured response into a :class:`TaskResult`."""
    blocks = extract_think_blocks(response)
    think_text = "\n".join(blocks)
    n_tokens, source = tokenize_fn(think_text)
    return TaskResult(
        task_id=task.task_id,
        task_class=task.task_class,
        reasoning_tokens=n_tokens,
        token_source=source,
        raw_response=response,
    )


def aggregate(results: List[TaskResult]) -> List[ReasoningProfile]:
    """Bucket results by task class and compute p50/p90/n per class."""
    by_class: Dict[TaskClass, List[int]] = {}
    source_for: Dict[TaskClass, str] = {}
    for r in results:
        by_class.setdefault(r.task_class, []).append(r.reasoning_tokens)
        # Prefer the API source label if any observation used it; else approx.
        if r.token_source == "tokenize_api" or r.task_class not in source_for:
            source_for[r.task_class] = r.token_source
    profiles: List[ReasoningProfile] = []
    for cls in sorted(by_class, key=lambda c: c.value):
        toks = by_class[cls]
        p = ReasoningProfile(
            task_class=cls,
            observed_tokens=toks,
            token_source=source_for.get(cls, "approx"),
        )
        p.recompute()
        profiles.append(p)
    return profiles


# --- Orchestration ---------------------------------------------------------


def probe(
    server: str,
    suite: Suite,
    *,
    client=None,
    tokenize_fn: Optional[TokenizeFn] = None,
    mock_responses: Optional[Dict[str, str]] = None,
    model: str = DEFAULT_MODEL,
    policy_path: str = str(DEFAULT_POLICY_PATH),
    budget_store_path: str = str(DEFAULT_BUDGET_STORE_PATH),
    write_artifacts: bool = True,
) -> ProbeReport:
    """Run the suite end-to-end and (optionally) write ``policy.json``.

    Parameters
    ----------
    server
        Base URL of the llama.cpp server (e.g. ``http://localhost:8080``).
    suite
        The :class:`~thinkcap.config.Suite` to run (from ``get_suite``).
    client
        An ``httpx.Client`` (or compatible) for the real path. Leave None
        with ``mock_responses`` for the offline / test path.
    tokenize_fn
        Override the token counter (tests inject a stub). Defaults to a
        server-backed counter with the char-based fallback.
    mock_responses
        ``{task_id: full_response}`` map; when present, the HTTP round-trip
        is skipped and the canned response is profiled directly. This is how
        the suite is profiled in CI without a live model.
    """
    if tokenize_fn is None:
        tokenize_fn = make_tokenize_fn(client=client, server=server, prefer_api=True)

    results: List[TaskResult] = []
    for task in suite.tasks:
        if mock_responses is not None and task.task_id in mock_responses:
            response = mock_responses[task.task_id]
        else:
            if client is None:
                raise RuntimeError(
                    f"no client and no mock response for task '{task.task_id}'; "
                    "pass either client= or mock_responses="
                )
            response = collect_response(client, server, task, model=model)
        results.append(profile_task(task, response, tokenize_fn))

    profiles = aggregate(results)
    policy = build_policy(profiles)

    report = ProbeReport(
        suite_name=suite.name,
        server=server,
        results=results,
        profiles=profiles,
        policy=policy,
    )

    if write_artifacts:
        save_policy(policy, policy_path)
        report.policy_path = policy_path
        store = BudgetStore(budget_store_path)
        store.load()
        for r in results:
            store.record(r.task_class, r.reasoning_tokens, outcome="observed")
        store.save()
        report.budget_store_path = budget_store_path

    return report


def render_table(report: ProbeReport):
    """Return a ``rich.table.Table`` for the probe report.

    Kept separate from :func:`probe` so the CLI can render any report (e.g.
    one reloaded from ``budgets.json``) and tests can assert on the row data
    without pulling in a terminal.
    """
    from rich.table import Table  # local import: rich is a hard dep but the
    # table is presentation, not model — keep it out of the import graph of
    # the pure profiling functions.

    table = Table(
        title=f"thinkcap probe — suite '{report.suite_name}' vs {report.server}",
        show_lines=True,
    )
    table.add_column("task_class", style="cyan", no_wrap=True)
    table.add_column("n", justify="right")
    table.add_column("p50", justify="right")
    table.add_column("p90 (budget)", justify="right", style="magenta")
    table.add_column("source")

    if not report.profiles:
        table.add_row("(no profiles)", "-", "-", "-", "-")

    for prof in report.profiles:
        budget = next(
            (p.budget_tokens for p in report.policy if p.task_class == prof.task_class),
            prof.p90,
        )
        table.add_row(
            prof.task_class.value,
            str(prof.n),
            str(prof.p50),
            str(budget),
            prof.token_source,
        )
    return table


def default_server() -> str:
    return DEFAULT_UPSTREAM
