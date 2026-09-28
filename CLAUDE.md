# TMW MCP Server

An MCP (Model Context Protocol) server that exposes TMW Transportation Management System data to AI models.

## Project Structure

```
tmw_mcp/
├── tmw_mcp.py              # MCP server — tool definitions and MCPServer setup
├── tmw_db.py               # Database layer — TmwDB class and all SQL queries
├── config.py               # Environment-backed settings (reads .env)
├── requirements.txt        # Direct dependencies
├── requirements-lock.txt   # Full pinned freeze of the working environment
├── requirements-docker.txt # Lock minus pywin32, for the Linux image
├── Dockerfile              # Multi-stage build with msodbcsql18
├── docker-compose.yml      # Container config; overrides driver + bind host
├── healthcheck.py          # Container probe — 401 means healthy
├── smoke_test.py           # End-to-end check against a running server
├── .env.example            # Template for local configuration
└── .venv/                  # Python virtual environment (gitignored)
```

## Running the Server

Local (stdio, the default) — one client, no auth:

```
z:\apps\tmw_mcp\.venv\Scripts\activate
python tmw_mcp.py
```

Network (streamable HTTP), served at `http://<host>:<port>/mcp`:

```
python tmw_mcp.py --transport http
```

Host, port, and keys come from the environment. Startup **fails** if
`TMW_MCP_HOST` is non-loopback while `TMW_MCP_API_KEYS` is empty, so the server
cannot be exposed unauthenticated by omission.

### Authentication

`BearerAuthMiddleware` requires `Authorization: Bearer <key>` on every HTTP
request. `TMW_MCP_API_KEYS` holds comma-separated `LABEL:SECRET` pairs — one key
per person, so a single tester can be revoked without rotating everyone, and the
log attributes each request to a label. Comparison uses `secrets.compare_digest`
against every key, so a wrong key costs the same time whatever its position.

Generate a key with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Secrets cannot contain a comma (the delimiter).

### Behind a reverse proxy

The transport does its own Host/Origin checking, and the SDK only auto-enables
it for loopback binds — binding `0.0.0.0` without `TMW_MCP_ALLOWED_HOSTS` leaves
DNS rebinding protection **off**, silently. Set it to the hostname clients dial,
which behind a proxy is the public name, not the bind address. The server logs a
warning at startup if that combination is detected.

Terminate TLS at the proxy; the app speaks plain HTTP.

## Docker

```
docker compose up -d --build
docker compose logs -f
python smoke_test.py http://127.0.0.1:8000/mcp <api-key>
```

`docker-compose.yml` reads `.env` for secrets and overrides three things the
container needs differently from Windows:

| Setting | Windows | Container | Why |
|---------|---------|-----------|-----|
| `TMW_DB_DRIVER` | `SQL Server Native Client 11.0` | `ODBC Driver 18 for SQL Server` | The Native Client does not exist on Linux |
| `TMW_DB_TRUST_SERVER_CERTIFICATE` | unset | `yes` | Driver 18 defaults to `Encrypt=yes` with full cert validation |
| `TMW_MCP_HOST` | `127.0.0.1` | `0.0.0.0` | Bind inside the container; `ports:` controls real exposure |

Three things that bite on the first run:

- **Short hostnames do not resolve.** `TMW_DB_SERVER=sqlhost` works on a
  domain-joined Windows box and fails in a container. Use an FQDN or IP, or the
  commented `extra_hosts:` block.
- **Windows Authentication is unavailable.** The container must use SQL auth, so
  `TMW_DB_USER` / `TMW_DB_PASSWORD` are required.
- **`requirements-docker.txt`, not `requirements-lock.txt`.** The lock file pins
  `pywin32`, which has no Linux wheel and aborts the build. Regenerate both
  together — the header in that file has the command.

The image runs as non-root (uid 10001), and `.dockerignore` excludes `.env` so
credentials are never baked into a layer; compose injects them at runtime.

`healthcheck.py` probes `/mcp` and treats **401 as healthy** — that proves both
the ASGI app and the auth middleware are live. A plain port check would pass
even if `BearerAuthMiddleware` had failed to attach, which is the failure most
worth catching.

### Reaching it from Nginx Proxy Manager

As shipped, the port is published to `127.0.0.1:8000` only, so NPM running on
the same host can reach it and nothing else on the network can. If NPM runs in
Docker, prefer the commented `networks:` block instead: delete `ports:`, join
NPM's network, and point the proxy host at `http://tmw-mcp:8000`. Nothing is
then exposed on the host at all.

Set `TMW_MCP_ALLOWED_HOSTS` to the public hostname either way — behind a proxy
that is the name clients dial, not the container.

## Configuration

All settings come from the environment via `config.py`, which loads `.env` at the project
root. `.env` is gitignored; `.env.example` documents every setting.

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

Never hardcode connection details — add a setting to `config.py` and `.env.example` instead.

Connection is lazy and **thread-local** — `TmwDB` connects on the first query in each
thread and reuses that thread's connection. `db.close()` closes only the calling thread's.
Build it with `TmwDB.from_settings(load_settings())`.

Per-thread is not optional: `pyodbc.threadsafety` is 1, so a connection cannot be shared
between threads, and the HTTP transport dispatches sync tool functions to a thread pool
(`anyio.to_thread.run_sync`). A single shared connection survives stdio only because one
client serialises calls; over HTTP it would interleave cursors and return wrong rows.
ODBC connection pooling is on by default, so the extra connects are cheap.

## TMW Data Model

Understanding the hierarchy is essential for interpreting query results correctly.

**Order (`ord_hdrnumber`)** — the customer-facing shipment request. An order consists of at least one leg. When a tractor changes mid-trip, the order spans multiple legs. When a load is cross-docked, the order spans multiple movements.

