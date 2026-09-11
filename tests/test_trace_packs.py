"""Trace-pack tests — the same primitive across delimiter surfaces.

The v0.2 roadmap item: DeepSeek-R1 / GLM-4.6-Z1 trace packs (different
delimiters, same primitive). The parser, profiler, and proxy must handle
``<think>`` ... ``</think>`` streams exactly as well as Qwen3.8's
``<|im_start|>think`` ... ``<|im_end|>``, and the learned policy's
inception token must close the right block.
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from thinkcap.config import (
    DEFAULT_INCEPTION_TOKEN,
    DEFAULT_TRACE_PACK,
    THINK_END,
    THINK_START,
    TRACE_PACKS,
    TaskClass,
    get_suite,
    get_trace_pack,
)
from thinkcap.cli import app
from thinkcap.profiler import probe
from thinkcap.trace_parser import (
    ThinkStreamParser,
    extract_think_blocks,
    parse_stream,
)

runner = CliRunner()


# --- registry ---------------------------------------------------------------


def test_registry_has_three_packs_with_qwen_default() -> None:
    assert sorted(TRACE_PACKS) == ["deepseek-r1", "glm-4.6-z1", "qwen3.8"]
    assert DEFAULT_TRACE_PACK.name == "qwen3.8"
    # The default pack must be byte-identical to the v0.1.0 surface.
    assert DEFAULT_TRACE_PACK.think_start == THINK_START
    assert DEFAULT_TRACE_PACK.think_end == THINK_END
    assert DEFAULT_TRACE_PACK.inception_token == DEFAULT_INCEPTION_TOKEN


def test_get_trace_pack_unknown_name_raises() -> None:
    import pytest

    with pytest.raises(KeyError):
        get_trace_pack("nope")


def test_non_qwen_packs_use_angle_bracket_delimiters() -> None:
    for name in ("deepseek-r1", "glm-4.6-z1"):
        pack = TRACE_PACKS[name]
        assert pack.think_start == "<think>"
        assert pack.think_end == "</think>"
        assert pack.inception_token.endswith("</think>")


# --- offline + streaming parse with pack delimiters -------------------------


def test_extract_deepseek_blocks_default_parser_misses() -> None:
    text = "<think>\nthe reasoning\n</think>the action"
    # The default (Qwen3.8) parser sees no blocks in this text.
    assert extract_think_blocks(text) == []
    pack = TRACE_PACKS["deepseek-r1"]
    assert extract_think_blocks(
        text, think_start=pack.think_start, think_end=pack.think_end
    ) == ["\nthe reasoning\n"]


def test_extract_deepseek_unterminated_tail() -> None:
    text = "<think>partial reasoning that never closes"
    pack = TRACE_PACKS["glm-4.6-z1"]
    assert extract_think_blocks(
        text, think_start=pack.think_start, think_end=pack.think_end
    ) == ["partial reasoning that never closes"]


def test_stream_parser_split_closer_across_chunks() -> None:
    # The hard case the Qwen parser handles: the closer `</think>` arrives
    # split across SSE deltas and must never leak into think/action text.
    pack = TRACE_PACKS["deepseek-r1"]
    chunks = [
        "<th",
        "ink>the reaso",
        "ning</thi",
        "nk> and the action",
    ]
    events, state = parse_stream(
        chunks, think_start=pack.think_start, think_end=pack.think_end
    )
    for kind, text in events:
        assert "<" not in text and "/th" not in text
    assert state.think_blocks == ["the reasoning"]
    assert state.in_think is False


def test_stream_parser_live_bytes_seen_mid_think() -> None:
    pack = TRACE_PACKS["deepseek-r1"]
    p = ThinkStreamParser(think_start=pack.think_start, think_end=pack.think_end)
    p.feed("<think>")
    p.feed("abc")
    assert p.in_think is True
    assert p.bytes_seen == 3
    p.feed("</think>")
    assert p.think_blocks == ["abc"]
    assert p.in_think is False


# --- profiler + policy with a pack ------------------------------------------


def _deepseek_resp(body: str) -> str:
    return f"<think>\n{body}\n</think>ACTION"


def test_probe_with_deepseek_pack_profiles_and_closes_think() -> None:
    suite = get_suite("coding-mini")
    mock = {
        "rename-01": _deepseek_resp("x " * 100),
        "rename-02": _deepseek_resp("x " * 100),
        "bugfix-01": _deepseek_resp("y " * 10),
        "refactor-01": _deepseek_resp("y " * 10),
        "newfile-01": _deepseek_resp("y " * 10),
    }
    policy_path = "unused"  # not written: write_artifacts=False
    report = probe(
        "http://mock",
        suite,
        mock_responses=mock,
        tokenize_fn=lambda t: (len(t.split()), "stub"),
        policy_path=policy_path,
        budget_store_path="unused",
        write_artifacts=False,
        trace_pack=TRACE_PACKS["deepseek-r1"],
    )
    rename = next(p for p in report.profiles if p.task_class == TaskClass.RENAME)
    assert rename.observed_tokens == [100, 100]
    # The policy inception token must close a </think> block.
    assert all(
        p.inception_token.endswith("</think>") for p in report.policy
    )


def test_probe_pack_distributions_do_not_mix(tmp_path) -> None:
    # A budgets.json reused across models keeps one distribution per pack.
    suite = get_suite("coding-mini")
    qwen_mock = {
        "rename-01": f"{THINK_START}{'x ' * 100}{THINK_END}A",
        "rename-02": f"{THINK_START}{'x ' * 100}{THINK_END}A",
        "bugfix-01": f"{THINK_START}{'y ' * 10}{THINK_END}A",
        "refactor-01": f"{THINK_START}{'y ' * 10}{THINK_END}A",
        "newfile-01": f"{THINK_START}{'y ' * 10}{THINK_END}A",
    }
    ds_mock = {
        "rename-01": _deepseek_resp("x " * 5),
        "rename-02": _deepseek_resp("x " * 5),
        "bugfix-01": _deepseek_resp("y " * 2),
        "refactor-01": _deepseek_resp("y " * 2),
        "newfile-01": _deepseek_resp("y " * 2),
    }
    common = dict(
        tokenize_fn=lambda t: (len(t.split()), "stub"),
        policy_path=str(tmp_path / "policy.json"),
        budget_store_path=str(tmp_path / "budgets.json"),
    )
    probe("http://mock", suite, mock_responses=qwen_mock, **common)
    probe(
        "http://mock",
        suite,
        mock_responses=ds_mock,
        trace_pack=TRACE_PACKS["deepseek-r1"],
        **common,
    )

    store = json.loads((tmp_path / "budgets.json").read_text(encoding="utf-8"))
    packs = {r["trace_pack"] for r in store["records"]}
    assert packs == {"qwen3.8", "deepseek-r1"}

    policy = json.loads((tmp_path / "policy.json").read_text(encoding="utf-8"))
    rename = next(b for b in policy["budgets"] if b["task_class"] == "rename")
    # Only the deepseek-r1 observations (5 words each) feed this policy.
    assert rename["budget_tokens"] == 5


# --- CLI surface ------------------------------------------------------------


def test_cli_probe_deepseek_profile_mock_end_to_end(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "probe",
            "--mock",
            "--profile",
            "deepseek-r1",
            "--policy",
            str(tmp_path / "policy.json"),
            "--budgets",
            str(tmp_path / "budgets.json"),
        ],
    )
    # --mock canned responses are Qwen3.8-shaped, so the deepseek pack
    # profiles them as zero-think responses — the pipeline still runs,
    # writes artifacts, and exits 0.
    assert result.exit_code == 0
    policy = json.loads((tmp_path / "policy.json").read_text(encoding="utf-8"))
    assert all(
        b["inception_token"].endswith("</think>") for b in policy["budgets"]
    )


def test_cli_probe_unknown_profile_is_clean_error() -> None:
    result = runner.invoke(app, ["probe", "--mock", "--profile", "nope"])
    assert result.exit_code == 2
    assert "unknown trace pack" in result.output
    assert "Traceback" not in result.output


def test_create_app_reports_trace_pack_in_health(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from thinkcap.proxy import create_app

    good = tmp_path / "policy.json"
    good.write_text(
        '{"version": "0.1.0", "budgets": '
        '[{"task_class": "default", "budget_tokens": 100}]}',
        encoding="utf-8",
    )
    app = create_app(
        policy_path=str(good),
        trace_pack=TRACE_PACKS["glm-4.6-z1"],
    )
    body = TestClient(app).get("/health").json()
    assert body["trace_pack"] == "glm-4.6-z1"
    assert body["classes"] == ["default"]


def test_cli_serve_accepts_profile_option(tmp_path, monkeypatch) -> None:
    import thinkcap.proxy as proxy_mod

    calls = {}
    monkeypatch.setattr(proxy_mod, "run", lambda **kw: calls.update(kw))
    result = runner.invoke(
        app,
        [
            "serve",
            "--profile",
            "deepseek-r1",
            "--policy",
            str(tmp_path / "missing.json"),
            "--port",
            "9921",
        ],
    )
    assert result.exit_code == 0
    assert calls["trace_pack"].name == "deepseek-r1"
