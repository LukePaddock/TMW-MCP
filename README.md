# TMW MCP Server

An [MCP](https://modelcontextprotocol.io) server that exposes TMW Suite
Transportation Management System data to AI models.

## Requirements

- Python 3.12+ (developed against 3.14)
- ODBC driver for SQL Server — by default `SQL Server Native Client 11.0`
- Network access to the TMW database, and rights to read it

## Setup

```powershell
py -3.14 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

copy .env.example .env   # then edit .env for your environment
```

## Running

```powershell
.venv\Scripts\activate
python tmw_mcp.py
```

The server speaks MCP over stdio, so it is normally launched by a client rather
than run by hand. Example client configuration:

```json
{
  "mcpServers": {
    "tmw": {
      "command": "Z:\\apps\\tmw_mcp\\.venv\\Scripts\\python.exe",
      "args": [
        "Z:\\apps\\tmw_mcp\\tmw_mcp.py"
      ]
    }
  }
}
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
| `TMW_CHECKCALL_LOOKBACK_DAYS` | no | `30` | How far back `truck_location` searches |

Leaving `TMW_DB_USER` and `TMW_DB_PASSWORD` blank uses Windows Authentication
(`Trusted_Connection=yes`). Setting only one of the two is an error.

## Layout

```
tmw_mcp.py         MCP server — tool definitions and server setup
tmw_db.py          Database layer — TmwDB class and all SQL queries
config.py          Environment-backed settings
requirements.txt   Direct dependencies
requirements-lock.txt  Full pinned freeze of the working environment
.env.example       Template for local configuration
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

## Data model

See [CLAUDE.md](CLAUDE.md) for the order / leg / movement hierarchy, status codes,
and stop status fields — the domain knowledge needed to read query results
correctly.
