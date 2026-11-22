# AGENTS.md — factorio-learning-environment (m0nklabs)

Agent charter for this repository. The shared user-level instructions cover
language, working style and pull-request rules; this file adds only what is
specific to this project.

## Purpose and scope

`factorio-learning-environment` (FLE) is an open-source Python framework for
developing and evaluating LLM agents in the game of Factorio: a Lua/gRPC game
bridge, a Python SDK, an evaluation harness and a CLI. This charter covers the
whole repository root.

The project descends from the upstream open-source project; upstream docs,
issue history and the paper are evidence, not authority over this repo.

## Stack and layout

- Python `>=3.10` (pyproject `requires-python`); package built with hatchling;
distributed as `factorio-learning-environment`.
- Core package: `fle/` (`fle/env`, `fle/cluster`, CLI entry points).
- Tests: `tests/` (pytest `testpaths = "tests"`, `pythonpath = "fle/env"`,
  `python_files = "test_*.py"`; subdirectories per domain: `actions`,
  `benchmarks`, `blueprints`, `complex`, `connect`, `entities`, `eval`,
  `functional`, `gym_env`).
- Docs: `docs/`; examples: `examples/`; data/config artifacts: `data/`,
  `configs/`, `factorio-docs-lua.json`, `factorio_graphql_schema.graphql`.
- Optional extras: `[eval]`, `[mcp]`, `[psql]`, `[dev]`.
- Runtime prerequisites for full evaluation: Docker and Factorio (2.0.73+).
  Unit tests must not require a running game cluster.

## Working rules

- Preserve upstream contributions and history; do not rewrite unrelated files
  or reformat the tree incidentally.
- Keep Lua bridge changes and Python SDK changes consistent: a bridge schema
  change requires the matching Python client and test update in the same PR.
- Never commit Factorio binaries, licenses, saves, world data or generated
  clusters; they are large and license-bound.
- No credentials or private endpoints in code, docs, tests or fixtures.
- Linux is the supported CI platform; jobs must not require a GPU.
- Prefer reuse: check `fle/` helpers and existing tests before adding a new
  abstraction.

## Verification

Commands are from `pyproject.toml` and `.pre-commit-config.yaml`; run them from
the repository root.

```bash
uv sync                                  # or: pip install -e ".[dev]"
pytest                                   # full suite (testpaths=tests)
pytest tests/<area> -x -q                # scoped run while iterating
pre-commit run --all-files               # ruff-check + ruff-format
```

A change is verified when the scoped test passes and `pre-commit` is clean;
tests that need Docker or a Factorio client are not assumed to run everywhere.

## Agent infrastructure

PR review runs through PR Piet (`m0nklabs/pr-piet`), installed here via
`.github/workflows/pr-piet.yml` on the default branch. It uses the org
self-hosted pool and the org secret `GUARDIAN_API_KEY`; no repository-level
secret is required. Request a review with a `/review` comment on a PR.

Repository-specific contribution mechanics stay in upstream `CONTRIBUTING.md`;
do not duplicate it here.

## Maintenance

Stable rules and pointers only. Dated status, logs and task history belong in
pull requests and commit messages, never in this file.
