"""Tests for the core primitive — the live think-stream parser.

These cover the surfaces the m2 governor and the m1 profiler both depend on:

* :func:`extract_think_blocks` — offline parse, incl. unterminated blocks;
* :class:`ThinkStreamParser` — incremental feed with split delimiters;
* :func:`count_tokens_approx` / :func:`estimate_tokens_from_bytes` — the
  honest fallback counters the table labels ``approx``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from thinkcap.config import THINK_END, THINK_START
from thinkcap.trace_parser import (
    LiveTraceState,
    ThinkStreamParser,
    count_tokens_approx,
    estimate_tokens_from_bytes,
    extract_think_blocks,
    parse_stream,
)

FIXTURE = Path(__file__).parent / "fixtures" / "think_trace_sample.txt"


# --- extract_think_blocks --------------------------------------------------


def test_extract_single_block_from_fixture() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    blocks = extract_think_blocks(text)
    assert len(blocks) == 1
    # body is everything between the delimiters; no leakage of the tags
    assert THINK_START not in blocks[0]
    assert THINK_END not in blocks[0]
    assert "word-boundary" in blocks[0]


def test_extract_multiple_blocks() -> None:
    text = (
        f"{THINK_START}first block{THINK_END}"
        f"action text between"
        f"{THINK_START}second block{THINK_END}"
    )
    assert extract_think_blocks(text) == ["first block", "second block"]


def test_extract_unterminated_block_yields_tail() -> None:
    # a stream cut mid-think must not be dropped — the profiler needs to
    # count it.
    text = f"{THINK_START}partial reasoning that never closes"
    assert extract_think_blocks(text) == ["partial reasoning that never closes"]


def test_extract_no_think_block_returns_empty() -> None:
    assert extract_think_blocks("just action, no thinking here") == []


# --- ThinkStreamParser (incremental, split delimiters) ---------------------


def test_parser_simple_stream_one_block() -> None:
    chunks = [
        THINK_START,
        "reasoning body",
        THINK_END,
        "action after",
    ]
    events, state = parse_stream(chunks)
    kinds = [k for k, _ in events]
    assert "think" in kinds
    assert "action" in kinds
    assert state.in_think is False
    assert len(state.think_blocks) == 1
    assert state.think_blocks[0] == "reasoning body"
    assert state.bytes_seen == len("reasoning body".encode("utf-8"))


def test_parser_delimiter_split_across_chunks() -> None:
    # The hard case: the opener `|>think` and closer `|>` arrive split
    # across SSE deltas. The parser must not emit them as action text.
    opener = THINK_START  # <|im_start|>think
    closer = THINK_END  # <|im_end|>
    # split the opener in the middle
    mid = len(opener) // 2
    chunks = [
        opener[:mid],
        opener[mid:],
        "the reasoning",
        closer[:3],
        closer[3:],
        " and the action",
    ]
    events, state = parse_stream(chunks)
    # no stray '<' '|' '>' fragments should appear as action/think text
    for kind, text in events:
        if kind == "action":
            assert "<|" not in text
            assert "|>" not in text
        if kind == "think":
            assert "<|" not in text
    assert state.think_blocks == ["the reasoning"]


def test_parser_live_bytes_seen_mid_think() -> None:
    # The governor needs a live counter *before* the block closes.
    p = ThinkStreamParser()
    p.feed(THINK_START)
    p.feed("abc")
    assert p.in_think is True
    assert p.bytes_seen == 3
    p.feed("defg")
    assert p.bytes_seen == 7
    p.feed(THINK_END)
    assert p.in_think is False
    assert p.think_blocks == ["abcdefg"]


def test_parser_action_before_think_emitted() -> None:
    p = ThinkStreamParser()
    events = p.feed("preamble action ")
    events += p.feed(THINK_START + "think body" + THINK_END)
    events += p.flush()
    actions = [t for k, t in events if k == "action"]
    assert any("preamble action" in a for a in actions)


def test_parser_two_blocks_one_stream() -> None:
    chunks = [
        THINK_START,
        "first",
        THINK_END,
        "middle action",
        THINK_START,
        "second",
        THINK_END,
    ]
    _, state = parse_stream(chunks)
    assert state.think_blocks == ["first", "second"]


def test_parser_flush_unterminated_mid_think() -> None:
    p = ThinkStreamParser()
    p.feed(THINK_START)
    p.feed("partial")
    events = p.flush()
    assert ("think", "partial") in events
    assert p.think_blocks == ["partial"]
    assert p.in_think is False


def test_parser_empty_input_no_events() -> None:
    events, state = parse_stream([])
    assert events == []
    assert state.bytes_seen == 0
    assert state.think_blocks == []


def test_state_snapshot_mid_stream() -> None:
    p = ThinkStreamParser()
    p.feed(THINK_START)
    p.feed("xyz")
    snap = p.state
    assert snap.in_think is True
    assert snap.bytes_seen == 3
    assert snap.injected is False


# --- token-approx fallbacks ------------------------------------------------


def test_count_tokens_approx_zero_for_empty() -> None:
    assert count_tokens_approx("") == 0


def test_count_tokens_approx_monotonic() -> None:
    short = count_tokens_approx("hello world")
    long = count_tokens_approx("hello " * 50)
    assert 0 < short < long


def test_count_tokens_approx_cjk_denser_than_latin() -> None:
    # CJK packs ~1.5 chars/token vs ~3.5 for latin, so the same char count
    # yields more tokens in CJK — the heuristic must reflect that.
    latin = count_tokens_approx("a" * 60)
    cjk = count_tokens_approx("字" * 60)
    assert cjk > latin


def test_estimate_tokens_from_bytes_zero() -> None:
    assert estimate_tokens_from_bytes(0) == 0


def test_estimate_tokens_from_bytes_grows() -> None:
    assert estimate_tokens_from_bytes(100) < estimate_tokens_from_bytes(400)


def test_live_trace_state_defaults() -> None:
    s = LiveTraceState()
    assert s.bytes_seen == 0
    assert s.in_think is False
    assert s.budget == 0
    assert s.injected is False
    s.reset(budget=620)
    assert s.budget == 620
    assert s.bytes_seen == 0
