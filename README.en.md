**English** | [简体中文](./README.md)

<div align="right"><sub><b>EN</b>&nbsp;&nbsp;⇄&nbsp;&nbsp;<a href="./README.md">中文</a></sub></div>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="./assets/hero-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="./assets/hero-light.svg">
  <img src="./assets/hero-light.svg" width="880" alt="thinkcap — per-task reasoning-budget governor for Qwen3.8 coding agents">
</picture>

<p align="center"><sub>The per-task reasoning-budget governor that stops Qwen3.8 coding agents overthinking.</sub></p>

<p align="center">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://github.com/SuperMarioYL/thinkcap/releases"><img src="https://img.shields.io/github/v/release/SuperMarioYL/thinkcap" alt="Latest release"></a>
  <a href="https://github.com/SuperMarioYL/thinkcap/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/SuperMarioYL/thinkcap/ci.yml?label=ci" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <a href="#share-this"><img src="https://img.shields.io/badge/anchor-Agent-purple.svg" alt="anchor: Agent"></a>
  <a href="#share-this"><img src="https://img.shields.io/badge/anchor-Coding%20Agent-5E5CE6.svg" alt="anchor: Coding Agent"></a>
</p>

**thinkcap stops a Qwen3.8 coding agent burning ~3,000 reasoning tokens on a one-line edit: it profiles the think trace per task class, learns a budget, and injects the inception thought mid-stream when a live trace crosses it — same diff, ~1/6 the reasoning.**

<h2><img src="https://api.iconify.design/tabler:topology-star-3.svg?color=%230071E3&width=24" height="22" align="absmiddle" alt=""> Architecture</h2>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="./assets/atlas-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="./assets/atlas-light.svg">
  <img src="./assets/atlas-light.svg" width="880" alt="Architecture: Coding Agent → thinkcap serve (:8088) → llama.cpp (:8080), response parsed mid-stream and inception injected over budget">
</picture>

## Contents