**Leg (`lgh_number`)** — a series of at least two stops assigned to a single tractor. A leg belongs to exactly one movement. A single leg can contain stops for multiple orders.

**Movement (`mov_number`)** — a series of legs where the loaded trailer remains consistent, even as the tractor changes between legs. The tractor can only change via a **Park/Hook**: the outgoing leg ends with a **DLT** (Drop Loaded Trailer) and the next leg begins with an **HLT** (Hook Loaded Trailer). Exception: empty legs may be added to the beginning or end of a movement with any tractor or trailer. Dispatchers view one movement at a time in the Trip Folder.

**Cross-Dock (XDU / XDL)** — when a load transfers between trailers at a facility, the order spans multiple movements. The outgoing leg ends with an **XDU** (Cross-Dock Unload) and the next movement begins with an **XDL** (Cross-Dock Load).

### Status Codes

| Code | Meaning |
|------|---------|
| AVL  | Available — unassigned |
| PLN  | Planned — assigned, not yet started |
| STD  | Started — in progress |
| DNE  | Done — completed |

Orders use a different set on `ord_status` — `CMP` (completed), `CAN` (cancelled),
`QTE` (quote), `PND`, `ICO`, `MST`, alongside `AVL` / `PLN` / `STD`. Don't assume the
leg vocabulary applies to `orderheader`.

`ord_invoicestatus`: `PPD`, `XIN`, `AVL`, `PND`, `CMP`. Invoice and pay statuses live
in `labelfile` under `InvoiceStatus` and `PayStatus`.

### Stop Status Fields

- `stp_status = 'DNE'` — driver has **arrived** at the stop
- `stp_departure_status = 'DNE'` — driver has **departed** the stop
- `arrival_date` / `departure_date` — actual times if the driver has been there; expected times if not yet

## Available MCP Tools

| Tool | Parameters | Description |
|------|-----------|-------------|
| `truck_location` | `trucks: list[str]` | Latest GPS check-call per truck (`TMW_CHECKCALL_LOOKBACK_DAYS` window) |
| `truck_plan` | `trucks: list[str]` | Active (PLN/STD) stop sequences for trucks, sorted by truck then trip order |
| `get_active_power` | _(none)_ | All active tractors with current driver and team leader |
| `get_active_legs` | _(none)_ | All active legs (AVL/PLN/STD) fleet-wide — can be large, filter in Python |
| `get_leg_stops` | `legs: list[str]` | All stops for given leg numbers |
| `get_movement_stops` | `movements: list[str]` | All stops across all legs within given movements |
| `get_order_stops` | `orders: list[str]` | All stops across every movement an order is part of |
| `search_orders` | many optional filters | Orders by date, customer, location, revenue type — see below |
| `summarize_orders` | `group_by` + same filters | Aggregated order totals instead of rows |
| `find_city_codes` | `name`, `state` | Resolve a city name to the numeric codes orders store |

## Database Layer (`tmw_db.py`)

### Key Patterns

- All multi-value filters use parameterized `IN (?, ?, ...)` placeholders — never string interpolation for user values
- Methods taking a list return `[]` for an empty one, without opening a connection. An empty list would build `IN ()`, which is a SQL syntax error, and an upstream filter that legitimately matches nothing should yield no rows rather than an error
- `_stop_row_to_dict(row)` — shared mapper used by `get_leg_stops`, `get_movement_stops`, and `get_order_stops`
- `_fetch_all_objects(cursor, cls)` — generic row-to-object mapper for dataclass-style models

### Order Search

`search_orders` and `summarize_orders` share one query each, not one query per
search form. `_ORDER_FILTERS` maps a filter name to a hardcoded SQL fragment and a
kind (`list` / `date` / `scalar`); `_build_where` assembles only the fragments whose
values were supplied. **Adding a searchable field is one line in that dict.**

- Fragments are hardcoded; user values are always parameterized. `{ph}` expands to
  `?, ?, ...` for list filters.
- Unknown filter names raise instead of being ignored, so a typo can never silently
  widen the result set.
- At least one filter is required — an unfiltered search would scan all ~326k orders.
- Both tools fetch `limit + 1` rows and return `truncated` so the model can tell a
  partial answer from a complete one.
- `summarize_orders` exists so aggregate questions don't pull thousands of rows into
  context. `_ORDER_GROUPS` maps a `group_by` name to a key expression and an optional
  label expression (e.g. `billto` → id plus `cmp_name`).

Filtering happens in SQL because `orderheader` is indexed for exactly these
predicates — `ordhdr_ordstartdate`, `dk_ocity`, `dk_dcity`, `dk_rev1_invstatus`,
`dk_ord_billto`, `dk_shipper`, `dk_ord_cns`. Pulling a date range and filtering in
Python would discard those seeks.

**`ord_origincity` / `ord_destcity` are `int` city codes, not names.** Call
`find_city_codes` first. Filtering on a joined `city.cty_nmstct LIKE` instead would
lose the index. Note the stored format is `'CHICAGO,IL/'` — with a trailing slash —
so match on `cty_name` and `cty_state`, which is what `resolve_cities` does.

Location has three precisions, exposed as separate filters rather than guessed at:
`origin_company` (a `cmp_id`, most precise), `origin_city` (code), `origin_state`.

### Adding a New Tool

1. Add a query method to `TmwDB` in `tmw_db.py`
2. Add a `@mcp.tool()` function in `tmw_mcp.py` that calls it
3. Write a clear docstring — this is what the model reads to understand the tool
4. If it needs a tunable value, add it to `Settings` in `config.py` and to `.env.example`
