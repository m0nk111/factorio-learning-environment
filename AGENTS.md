# AGENTS.md — Factorio Learning Environment (fork)

## Purpose and scope

A fork of [JackHopkins/factorio-learning-environment](https://github.com/JackHopkins/factorio-learning-environment):
a non-saturating, open-ended environment for evaluating LLM agents in Factorio. The fork lives at
[m0nklabs/factorio-learning-environment](https://github.com/m0nklabs/factorio-learning-environment)
and exists to keep the environment working on current Factorio releases and to feed a sandbox
adapter into the operator's Gamer preset. This file covers the whole checkout.

## Stack and layout

- Python package `fle/` (requires-python >=3.10; 3.12 in practice), version from `pyproject.toml`.
- `fle/env/` — game binding: RCON client, `FactorioInstance`, per-tool `server.lua` scripts and the
  shared Lua layer in `fle/env/mods/`. That Lua is injected into the running game over RCON, not
  mounted as a Factorio mod.
- `fle/cluster/` — docker launcher and scenario/config assets used to start game servers.
- `fle/eval/`, `fle/agents/`, `fle/benchmarks/` — evaluation harnesses and agents.
- `tests/` — pytest suite; `tests/actions/` needs a live cluster (session fixture in
  `tests/conftest.py` starts one).

## Working rules

- **Never publish to the upstream repository.** Read `JackHopkins/factorio-learning-environment`,
  never push, open issues, PRs, comments or reactions there. Fork work stays in `m0nklabs`.
- **Never touch the operator's live Factorio world.** The campaign runs as a native systemd service
  on ports 34197/udp and 34199/tcp with its own companion; experiments use a copy of a save in a
  throwaway container. The cluster defaults exist for this reason: `FLE_FACTORIO_IMAGE`,
  `FLE_GAME_PORT_BASE` (35197), `FLE_RCON_PORT_BASE` (27100), `FLE_RCON_PASSWORD`.
- **`storage` holds data only.** Factorio cannot serialise functions, so a world whose `storage`
  contains them cannot be saved (the server then exits). Lua helpers live on the session global
  `FLE`; keep new ones there and leave `storage` to serialisable state.
- **The Lua layer is single-source.** `fle/env/mods/*.lua` and the per-tool `server.lua` files are
  injected over RCON and read filesystem paths from `FLE_MODS_DIR` when overridden; do not copy
  them into the game image.
- Preserve unrelated work, keep changes small and testable, and verify with a real server before
  claiming behaviour. A finding in another repository is a handoff, not a fix.

## Verification

Run from the repository root:

- Install/sync: `uv sync --dev`
- Lint: `pre-commit run --all-files` (CI: `.github/workflows/lint.yml`)
- Unit tests: `uv run pytest -n 4 --dist=load -v --tb=short tests/` — `tests/actions/` requires a
  running cluster; the container-free subset is safe on its own.
- Cluster tests: `uv run python fle/run.py cluster start -n 4`, then
  `uv run pytest -n 4 --dist=load -v -s --tb=short tests/actions/`, then
  `uv run python fle/run.py cluster stop` (CI: `.github/workflows/factorio-test.yml`).
- Lua syntax check without a game: `python -c "from lupa.lua54 import LuaRuntime;
  [LuaRuntime().compile(open(p).read()) for p in ['fle/env/mods/utils.lua']]"`.
- `python -m fle.env.protocols._mcp` serves the MCP surface (FastMCP, stdio); it needs `mcp<2` plus
  `fastmcp`, as declared in the `mcp` extra, and a discoverable `factorio_*` container.

## On-demand references

- Shared conventions, review rules and operator preferences: `~/.agents/AGENTS.md` (loaded already;
  no need to restate them here).
- Review gate: `.github/workflows/pr-piet.yml` calls the org reusable workflow; a PR is merge-ready
  only after PR-Piet's review of the current head, per the shared rules.
- GitHub workflow conventions for this operator: `github-workflow` skill.

## Maintenance

Stable rules and pointers only. Dated findings, port notes for a specific Factorio build and test
transcripts belong in issue [#1](https://github.com/m0nklabs/factorio-learning-environment/issues/1)
and the pull request that carries the change, not in this file.