# TMW MCP Server

An [MCP](https://modelcontextprotocol.io) server that exposes TMW Suite
Transportation Management System data to AI models.

Runs two ways: over **stdio** for a single local client, or over **streamable
HTTP** for several people at once, with bearer-token authentication.

## Requirements

- Python 3.12+ (developed against 3.14)
- An ODBC driver for SQL Server
  - Windows: `SQL Server Native Client 11.0`
  - Linux / Docker: `ODBC Driver 18 for SQL Server` (installed by the image)
- Network access to the TMW database, and rights to read it
- Docker and Docker Compose, if deploying as a container

A read-only SQL login is strongly recommended - nothing here writes.

## Setup

```powershell
py -3.14 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env   # then edit .env for your environment
```

## Running

### stdio (default)

One client, no authentication. Normally launched by the client rather than run
by hand.

```powershell
.venv\Scripts\activate
python tmw_mcp.py
```

### HTTP

Serves MCP at `http://<host>:<port>/mcp`.

```powershell
python tmw_mcp.py --transport http
```

Host, port and keys come from the environment. Startup **fails** if
`TMW_MCP_HOST` is non-loopback while `TMW_MCP_API_KEYS` is empty, so the server
cannot be exposed unauthenticated by accident.

## Authentication

HTTP requests require `Authorization: Bearer <key>`. Keys live in
`TMW_MCP_API_KEYS` as comma-separated `LABEL:SECRET` pairs:

```
TMW_MCP_API_KEYS=alice:xLb3...,bob:9fQz...
```

One key per person. A single user can then be revoked without rotating
everyone, and the log attributes each request to a label:

```
INFO  authenticated alice -> /mcp
```

Generate a key with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Secrets cannot contain a comma, since that is the delimiter.

## Connecting a client

### Claude Code - HTTP directly

```bash
claude mcp add --transport http tms http://192.168.1.50:8000/mcp --header "Authorization: Bearer <key>"
```

### Claude Desktop - stdio

`claude_desktop_config.json` supports stdio servers only. To run the server
locally on each machine:

```json
{
  "mcpServers": {
    "tms": {
      "command": "Z:\\apps\\tmw_mcp\\.venv\\Scripts\\python.exe",
      "args": ["Z:\\apps\\tmw_mcp\\tmw_mcp.py"]
    }
  }
}
```

### Claude Desktop - against a shared HTTP server

Claude Desktop cannot point at an HTTP MCP server from its config file, and
Custom Connectors added through the web portal connect **from Anthropic's cloud
infrastructure**, so they cannot reach a server on a private network. Bridge
stdio to HTTP instead:

```json
{
  "mcpServers": {
    "tms": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "http://192.168.1.50:8000/mcp",
        "--header", "Authorization: Bearer ${TMW_KEY}"
      ],
      "env": { "TMW_KEY": "<that user's key>" }
    }
  }
}
```

This needs Node on each machine, but no Python, repository checkout, or database
credentials. The key sits in `env` rather than `args` so it stays out of process
listings.

## Docker

```bash
cp compose.yml.example compose.yml   # then edit for your environment
docker compose up -d --build
docker compose logs -f
```

`compose.yml` reads `.env` for secrets and overrides what the container needs
differently from Windows - the ODBC driver name, `TrustServerCertificate`, and
the bind address. As shipped the port is published to `127.0.0.1:8000` only, so
a reverse proxy on the same host can reach it and nothing else can.

Notable gotchas:

- Short hostnames do not resolve in a container. Use an FQDN or IP for
  `TMW_DB_SERVER`, or the commented `extra_hosts:` block in the compose file.
- Windows Authentication is unavailable, so `TMW_DB_USER` / `TMW_DB_PASSWORD`
  are required.
- The build uses `requirements-docker.txt`; `requirements-lock.txt` pins
  `pywin32`, which has no Linux wheel.

The image runs as a non-root user, and `.dockerignore` excludes `.env` so
credentials are never baked into a layer.

## Testing

`smoke_test.py` checks a running HTTP server end to end - auth rejection, the
MCP handshake, real tool calls, and concurrent calls compared by value. It exits
non-zero on failure, so it works as a deployment gate.

```bash
python smoke_test.py http://127.0.0.1:8000/mcp <api-key>
```

## Configuration

All settings come from the environment, read from `.env` at the project root when
present. `.env` is gitignored; `.env.example` documents every setting.

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `TMW_DB_SERVER` | yes | — | SQL Server host |
| `TMW_DB_DATABASE` | yes | — | Database name |
| `TMW_DB_DRIVER` | yes | — | ODBC driver name |
| `TMW_DB_USER` | no | — | SQL auth username; blank means Windows Auth |
| `TMW_DB_PASSWORD` | no | — | SQL auth password; must accompany `TMW_DB_USER` |
| `TMW_DB_TIMEOUT` | no | `30` | Connection (login) timeout, seconds |
| `TMW_DB_ENCRYPT` | no | — | `Encrypt=` value; needed for ODBC Driver 18 |
| `TMW_DB_TRUST_SERVER_CERTIFICATE` | no | — | `TrustServerCertificate=`; set `yes` in Docker |
| `TMW_CHECKCALL_LOOKBACK_DAYS` | no | `30` | How far back `truck_location` searches |
| `TMW_MAX_SEARCH_ROWS` | no | `200` | Hard cap on `search_orders` / `summarize_orders` rows |
| `TMW_MCP_HOST` | no | `127.0.0.1` | Bind address for `--transport http` |
| `TMW_MCP_PORT` | no | `8000` | Bind port for `--transport http` |
| `TMW_MCP_ALLOWED_HOSTS` | no | — | Host allow-list; required in practice off loopback |
| `TMW_MCP_ALLOWED_ORIGINS` | no | — | Origin allow-list |
| `TMW_MCP_API_KEYS` | no | — | `LABEL:SECRET` pairs; required off loopback |

Leaving `TMW_DB_USER` and `TMW_DB_PASSWORD` blank uses Windows Authentication
(`Trusted_Connection=yes`). Setting only one of the two is an error.

Behind a reverse proxy, set `TMW_MCP_ALLOWED_HOSTS` to the hostname clients
dial - the public name, not the bind address. Binding a non-loopback address
without it leaves DNS rebinding protection off.

## Layout

```
tmw_mcp.py              MCP server — tool definitions, auth middleware, transports
tmw_db.py               Database layer — TmwDB class and all SQL queries
config.py               Environment-backed settings
healthcheck.py          Container probe — 401 means healthy
smoke_test.py           End-to-end check against a running HTTP server
requirements.txt        Direct dependencies
requirements-lock.txt   Full pinned freeze of the working environment
requirements-docker.txt Lock minus pywin32, for the Linux image
Dockerfile              Multi-stage build with msodbcsql18
compose.yml.example     Container config template (copy to compose.yml)
.env.example            Template for local configuration
```

## Tools

| Tool | Parameters | Description |
|------|-----------|-------------|
| `truck_location` | `trucks: list[str]` | Latest GPS check call per truck |
| `truck_plan` | `trucks: list[str]` | Active (PLN/STD) stop sequences per truck |
| `get_active_power` | _(none)_ | Active tractors with current driver and team leader |
| `get_active_legs` | _(none)_ | All active legs (AVL/PLN/STD) fleet-wide |
| `get_leg_stops` | `legs: list[str]` | All stops for the given legs |
| `get_movement_stops` | `movements: list[str]` | All stops across all legs in the given movements |
| `get_order_stops` | `orders: list[str]` | All stops across every movement an order touches |
| `search_orders` | many optional filters | Orders by date, customer, location, revenue type |
| `summarize_orders` | `group_by` + same filters | Aggregated order totals instead of rows |
| `find_city_codes` | `name`, `state` | Resolve a city name to the numeric codes orders store |

`search_orders` and `summarize_orders` share one filter set: date ranges,
`billto`, `shipper`, `consignee`, `status`, `invoice_status`, `revtype1`-`4`,
origin and destination by company / city code / state, and `min_charge`. At
least one filter is required, results are capped by `TMW_MAX_SEARCH_ROWS`, and
both return a `truncated` flag so a partial answer is distinguishable from a
complete one.

Order origin and destination cities are stored as integer codes, so resolve a
name with `find_city_codes` first.

## Data model

See [CLAUDE.md](CLAUDE.md) for the order / leg / movement hierarchy, status codes,
stop status fields, and the reasoning behind the search and threading design -
the domain knowledge needed to read query results correctly.
