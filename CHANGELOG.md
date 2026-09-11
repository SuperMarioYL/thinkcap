# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
