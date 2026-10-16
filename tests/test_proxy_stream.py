"""The serve proxy's live endpoint and upstream failure contract.

v0.2.0 shipped two defects here that no test covered because nothing ever
POSTed to ``/v1/chat/completions``: the ``Request`` annotation was
unresolvable (module-level ``from __future__ import annotations`` combined
with a create_app-local fastapi import), so every request 422'd, and an
upstream llama.cpp failure surfaced as HTTP 200 followed by a truncated SSE
stream because ``raise_for_status`` ran inside the response generator.
These tests pin the fixed contracts with httpx.MockTransport upstreams.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from thinkcap.config import TaskClass
from thinkcap.policy import BudgetPolicy
from thinkcap.proxy import _maybe_inject, create_app
from thinkcap.trace_parser import LiveTraceState


def _sse_body() -> str:
    # Built explicitly to keep the delimiters byte-identical to the Qwen3.8
    # surface the parser gates on.
    frames = [
        '{"choices":[{"delta":{"content":"<|im_start|>think"}}]}',
        '{"choices":[{"delta":{"content":"rename foo to bar"}}]}',
        '{"choices":[{"delta":{"content":"<|im_end|>"}}]}',
        '{"choices":[{"delta":{"content":"sed done"}}]}',
        "[DONE]",
    ]
    return "".join(f"data: {f}\n\n" for f in frames)


def _handler(status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(
                status, json={"error": {"message": "boom"}}
            )
        return httpx.Response(
            200,
            content=_sse_body().encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    return handler


@pytest.fixture
def mock_upstream(monkeypatch):
    """Point the proxy's httpx.AsyncClient at a MockTransport upstream."""

    def _install(handler) -> None:
        real_client = httpx.AsyncClient

        def factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler))

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    return _install


def test_post_endpoint_streams_upstream_sse(mock_upstream) -> None:
    # fail-before: v0.2.0 returned 422 {"detail":[{"loc":["query","request"],
    # "msg":"Field required"...}]} for every POST — the Request annotation
    # never resolved, so the endpoint was a required-query-param trap.
    mock_upstream(_handler())
    client = TestClient(create_app(policy_path=None))
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "rename foo to bar"}]},
    )
    assert resp.status_code == 200
    assert resp.headers["x-thinkcap-class"] == TaskClass.RENAME.value
    assert resp.headers["content-type"].startswith("text/event-stream")
    # Every upstream frame is re-emitted (re-serialized JSON, so the check is
    # semantic, not byte-verbatim): opener delimiter, think body, closer, and
    # the terminal DONE marker all arrive downstream.
    assert '"<|im_start|>think"' in resp.text
    assert "rename foo to bar" in resp.text
    assert '"<|im_end|>"' in resp.text
    assert "data: [DONE]" in resp.text


def test_health_still_reports_config(mock_upstream) -> None:
    mock_upstream(_handler())
    body = TestClient(create_app(policy_path=None)).get("/health").json()
    assert body["status"] == "ok"
    assert body["enforce"] is False
    assert body["trace_pack"] == "qwen3.8"


def test_upstream_5xx_surfaces_real_status_not_200(mock_upstream) -> None:
    # fail-before: raise_for_status ran inside the generator after FastAPI
    # committed the 200 StreamingResponse, so the agent got HTTP 200 and a
    # truncated stream instead of an error.
    mock_upstream(_handler(status=500))
    client = TestClient(create_app(policy_path=None))
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "rename foo to bar"}]},
    )
    assert resp.status_code == 500
    body = resp.json()
    assert "error" in body
    assert "500" in body["error"]["message"]


def test_unreachable_upstream_is_502(monkeypatch) -> None:
    real_client = httpx.AsyncClient

    def refusing_client(**kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("[Errno 61] Connection refused", request=request)

        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", refusing_client)
    client = TestClient(create_app(policy_path=None))
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "rename foo to bar"}]},
    )
    assert resp.status_code == 502
    body = resp.json()
    assert "error" in body
    assert "Connection refused" in body["error"]["message"]


# --- the governor decision is once per turn in BOTH modes ------------------


def test_maybe_inject_dry_run_is_sticky(caplog) -> None:
    # fail-before: state.injected was only set in the enforce branch, so the
    # dry-run "would-inject" log fired again on every delta over budget.
    policy = BudgetPolicy(task_class=TaskClass.RENAME, budget_tokens=10)
    state = LiveTraceState(bytes_seen=1000, in_think=True, budget=10)
    with caplog.at_level("INFO", logger="thinkcap.proxy"):
        assert _maybe_inject(state, policy, enforce=False) is True
        assert state.injected is True
        assert _maybe_inject(state, policy, enforce=False) is False
        assert state.injected is True
    would = [r for r in caplog.records if "would-inject" in r.getMessage()]
    assert len(would) == 1
    # The stale v0.1 text ("governor ships in m2") is gone.
    assert all("governor ships in m2" not in r.getMessage() for r in caplog.records)
