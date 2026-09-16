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
├── .env.example            # Template for local configuration
└── .venv/                  # Python virtual environment (gitignored)
```

## Running the Server

```
z:\apps\tmw_mcp\.venv\Scripts\activate
python tmw_mcp.py
```

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
| `TMW_CHECKCALL_LOOKBACK_DAYS` | no | `30` | How far back `truck_location` searches |

Never hardcode connection details — add a setting to `config.py` and `.env.example` instead.

Connection is lazy — `TmwDB` connects on the first query and reuses the connection. Call
`db.close()` to tear it down explicitly. Build it with `TmwDB.from_settings(load_settings())`.

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

## Database Layer (`tmw_db.py`)

### Key Patterns

- All multi-value filters use parameterized `IN (?, ?, ...)` placeholders — never string interpolation for user values
- `_stop_row_to_dict(row)` — shared mapper used by `get_leg_stops`, `get_movement_stops`, and `get_order_stops`
- `_fetch_all_objects(cursor, cls)` — generic row-to-object mapper for dataclass-style models

### Adding a New Tool

1. Add a query method to `TmwDB` in `tmw_db.py`
2. Add a `@mcp.tool()` function in `tmw_mcp.py` that calls it
3. Write a clear docstring — this is what the model reads to understand the tool
4. If it needs a tunable value, add it to `Settings` in `config.py` and to `.env.example`
