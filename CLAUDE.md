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
├── compose.yml.example     # Container config template; copy to compose.yml
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
cp compose.yml.example compose.yml   # then edit for your environment
docker compose up -d --build
docker compose logs -f
python smoke_test.py http://127.0.0.1:8000/mcp <api-key>
```

`compose.yml` reads `.env` for secrets and overrides three things the
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

**Write the bare hostname**, e.g. `mcp.example.com`. The transport matches a bare
entry exactly and treats `host:*` as "that host with any port" — `host:*` does
**not** match a host with no port. On 443 the forwarded `Host` header carries no
port, so a `host:*` entry alone returns `421 Invalid Host header` for every
request. `_hosts()` in `config.py` expands either spelling to both, so both work,
but bare is the form to write. Setting `TMW_MCP_ALLOWED_ORIGINS` while leaving
`TMW_MCP_ALLOWED_HOSTS` empty is now a startup error, since that combination
enables the check with an empty allow-list and rejects everything.

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
| `TMW_MCP_TRUSTED_PROXIES` | no | — | IPs/CIDRs whose forwarded headers are believed |
| `TMW_MCP_AUTH_LOG` | no | — | Path for the fail2ban-friendly auth failure log |

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
| `get_order_freight` | `orders`, `stop_type` | Freight lines per order; DRP (delivery) copies by default |
| `summarize_order_freight` | `orders` | Per-order freight totals, with PUP vs DRP `in_sync` flag |
| `search_freight` | freight + order filters | Freight by commodity, weight, temperature, dimensions |

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

### Freight Detail

Freight lines live in `freightdetail` and hang off **stops**, never off orders
directly. `freightdetail.order_hdrnumber` exists and is indexed but is **NULL on
all 1.64M rows** in this database, so the path is always
`freightdetail -> stops -> orderheader`.

`stops.stp_type` is the field that matters: `PUP`, `DRP` or `NONE`.

| stp_type | Freight rows | Meaning |
|----------|-------------|---------|
| `NONE` | 849k | In-transit copies on `DLT` / `HLT` / `BMT` stops |
| `DRP` | 367k | Deliveries — **the authoritative record** |
| `PUP` | 343k | Pickups |

A stop may carry **more than one freight row** — rare (about 1,024 stops of 1.64M,
at most 7 rows) but legitimate, such as five commodities delivered to one stop. All
three tools handle it: they key on `fgt_number` and aggregate with `SUM`/`COUNT`
rather than assuming one row per stop, and `summarize_order_freight` reports
`drp_lines` (freight rows) separately from `drp_stops` (distinct stops).

`fgt_sequence` is **not unique within a stop** — some stops have every row at
sequence 1 — so every `ORDER BY` ends with `fgt_number` to keep the ordering
total. Without that tiebreaker, which rows fall inside `TOP (?)` could vary
between identical calls.

The same freight is written onto every stop it passes through, so **filtering by
`stp_type` is not optional** — most rows are in-transit duplicates. All freight
tools default to `DRP`.

On an order with more than one pickup or delivery, the PUP and DRP copies can
disagree. DRP is the truth. `summarize_order_freight` returns the DRP figures as
the headline numbers plus `pup_weight` / `pup_pieces` and an `in_sync` flag, so a
discrepancy is visible rather than hidden behind a single total. Measured on a
200-order sample: 196 agreed, 3 differed.

`stp_type='DRP'` corresponds to `stp_event='LUL'` (Live Unload) for 363,159 of
366,824 stops. Note there is **no `stp_event` value of `DRP`** — that code does
not exist in this database. `eventcodetable.fgt_event` carries an equivalent
PUP/DRP classification and agrees with `stp_type` on all but one row, but
`stp_type` is used because it needs one fewer join.

`fgt_description` is populated from the selected `cmd_code` but is freeform and
user-editable, so filter on `commodities` (the `cmd_code`) for anything that has
to be reliable, and treat the description as a display field.

**Weight is normalised to pounds; counts are not normalised at all.**

`_WEIGHT_LBS` converts `fgt_weight` to pounds in SQL, and `min_weight` /
`max_weight` compare against it. `weight_basis` says which unit the caller's
threshold is in, so `min_weight=10000, weight_basis="KGS"` means 10,000 kg and
matches a 22,046 lb row. `summarize_order_freight` sums the normalised value and
sets `mixed_weight_units` when more than one source unit contributed. Order 70475
mixes 5 KGS lines with 1 LBS line: the normalised total is 167,247 lbs, where a raw
SUM gave 114,110 - a 32% understatement.

**Only KGS is converted.** `TON` and `MTN` are mislabelled pounds in this data -
the largest `TON` values are around 61,460, which as short tons would be 123
million lbs - so they, and rows with a missing or unknown unit, are treated as
pounds. That covers 169 rows; `LBS` and `KGS` are 99.97% of rows with a weight.

**Dimensions are normalised to inches, temperatures to Fahrenheit.**

`min_length` / `min_width` / `min_height` compare against inches, with
`dimension_basis` naming the caller's unit (`INS`, `FET`, `MTR`, `YRD`, `CM`), so
`min_length=40, dimension_basis="FET"` means 40 feet and matches a 480 inch row.
Length, width and height each have their own unit column and 168 rows disagree
between them, so each is converted against its own unit, not the length unit.

Unlike the weight labels, these are reliable: 97% of `FET` lengths are 60 or
under (real feet), 90% of `INS` and 85% of `N` lengths fall in 61-700 (real
inches), and `MTR` averages 6.34m x 2.76m x 2.80m. **`N` is inches** - it tracks
`INS` exactly and is almost certainly a truncated "IN". Unlabelled rows average
about 130 and are treated as inches.

`min_temp` / `max_temp` compare against Fahrenheit, with `temp_basis` `"F"` or
`"C"`. **The conversion is affine, not a scale factor**, so thresholds go through
`_to_fahrenheit` rather than being multiplied - `min_temp=0, temp_basis="C"` means
32 F. `F` is dominant (5,176 rows) and `C` is genuine (-20..28); unlabelled rows
match the F range and are treated as F.

`summarize_order_freight` normalises before aggregating, because a `MIN` across
mixed C and F rows, or a `MAX` across `FET` and `INS`, is meaningless. It returns
`temp_unit` `"F"` and `dimension_unit` `"INS"` to say so.

Every result row carries `length_in`, `width_in`, `height_in`, `low_temp_f` and
`high_temp_f` beside the stored values and their units.

**Counts have no conversion** because `PCS`, `PLT`, `COIL` and `CAS` have no fixed
ratio. `min_count` therefore spans unlike units unless paired with `count_unit`,
and `summarize_order_freight` sets `mixed_count_units` when `pieces` added
different units - treat that total as unreliable rather than real.

**Stored weights contain bad data.** 27,033 LBS rows exceed 80,000 lbs, 87 exceed
1,000,000 and the largest is 505,000,010,266. Average LBS weight is 922,260, far
above a legal truckload, so the inflation is not confined to a few rows. Any
weight aggregate should be read with that in mind.

Units are stored per row and are not normalised: `fgt_weightunit` is mostly `LBS`
with some `KGS`, `TON` and `MTN`; `fgt_countunit` includes `PCS`, `PLT`, `COIL`
and `CAS`. Summing weight across rows therefore mixes units — report the unit
alongside any total rather than assuming pounds.

**`search_freight` uses a deferred join**, and must keep doing so. Selecting all
26 output columns across five tables in the same query as the `TOP` / `ORDER BY`
makes the optimiser abandon the ordered scan the row goal allows: `min_weight`
plus a date range took **24 seconds**. Picking `fgt_number` first with a narrow
projection, then widening over at most `limit` rows, returns the identical result
in **~450ms**. Adding an output column to the wide half is safe; moving the
filtering and ordering into it is not.

### Adding a New Tool

1. Add a query method to `TmwDB` in `tmw_db.py`
2. Add a `@mcp.tool()` function in `tmw_mcp.py` that calls it
3. Write a clear docstring — this is what the model reads to understand the tool
4. If it needs a tunable value, add it to `Settings` in `config.py` and to `.env.example`
