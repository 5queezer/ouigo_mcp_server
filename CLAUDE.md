# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project goal

This repo started from `mcp-oauth-template` (FastMCP + GitHub OAuth, see README.md). The goal is to build an **OUIGO train MCP server** (station lookup, schedule search, live prices), similar in scope to the Renfe MCP server (`search_trains`, `find_station`, `get_train_prices` tools). The OUIGO server should be added as a new application factory, following the pattern of `examples/polymarket_server.py`. It should not change the template core.

## Commands

All tooling runs through `uv` (Python 3.12–3.14). Use `--frozen` so the lockfile is not rewritten.

```bash
uv sync --frozen --all-groups        # install (make install)
make check                           # ruff lint + ruff format --check + ty + pytest + pip-audit
uv run pytest -q                     # all tests
uv run pytest tests/test_polymarket.py::test_name -q   # single test
uv run ruff format .                 # apply formatting
make run MCP_APP=examples.polymarket_server:build_app  # local server in demo (anonymous) mode
make docker-smoke                    # build image + health/tool-list smoke test
```

The local server listens on `http://127.0.0.1:8080`, with `/mcp` (MCP endpoint) and `/health`. CI also runs `shellcheck deploy.sh`, `uv build`, and `scripts/check_artifacts.py dist`.

## Architecture

- **`mcp_server/`** is the reusable core and is kept small:
  - `create_app(mcp, allow_anonymous=..., cors_origins=...)` wraps a `FastMCP` instance. It serves stateless JSON HTTP at `/mcp` and adds `/health`. It refuses an unauthenticated server unless `allow_anonymous=True`.
  - `auth_from_env()` returns a `GitHubAuthProvider`, which enforces a numeric GitHub user-ID allowlist on every request. It returns `None` only when `MCP_AUTH_MODE=demo`.
  - `get_current_sub()` returns the caller's identity for the current request. Requests are stateless, so there is no session-cached identity.
- **`python -m mcp_server`** (`__main__.py`) runs Uvicorn with exactly one worker. It loads the factory named by `MCP_APP=module:factory` (default `examples.echo_server:build_app`).
- **Application factories** live in `examples/` and export `build_app()`. A factory must be import-safe without secrets: build the auth provider *inside* the factory, never at module level. The standard shape is:
  ```python
  auth = auth_from_env()
  mcp = FastMCP("name", auth=auth)
  # @mcp.tool() definitions...
  return create_app(mcp, allow_anonymous=auth is None)
  ```
- **`examples/polymarket_server.py`** is the closest model for the OUIGO server. It is a read-only external HTTP API wrapper that uses `httpx.AsyncClient` with a timeout, `Annotated[..., Field(...)]` tool parameters, and strict validation of upstream payloads.
- **Packaging/Docker:** setuptools packages only `mcp_server*` and `examples*`, and the Dockerfile copies only those two directories. Put new server code in one of them. Otherwise, update `pyproject.toml` and the Dockerfile too.

## Conventions

- Tests mock only external HTTP boundaries (GitHub, upstream APIs) and use the real FastMCP HTTP flow (see `tests/conftest.py`, `tests/test_polymarket.py`). Tests must not depend on network or credentials.
- Ruff: line length 100, rules `E4,E7,E9,F,I,UP,B`. The type checker is `ty`, not mypy.
- Update README and CHANGELOG when a change affects setup, configuration, deployment, security behavior, or the public API. Document breaking changes in `docs/migration.md`.
- Releases: bump the version in `pyproject.toml`, run `uv lock`, and update CHANGELOG. CI tags `v<version>` on `main`.
- OAuth state uses FastMCP's encrypted file store under `FASTMCP_HOME`. This is why `deploy.sh` (Cloud Run) pins the service to one instance.
