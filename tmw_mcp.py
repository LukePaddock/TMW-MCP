import argparse
import logging
import secrets

from mcp.server.mcpserver import MCPServer

from config import LOOPBACK_HOSTS, load_settings
from tmw_db import TmwDB

logger = logging.getLogger("tmw_mcp")

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


@mcp.tool()
def find_city_codes(name: str, state: str | None = None) -> list[dict]:
    """Resolve a city name to the numeric city codes TMW stores on orders.

    Order origin/destination cities are integer codes, not names, so call this
    first and pass the returned cty_code values to search_orders as
    origin_city / dest_city. Matches on the start of the city name; pass a
    two-letter state to narrow it.
    """
    return db.resolve_cities(name, state)


@mcp.tool()
def search_orders(
    started_after: str | None = None,
    started_before: str | None = None,
    completed_after: str | None = None,
    completed_before: str | None = None,
    billto: list[str] | None = None,
    shipper: list[str] | None = None,
    consignee: list[str] | None = None,
    status: list[str] | None = None,
    invoice_status: list[str] | None = None,
    revtype1: list[str] | None = None,
    revtype2: list[str] | None = None,
    revtype3: list[str] | None = None,
    revtype4: list[str] | None = None,
    origin_company: list[str] | None = None,
    dest_company: list[str] | None = None,
    origin_city: list[int] | None = None,
    dest_city: list[int] | None = None,
    origin_state: list[str] | None = None,
    dest_state: list[str] | None = None,
    min_charge: float | None = None,
    orders: list[int] | None = None,
    order_numbers: list[str] | None = None,
    limit: int = 200,
) -> dict:
    """Search orders by any combination of date, customer, location, and revenue type.

    At least one filter is required. Dates are ISO strings ('2025-06-01') and the
    *_before bounds are exclusive. All list filters match any of the given values.

    Locations come in three precisions: origin_company / dest_company are company
    IDs (the exact facility), origin_city / dest_city are numeric city codes from
    find_city_codes, and origin_state / dest_state are two-letter states.

    Order statuses here are CMP (completed), CAN (cancelled), QTE (quote), AVL,
    PLN, STD, PND — not the AVL/PLN/STD/DNE set used for legs and movements.

    Returns {"orders": [...], "count": n, "truncated": bool}. When truncated is
    true, more orders matched than were returned — narrow the filters or raise
    limit rather than treating the result as complete. Use summarize_orders
    instead of paging through everything to answer totals.
    """
    return db.search_orders(
        limit=limit,
        started_after=started_after,
        started_before=started_before,
        completed_after=completed_after,
        completed_before=completed_before,
        billto=billto,
        shipper=shipper,
        consignee=consignee,
        status=status,
        invoice_status=invoice_status,
        revtype1=revtype1,
        revtype2=revtype2,
        revtype3=revtype3,
        revtype4=revtype4,
        origin_company=origin_company,
        dest_company=dest_company,
        origin_city=origin_city,
        dest_city=dest_city,
        origin_state=origin_state,
        dest_state=dest_state,
        min_charge=min_charge,
        orders=orders,
        order_numbers=order_numbers,
    )


@mcp.tool()
def summarize_orders(
    group_by: str,
    started_after: str | None = None,
    started_before: str | None = None,
    completed_after: str | None = None,
    completed_before: str | None = None,
    billto: list[str] | None = None,
    shipper: list[str] | None = None,
    consignee: list[str] | None = None,
    status: list[str] | None = None,
    invoice_status: list[str] | None = None,
    revtype1: list[str] | None = None,
    revtype2: list[str] | None = None,
    revtype3: list[str] | None = None,
    revtype4: list[str] | None = None,
    origin_company: list[str] | None = None,
    dest_company: list[str] | None = None,
    origin_city: list[int] | None = None,
    dest_city: list[int] | None = None,
    origin_state: list[str] | None = None,
    dest_state: list[str] | None = None,
    min_charge: float | None = None,
    limit: int = 200,
) -> dict:
    """Aggregate orders into totals instead of listing them row by row.

    Takes the same filters as search_orders plus a required group_by, one of:
    revtype1, revtype2, revtype3, revtype4, status, invoice_status, billto,
    shipper, consignee, origin_city, dest_city, origin_state, dest_state,
    month, year.

    Each group returns order_count, total_charge, total_miles, total_weight,
    and rev_per_mile, sorted by total_charge descending. Prefer this over
    search_orders for any "how much / how many / which is biggest" question —
    it answers in a handful of rows what would otherwise take thousands.
    """
    return db.summarize_orders(
        group_by=group_by,
        limit=limit,
        started_after=started_after,
        started_before=started_before,
        completed_after=completed_after,
        completed_before=completed_before,
        billto=billto,
        shipper=shipper,
        consignee=consignee,
        status=status,
        invoice_status=invoice_status,
        revtype1=revtype1,
        revtype2=revtype2,
        revtype3=revtype3,
        revtype4=revtype4,
        origin_company=origin_company,
        dest_company=dest_company,
        origin_city=origin_city,
        dest_city=dest_city,
        origin_state=origin_state,
        dest_state=dest_state,
        min_charge=min_charge,
    )


class BearerAuthMiddleware:
    """Require `Authorization: Bearer <key>` on every HTTP request.

    Keys are labelled in TMW_MCP_API_KEYS so the log names who called. Each
    tester gets their own key: one can then be revoked without rotating the
    others, and the access log attributes requests to a person.
    """

    def __init__(self, app, api_keys: dict[str, str]):
        self.app = app
        # Reverse the mapping — lookup is by presented secret, not by label.
        self._by_secret = {secret: label for label, secret in api_keys.items()}

    def _label_for(self, token: str) -> str | None:
        # compare_digest against every key so a wrong key costs the same time
        # as a right one, whatever its position.
        match = None
        for secret, label in self._by_secret.items():
            if secrets.compare_digest(token, secret):
                match = label
        return match

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        token = header[7:] if header[:7].lower() == "bearer " else ""
        label = self._label_for(token) if token else None

        if label is None:
            client = scope.get("client")
            logger.warning(
                "rejected unauthenticated request to %s from %s",
                scope.get("path"),
                client[0] if client else "unknown",
            )
            from starlette.responses import JSONResponse

            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        logger.info("authenticated %s -> %s", label, scope.get("path"))
        scope.setdefault("state", {})["api_key_label"] = label
        await self.app(scope, receive, send)


def run_http(settings) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    if settings.mcp_host not in LOOPBACK_HOSTS and not settings.mcp_allowed_hosts:
        logger.warning(
            "TMW_MCP_ALLOWED_HOSTS is empty while bound to %s — DNS rebinding "
            "protection is off. Set it to the hostname clients connect to.",
            settings.mcp_host,
        )

    transport_security = None
    if settings.mcp_allowed_hosts or settings.mcp_allowed_origins:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.mcp_allowed_hosts,
            allowed_origins=settings.mcp_allowed_origins,
        )

    app = mcp.streamable_http_app(
        host=settings.mcp_host,
        transport_security=transport_security,
    )
    if settings.api_keys:
        app = BearerAuthMiddleware(app, settings.api_keys)
        logger.info(
            "bearer auth enabled for %d key(s): %s",
            len(settings.api_keys),
            ", ".join(sorted(settings.api_keys)),
        )
    else:
        logger.warning("no API keys configured — the server is unauthenticated")

    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port)


def main() -> None:
    parser = argparse.ArgumentParser(description="TMW MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio for a local client (default), http to serve over the network",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run()
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    run_http(settings)


if __name__ == "__main__":
    main()