- [Why this exists](#why-this-exists)
- [Install & Quickstart](#install--quickstart)
- [Usage](#usage)
- [Demo](#demo)
- [Configuration](#configuration)
- [vs ponytail](#vs-ponytail)
- [Roadmap](#roadmap)
- [License](#license)
- [Share this](#share-this)

## Why this exists

Qwen3.8-27B run locally via llama.cpp behind a coding agent defaults to overthinking: it lights ~3,000 reasoning tokens on a trivial rename. The only lever today is llama.cpp's static `--reasoning-budget` — a global number you guess once and leave, blind to which task is in front of the model and to the fact that the model is mid-overthink *this turn*. thinkcap's new verb is **per-task think-trace profiling → mid-trace inception governance**: measure how many reasoning tokens Qwen3.8 spends per task class, learn a budget, and when a live trace exceeds it inject the inception pattern to force the model to act.

Why now, not 24 months ago. Three things moved in the last ~6 months: (1) Qwen3.8-27B shipped with a per-model overthinking default that [Simon Willison](https://simonwillison.net/2026/Aug/16/qwen-38-27b/) publicly flagged *this month* — before this the "Qwen overthinks" signal was not model-pinned; (2) [ggerganov shipped the inception pattern + `--reasoning-budget`](https://x.com/ggerganov/status/2089214161884414147) in llama.cpp — the mid-trace truncation mechanism did not exist, you could only cap *after* a turn; (3) a Qwen3.6 Jacobian lens was shown to survive the 3.6→3.8 bump, so the `<|im_start|>think` structure is a stable surface to build a governor on. This is the Qwen3.8-specific primitive a Claude/GPT coding agent structurally cannot reproduce: their reasoning traces are not surfaced mid-stream to a local proxy, so there is no `LiveTraceState` to gate on. It is the closest thing to [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) — the 104k★ "make the agent think less" demand signal — built for the runtime layer where the waste actually happens, not the prompt layer above it.

## Install & Quickstart

```bash
pipx install thinkcap                      # or pip install -e .
thinkcap probe --server http://localhost:8080 --suite coding-mini
thinkcap serve --policy policy.json --dry-run
```

Pre-req: you already run a local llama.cpp + Qwen3.8-27B GGUF on `:8080` with `--reasoning-budget 0` (uncapped). `probe` sends 5 canned tasks, parses every `<|im_start|>think` block from the streamed response, counts reasoning tokens via the server's `/tokenize` endpoint (falling back to an honest char-based estimate labeled `approx` when the endpoint is absent — the m1 falsifier check), and prints a per-task-class rich table + writes `policy.json`. `serve` stands up an OpenAI-compatible streaming reverse proxy on `:8088`; repoint your coding agent's `base_url` at it.

<details><summary>No server? Run the CI / demo path</summary>

```bash
thinkcap probe --mock --policy ./policy.json --budgets ./budgets.json
```
`--mock` runs the full pipeline against built-in canned responses — no live llama.cpp needed. The README demo gif is rendered this way. The real before/after (3,100 → ~620 tokens) needs your own server.
</details>

## Usage

<h3><img src="https://api.iconify.design/tabler/terminal-2.svg?color=%230071E3&width=20" height="18" align="absmiddle" alt=""> Five common workflows</h3>

```bash
# 1. Baseline: run coding-mini against your llama.cpp, get policy.json
thinkcap probe --server http://localhost:8080 --policy ./policy.json

# 2. Dry-run the governor (v0.1 default = advisory): logs "would inject at N tokens", never mutates the stream
thinkcap serve --policy ./policy.json --dry-run --port 8088

# 3. Repoint your coding agent (Cline / Aider / continue.dev all speak OpenAI)
#    base_url = http://127.0.0.1:8088/v1

# 4. Inspect the observation log (raw per-class token distribution)
cat budgets.json

# 5. Health / state (upstream, live classes, enforce?)
curl -s http://127.0.0.1:8088/health
```

Three subcommands: `probe` (measure + write policy), `serve` (OpenAI-compatible reverse proxy — advisory/dry-run in v0.1), `suites` (list task suites). Full flags via `thinkcap probe --help`.

## Demo

![demo](assets/demo.gif)

Asciinema recording in `demos/thinkcap-demo.cast` (`thinkcap probe --mock` → rich table → `policy.json` → `thinkcap serve --dry-run` + `/health`). The vhs tape is `docs/demo.tape`; `.github/workflows/demo.yml` re-renders the gif on tag.

The v0.1 demo uses `--mock` (canned responses, no server), so the token numbers are a scaled mechanism demo, not real Qwen3.8 output. The real before/after (3,100 → ~620 reasoning tokens on a one-line rename, same diff) needs your own llama.cpp + Qwen3.8 running `thinkcap probe` (no `--mock`), then `thinkcap serve --accept` — `--accept` stays off-by-default until m3's pass-rate parity suite passes. We do not ship a governor that breaks coding agents.

## Configuration

| Key | Type | Default | Meaning |
|---|---|---|---|
| `--server` | URL | `http://localhost:8080` | Your llama.cpp server (`probe`) |
| `--suite` | str | `coding-mini` | Task suite (5 tasks across rename/bugfix/refactor/new_file/default) |
| `--policy` | path | `policy.json` | Per-class budget `probe` writes / `serve` reads |
| `--budgets` | path | `budgets.json` | Raw observation append log (re-probing a class widens its distribution) |
| `--port` | int | `8088` | `serve` listen port |
| `--upstream` | URL | `http://localhost:8080` | Upstream llama.cpp for `serve` |
| `--dry-run/--accept` | bool | `--dry-run` | advisory (log would-inject) vs enforcing (inject after m3 parity) |

`policy.json` shape:

```json
{
  "version": "0.1.0",
  "budgets": [
    { "task_class": "rename",   "budget_tokens": 620,  "source": "p90_observed", "inception_token": " ...Acting now.\n<|im_end|>" },
    { "task_class": "default",  "budget_tokens": 2048, "source": "manual",        "inception_token": " ...Acting now.\n<|im_end|>" }
  ]
}
```

## vs ponytail

Positioning, not bragging — [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) is the 104k★ "think less" demand signal this rides. Different layer, different verb.

| Axis | thinkcap | ponytail |
|---|:---:|:---:|
| Sees the live `<|im_start|>think` stream mid-turn | ✓ | — |
| Per-task-class budget (rename vs refactor) | ✓ | — |
| Acts mid-stream (inception injection at N tokens) | ✓ | — |
| Model-agnostic / any coding agent | — | ✓ |
| Zero install friction (prompt skill, no proxy) | — | ✓ |

thinkcap is Qwen3.8-pinned by construction (the truncatable trace surface is the wedge); ponytail is broader and frictionless. If you want a nudge that works everywhere, use ponytail. If you run Qwen3.8 behind an agent and want the waste stopped at the token layer, use thinkcap.

## Roadmap

- [x] **m1 — profile reasoning trace**: `thinkcap probe` runs 5 tasks, parses `<|im_start|>think`, counts tokens via `/tokenize` (with `approx` fallback), emits a rich table + `policy.json`.
- [ ] **m2 — proxy inception**: `thinkcap serve` actually injects the inception token mid-stream and flushes the rest of the think block as suppressed; ships `--dry-run` default.
- [ ] **m3 — probe & govern loop**: closed loop — `probe` learns per-class p90, `serve` enforces; 10-task suite shows pass-rate parity at lower reasoning-token totals; `--accept` flips advisory → enforcing.
- [ ] **v0.2** — DeepSeek-R1 / GLM-4.6-Z1 trace packs (different delimiter, same primitive).
- [ ] **v0.3+** — a Cline/Aider "reasoning policy" hook integration; a monthly "reasoning-token burn report".

## License

[MIT](./LICENSE). File issues / PRs directly on [SuperMarioYL/thinkcap](https://github.com/SuperMarioYL/thinkcap).

## Share this

```
thinkcap — the per-task reasoning-budget governor for your Qwen3.8 Coding Agent. Profiles the think trace, injects inception mid-stream, same diff for ~1/6 the reasoning. https://github.com/SuperMarioYL/thinkcap
```

<p align="center"><sub><a href="./LICENSE">MIT</a> © 2026 SuperMarioYL</sub></p>
