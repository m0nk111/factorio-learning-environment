# AGENTS.md — factorio-learning-environment (m0nklabs)

Agent charter for this repository. The shared user-level instructions cover
language, working style and pull-request rules; this file adds only what is
specific to this project.

## Purpose and scope

`factorio-learning-environment` (FLE) is an open-source Python framework for
developing and evaluating LLM agents in the game of Factorio: a Lua/gRPC game
bridge, a Python SDK, an evaluation harness and a `fle` CLI. This charter covers
the whole repository root.

The project derives from an upstream open-source project. Upstream docs, issue
history and its paper are evidence about the code, not authority over this
repository.

## Stack and layout

- Python `>=3.10` (`pyproject.toml` `requires-python`); built with hatchling and
distributed as `factorio-learning-environment` (currently `0.4.x`).
- Core package: `fle/` (including `fle/env`, cluster and CLI entry points).
- Tests: `tests/` — pytest sets `testpaths = "tests"`, `pythonpath = "fle/env"`,
`python_files = "test_*.py"`; subdirectories per domain (`actions`, `benchmarks`,
`blueprints`, `complex`, `connect`, `entities`, `eval`, `functional`, `gym_env`).
- Docs: `docs/`; examples: `examples/`; data and schema artifacts: `data/`,
`configs/`, `factorio-docs-lua.json`, `factorio_graphql_schema.graphql`.
- Optional extras: `[eval]`, `[mcp]`, `[psql]`, `[dev]`.
- Full evaluation needs Docker and Factorio (2.0.73+); unit tests must not
require a running game cluster.

## Working rules

- Preserve upstream history and unattended files; do not reformat or rewrite
unrelated parts of the tree.
- Keep the Lua bridge and the Python client consistent: a bridge/schema change
needs its matching Python-side and test update in the same pull request.
- Never commit Factorio binaries, licences, saves, world data or generated
cluster output — large and licence-bound.
- No credentials or private endpoints in code, docs, tests or fixtures.
- CI targets Linux and must not require a GPU; GPU/cluster work stays local.
- Reuse first: check `fle/` helpers and existing tests before adding an
abstraction.

## Verification

Derived from `pyproject.toml` and `.pre-commit-config.yaml`; run from the
repository root.

```bash
uv sync                          # or: pip install -e ".[dev]"
pytest                           # full suite (testpaths=tests)
pytest tests/<area> -x -q        # scoped run while iterating
pre-commit run --all-files       # ruff-check + ruff-format
```

A change is verified when the scoped tests pass and pre-commit is clean. Tests
that need Docker or a Factorio client are not assumed to run in every
environment; say which ones you actually ran.

## Agent infrastructure

Code review runs through PR Piet (`m0nklabs/pr-piet`) via
`.github/workflows/pr-piet.yml` on the default branch. It uses the org
self-hosted runner pool and the org secret `GUARDIAN_API_KEY`, so no
repository-level secret or runner is needed. Request a review with a `/review`
comment on a pull request; `/review2` adds the second-opinion tier.

Contribution mechanics stay in `CONTRIBUTING.md`; do not duplicate them here.

## Maintenance

Stable rules and pointers only. Dated status, logs and task history belong in
pull requests and commit messages, never in this file.
