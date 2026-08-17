"""The core primitive: parse a live Qwen3.8 ``<|im_start|>think`` stream.

This module is the new-verb surface the whole governor gates on. It exposes a
mid-turn ``in_think`` / ``bytes_seen`` state that lets the m2 proxy decide
*when* to inject the inception thought — a signal Claude/GPT structurally
cannot give a local proxy because their reasoning traces are not surfaced
mid-stream.

Two entry points:

* :class:`ThinkStreamParser` — incremental. Feed it SSE ``content`` deltas
  one at a time; it emits ``("think", chunk)`` / ``("action", chunk)``
  events and keeps a live ``bytes_seen`` counter even when the closing
  ``<|im_end|>`` has not arrived yet. This is what the proxy wires into the
  stream.
* :func:`extract_think_blocks` — offline. Parse a fully-captured response
  into a list of think-block strings. This is what the profiler uses to
  count reasoning tokens after a probe run.

The delimiter tokens may be split across SSE deltas, so the parser keeps a
tail of ``len(delimiter) - 1`` bytes that could be a partial match and only
emits text once it is certain no delimiter starts inside it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Tuple

from .config import THINK_END, THINK_START

#: A streaming event: ``(kind, text)`` where kind is ``"think"`` or
#: ``"action"`` (non-think content the model emits outside a think block).
TraceEvent = Tuple[str, str]


def count_tokens_approx(text: str) -> int:
    """Honest char-based token estimate used when ``/tokenize`` is unavailable.

    Qwen3.8 uses a BPE tokenizer; the real count needs the server's
    ``/tokenize`` endpoint. When that endpoint is absent (or the special-token
    literal confuses it — the m1 falsifier check), this heuristic is the
    fallback the profiler labels ``reasoning_tokens (approx)``.

    Rule of thumb: ~1 token per 3.5 latin chars, ~1 token per 1.5 CJK chars.
    It is deliberately conservative (slight over-estimate) so a budget set
    from it errs toward *not* under-governing.
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(1, round(cjk / 1.5 + other / 3.5))


