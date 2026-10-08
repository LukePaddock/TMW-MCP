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

## Logging failed authentication (fail2ban)

Set `TMW_MCP_AUTH_LOG` and every rejected request appends one line:

```
2026-10-05 13:49:31 WARNING authentication failed from 203.0.113.45 reason=no_key path=/mcp
```

`reason` is `no_key` or `invalid_key`. The file rotates at 5 MB, keeping three
old copies. Successful requests go to the general server log, never here, so
this file contains only failures.

### Getting the real client IP

`TMW_MCP_TRUSTED_PROXIES` takes a comma-separated list of IPs or CIDR blocks.
A forwarded header is believed **only** when the TCP peer falls inside one of
them; otherwise the peer address is logged and the headers are ignored.

This matters because `X-Forwarded-For` is attacker-controlled. Nginx appends
rather than replaces, so a request arriving with `X-Forwarded-For: 8.8.8.8`
reaches the app as `8.8.8.8, <real client>`. Reading the **first** entry — the
usual mistake — lets anyone get an arbitrary address banned by fail2ban. The
resolver therefore prefers `X-Real-IP`, which nginx overwrites, and falls back
to the **last** `X-Forwarded-For` entry.

Leave `TMW_MCP_TRUSTED_PROXIES` empty and headers are ignored entirely, which
is correct for a direct bind and wrong behind a proxy — the log would then
record the proxy's address, and fail2ban would ban the proxy. The server logs a
warning at startup if the auth log is enabled without it.

Uvicorn is started with `proxy_headers=False` deliberately. It ships its own
forwarded-header handling that trusts `127.0.0.1` by default and rewrites
`scope["client"]` before any middleware runs — a second, looser trust model that
would silently override this one.

### Wiring up fail2ban

`fail2ban/` holds a filter and a jail:

```bash
sudo cp fail2ban/tmw-mcp.filter.conf /etc/fail2ban/filter.d/tmw-mcp.conf
sudo cp fail2ban/tmw-mcp.jail.conf   /etc/fail2ban/jail.d/tmw-mcp.conf
sudo fail2ban-regex /opt/tmw-mcp/logs/auth.log /etc/fail2ban/filter.d/tmw-mcp.conf
sudo systemctl reload fail2ban && sudo fail2ban-client status tmw-mcp
```

Run `fail2ban-regex` before reloading — it reports how many lines matched, and
is the authoritative check that the pattern works on your version.

Two things that catch people out:

- **The container runs as uid 10001.** Create the host log directory and give
  it ownership, or the server cannot write to it:
  `sudo mkdir -p /opt/tmw-mcp/logs && sudo chown 10001:10001 /opt/tmw-mcp/logs`
- **Bans do nothing against dockerised NPM unless they target `DOCKER-USER`.**
  Docker DNATs published ports in `nat PREROUTING`, so the traffic traverses
  `FORWARD` and never reaches `INPUT`, where fail2ban's stock actions insert
  their rules. The supplied jail sets `chain=DOCKER-USER`. If NPM runs directly
  on the host instead, remove that override.

### The zero-code alternative

NPM writes its own access logs with real client IPs already, so a jail watching
those for `401` responses needs no application changes. It is coarser — it
cannot distinguish a bad key from any other 401, and it bans per proxy host
rather than per endpoint — but if you want something running in five minutes,
start there.

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
| `TMW_MCP_TRUSTED_PROXIES` | no | — | IPs/CIDRs whose forwarded headers are believed |
| `TMW_MCP_AUTH_LOG` | no | — | Path for the fail2ban-friendly auth failure log |

Leaving `TMW_DB_USER` and `TMW_DB_PASSWORD` blank uses Windows Authentication
(`Trusted_Connection=yes`). Setting only one of the two is an error.

Behind a reverse proxy, set `TMW_MCP_ALLOWED_HOSTS` to the hostname clients
dial - the public name, not the bind address - and write it bare:

```
TMW_MCP_ALLOWED_HOSTS=mcp.example.com
TMW_MCP_ALLOWED_ORIGINS=https://mcp.example.com
```

A `host:*` entry matches only a host *with* a port, and on 443 the forwarded
`Host` header has none - so `host:*` alone returns `421 Invalid Host header` on
every request. Both spellings are accepted and expanded, but bare is correct.

Binding a non-loopback address without `TMW_MCP_ALLOWED_HOSTS` leaves DNS
rebinding protection off entirely.

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
| `search_stops` | `scope` + many optional filters | Stops by identity, status, date, appointment, location, driver, truck |
| `search_drivers` | `name` + many optional filters | Drivers by code, name, status, assignment, licence |
| `search_orders` | many optional filters | Orders by date, customer, location, revenue type |
| `summarize_orders` | `group_by` + same filters | Aggregated order totals, split by currency |
| `find_city_codes` | `name`, `state` | Resolve a city name to the numeric codes orders store |
| `get_order_freight` | `orders`, `stop_type` | Freight lines per order; DRP (delivery) copies by default |
| `summarize_order_freight` | `orders` | Per-order freight totals, with PUP vs DRP `in_sync` flag |
| `search_freight` | freight + order filters | Freight by commodity, weight, temperature, dimensions |

**Charges carry a currency.** 84% of orders in this database are Canadian
dollars and 16% US, so `search_orders` returns `currency` (the stored label,
e.g. `CA$`) and `currency_code` (`CAD`, `USD` or `UNK`) next to
`total_charge`. Filter with `currency=["CAD"]` or `currency=["US$"]` —
spellings are interchangeable, which matters because some orders are stored as
`US` rather than `US$`.

