# ouigo_mcp_server

A [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) server for [OUIGO Spain](https://www.ouigo.com/es/) high-speed trains.
It looks up stations, lists trains with fares for a day, and shows the lowest fare per day over a date range.
The tools are read-only and never book tickets.

**This is an unofficial client.** OUIGO does not publish an API. The server calls the undocumented JSON API behind OUIGO's web shop, which can change or stop working without notice.

## On this page

- [Tools](#tools)
- [Install](#install)
- [Use over stdio](#use-over-stdio)
- [Run as a remote HTTP server](#run-as-a-remote-http-server)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [How it works](#how-it-works)
- [Development](#development)
- [License](#license)

## Tools

| Tool | Purpose |
| --- | --- |
| `find_station(query)` | Find stations by name, synonym, or short code. Ignores case and accents. |
| `search_trains(origin, destination, date, adults=1)` | List trains for one day with departure and arrival times, duration, base fare, and fare package prices |
| `get_price_calendar(origin, destination, start_date, end_date, adults=1)` | Return the lowest fare per day for up to 62 days. The price is `null` when no train runs. |

`origin` and `destination` accept a station code or an unambiguous name. `Madrid` resolves to `MT1`, which covers all Madrid stations.
Dates use `YYYY-MM-DD`. Searches support 1 to 9 adult passengers, and prices are in euros for the whole party.

## Install

Requirements: Python 3.12, 3.13, or 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/5queezer/ouigo_mcp_server.git
cd ouigo_mcp_server
uv sync --frozen --no-dev
```

Use `uv sync --frozen --group dev` instead if you want to run the tests.

## Use over stdio

Local agents and desktop clients can start the server as a private stdio child process.
The process opens no network port, so it runs without authentication.

Configure the client to run the project's Python from the checkout directory.
Replace `/path/to/ouigo_mcp_server` with the absolute path of your checkout.
For clients that use the common `mcpServers` JSON format, such as Claude Desktop:

```json
{
  "mcpServers": {
    "ouigo": {
      "command": "/path/to/ouigo_mcp_server/.venv/bin/python",
      "args": [
        "-c",
        "from examples.ouigo_server import _new_server; _new_server(None).run(transport='stdio', show_banner=False)"
      ],
      "cwd": "/path/to/ouigo_mcp_server"
    }
  }
}
```

For [Hermes Agent](https://github.com/NousResearch/hermes-agent), add an entry under `mcp_servers` in its `config.yaml`:

```yaml
mcp_servers:
  ouigo:
    command: /path/to/ouigo_mcp_server/.venv/bin/python
    args:
      - -c
      - "from examples.ouigo_server import _new_server; _new_server(None).run(transport='stdio', show_banner=False)"
    cwd: /path/to/ouigo_mcp_server
    enabled: true
    timeout: 120
    tools:
      include:
        - find_station
        - search_trains
        - get_price_calendar
```

The command must run with the checkout as its working directory so Python can import `examples`.
`_new_server` is an internal helper; check this command when you update to a new revision.

## Run as a remote HTTP server

The HTTP server serves MCP at `/mcp` and a health check at `/health`.
Select the OUIGO server with `MCP_APP=examples.ouigo_server:build_app`.

### Demo mode

**Demo mode has no authentication. Use it only on a trusted network.**

```bash
HOST=127.0.0.1 MCP_AUTH_MODE=demo MCP_APP=examples.ouigo_server:build_app uv run python -m mcp_server
curl http://localhost:8080/health
```

Connect an MCP client to `http://localhost:8080/mcp`.

### GitHub OAuth

Without `MCP_AUTH_MODE=demo`, the server requires GitHub OAuth and admits only allowlisted GitHub users.

1. Create a GitHub OAuth app. For local use, set the homepage URL to `http://localhost:8080` and the callback URL to `http://localhost:8080/auth/callback`.
2. Copy `.env.example` to `.env`, then set `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, and `GITHUB_ALLOWED_USER_IDS`. The allowlist takes immutable numeric GitHub IDs, which `GET https://api.github.com/users/{login}` returns in its `id` field.
3. Start the server:

   ```bash
   MCP_APP=examples.ouigo_server:build_app uv run --env-file .env python -m mcp_server
   ```

`make run-ouigo` starts the same factory and reads the environment from your shell.
To add the server to Claude as a custom connector, follow Anthropic's [custom connector guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) with the server's `/mcp` URL.

## Configuration

| Variable | Required | Meaning |
| --- | --- | --- |
| `MCP_APP` | Yes, for HTTP | Set to `examples.ouigo_server:build_app`. The default is the template's echo server. |
| `MCP_AUTH_MODE` | No | Defaults to `github`. Use `demo` for anonymous access. |
| `BASE_URL` | GitHub mode | Public origin without a path. Must use HTTPS except on `localhost`. |
| `GITHUB_CLIENT_ID` | GitHub mode | GitHub OAuth app client ID |
| `GITHUB_CLIENT_SECRET` | GitHub mode | GitHub OAuth app client secret |
| `GITHUB_ALLOWED_USER_IDS` | GitHub mode | Comma-separated numeric GitHub user IDs |
| `OUIGO_API_USERNAME`, `OUIGO_API_PASSWORD` | No | Override the OUIGO web API client credentials |
| `FASTMCP_HOME` | No | Directory for FastMCP's encrypted OAuth state |
| `HOST`, `PORT`, `LOG_LEVEL` | No | Uvicorn process settings |

Restart the server after you change the configuration.

## Deployment

### Container

```bash
docker build --tag ouigo-mcp .
docker run --rm -p 8080:8080 -e MCP_APP=examples.ouigo_server:build_app -e MCP_AUTH_MODE=demo ouigo-mcp
```

The image runs one Uvicorn worker as UID/GID 10001 and stores OAuth state in `/data/fastmcp`.
Mount durable storage there if you use GitHub OAuth.
For GitHub mode, pass the GitHub variables and `BASE_URL` instead of `MCP_AUTH_MODE=demo`.

### Cloud Run

`deploy.sh` deploys to Cloud Run with GitHub OAuth and a GitHub client secret from Secret Manager:

```bash
export GITHUB_CLIENT_ID='your-oauth-app-client-id'
export GITHUB_ALLOWED_USER_IDS='12345678'
export GITHUB_CLIENT_SECRET_REF='github-client-secret:latest'

./deploy.sh ouigo-mcp europe-west1 my-project examples.ouigo_server:build_app
```

After the first deployment, set the OAuth app's homepage URL to the printed service URL and its callback URL to `<service-url>/auth/callback`.
The script limits the service to one instance because OAuth state is stored locally.
See the [template README](https://github.com/5queezer/mcp-oauth-template#cloud-run-reference-deployment) for the deployment sequence and demo deployments.

## How it works

The server calls the API behind OUIGO's web shop at `https://mdw01.api-es.ouigo.com/api`:

| Endpoint | Used for |
| --- | --- |
| `POST /Token/login` | Gets a bearer token with the web shop's own client credentials |
| `GET /Data/GetStations` | Station list for `find_station` and name resolution |
| `POST /Sale/journeysearch` | Trains and fares for `search_trains` |
| `POST /Calendar/prices` | Daily lowest fares for `get_price_calendar` |

The server caches the token and the station list in memory.
When the token expires, it logs in again once and retries; concurrent calls share one login.

Limitations:

- Only OUIGO Spain is supported.
- Only adult passengers are supported.
- OUIGO can change its API, rotate the web-client credentials, or add bot protection to search at any time.

## Development

```bash
uv sync --frozen --group dev
make check                                # Ruff, ty, pytest, and pip-audit
uv run pytest tests/test_ouigo.py -q      # OUIGO tests only
```

The tests mock OUIGO's API and need no network access.

This repository is built on [mcp-oauth-template](https://github.com/5queezer/mcp-oauth-template), which provides the HTTP server, GitHub OAuth, container, and Cloud Run tooling in `mcp_server/`, `Dockerfile`, and `deploy.sh`.
The template's own examples remain in `examples/`.
Its [README](https://github.com/5queezer/mcp-oauth-template#readme) documents the OAuth and storage model and the Python API for building other servers.

## License

[MIT](LICENSE)