def estimate_tokens_from_bytes(n_bytes: int) -> int:
    """Convert a live ``bytes_seen`` counter into an approximate token count.

    The streaming parser only sees bytes (it has not buffered the whole think
    text), so the governor compares an *estimate* against the token-budget.
    UTF-8 averages ~3 bytes/char for CJK and ~1 byte/char for ascii; a
    ~4-bytes-per-token ratio is the safe middle ground.
    """
    if n_bytes <= 0:
        return 0
    return max(1, n_bytes // 4)


def extract_think_blocks(text: str) -> List[str]:
    """Offline parse: return every think-block body in ``text``.

    A think block runs from ``<|im_start|>think`` to the next ``<|im_end|>``.
    An unterminated block (stream cut mid-think) yields its tail as the body,
    so a partially-captured probe response is still profiled rather than
    dropped.
    """
    blocks: List[str] = []
    cursor = 0
    n = len(text)
    while cursor < n:
        start = text.find(THINK_START, cursor)
        if start == -1:
            break
        body_start = start + len(THINK_START)
        end = text.find(THINK_END, body_start)
        if end == -1:
            blocks.append(text[body_start:])
            break
        blocks.append(text[body_start:end])
        cursor = end + len(THINK_END)
    return blocks


@dataclass
class LiveTraceState:
    """The mid-turn state the governor gates on (mvp_plan.md §2).

    ``bytes_seen`` counts UTF-8 bytes of think content streamed so far; the
    governor converts it to a token estimate via
    :func:`estimate_tokens_from_bytes` and compares against ``budget``.
    ``injected`` is sticky: once the inception thought has been injected for
    this turn, the governor will not inject again.
    """

    bytes_seen: int = 0
    in_think: bool = False
    budget: int = 0
    injected: bool = False
    think_blocks: List[str] = field(default_factory=list)

    def reset(self, budget: int = 0) -> None:
        self.bytes_seen = 0
        self.in_think = False
        self.budget = budget
        self.injected = False
        self.think_blocks.clear()


class ThinkStreamParser:
    """Incremental parser for a streamed Qwen3.8 response.

    Feed SSE ``content`` deltas via :meth:`feed`; it returns the list of
    events that fully resolved from that chunk (partial-delimiter tails are
    buffered, not emitted). Live counters (``bytes_seen``, ``in_think``) are
    readable at any point mid-stream — that is exactly what the m2 proxy
    needs to decide whether to inject the inception thought *now*.

    The parser is single-pass and allocation-light; a turn emits O(number of
    deltas) events, not O(text length).
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False
        self._current_think = ""
        # Bytes of think content that have been *confirmed* (locked into
        # ``_current_think`` / a closed ``think_blocks`` entry). The live
        # ``bytes_seen`` counter additionally counts the held tail
        # tentatively (see the property below) so the governor gets a signal
        # even before a split closer resolves — at the cost of a ≤9-byte
        # over-count during the held-tail window, which the byte→token
        # estimate tolerates.
        self._confirmed_think_bytes = 0
        self.think_blocks: List[str] = []

    # -- live state ----------------------------------------------------------

    @property
    def in_think(self) -> bool:
        return self._in_think

    @property
    def bytes_seen(self) -> int:
        """Live think-bytes counter: confirmed + tentatively-held tail.

        While inside a think block, the parser holds up to
        ``len(THINK_END) - 1`` bytes as a possible split closer; those bytes
        are reported here *tentatively* (they will be retracted if they turn
        out to be a delimiter) so the governor can decide to inject *before*
        the block closes. Outside a think block only confirmed bytes count.
        """
        base = self._confirmed_think_bytes
        if self._in_think and self._buf:
            base += len(self._buf.encode("utf-8"))
        return base

    @property
    def state(self) -> LiveTraceState:
        return LiveTraceState(
            bytes_seen=self.bytes_seen,
            in_think=self._in_think,
            budget=0,
            injected=False,
            think_blocks=list(self.think_blocks),
        )

    # -- feeding ------------------------------------------------------------

    def feed(self, chunk: str) -> List[TraceEvent]:
        """Absorb a ``content`` delta; return the events it produced.

        A single chunk may produce zero events (all of it buffered as a
        possible partial delimiter), one, or many (it crosses both a think
        open and close).
        """
        if not chunk:
            return []
        events: List[TraceEvent] = []
        self._buf += chunk
        # Loop: each iteration either emits a confirmed chunk or returns
        # because the remaining buffer is shorter than a delimiter and might
        # be a partial match we must hold for the next feed().
        while True:
            if not self._in_think:
                idx = self._buf.find(THINK_START)
                if idx == -1:
                    # No opener in view. Emit the prefix that cannot be part
                    # of a (split) opener; keep the tail as a candidate.
                    safe = len(self._buf) - (len(THINK_START) - 1)
                    if safe > 0:
                        out = self._buf[:safe]
                        self._buf = self._buf[safe:]
                        if out:
                            events.append(("action", out))
                    return events
                # Opener found: emit preceding action text, enter think.
                pre = self._buf[:idx]
                if pre:
                    events.append(("action", pre))
                self._buf = self._buf[idx + len(THINK_START):]
                self._in_think = True
                # fall through: immediately try to drain think content
            # We are inside a think block: look for the closer.
            idx = self._buf.find(THINK_END)
            if idx == -1:
                # No closer yet. Emit think text but hold a tail that could
                # be a split closer. The held tail still counts toward the
                # live ``bytes_seen`` counter (tentatively, via the
                # property) so the governor has a signal before the block
                # closes; only the *emitted* prefix is locked into
                # ``_confirmed_think_bytes`` here.
                safe = len(self._buf) - (len(THINK_END) - 1)
                if safe > 0:
                    out = self._buf[:safe]
                    self._buf = self._buf[safe:]
                    self._current_think += out
                    self._confirmed_think_bytes += len(out.encode("utf-8"))
                    if out:
                        events.append(("think", out))
                return events
            # Closer found: emit think text up to it, close the block.
            body = self._buf[:idx]
            if body:
                self._current_think += body
                self._confirmed_think_bytes += len(body.encode("utf-8"))
                events.append(("think", body))
            self.think_blocks.append(self._current_think)
            self._current_think = ""
            self._buf = self._buf[idx + len(THINK_END):]
            self._in_think = False
            # loop again: there may be more openers/closers in the remainder

    def flush(self) -> List[TraceEvent]:
        """Drain any held buffer at end-of-stream.

        A clean stream ends with an empty buffer (the last ``<|im_end|>``
        closed everything). If the stream was cut mid-think, the held tail is
        counted as the (unterminated) think body so the profile is not lost;
        if it was cut mid-action-text, it is emitted as action.
        """
        events: List[TraceEvent] = []
        if not self._buf:
            if self._in_think and self._current_think:
                # already accounted in bytes_seen; just record the block
                self.think_blocks.append(self._current_think)
                self._current_think = ""
                self._in_think = False
            return events
        if self._in_think:
            # Held tail resolves as think content (stream cut mid-think);
            # lock it into confirmed so the final think_block body is whole.
            self._current_think += self._buf
            self._confirmed_think_bytes += len(self._buf.encode("utf-8"))
            events.append(("think", self._buf))
            self.think_blocks.append(self._current_think)
            self._current_think = ""
            self._buf = ""
            self._in_think = False
        else:
            events.append(("action", self._buf))
            self._buf = ""
        return events


def parse_stream(
    chunks: List[str],
) -> Tuple[List[TraceEvent], LiveTraceState]:
    """Convenience: feed a list of deltas, return all events + final state."""
    parser = ThinkStreamParser()
    events: List[TraceEvent] = []
    for ch in chunks:
        events.extend(parser.feed(ch))
    events.extend(parser.flush())
    return events, parser.state


TokenizeFn = Callable[[str], Tuple[int, str]]
"""A function ``(text) -> (token_count, source_label)``.

The profiler injects one of these; the real one calls the server's
``/tokenize`` endpoint, the test one is a fixed stub."""


def make_tokenize_fn(
    client=None,
    server: str = "",
    prefer_api: bool = True,
) -> TokenizeFn:
    """Build a tokenize function bound to a server (or pure-fallback).

    With ``prefer_api=True`` and a non-None ``client``, it POSTs to
    ``{server}/tokenize`` and reads ``len(tokens)``. On any error (endpoint
    missing, special tokens confusing the tokenizer — the m1 falsifier), it
    falls back to :func:`count_tokens_approx` and labels the source
    ``"approx"`` so the rich table honestly says so.
    """

    def _tokenize(text: str) -> Tuple[int, str]:
        if prefer_api and client is not None and server:
            try:
                resp = client.post(
                    f"{server.rstrip('/')}/tokenize",
                    json={"content": text},
                    timeout=30,
                )
                resp.raise_for_status()
                payload = resp.json()
                toks = payload.get("tokens") or payload.get("pieces") or []
                if isinstance(toks, list):
                    return len(toks), "tokenize_api"
            except Exception:
                pass
        return count_tokens_approx(text), "approx"

    return _tokenize
