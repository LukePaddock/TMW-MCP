from mcp.server.mcpserver import MCPServer

from config import load_settings
from tmw_db import TmwDB

mcp = MCPServer("TMW MCP", instructions="""
You are connected to a TMW Transportation Management System (TMS) database.

## Core Data Model

**Order**: The customer-facing shipment request. An order consists of at least one leg, but may span
multiple legs (when the tractor changes mid-trip) and multiple movements (when the load is cross-docked).

**Leg (lgh_number)**: A series of at least two stops assigned to a single tractor. A leg belongs to
exactly one movement. A single leg can contain stops for multiple orders.

**Movement (mov_number)**: A series of legs where the loaded trailer remains consistent throughout,
even as the tractor changes between legs. The tractor can only change via a Park/Hook: the outgoing
leg ends with a DLT (Drop Loaded Trailer) stop and the incoming leg begins with an HLT (Hook Loaded
Trailer) stop. Exception: empty legs may be added to the beginning or end of a movement with any
tractor or trailer. Dispatchers view one movement at a time in the Trip Folder.

**Cross-Dock (XDU / XDL)**: When a load is transferred between trailers at a facility, the order
spans multiple movements. The outgoing leg ends with an XDU (Cross-Dock Unload) and the next
movement begins with an XDL (Cross-Dock Load).

## Stop Status Fields
- `stp_status = 'DNE'`: the driver has arrived at the stop.
- `stp_departure_status = 'DNE'`: the driver has departed the stop.
- Arrival/departure dates reflect actual times if the driver has been there, or expected times if not yet.

## Leg / Movement Statuses
- `AVL` — Available (unassigned)
- `PLN` — Planned (assigned, not yet started)
- `STD` — Started (in progress)
- `DNE` — Done (completed)
""")
settings = load_settings()
db = TmwDB.from_settings(settings)


@mcp.tool()
def truck_location(trucks: list[str]) -> list[dict]:
    """Get the latest reported location for one or more trucks by their IDs."""
    return db.get_truck_location(trucks)


@mcp.tool()
def get_active_legs() -> list[dict]:
    """Get all active legs in the system with status Available (AVL), Planned (PLN), or Started (STD).
    Includes tractor, trailer, carrier, start/end city, mileage, and a comma-separated list of associated orders.
    Results are large — always filter via Python before returning to context.
    """
    return db.get_active_legs()


@mcp.tool()
def get_leg_stops(legs: list[str]) -> list[dict]:
    """Get all stops for one or more legs (lgh_number)."""
    return db.get_leg_stops(legs)


@mcp.tool()
def get_movement_stops(movements: list[str]) -> list[dict]:
    """Get all stops across all legs within one or more movements (mov_number)."""
    return db.get_movement_stops(movements)


@mcp.tool()
def get_order_stops(orders: list[str]) -> list[dict]:
    """Get all stops across every movement and leg associated with one or more orders.
    Returns the full stop sequence including all movements an order is part of, not just direct stops.
    """
    return db.get_order_stops(orders)


@mcp.tool()
def get_active_power() -> list[dict]:
    """Get all active trucks with their current driver and dispatch team leader.
    Only includes non-retired tractors with active (non-terminated) drivers.
    """
    return db.get_active_power()


@mcp.tool()
def truck_plan(trucks: list[str]) -> list[dict]:
    """Get all active (Planned/Started) trips and their stop sequences for one or more trucks."""
    return db.get_truck_plan(trucks)


if __name__ == "__main__":
    mcp.run()