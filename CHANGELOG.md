# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-11-20

### Fixed

- `thinkcap serve` no longer crashes with a raw `KeyError`/`TypeError`
  traceback on a malformed `policy.json` row. A row that is not a mapping,
  omits `task_class`/`budget_tokens`, or carries a non-integer budget is now
  a clean `error: policy ... is not a valid policy.json: ...` with exit 2,
  and programmatic `create_app` callers degrade to passthrough. Before this
  release only invalid JSON and a non-list `budgets` were handled; a
  hand-edited or partially written row escaped as a traceback.
- A `budgets.json` that is valid JSON but not the store shape (a `records`
  value that is not a list of objects, or a bare scalar) is now preserved as
  a `.corrupt-<ns>` sibling and probing starts fresh, instead of being read
  as an empty history and overwritten by the next save. An existing store
  that cannot be read is a clean error rather than an empty history.
- `thinkcap probe` refuses a run in which no captured response contains the
  selected `--profile`'s delimiters, instead of recording 0-token
  observations and writing a policy with fabricated 1-token budgets (for
  example `--mock --profile deepseek-r1` against the Qwen3.8-shaped demo
  fixtures). The error names the pack and its delimiters, and no artifact is
  written.
- Probe HTTP calls now carry explicit `httpx.Timeout` objects: 180s read /
  10s connect for chat completions and 30s read / 10s connect for
  `/tokenize`. The previous bare float timeouts also raised the connect
  budget, silently voiding the bounded 10s connect added in v0.2.0.
- `save_policy` creates the parent directory of `--policy` when it does not
  exist, matching the `--budgets` writer, instead of failing after the run
  had already been recorded.

### Changed

- Version lockstep to 0.4.0 across every surface (`VERSION`, pyproject.toml,
  the config fallback, the `policy.json`/`budgets.json` file versions, the
  FastAPI app version, `web/site.json` content_version) and
  `CHANGELOG.md`/README updates describing the fixed contracts.

## [0.3.0] - 2026-10-17

### Fixed

- `thinkcap serve` now actually serves: every `/v1/chat/completions` request
  was rejected with 422 because the `Request` annotation could not be
  resolved (module-level `from __future__ import annotations` combined with a
  `create_app`-local fastapi import bound it as a required query parameter).
  The fastapi/httpx imports now live at module level, and a new streaming
  test suite exercises the endpoint end-to-end against a mock upstream.
- Upstream llama.cpp failures surface as real HTTP status codes: a 4xx/5xx
  from the upstream returns that status with an OpenAI-shaped
  `{"error": ...}` JSON body, and an unreachable upstream returns 502 —
  instead of HTTP 200 followed by a truncated SSE stream.
- The proxy's upstream client now uses bounded timeouts
  (`httpx.Timeout(180.0, connect=10.0)`, matching the probe path) instead of
  `timeout=None`, so a wedged llama.cpp fails the agent's request instead of
  hanging it forever.
- The advisory dry-run "would-inject" decision is once per turn (the sticky
  flag is set in both modes) instead of logging on every streamed delta, and
  the stale "governor ships in m2" log text is replaced with the honest
  advisory note.
- A corrupt `budgets.json` (e.g. truncated by a killed probe) is preserved
  as `budgets.json.corrupt-<timestamp>` and probing starts fresh, instead of
  silently destroying the accumulated observation history on the next save;
  if the corrupt file cannot be backed up, probe exits 2 with a clean error
  rather than overwrite it.

### Changed

- Version lockstep to 0.3.0 across every surface (`VERSION`, pyproject.toml,
  the config fallback, the `policy.json`/`budgets.json` file versions, the
  FastAPI app version, `web/site.json` content_version) plus a new
  `thinkcap --version` flag alongside the existing `version` subcommand.

## [0.2.0] - 2026-09-11

### Fixed

- `thinkcap probe` against an unreachable or erroring `--server` now prints a
  clean `error:` line and exits 2 instead of dumping a raw httpx traceback
  (exit 1); the client also gets a bounded connect timeout.
- `thinkcap serve` with a malformed `policy.json` (invalid JSON or a non-list
  `budgets`) now fails fast with a clean error and exit 2 before uvicorn
  starts; `create_app` degrades to a passthrough-only warning for programmatic
  callers. The missing-file warn-and-continue contract is unchanged.
- `policy.json` is now rebuilt from the accumulated `budgets.json`
  distribution, so re-probing a class widens its budget (documented
  `--budgets` contract) instead of silently replacing it with the latest run.
  Store records also keep their token-count provenance (`tokenize_api` vs
  `approx`) and trace pack; v0.1.0 files load unchanged.

### Added

- Trace packs (roadmapped v0.2): `probe` and `serve` accept `--profile`
  to select the model delimiter surface — `qwen3.8` (default, unchanged),
  `deepseek-r1` and `glm-4.6-z1` (both `<think>`/`</think>`). The same
  parser, policy, and advisory governor govern every surface, and learned
  policies carry the pack's inception token. Distributions from different
  packs never mix in one `budgets.json`.

### Changed

- Version lockstep to 0.2.0 across every surface: `VERSION`,
  `pyproject.toml`, the CLI/config fallback, the `policy.json` and
  `budgets.json` file versions, the FastAPI app version, and
  `web/site.json` `meta.content_version`.

## [0.1.0] - 2026-08-17

### Added

- Initial release: `thinkcap probe` (m1) runs the coding-mini suite against
  a local llama.cpp server, parses `<|im_start|>think` blocks from the
  streamed response (with an honest char-based fallback when `/tokenize` is
  unavailable), and emits a per-task-class rich table plus `policy.json`.
- `thinkcap serve` (m2 surface) ships as an OpenAI-compatible advisory
  streaming reverse proxy: passthrough with dry-run "would inject" logging;
  enforcing injection stays gated on the m3 parity suite.
- `thinkcap suites` lists available probe suites; `thinkcap probe --mock`
  runs the full pipeline offline for demos and CI.
