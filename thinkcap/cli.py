"""``thinkcap`` CLI — the entry point installed by ``pipx install thinkcap``.

Subcommands:

* ``probe`` (m1) — run a task suite against the local llama.cpp, print the
  per-task-class reasoning-token table, write ``policy.json``.
* ``serve`` (m2) — start the OpenAI-compatible streaming proxy. v0.1 ships
  this as a passthrough / dry-run surface; the inception governor is gated
  on m3's parity suite.
* ``version`` — print the installed version.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .config import (
    DEFAULT_BUDGET_STORE_PATH,
    DEFAULT_MODEL,
    DEFAULT_POLICY_PATH,
    DEFAULT_PORT,
    DEFAULT_UPSTREAM,
    VERSION,
    available_suite_names,
    get_suite,
)

app = typer.Typer(
    name="thinkcap",
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Per-task reasoning-budget governor for Qwen3.8 coding agents. "
        "Probe the think trace, govern the budget mid-stream."
    ),
)
console = Console()


@app.command()
def version() -> None:
    """Print the installed thinkcap version."""
    console.print(f"thinkcap {VERSION}")


@app.command()
def probe(
    server: str = typer.Option(
        DEFAULT_UPSTREAM,
        "--server",
        envvar="THINKCAP_SERVER",
        help="Base URL of your llama.cpp server (default http://localhost:8080).",
    ),
    suite: str = typer.Option(
        "coding-mini",
        "--suite",
        help=f"Task suite to run. Available: {', '.join(available_suite_names())}.",
    ),
    model: str = typer.Option(
        DEFAULT_MODEL,
        "--model",
        help="Model name reported to the server (informational for llama.cpp).",
    ),
    policy_out: Path = typer.Option(
        DEFAULT_POLICY_PATH,
        "--policy",
        help="Where to write the learned policy.json.",
    ),
    budgets_out: Path = typer.Option(
        DEFAULT_BUDGET_STORE_PATH,
        "--budgets",
        help="Where to append the raw observations (budgets.json).",
    ),
    mock: bool = typer.Option(
        False,
        "--mock",
        help="Run against the built-in canned responses (no server needed). "
        "Used by the demo and CI.",
    ),
) -> None:
    """Run a task suite and profile per-class reasoning tokens."""
    from .profiler import probe as run_probe, render_table

    try:
        suite_obj = get_suite(suite)
    except KeyError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2)

    mock_responses = None
    client = None
    if mock:
        mock_responses = _builtin_mock_responses()
        console.print(
            f"[dim]mock mode: profiling {len(mock_responses)} canned "
            f"responses (no live server)[/dim]"
        )
    else:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            console.print(f"[red]error:[/red] httpx not installed: {exc}")
            raise typer.Exit(code=2)
        client = httpx.Client(timeout=None)
        # quick falsifier check: is /tokenize reachable?
        _check_tokenize(client, server)

    report = run_probe(
        server=server,
        suite=suite_obj,
        client=client,
        mock_responses=mock_responses,
        model=model,
        policy_path=str(policy_out),
        budget_store_path=str(budgets_out),
    )

    console.print(render_table(report))
    console.print(
        f"\n[green]wrote[/green] {policy_out} "
        f"({len(report.policy)} class budgets) "
        f"[dim]and appended observations to {budgets_out}[/dim]"
    )


@app.command()
def serve(
    port: int = typer.Option(
        DEFAULT_PORT, "--port", help="Port to serve the OpenAI-compatible proxy on."
    ),
    upstream: str = typer.Option(
        DEFAULT_UPSTREAM,
        "--upstream",
        envvar="THINKCAP_UPSTREAM",
        help="Upstream llama.cpp server URL.",
    ),
    policy: Optional[Path] = typer.Option(
        DEFAULT_POLICY_PATH,
        "--policy",
        help="policy.json to enforce (from `thinkcap probe`).",
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--accept",
        help="v0.1 default: log 'would inject' but never mutate the stream. "
        "--accept flips to enforcing (gated on the m3 parity suite).",
    ),
) -> None:
    """Start the OpenAI-compatible streaming proxy (m2 surface).

    v0.1 ships this as a passthrough / dry-run. The inception governor
    (mid-stream injection) is wired in m2 and enforcing is gated on m3's
    pass-rate-parity suite — do not ship a governor that breaks agents.
    """
    from . import proxy as proxy_mod

    if not Path(policy).exists():
        console.print(
            f"[yellow]warn:[/yellow] policy {policy} not found; running "
            f"passthrough-only (run `thinkcap probe` first)."
        )
        policy = None  # type: ignore[assignment]

    proxy_mod.run(
        upstream=upstream,
        port=port,
        policy_path=str(policy) if policy else None,
        dry_run=dry_run,
        enforce=not dry_run,
    )


def _check_tokenize(client, server: str) -> None:
    """m1 falsifier check: is the server's /tokenize reachable?

    If it isn't, the profiler silently falls back to the char-based
    approximation and labels the column ``reasoning_tokens (approx)``. We
    warn once up front so the user knows the column is heuristic.
    """
    try:
        resp = client.post(
            f"{server.rstrip('/')}/tokenize",
            json={"content": "<|im_start|>think hello"},
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"status {resp.status_code}")
        body = resp.json()
        if not isinstance(body.get("tokens"), list):
            raise RuntimeError("no 'tokens' list")
        console.print(
            f"[dim]/tokenize reachable ({len(body['tokens'])} tokens for "
            f"sample) — column will be 'reasoning_tokens'.[/dim]"
        )
    except Exception:
        console.print(
            "[yellow]warn:[/yellow] /tokenize not usable on this server; "
            "reasoning tokens will be a char-based estimate "
            "(column labeled 'approx')."
        )


def _builtin_mock_responses() -> dict:
    """Canned full responses for ``--mock`` and the CI suite.

    Each maps a coding-mini task_id to a realistic streamed Qwen3.8 response
    (think block + action diff), with reasoning-token counts that span the
    expected range: rename ~620, bugfix ~1800, refactor ~2400, new_file
    ~1200, rename-02 ~580. The fixture file holds a verbatim copy the tests
    parse against.
    """
    from .config import THINK_END, THINK_START

    def think(reasoning: str, action: str) -> str:
        return f"{THINK_START}\n{reasoning}\n{THINK_END}{action}"

    return {
        "rename-01": think(
            "The user wants to rename `foo` to `bar` in src/utils.py only. "
            "Scope: single file, single identifier. No other references. "
            "I will use a word-boundary sed to avoid partial matches. "
            "No tests need updating since foo is local. This is a one-line "
            "edit; I should not over-deliberate." * 1,
            "```bash\nsed -i 's/\\bfoo\\b/bar/g' src/utils.py\n```",
        ),
        "bugfix-01": think(
            "The loop bound is `range(len(items) + 1)` which iterates one past "
            "the end and indexes items[len(items)] — an IndexError on the last "
            "pass. The fix is `range(len(items))`. I need to verify no caller "
            "depends on the extra iteration (an off-by-one that silently "
            "worked because the body guards against None). Reading the body: "
            "it accesses items[i] unconditionally, so the +1 is a genuine bug. "
            "Apply the bound correction and leave the rest of the function "
            "untouched." * 2,
            "```python\nfor i in range(len(items)):\n    process(items[i])\n```",
        ),
        "refactor-01": think(
            "handle_request inlines seven validation checks before the real "
            "work. Extracting them into validate(req) -> list[str] keeps the "
            "early-return semantics if I return the list of errors and the "
            "caller branches on emptiness. Signature: validate(req: Request) "
            " -> list[str]. Move the body verbatim, replace `return None` with "
            "appending to errors. Keep the order identical so error messages "
            "don't reorder. Add a docstring. The caller becomes: errs = "
            "validate(req); if errs: return 400, errs. No behavior change." * 2,
            "```python\ndef validate(req: Request) -> list[str]:\n    errs = []\n    ...\n    return errs\n```",
        ),
        "newfile-01": think(
            "New config.py exporting a Settings dataclass with host:str and "
            "port:int. Use dataclasses.dataclass, frozen=False (callers may "
            "override). Provide a default instance `settings = Settings()`. "
            "No pydantic dep — stdlib only. Add a from_env classmethod for "
            "completeness? The user asked for two fields and a default; do not "
            "over-build. Ship the minimal dataclass + default instance.",
            "```python\nfrom dataclasses import dataclass\n\n@dataclass\nclass Settings:\n    host: str = '0.0.0.0'\n    port: int = 8088\n\nsettings = Settings()\n```",
        ),
        "rename-02": think(
            "Rename do_thing -> process_request across handlers.py and "
            "tests/test_handlers.py. Two files. Check for dynamic dispatch "
            "on the name (getattr): none in either file. Word-boundary "
            "replace in both. No public API change since do_thing was "
            "private. Run the test file after to confirm.",
            "```bash\nsed -i 's/\\bdo_thing\\b/process_request/g' handlers.py tests/test_handlers.py\n```",
        ),
    }


@app.command()
def suites() -> None:
    """List the available probe suites."""
    for name in available_suite_names():
        s = get_suite(name)
        console.print(f"[cyan]{name}[/cyan] — {len(s.tasks)} tasks")


@app.callback()
def main() -> None:
    """thinkcap — per-task reasoning-budget governor for Qwen3.8."""
    # no-op callback; subcommands carry the logic.
    return


if __name__ == "__main__":  # pragma: no cover
    app()
