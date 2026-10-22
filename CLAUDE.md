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

`connect()` calls `setencoding(locale.getencoding())` so `str` parameters bind
as **varchar**. pyodbc's default is nvarchar, and comparing a varchar column to
an nvarchar parameter under this database's SQL collation converts the column
on every row and rules out a seek. That one line took stops-by-truck from 84ms
to 5ms, stops-by-company from 275ms to 4ms, and an exact reference lookup from
240ms to 1ms, with identical results across every tool.

The encoding is **per platform, not hardcoded**: the driver reads narrow
character data in its own assumed encoding — the ANSI code page (cp1252) for
Native Client on Windows, the process locale (UTF-8 in `python:slim`) for
msodbcsql18 on Linux — and converts to the server code page from there.
Hardcoding cp1252 would mangle non-ASCII input in the container, e.g. the en
dash in reference `126 – 4517115 /179-5929935`. `locale.getencoding()` gives
the right answer on both and ignores Python's UTF-8 mode, which would otherwise
report UTF-8 on Windows. ASCII is identical either way, so a mismatch here
shows up only on accented or typographic characters.

Per-thread is not optional: `pyodbc.threadsafety` is 1, so a connection cannot be shared
between threads, and the HTTP transport dispatches sync tool functions to a thread pool
(`anyio.to_thread.run_sync`). A single shared connection survives stdio only because one
client serialises calls; over HTTP it would interleave cursors and return wrong rows.
ODBC connection pooling is on by default, so the extra connects are cheap.

**Connection loss recovers on its own.** Because connections are cached per
thread for the life of the process, a connection that died in a network blip,
a SQL Server restart or a failover used to stay cached. That thread then
failed every call with "Communication link failure" until the server was
restarted, while threads that hadn't connected yet worked: an intermittent
outage that outlived the real one. Now every public query method is wrapped
in `@_reconnecting`:

