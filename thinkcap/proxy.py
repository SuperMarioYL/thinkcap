"""``thinkcap serve`` — OpenAI-compatible streaming reverse proxy.

m1 ships the profiler; this module is the m2 surface — a FastAPI app that
sits between the coding agent and llama.cpp, parses the live
``<|im_start|>think`` stream, and (once the live token estimate crosses the
class budget) injects the inception thought mid-stream so the model acts
instead of over-thinking.

Scope in v0.1:

* the **passthrough** is real and runnable: ``/v1/chat/completions`` is
  forwarded to the upstream llama.cpp and streamed back unchanged. This is
  the substrate the m2 governor plugs into and it is what ``--dry-run``
  (the v0.1 default) means — observe, do not inject.
* the **inception injection** is a single marked hook
  (:func:`_maybe_inject`) that currently no-ops in dry-run mode. Wiring the
  real injection (write the inception token, flush the rest of the think
  block as suppressed) is the m2 milestone and is gated behind
  ``enforce=False`` until the m3 parity suite passes.

The app is importable and testable without a live upstream via FastAPI's
``TestClient`` against a stubbed transport.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import (
    DEFAULT_PORT,
    DEFAULT_TRACE_PACK,
    DEFAULT_UPSTREAM,
    TaskClass,
    TracePack,
)
from .policy import BudgetPolicy, policy_index, load_policy, should_inject
from .trace_parser import LiveTraceState, ThinkStreamParser

log = logging.getLogger("thinkcap.proxy")

#: Upstream HTTP budgets. A wedged llama.cpp must fail the agent's request
#: instead of hanging it forever — the same failure class the v0.2.0 probe
#: fix removed on the CLI side (cli.probe's bounded connect timeout). The
#: 180s read budget matches the per-request timeout the profiler already
#: accepts for slow prompt eval.
UPSTREAM_TIMEOUT = httpx.Timeout(180.0, connect=10.0)


def classify_request(messages: List[dict]) -> TaskClass:
    """Best-effort task-class guess from the last user message.

    v0.1 deliberately uses a cheap keyword heuristic — the *real* classifier
    is the agent's own task label (Cline/Aider pass it), wired in m2. The
    governor only needs a class to look up the budget; a wrong guess falls
    back to the ``default`` policy row, which is the safe global budget.
    """
    last_user = ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            last_user = str(m.get("content", ""))
            break
    text = last_user.lower()
    if any(k in text for k in ("rename", "重命名", "改名")):
        return TaskClass.RENAME
    if any(k in text for k in ("fix", "bug", "修复", "修正")):
        return TaskClass.BUGFIX
    if any(k in text for k in ("refactor", "extract", "重构", "抽取")):
        return TaskClass.REFACTOR
    if any(k in text for k in ("create", "new file", "新建", "创建")):
        return TaskClass.NEW_FILE
    return TaskClass.DEFAULT


def create_app(
    upstream: str = DEFAULT_UPSTREAM,
    policy_path: Optional[str] = None,
    *,
    enforce: bool = False,
    dry_run: bool = True,
    trace_pack: Optional[TracePack] = None,
):
    """Build the FastAPI app.

    ``enforce=False`` (the v0.1 default, mirrored by ``dry_run=True``) means
    the proxy will *log* "would inject at N tokens" but never mutate the
    stream. Flipping ``enforce=True`` (m3's ``--accept``) is what turns the
    advisory into a governor — and that flip is gated on the parity suite.

    ``trace_pack`` selects the delimiter surface the upstream model emits
    (default: Qwen3.8); the live parser and the policy's inception token
    must match the model behind the upstream server.
    """
    # fastapi/httpx are imported at module level: the endpoint's `Request`
    # annotation must resolve against this module's globals (the module uses
    # `from __future__ import annotations`, so a create_app-local import left
    # FastAPI unable to bind it — every request 422'd in v0.2.0). The pure
    # modules (trace_parser, policy) still import without fastapi; only this
    # module pays the dependency.
    pack = trace_pack or DEFAULT_TRACE_PACK
    policies: List[BudgetPolicy] = []
    if policy_path:
        try:
            policies = load_policy(policy_path)
        except (FileNotFoundError, ValueError, OSError) as exc:
            # ValueError covers json.JSONDecodeError and a malformed
            # budgets shape; programmatic callers (tests, uvicorn
            # factories) still get an app — the CLI fail-fasts earlier,
            # this keeps create_app itself total.
            log.warning(
                "policy %s unusable (%s); running passthrough-only",
                policy_path,
                exc,
            )
    by_class = policy_index(policies)

    app = FastAPI(
        title="thinkcap",
        version="0.3.0",
        description="Per-task reasoning-budget governor for Qwen3.8 coding agents.",
    )
    app.state.upstream = upstream.rstrip("/")
    app.state.by_class = by_class
    app.state.enforce = enforce or (not dry_run)
    app.state.trace_pack = pack

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "upstream": app.state.upstream,
            "enforce": app.state.enforce,
            "trace_pack": app.state.trace_pack.name,
            "classes": [c.value for c in app.state.by_class],
        }

    def _upstream_error(status_code: int, message: str) -> JSONResponse:
        # OpenAI-shaped error body so agent SDKs surface the real failure
        # instead of parsing a truncated SSE stream that started with 200.
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "message": message,
                    "type": "upstream_error",
                    "code": status_code,
                }
            },
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        messages = body.get("messages", [])
        task_class = classify_request(messages)
        policy = app.state.by_class.get(task_class) or app.state.by_class.get(
            TaskClass.DEFAULT
        )

        upstream_url = f"{app.state.upstream}/v1/chat/completions"
        body_for_upstream = dict(body)
        body_for_upstream["stream"] = True

        # Start the upstream request BEFORE returning the response so an
        # upstream failure maps to a real status code; raise_for_status
        # inside the generator ran after the 200 was already committed.
        client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)
        try:
            upstream = await client.send(
                client.build_request("POST", upstream_url, json=body_for_upstream),
                stream=True,
            )
        except httpx.HTTPError as exc:
            await client.aclose()
            return _upstream_error(
                502,
                f"cannot reach llama.cpp upstream at {app.state.upstream}: {exc}",
            )
        if upstream.status_code >= 400:
            detail = (await upstream.aread()).decode("utf-8", "replace")[:500]
            await upstream.aclose()
            await client.aclose()
            return _upstream_error(
                upstream.status_code,
                f"upstream llama.cpp at {app.state.upstream} returned "
                f"{upstream.status_code}: {detail}",
            )

        async def generate() -> AsyncIterator[bytes]:
            try:
                parser = ThinkStreamParser(
                    think_start=app.state.trace_pack.think_start,
                    think_end=app.state.trace_pack.think_end,
                )
                state = LiveTraceState(budget=policy.budget_tokens if policy else 0)
                async for line in upstream.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data == "[DONE]":
                            yield b"data: [DONE]\n\n"
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            yield (line + "\n").encode()
                            continue
                        delta = (
                            (obj.get("choices") or [{}])[0].get("delta") or {}
                        ).get("content") or ""
                        if delta:
                            parser.feed(delta)
                            state.in_think = parser.in_think
                            state.bytes_seen = parser.bytes_seen
                            # m2 hook: decide whether to inject.
                            _maybe_inject(
                                state, policy, app.state.enforce
                            )
                        yield ("data: " + json.dumps(obj) + "\n\n").encode()
                # end of stream — flush any held parser buffer
                for kind, text in parser.flush():
                    # In passthrough/dry-run we do not emit synthesized
                    # content; this is where m2 would emit the inception
                    # token and suppressed think tail.
                    pass
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"X-thinkcap-class": task_class.value},
        )

    return app


def _maybe_inject(
    state: LiveTraceState,
    policy: Optional[BudgetPolicy],
    enforce: bool,
) -> bool:
    """The m2 governor hook. In v0.1 it only *logs*; m2 writes the token.

    Returns True when the gate fired (so a future m2 impl can branch on it).
    """
    if policy is None:
        return False
    if not should_inject(state, policy):
        return False
    # The gate is sticky in BOTH modes: one decision per turn, whether the
    # governor mutates the stream (enforce) or only logs (dry-run) —
    # otherwise the advisory log fires again on every streamed delta after
    # the budget is crossed.
    state.injected = True
    if enforce:
        # m2: emit policy.inception_token into the stream, mark
        # state.injected = True, and flush the remainder of the think block
        # as suppressed. Gated until the m3 parity suite passes.
        log.info(
            "INJECT (enforce) class=%s budget=%d est_tokens~%d",
            policy.task_class.value,
            policy.budget_tokens,
            state.bytes_seen // 4,
        )
        return True
    log.info(
        "would-inject (dry-run) class=%s budget=%d est_tokens~%d — "
        "advisory only, the stream is not mutated",
        policy.task_class.value,
        policy.budget_tokens,
        state.bytes_seen // 4,
    )
    return True


def run(
    upstream: str = DEFAULT_UPSTREAM,
    port: int = DEFAULT_PORT,
    policy_path: Optional[str] = None,
    *,
    enforce: bool = False,
    dry_run: bool = True,
    trace_pack: Optional[TracePack] = None,
) -> None:
    """Start the uvicorn server (called by the ``serve`` CLI command)."""
    import uvicorn

    app = create_app(
        upstream=upstream,
        policy_path=policy_path,
        enforce=enforce,
        dry_run=dry_run,
        trace_pack=trace_pack,
    )
    log.info(
        "thinkcap serve on :%d -> %s (enforce=%s, trace_pack=%s)",
        port,
        upstream,
        app.state.enforce,
        app.state.trace_pack.name,
    )
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