`summarize_orders` splits every group by currency for the same reason: money is
only additive within one currency. One logical group comes back as several
rows, each with its own `currency`, so report a total with its currency and
never add two rows together. Group by `currency` to get the per-currency
figures directly. Charges are not converted between currencies — the stored
exchange rates are too sparse in the CAD-to-USD direction to trust.

`search_drivers` looks up the 676-row driver table by code or by name. Because
the table is small, `name` is a substring search and word order does not
matter: `"smith"`, `"john smith"` and `"smi"` all find SMITH,JOHN. Other
filters cover status, dispatch team, terminal, fleet, assigned truck, licence
state and class, and hire or termination date ranges. The `driver_code` it
returns is what `search_stops(drivers=[...])` and `truck_plan` expect.

Two things to know about the data. **`OUT` means terminated** — 472 of 676
drivers are OUT, so pass `active_only=True` for the 204 current ones, or
`status=["USE"]` for the 97 on the road. And TMW stores `2049-12-31` as the
termination date of a driver who has not left, so `termination_date` comes back
null in that case rather than claiming a date in 2049.

**Personal data is not exposed.** The table holds social security numbers,
dates of birth, licence numbers, home addresses and gender markers; none are
returned or searchable. Work phone, email, and the licence state and class are.

`search_stops` answers every stop question through one query. Filters cover
identity (`orders`, `legs`, `movements`, `stops`), classification
(`stop_types` PUP/DRP/NONE, `events`), progress (`stop_status`,
`departure_status`, `unarrived`, `undeparted`), actual times
(`arrived_after`/`before`, `departed_after`/`before`), the appointment window
(`appt_after`/`before`, `appt_latest_after`/`before`, `firm_appt`,
`late_arrival`), place (`companies`, `cities`, `states`, `zips`), people and
equipment (`drivers`, `trucks`, `carriers`, `trailers`) and customer paperwork
(`reference_numbers` with `reference_types` such as `B/L #` or `LOAD #`). Every
`search_orders` filter works here too, so "stops in Texas on that customer's
loads" is a single call.

`scope` is the argument to get right. `scope="stop"` returns the matching stops;
`scope="movement"` returns every stop on their movements — the Trip Folder view
— and `limit` then counts movements rather than rows. Three quarters of
movements carry more than one order, so movement scope normally includes
co-loaded freight sharing the trailer. The three tools this replaced map to
`search_stops(orders=[...], scope="movement")`, `search_stops(legs=[...])` and
`search_stops(movements=[...])`.

`drivers` matches either seat. `trailers` and `late_arrival` cannot use an
index, so pair them with a date or status filter.

`search_orders` and `summarize_orders` share one filter set: date ranges,
`billto`, `shipper`, `consignee`, `status`, `invoice_status`, `revtype1`-`4`,
origin and destination by company / city code / state, and `min_charge`. At
least one filter is required, results are capped by `TMW_MAX_SEARCH_ROWS`, and
both return a `truncated` flag so a partial answer is distinguishable from a
complete one.

Order origin and destination cities are stored as integer codes, so resolve a
name with `find_city_codes` first.

### Freight

Freight lines hang off **stops**, not orders, and the same freight is written onto
pickup, delivery and in-transit stops. `stops.stp_type` distinguishes them —
`PUP`, `DRP` or `NONE` — and most of the 1.64M rows are `NONE` (in-transit
duplicates), so every freight tool defaults to `DRP`, the delivery copies, which
are the authoritative record.

On orders with several pickups or deliveries the two copies can drift.
`summarize_order_freight` reports the DRP figures plus the pickup totals and an
`in_sync` flag, so a mismatch is surfaced rather than hidden.

`search_freight` accepts order-level filters too, so "oversize freight for this
customer last quarter" is one call.

Weight is stored per row with its own unit. `min_weight` / `max_weight` compare
after normalising every row to pounds, and `weight_basis` says which unit your
threshold is in - `min_weight=10000, weight_basis="KGS"` means 10,000 kg. Only
`KGS` is converted; `TON` and `MTN` are mislabelled pounds in this data. Use
`weight_unit` to scope to rows stored in one unit.

Dimensions normalise to inches and temperatures to Fahrenheit the same way, via
`dimension_basis` (`INS`, `FET`, `MTR`, `YRD`, `CM`) and `temp_basis` (`F`, `C`).
`min_length=40, dimension_basis="FET"` means 40 feet, and `min_temp=0,
temp_basis="C"` means freezing rather than 0 F. Dimension unit labels are reliable
here, and the undocumented unit `N` behaves as inches. Results carry `length_in`,
`width_in`, `height_in`, `low_temp_f` and `high_temp_f` beside the stored values.
Scope by stored unit with `dimension_unit` or `temp_unit`.

Counts are not converted, since `PCS`, `PLT` and `COIL` have no fixed ratio - pair
`min_count` with `count_unit`. `summarize_order_freight` flags
`mixed_weight_units` and `mixed_count_units` so a total built from unlike units is
visible.

## Data model

See [CLAUDE.md](CLAUDE.md) for the order / leg / movement hierarchy, status codes,
stop status fields, and the reasoning behind the search and threading design -
the domain knowledge needed to read query results correctly.