- On a connection-class error (SQLSTATE `08xxx`, or pyodbc's "closed
  connection"), it discards that thread's connection and runs the method once
  more, which reconnects lazily. Re-running is safe because the server only
  reads.
- A failure to *connect* is not retried. The login timeout has already been
  spent, and a second attempt would only double the wait.
- Either way the caller gets `DatabaseUnavailable` ("The TMW database is
  unavailable … Try again shortly"). Ordinary SQL errors are never retried.
- Wrapped methods calling each other don't stack retries.

This was tested by simulating each case in-process, because the read-only
account has no `KILL` permission: a mid-session `08S01` drop, a connection
closed underneath, the database down at connect then restored, a drop while
the database was still down, an ordinary SQL error (one attempt, no retry),
and two threads with only one broken.

Connections use **`autocommit=True`**. pyodbc's default opens a transaction on
the first statement, and since this server never commits, every cached
connection sat inside one for its whole life (`@@TRANCOUNT` was 1, and is now
0).

**Errors must be `ToolError` to reach the model.** The MCP SDK treats any
other exception as a crash and shows the model only "Error executing tool
<name>". That hid every deliberate message here: "At least one filter is
required", "Unknown filter", a bad date, currency or scope. `_tool_errors` in
`tmw_mcp.py` wraps every tool and converts `ValueError` (bad arguments) and
`DatabaseUnavailable` into `ToolError`; anything else is still a crash, logged
with its traceback. The wrapper keeps each tool's signature, and the tool
schemas were verified identical with and without it.

## TMW Data Model

Understanding the hierarchy is essential for interpreting query results correctly.

**Order (`ord_hdrnumber`)** — the customer-facing shipment request. An order consists of at least one leg. When a tractor changes mid-trip, the order spans multiple legs. When a load is cross-docked, the order spans multiple movements.

**Leg (`lgh_number`)** — a series of at least two stops assigned to a single tractor. A leg belongs to exactly one movement. A single leg can contain stops for multiple orders.

**Movement (`mov_number`)** — a series of legs where the loaded trailer remains consistent, even as the tractor changes between legs. The tractor can only change via a **Park/Hook**: the outgoing leg ends with a **DLT** (Drop Loaded Trailer) and the next leg begins with an **HLT** (Hook Loaded Trailer). Exception: empty legs may be added to the beginning or end of a movement with any tractor or trailer. Dispatchers view one movement at a time in the Trip Folder.

**Cross-Dock (XDU / XDL)** — when a load transfers between trailers at a facility, the order spans multiple movements. The outgoing leg ends with an **XDU** (Cross-Dock Unload) and the next movement begins with an **XDL** (Cross-Dock Load).

### Status Codes

Leg status (`lgh_outstatus`), counts across all 475k legs:

| Code | Meaning | Legs |
|------|---------|------|
| AVL  | Available — unassigned | 334 |
| PLN  | Planned — assigned, not yet started | 82 |
| DSP  | Dispatched | 1 |
| STD  | Started — in progress | 81 |
| CMP  | Completed | 474,512 |
| CAN  | Cancelled | 564 |

**Completed legs are `CMP`, not `DNE`.** An earlier version of this table and
of the server instructions said `DNE`, which matches no leg at all; `DNE` is a
*stop* status (below). Of the 334 AVL legs, 316 are 2026 loads not yet given a
truck and 18 are stale, back to 2004.

Orders use a different set on `ord_status` — `CMP` (completed), `CAN` (cancelled),
`QTE` (quote), `PND`, `ICO`, `MST`, alongside `AVL` / `PLN` / `STD`. Don't assume the
leg vocabulary applies to `orderheader`.

`ord_invoicestatus`: `PPD`, `XIN`, `AVL`, `PND`, `CMP`. Invoice and pay statuses live
in `labelfile` under `InvoiceStatus` and `PayStatus`.

### Revenue Types

`labelfile.userlabelname` names the four `ord_revtypeN` columns:

| Column | Meaning | Values |
|--------|---------|--------|
| `ord_revtype1` | **Booking company** — the sub-company that booked the load | EPT, JSV, TSC, PKS; CFS retired 2018 |
| `ord_revtype2` | Region | LOCAL, INBOUN, OUTBOU |
| `ord_revtype3` | Taxable | YES, NO |
| `ord_revtype4` | **Booking agent** — the person who booked it | ~20 first names |

Revtype1 and 4 are exposed under their meaning as `booking_company` /
`booking_agent` — filter, `group_by`, a decoded name on `search_orders` rows, and
a list tool each. `booking_company`, not `company`, because `companies` and
`*_company` already mean a `cmp_id` facility.

**RevType4 was repurposed.** It first held a load class — `LEGAL` (164,395
orders), `VAN` (38,633), `WIDTH`, `WEIGHT`, `HEIGHT`, `WH`, `XATA`, all retired,
last used 2022. Agent codes overlap them from 2003, so no date cut-off separates
the eras. `_REVTYPE4_LEGACY` lists them; `booking_agent` is NULL for those orders,
the filter never matches them, and `group_by="booking_agent"` folds them into one
NULL group. Otherwise `LEGAL` would be the busiest agent in history. The raw
`revtype4` filter and group are unchanged and still see them.

Both filters accept the **code or the name** (`_label_filter`), because codes are
truncated to six characters and four agents differ: `CAMER`/CAMERON,
`JENN`/JENNIFER, `STEW`/STEWART, `KRIS`/KRISTEN. Collation is case-insensitive.
Agents are not tied to one company — MADDIE books for all four — so the two
filters compose.

### Stop Status Fields

- `stp_status = 'DNE'` — driver has **arrived** at the stop
- `stp_departure_status = 'DNE'` — driver has **departed** the stop
- `arrival_date` / `departure_date` — actual times if the driver has been there; expected times if not yet

## Available MCP Tools

| Tool | Parameters | Description |
|------|-----------|-------------|
| `truck_plan` | `trucks: list[str]` | Active (PLN/STD) stop sequences for trucks, sorted by truck then trip order |
| `search_trucks` | many optional filters, `near_*` | Tractors: status, driver and team, GPS position, nearest-to search, specs, plan |
| `search_trailers` | many optional filters, `near_*` | Trailers: types, length, status, availability, last known place, last load |
| `search_legs` | many optional filters | Legs with total / loaded / empty / bobtail miles from their stops, plus `totals`, `comment` and `permit_required`; `active_only` replaces `get_active_legs` |
| `search_stops` | `scope` + many optional filters | Stops by identity, status, date, appointment, location, driver, truck - see below |
| `search_drivers` | `name` + many optional filters | Drivers by code, name, status, assignment, licence - see below |
| `search_orders` | many optional filters | Orders with revenue from invoices — see Revenue below |
| `summarize_orders` | `group_by` + same filters | Aggregated revenue (from invoices) instead of rows, split by currency |
| `search_invoice` | invoice + chain filters | Invoices with tax split out; credit/rebill chains; totals by currency |
| `search_invoice_details` | `billing_state` + line filters | Billing lines, invoiced or pending, with charge types and tax flag |
| `find_city_codes` | `name`, `state` | Resolve a city name to the numeric codes orders store |
| `list_booking_agents` | `include_retired` | Booking agents (revtype4) with order counts and first/last order |
| `list_booking_companies` | `include_retired` | Booking sub-companies (revtype1), same shape |
| `get_order_freight` | `orders`, `stop_type` | Freight lines per order; DRP (delivery) copies by default |
| `summarize_order_freight` | `orders` | Per-order freight totals, with PUP vs DRP `in_sync` flag |
| `search_freight` | freight + order filters | Freight by commodity, weight, temperature, dimensions |
| `search_reference_numbers` | ref + order filters | Every reference number on orders, stops, freight and invoice lines, with its owner |

## Database Layer (`tmw_db.py`)

### Key Patterns

- All multi-value filters use parameterized `IN (?, ?, ...)` placeholders — never string interpolation for user values
- Methods taking a list return `[]` for an empty one, without opening a connection. An empty list would build `IN ()`, which is a SQL syntax error, and an upstream filter that legitimately matches nothing should yield no rows rather than an error
- `_stop_row_to_dict(row)` — shared mapper for `search_stops` results, 30 columns wide
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
  A name the tool's JSON schema does not know is dropped *before* `_build_where`
  runs, so a misspelled parameter surfaces as this error rather than as
  "Unknown filter". The message says to check the spelling for that reason.
- Both tools fetch `limit + 1` rows and return `truncated` so the model can tell a
  partial answer from a complete one.
- `summarize_orders` exists so aggregate questions don't pull thousands of rows into
  context. Its groups are always split by currency — see below. `_ORDER_GROUPS` maps a `group_by` name to a key expression and an optional
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

### Driver Search

`search_drivers` reads `manpowerprofile`, which is **676 rows**. That one fact
inverts the rule the rest of this server follows. `search_orders` refuses a
`LIKE` on city names because it would throw away an index seek across 326k
rows; here a full scan costs nothing, so `name` does substring matching and a
caller never needs to know a driver code to find a driver. Every query measures
12-24ms.

`name` is split on whitespace and commas, and each token becomes its own `LIKE`
against `mpp_lastfirst` (stored `'LASTNAME,FIRSTNAME'`), all ANDed. One column
covers both names, and because the tokens are independent, word order stops
mattering: `"smith"`, `"john smith"`, `"smith john"` and `"smi"` all find
`SMITH,JOHN`. Terms go through `_like_term`, which escapes `\`, `%`, `_` and
`[` and pairs with `ESCAPE '\'` — without it a `%` in a name would match the
whole table, and `[a-z]` would open a T-SQL character class.

#### Personal data is deliberately excluded

This table holds **555 full-length SSNs, 651 dates of birth, 646 licence
numbers, 626 home addresses and 381 gender markers**. None of `mpp_ssn`,
`mpp_dateofbirth`, `mpp_licensenumber`, `mpp_address1/2`, `mpp_homephone`,
`mpp_gender`, `mpp_nbrdependents`, `mpp_password` or the pay-rate columns is
selected, returned, or available as a filter. Everything a tool returns enters
a model's context and travels to whatever client is connected, so the exclusion
is in the SELECT list, not in a post-filter that a future edit could drop.

Work contact details (`mpp_currentphone`, `mpp_email`) and the licence **state
and class** are included — operational, not identifying. A test asserts no
output key or filter name matches a list of sensitive terms, so adding one back
by accident fails the check.

#### Two data traps

**`OUT` means terminated, not "out on the road".** `DrvStatus` decodes AVL
Available, PLN Planned, USE On the Road, OUT **Terminated** — and 472 of 676
rows are OUT. `active_only` gives the 204 current drivers; `status=["USE"]`
gives the 97 actually rolling. Every result row carries a plain `terminated`
boolean so the code is hard to misread.

**Dates carry sentinels.** A driver who has not left has `mpp_terminationdt`
= `2049-12-31 23:59`, and 15 rows have `mpp_hiredate` = `1950-01-01`. Both are
stored as real datetimes, so reported raw, every current driver appears to be
terminated in 2049. `_real_date` maps anything above 2040 or below 1950-01-02
to `None`. The alignment is exact: all 204 non-OUT drivers carry the future
sentinel, all 463 OUT drivers a genuine date, and 9 carry the low sentinel.
`terminated_after` / `terminated_before` also bound against real dates only, so
"who left this year" cannot sweep in the active roster.

#### Other notes

`mpp_id` is the code `legheader.lgh_driver1` / `lgh_driver2` carry, and it
matches for **all 594** distinct drivers that appear on a leg — so
`driver_code` feeds straight into `search_stops(drivers=[...])` or
`truck_plan`.

`_DRIVER_MAX_ROWS = 1000` overrides `TMW_MAX_SEARCH_ROWS` for this tool.
The global cap defaults to 200 and exists to stop a 326k-row order scan filling
a context window; on a 676-row table it merely made "list the active drivers"
truncate by four, since the roster is 204.

Three rows (`CU`, `UNKOWN`, `WESC02`) have no name at all, so `mpp_lastfirst`
is `','`, which sorts ahead of every real driver. None appears on any leg, so
the `ORDER BY` pushes them last instead of letting them lead every unfiltered
result.

`mpp_type` is `UNK` on all 676 rows and is not exposed. `mpp_carrier` and
`mpp_employedby` are empty throughout, as are `mpp_next_stoparrival`,
`mpp_next_legnumber`, `mpp_last_home` and the pay rates.

**This is the one search with no required filter**, because listing 676 rows is
cheap and "who are our drivers" is a fair question. The cost is that a
misspelled parameter — dropped by the JSON schema before the server sees it —
degrades to a full listing rather than raising `Unknown filter`, the way it
would for orders, stops or freight. `count` and `truncated` make that visible,
but it is a real difference in behaviour.

### Stop Search

`search_stops` replaced `get_leg_stops`, `get_movement_stops` and
`get_order_stops`, which were byte-identical 48-line methods differing only in
their one `WHERE` line. `_STOP_FILTERS` follows `_ORDER_FILTERS`, and the
assembler is handed `{**_ORDER_FILTERS, **_STOP_FILTERS}`, so an order filter
such as `billto` or `revtype1` composes with any stop filter in one query.

**`scope` is the one argument that changes the meaning of the answer, not just
its size.**

| `scope` | Returns | `limit` bounds |
|---------|---------|----------------|
| `"stop"` (default) | the matching stops themselves | stops |
| `"movement"` | every stop on every movement a match belongs to — the Trip Folder view | **movements** |

This is not cosmetic: **267,192 of 344,345 movements carry stops from more than
one order**, so for roughly three orders in four, movement scope returns stops
the order does not own — the co-loaded freight sharing the trailer. Order 373
has 2 stops of its own and 7 in its movement. The old `get_order_stops` was
movement scope; the other two were stop scope. Collapsing `orders` into a plain
`s.ord_hdrnumber IN (...)` would therefore have silently shrunk the most common
lookup in the system, which is why scope stayed an explicit argument rather
than being inferred from which filters were supplied.

Movement scope drops an overflow movement whole rather than truncating it
mid-trip, which would read as a short trip. Fan-out is bounded — 4.8 stops per
movement on average, 84 at the worst, 199 movements above 20 — so `limit`
movements is at most a few thousand rows.

It also surfaces stops with `ord_hdrnumber = 0`, which is **not an order**:
there is no row 0 in `orderheader`. Those 790k stops are **non-billable
stops**, and they are not all empty. Five years of them, by event and the load
status of the segment driven to them:

| Event | Stops | Load status |
|---|---|---|
| `DMT` drop empty trailer | 41,093 | empty |
| `BMT` begin empty | 34,698 | mostly unrecorded; 5,222 empty |
| `HLT` hook loaded trailer | 25,189 | bobtail *to* the hook (14,284) |
| `DLT` drop loaded trailer | 25,186 | **loaded, every one** |
| `BBT` begin bobtail | 16,433 | mixed |
| `RTP` route point | 13,777 | **mostly loaded** (10,773) |
| `HMT` hook empty trailer | 12,883 | bobtail / empty |
| `EMT`, `EBT` end empty / bobtail | 12,041 | empty / bobtail |
| `NBCST`, `NBS` non-billable customs / stop | 781 | mixed |
| `INSP`, `SCALE` | 358 | loaded |

DLT and HLT are by definition the two halves of a Park/Hook on a *loaded*
trailer, so "no order" must never be read as "empty". This doc and the server
instructions once described these as empty equipment moves, which was wrong.
They are legitimate trip context, and `ord_number` / `order_status` come back
NULL for them.

Naming: `stop_status` and `departure_status` are the stop's, while plain
`status` and `invoice_status` are the **order's**, inherited from
`_ORDER_FILTERS`. Likewise `cities` / `states` / `companies` are where the stop
is, and `origin_city` / `dest_state` / `origin_company` are the order's
endpoints. `orders` is the one deliberate collision — the stop table wins,
because `s.ord_hdrnumber` seeks `sk_stp_ordnum` directly where
`oh.ord_hdrnumber` would go through the join.

Filtering is in SQL because `stops` is indexed for almost exactly these
predicates: `sk_stp_ordnum`, `dk_lghnum`, `dk_mov`, `dk_stp_type`,
`sk_stp_arrvdt`, `dk_stops_sch_seq`, `dk_stpdetstatus`, `dk_cmparrival`,
`ix_stp_city`, `sk_stops_stp_refnum`, `ix_stops_HLT`, plus `dk_lgh_driver1`,
`ix_lh_dr2_outst_stdt`, `dk_tractor` and `dk_lgh_carrier_enddate` on
`legheader`. Measured: leg or movement lookup 4ms, a filtered search 16-27ms.

Two filters have no index and scan 1.6M rows alone — `trailers`
(`stops.trl_id` is unindexed) and `late_arrival` (a column-to-column
comparison). Both are documented as needing a date or status partner.

**`drivers` matches either seat** — a team driver's stops are found whichever
seat they held. It is written as two indexed seeks `UNION`ed into an
`s.lgh_number IN (...)`, **not** as
`lgh_driver1 IN (..) OR lgh_driver2 IN (..)`: the `OR` form cannot use
`dk_lgh_driver1` and `ix_lh_dr2_outst_stdt` at the same time and scanned
`legheader` for **3.1 seconds**, against milliseconds for the `UNION`. That is
what the `list_x2` filter kind is for — `str.format` fills every `{ph}` in a
fragment, so a fragment naming the placeholder set twice needs its values bound
twice over.

`search_stops` uses the same **deferred join** as `search_freight`, for the same
reason: 30 output columns across six tables in the query that also carries the
`TOP` / `ORDER BY` costs the optimiser the ordered scan the row goal allows.
Selection order is most-recent-first; presentation order is trip order
(movements by first arrival, then chronological within each), which is what
preserved the old methods' output ordering exactly. Every `ORDER BY` ends with
`stp_number`, because stops in a leg can share an arrival date and without the
tiebreaker which rows land inside `TOP` could vary between identical calls.

Equivalence was verified against the three removed methods on an eight-order
sample: all 31 rows identical, in the same order, on all three mappings.

#### Currency

**This is a mixed-currency database and `ord_totalcharge` is meaningless
without `ord_currency` beside it.** The split:

| Stored value | Orders | Folds to |
|---|---|---|
| `CA$` | 259,164 | CAD |
| `US$` | 49,939 | USD |
| `UNK` | 16,969 | UNK |
| `US` | 549 | USD |
| *(blank)* | 141 | UNK |
| `CDN $` | 2 | CAD |

So one currency is stored under two names in both directions, and there is no
`Currency` row in `labelfile` to decode them. `_fold_currency` folds the
spellings in SQL (for any column: order, invoice header), `_norm_currency`
folds the caller's input the same way, so `currency=["US$"]`, `["USD"]` and
`["us"]` are the same query. That matters because the 549 rows spelled `US`
would otherwise be missed by the obvious `US$`.

**Revenue currency comes from the invoice, not the order** (see Revenue
below). 10,941 orders stored as `UNK` were invoiced in CA$ or US$, and 1,484
were invoiced in a different currency from the one on the order.
`search_orders` returns `revenue_currency` (from invoices, else the order) and
`order_currency` (the order's folded code). The `currency` filter tests the
revenue currency via `_REVENUE_CURRENCY`: the latest live invoice's, since a
currency correction rebills in the right one.

**`summarize_orders` splits every group by currency**, and that was a bug fix,
not a feature. Each `_ORDER_GROUPS` key spans several currencies here, so a
single `SUM` per group added Canadian and US dollars together. Grouping
2024-onward orders by `revtype1`, `EPT` once reported one total of
**92,407,307**, mixing three currencies. The currency is part of the
`GROUP BY`, so each row is single-currency by construction, and one logical
group can come back as several rows. `split_by_currency` says so. Grouping by
`currency` keys on the revenue currency.

**Charges are never converted.** There is a `currency_exchange` table
(`cex_from_curr`, `cex_to_curr`, `cex_date`, `cex_rate`), but it is unusable in
the direction that matters: `CA$`→`US$` has **7 rates across 20 years**, with
gaps of 3,134, 2,375 and 1,126 days, so converting a 2017 order would apply a
2013 rate. `US$`→`CA$` is dense by contrast (273 roughly monthly rows, some
forward-dated to 2031), so converting everything *to* CAD would be feasible if
it is ever wanted — but presenting a converted figure as authoritative when the
rate is years stale is a worse failure than reporting two currencies
separately.

### Revenue

**`ord_totalcharge` is not revenue.** An audit of five years of completed,
invoiced orders (87,493) found the order total captured **66.8%** of what was
invoiced in CAD and **51.2%** in USD:

- **37,184 invoiced orders (42%) have `ord_totalcharge` = 0.** Linehaul and
  accessorial are zero too. Those customers are rated only at invoicing, which
  hid 71M of billing. They concentrate in a few bill-tos: LAUBUR, CORDUN01,
  JFEBUR and CATMOR.
- **Corrections are not written back.** On 926 credited and rebilled orders,
  the order kept the *original* amount on 400 (43%). Supplementals are missing
  from the order on a quarter of those orders.
- **Typos.** 15 orders hold totals of 250k to 47.25M on loads billed a few
  thousand, adding 204M of revenue that was never billed. Order 287123 shows
  47,250,000 and was invoiced 3,500.
- Ordinary orders with a real total are mostly right: 85% match the invoice to
  the cent.

Revenue is therefore read from **billing lines** (`invoicedetail`). They exist
for an order's whole life: created when it is rated, held with
`ivh_hdrnumber = 0` ("pending"), and moved onto the invoice when it is billed.
Rated uninvoiced orders have pending lines matching `ord_totalcharge` on 99%.
The rule, applied identically by `_revenue_lines` (SQL, set-based),
`_order_revenue` (Python, per search page) and `_REVENUE_AMOUNT` (scalar, for
`min_charge`):

1. **Live invoices** (status not `CAN`): the net of all of them before tax,
   covering the original, credits, rebills and supplementals. Credits reverse
   their target exactly on 966 of 967. Pending lines are then ignored: 57 of
   the 102 left on invoiced orders duplicate an invoiced line.
2. **Not billable**: `CAN` cancelled, `QTE` quote and `MST` master template
   give 0. Unfiltered summaries used to count 17.6M of cancelled orders, 3.2M
   of quotes, and a 1.5M typo sitting on a cancelled order. `ICO` (Cancelled
   Billable) *is* billable.
3. **Pending lines**: their sum.
4. **Every invoice cancelled** gives 0, because the bill was withdrawn. There
   are 212 such completed orders, one with a 23.2M typo that a fallback would
   resurrect.
5. Otherwise, `ord_totalcharge`, flagged `revenue_source: "order"`. Among
   completed orders over five years this is now zero.

**Tax is excluded from revenue and identified by `chargetype.cht_basis =
'TAX'`, never `ivd_type`.** The `GST` code is typed `LI` on 26,783 lines worth
3.7M. Testing `ivd_type` made 7,844 orders look 12% under-billed, when the gap
was really 13% HST.

**Currency is the invoice header's.** `invoicedetail.cur_code` is NULL on most
invoiced lines and `US` on 2,496 lines of CA$ orders. An `UNK` invoice falls
back to the order's currency. 130 orders were invoiced in two currencies. On
129 of them that is a currency correction where every currency but one nets to
zero, and the zero-net rows are dropped. The remaining one, 305009, is an LH in
CAD plus a SUPL in USD; it comes back as `revenue: null` with
`revenue_by_currency`.

`search_orders` returns `revenue`, `revenue_currency`, `revenue_source`, `tax`,
`invoices` (live invoice numbers), `corrected` and `supplemental`, with
`order_total_charge` for comparison. The old `total_charge` field was removed,
so it cannot be quoted by mistake. `summarize_orders` splits revenue into
`invoiced_revenue`, `pending_revenue` and `order_only_revenue`, and counts
`non_revenue_orders`.

Before and after, 2024-onward EPT in CAD: the old code reported **76.9M**, of
which 23.3M was one cancelled-invoice typo, plus cancelled orders and quotes.
Revenue is **51.0M**.

**`min_charge` and `currency` are revenue tests.** In `search_orders` and the
other searches they are correlated subqueries in `WHERE`, which is fine under
`TOP` with a date range. `summarize_orders` pops them and applies them to the
revenue it has already computed, because as `WHERE` clauses they took **12s**
over a year, against 0.8s. Both paths agree: one PKS week at 2,000 CAD or more
gives 39 orders and 194,087.77 either way.

Timing: `search_orders` adds about 40ms for revenue on a 200-row page.
`summarize_orders` takes 0.8s for a year and 1.7s for five years, up from about
0.5s.

### Invoices

`invoiceheader` (322k) and `invoicedetail` (1.0M). **A wrong invoice is never
edited.** It is reversed by a credit memo and replaced, so an order's billing
is a chain whose numbers share the order's plus a letter: 346778A (LH), then
346778B (CRD, `ivh_applyto` = 346778A), then 346778C (RBIL).

| `ivh_definition` | 5 years | Meaning |
|---|---|---|
| `LH` | 87,753 | Original invoice |
| `CRD` | 967 | Credit memo: negative, reverses `ivh_applyto` exactly |
| `RBIL` | 932 | Rebill after a credit |
| `SUPL` | 452 | Supplemental: extra charges billed later |
| `MISC` | 3 | Miscellaneous |

Invoice status (`InvoiceStatus`): `XFR` transferred to accounting, `PRN`
printed, `RTP` ready to print, `HLD` / `HLA` on hold, `CAN` cancelled (223).
Cancelled invoices are hidden by default and count toward nothing.

- `ivh_totalcharge` equals the sum of its lines on every invoice but 3, and
  **includes tax**. `search_invoice` returns `total`, `tax` and
  `pretax_total`.
- `ivh_billto` differs from the order's `ord_billto` on 1,633 invoices. For
  billing questions the invoice's is the authoritative one.
- **Payment is not in TMW.** `ivh_totalpaid`, `ivh_paid_amount`,
  `ivh_pay_status` and `ivh_paid_indicator` are empty on every invoice, so none
  is exposed. 11,475 invoices sit on master bills (`ivh_mbnumber`), which are
  exposed.
- `invoicedetail.ord_hdrnumber` equals its header's on all 320,865 lines
  checked.

`search_invoice` picks newest bill date first and presents chains grouped by
order in issue order, so a credit sits under its target. `totals_by_currency`
covers every match.

`search_invoice_details` covers both stages of a line's life. `billing_state`
is derived per line (`_LINE_STATE`):

| State | Meaning | Default |
|---|---|---|
| `invoiced` | on a live invoice | shown |
| `pending` | rated, not yet invoiced | shown |
| `cancelled` | on a cancelled invoice | hidden |
| `superseded` | left pending on an order since invoiced | hidden |

`billing_state="all"` shows the inactive ones. `ivd_type` is `SUB`
(linehaul), `LI` (accessorials, fuel and GST), `TAX`, or `DRP` / `PUP`
(per-stop lines, almost always 0). Zero lines are hidden unless
`include_zero_lines`. Order filters apply through the line's order. `orders`
seeks `invoicedetail`'s own `ord_hdrnumber` index, so it reaches pending lines,
which have no header.

Indexes used: `dk_ivh_ord_hdrnum` (`ord_hdrnumber`, `ivh_billdate`),
`sk_ivh_billdt`, `sk_ivh_billto`, `uk_invnum`, `dk_applyto`; on lines,
`sk_ivd_invoice` (`ivh_hdrnumber`), `ord_number` (`ord_hdrnumber`) and the
clustered `pk_ivd_number`. Measured: 14 to 450ms.

### Leg Search and Mileage

`search_legs` replaced `get_active_legs`, which returned every AVL/PLN/STD leg
unfiltered and missed `DSP`. `active_only=True` returns the same set plus DSP,
which is 498 legs, and `_LEG_MAX_ROWS = 1000` lets that come back whole, for
the same reason as `_DRIVER_MAX_ROWS`.

**Miles come from the stops.** `stp_lgh_mileage` is the distance from the
previous stop on the leg to this one, and `stp_loadstatus` describes **that
same segment**. A pickup (`LLD`) is `MT` because the truck ran empty to reach
it, and the delivery (`LUL`) is `LD`. Summing by status splits a leg:

| `stp_loadstatus` | Stops | Miles (all time) | Reported as |
|---|---|---|---|
| `LD` | 787k | 157.8M | `loaded_miles` |
| `MT` | 415k | 31.2M | `empty_miles` |
| `BT` | 146k | 1.95M | `bobtail_miles`: tractor, no trailer |
| `NON`, `UND`, NULL | 296k | 51k | `unclassified_miles` |

Bobtail is kept separate because it is neither loaded nor pulling an empty.
Whether it counts as deadhead is a business decision, not something this
server should make. `legheader.lgh_mtmiles` and `lgh_actualmiles` look like
they would answer this, but neither is populated on any 2025+ leg.

Details that matter:

- The first stop's mileage is **NULL**, not 0. A NULL after the first stop is
  normally a zero-distance stop, such as a pickup at the previous stop's
  location. In 4,140 such legs the stop sum still equals `lgh_miles`, so NULL
  does not mean missing data.
- `-1` is a "not computed" sentinel on 1,250 stops; `_LEG_MILES` sums only
  `> 0`.
- `lgh_miles` matches the stop sum on 88% of completed 2025+ legs. Where they
  disagree the stops are usually larger (3,312 legs, by 94 miles on average,
  1,578 at most). The header is larger on 2,689 legs, by 5 on average, 822 of
  which have no stop mileage at all. Both are returned, with `miles_match`.
- **Stops order by `stp_mfh_sequence`.** It matches arrival order on all but one
  of 302k recent stops. `stp_lgh_sequence` looks like the leg-level one, but it
  disagrees with arrival order on 222k stops and repeats within a leg 78,840 times.

Verified: a truck-month's loaded, empty and total miles equal an independent
SQL sum exactly, and order 364820's leg reproduces its stops by hand. That is
391 empty to the pickup plus 128 + 0 + 128 loaded, 647 in total.

`totals` is a second query over **every** matching leg, not the returned page,
and it is identical whatever `limit` is (checked at 5 and 1000 across 2,976
September legs). Aggregate questions are answered from it, so truncation never
shrinks a total.

Leg filters hit `legheader` indexes: `TractorDates`, `ix_lh_prtrl_outst`,
`dk_lgh_carrier_enddate`, `ix_lh_stdt_outstat`, `dk_lgh_enddate`, `uk_status`,
and the two driver indexes, through the same `UNION` as `search_stops`.
`booking_company` reads `lgh_class1`, which equals the booked order's
`revtype1` on 99.9% of legs. `trip_types` reads `lgh_type1` (`LghType1`, "Trip
Type": LOCAL, REG = Deck, REEF, LIVEST, VAN, ...), and both take a code or a name.

Order filters (`_LEG_ORDER_FILTERS`) reach a leg **through its stops**:
`lh.lgh_number IN (SELECT s.lgh_number FROM stops s JOIN orderheader oh ...)`.
`legheader.ord_hdrnumber` names only one order, and a leg can carry several,
so filtering on it would miss legs. Miles on a shared leg are the leg's, not
one order's share. Measured: 50 to 160ms for leg filters, and 370 to 420ms with
an order filter.

### Trucks and Trailers

`search_trucks` (`tractorprofile`, 422 rows) and `search_trailers`
(`trailerprofile`, 1,061) replaced `truck_location` and `get_active_power`.
Neither requires a filter, since the fleets are small. `_EQUIPMENT_MAX_ROWS =
1000`.

**"Active" is not the retire date.** 200 tractors and 664 trailers are
unretired, but only 181 tractors and 530 trailers ran a leg in the last year;
retirements are not kept up. Active therefore means *not retired and used on
a leg within `used_within_days`*, which defaults to 365, the window
`get_active_power` used. 168 trucks ran in the last 30 days. Pass 0 to drop
the usage test.

**Truck fields were checked against the legs** on the 200 unretired units:

| Field | Agreement |
|---|---|
| `trc_status` vs open legs | 197 / 200: USE has a started leg on 98/100, PLN a planned leg on 33/33, AVL nothing open on 66/67 |
| `trc_driver` vs last leg's driver | 152 / 158 |
| `trc_trailer1` vs last leg's trailer | 132 / 133 |
| `trc_teamleader` vs the driver's `mpp_teamleader` | **50 / 74**; set on only 74 trucks |

AVL is not proof a truck is ready: 20 AVL units had not run in 90+ days. So
each truck also returns `current_leg` / `current_leg_status` and `last_used`
from the legs. **Teams belong to drivers**, so `team_leader` is the assigned
driver's `mpp_teamleader`, and `trc_type4` ("Dispatched By") is retired and
ignored. One truck (588) still has a terminated driver assigned;
`driver_terminated` flags it rather than hiding it.

**GPS.** `trc_gps_latitude` / `trc_gps_longitude` / `trc_gps_desc` /
`trc_gps_date` hold the latest check call. They equal the newest `checkcall`
row to the minute for all 170 units pinging in the last 30 days, so location
search reads the profile rather than 2.1M check calls. **Trailers have no
GPS:** check calls come from tractors (`ckc_asgntype` `TRC`) and drivers
(`DRV`) only, and one trailer has a stale stored position. A trailer's
location is its available-at company, else its available-at city, which is
usually where it was last dropped. The result says so in
`near.measured_from`.

**Coordinates are seconds of arc, with longitude positive for WEST**, on
`tractorprofile`, `city` (`cty_latitude` / `cty_longitude`, 98% populated)
and `company` (`cmp_latseconds` / `cmp_longseconds`, 92%). `_deg` converts
and negates. "Near" takes one of a lat/long, `near_city` or `near_company`,
and a radius in miles (default 50). Distance is straight-line haversine,
written by `_miles_from` with the origin as numeric literals: the origin is
always a float from `float()` or the city/company row, so no user text
reaches the SQL, and the expression can sit in both SELECT and WHERE. Checked
against an independent Python haversine: identical to 0.1 mile.

Decoded labels: `TrcStatus`, `TrcType1` (Ownership: `COMPAN`, `O/O`),
`Fleet`, `Terminal`, `TeamLeader`; `TrlStatus`, `TrlType1` (Trailer Type),
`TrlType2` (Length: 53 / 48), `TrlType3` (Ownership), `TrlType4` (`FR`
flatbed, `TW` temperature-controlled, `OE` other). All filters on these take
a code or a name.

**Deliberately not exposed:** purchase cost, cost per mile, insurer, policy
and coverage, owner, and the free-text `trc_misc*` / `trl_misc*` notes (sale
history, the occasional personal remark, and the trailer reefer-unit model).
`trl_len` / `trl_wdth` / `trl_ht` mix feet and inches, so length comes from
`TrlType2`.

**Performance trap: `lgh_primary_pup` has no index.** A trailer usage test
written as `EXISTS ... OR lgh_primary_pup = tr.trl_id`, or as an `IN` / CTE
`UNION` of primary and pup, let the optimiser correlate the pup branch. It
probed `legheader` once per trailer: **27–34s**, against 36ms for the primary
branch alone. The pups used in the window (a handful) are fetched first with
one range seek and inlined as a list, which brings trailer searches to
0.2–0.5s with identical counts. Likewise `last_used` for trailers is the latest
leg *start* (covered by `ix_lh_prtrl_outst`); `MAX(lgh_enddate)` needed a key
lookup per leg and took 2.6s.

### Leg comments and permits

`search_legs` returns `comment` (`lgh_comment`), the dispatcher's note on
about 8% of 2025+ legs: border and ACE notes, hook/split counts, pay
instructions. `comment_contains` is an escaped substring `LIKE`.
`lgh_trc_comment` and `lgh_etacomment` are empty and not exposed.

`permit_required` is `lgh_permitnumbers = 'PERMIT EXIST'`. Despite the
column name, that literal on 3,260 legs since 2025 is a flag, not a list of
numbers; the rest are `''` or NULL. It is mostly on `OD`, `LOCAL` and `DRA`
trip types. `lgh_permit_status` is only ever `UNK`, and the TMW permit module
(`Permits`, `Permit_Requirements`, ...) is empty, so permit numbers, costs and
validity dates do not exist here.

### Reference Numbers

`referencenumber` (1.2M rows) is polymorphic: `ref_table` names the owner and
`ref_tablekey` its key, with `ord_hdrnumber` carried alongside. One order's
paperwork is spread across owners: B/L # on the order, seals and contacts on
stops, PAPS/ACE numbers on freight lines, and copies on invoice lines.
`search_reference_numbers` is the only place to see all of it together.

| `ref_table` | Rows | Exposed as | Owner key |
|---|---|---|---|
| `stops` | 562k | `stop` | `stp_number` |
| `orderheader` | 529k | `order` | `ord_hdrnumber` |
| `invoicedetail` | 72k | `invoice` | `ivd_number` (90% name a `stp_number`) |
| `freightdetail` | 47k | `freight` | `fgt_number` → its stop |
| `paydetail`, `fuelcardrequestlog`, `legheader`, `invoiceheader` | 2.8k | **not exposed** | no order, newest 2023 |

**"Every reference has an order" is 99% true.** Of the four exposed owners,
12.4k rows carry `ord_hdrnumber = 0`, almost all on stops. Those sit on
non-billable stops, such as a B/L on a `DLT` drop or a route point, so they are
returned with a NULL order and their `mov_number` / `lgh_number`. Where the
ref's order disagrees with its owner's, it is 0.02% (113 stops, 6 freight, 87
invoice lines), and the ref's own value is reported.

**`stops.stp_refnum` is only the primary reference.** It matches 293k of the
562k stop references, and order-level references are never on a stop, so
`search_stops(reference_numbers=...)` misses about half. Its docstring points
callers to `search_reference_numbers`.

**Excluded on purpose:** every `TCH*` type (TCH fuel card numbers, card IDs
and money codes, which are spendable credentials, including three money codes
filed against orders) and 15.5k blank numbers. The excluded owner tables also
hold driver pay notes.

Matching: `reference_numbers` is exact and seeks `sk_ref_num` (1ms).
The caller's values are stripped, but SQL Server ignores only *trailing*
spaces, and 1,367 stored numbers have leading ones (mostly free text such as
`' 525 piglets'`). `reference_contains` is a substring `LIKE`, about 0.6s
because no index serves a leading wildcard. It catches those and loosely
formatted numbers. 63 reference types are in `labelfile` `ReferenceNumbers`;
the EDI-loaded ones (`CR`, `CON`, `TEL`, `ST#`, `OID`, `NSH`, ...) are not, so
`reference_type_name` is NULL for them.

Selection is **newest first by `ref_id`**, the clustered key, so a truncated
result keeps the most recently entered references, and a broad filter stops
after a short backward scan. Presentation groups results by order, newest
first, then order → stop → freight → invoice. It uses the same deferred join as
stops and freight. The same number often appears on the order, its stop and
the invoice line; these are copies, and `attached_to` separates them.

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

1. Add a query method to `TmwDB` in `tmw_db.py`, decorated with `@_reconnecting`
   (it opens with `if not self.conn: self.connect()` like the others). Raise
   `ValueError` for bad arguments.
2. Add a `@mcp.tool()` function in `tmw_mcp.py`, decorated with `@_tool_errors`
   directly beneath it, that calls the method
3. Write a clear docstring — this is what the model reads to understand the tool
4. If it needs a tunable value, add it to `Settings` in `config.py` and to `.env.example`
