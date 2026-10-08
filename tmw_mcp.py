import argparse
import ipaddress
import logging
import logging.handlers
import secrets

from mcp.server.mcpserver import MCPServer

from config import LOOPBACK_HOSTS, load_settings
from tmw_db import TmwDB

logger = logging.getLogger("tmw_mcp")

# Separate logger so the auth log holds only failures, in one stable format
# that fail2ban can parse. It does not propagate into the general server log.
auth_logger = logging.getLogger("tmw_mcp.auth")
auth_logger.propagate = False


def client_ip(scope, trusted_proxies) -> str:
    """Best-effort real client IP, trusting forwarded headers only from a proxy.

    `X-Forwarded-For` is client-supplied: nginx APPENDS to whatever arrived, so
    a request carrying `X-Forwarded-For: 8.8.8.8` becomes `8.8.8.8, <real ip>`.
    Reading the FIRST entry would let anyone get an arbitrary address banned,
    so take the LAST, and only when the peer is a configured trusted proxy.
    `X-Real-IP` is preferred because nginx overwrites rather than appends it.

    With no trusted proxies configured, headers are ignored entirely and the
    TCP peer is used - correct for a direct bind, and the safe default.
    """
    peer = scope.get("client")
    peer_ip = peer[0] if peer else ""
    if not peer_ip:
        return "unknown"
    if not trusted_proxies:
        return peer_ip
    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return peer_ip
    if not any(addr in net for net in trusted_proxies):
        # Not a proxy we trust, so its headers are not evidence of anything.
        return peer_ip

    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
    real_ip = headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return peer_ip


def configure_auth_log(path: str) -> None:
    """Send authentication failures to their own rotating file for fail2ban."""
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
    )
    auth_logger.addHandler(handler)
    auth_logger.setLevel(logging.WARNING)

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
def search_drivers(
    name: str | None = None,
    drivers: list[str] | None = None,
    other_ids: list[str] | None = None,
    status: list[str] | None = None,
    active_only: bool = False,
    terminated_only: bool = False,
    trucks: list[str] | None = None,
    team_leaders: list[str] | None = None,
    terminals: list[str] | None = None,
    fleets: list[str] | None = None,
    divisions: list[str] | None = None,
    domiciles: list[str] | None = None,
    companies: list[str] | None = None,
    license_states: list[str] | None = None,
    license_classes: list[str] | None = None,
    cities: list[int] | None = None,
    states: list[str] | None = None,
    hired_after: str | None = None,
    hired_before: str | None = None,
    terminated_after: str | None = None,
    terminated_before: str | None = None,
    available_after: str | None = None,
    available_before: str | None = None,
    trainers: bool = False,
    trainees: bool = False,
    limit: int = 200,
) -> dict:
    """Find drivers by code, name, status, assignment, or licence.

    Unlike the order, stop and freight searches, NO filter is required here —
    the driver table is only 676 rows, so an unfiltered call is a reasonable
    "who are our drivers". For the same reason name matching is a substring
    search, which the bigger tables cannot afford.

    NAME SEARCH. name is split on whitespace and commas, and every token must
    appear in the stored 'LASTNAME,FIRSTNAME'. So "smith", "john smith",
    "smith john" and "smi" all find SMITH,JOHN. Matching is case-insensitive
    and wildcards are taken literally, so a '%' in the name matches a percent
    sign rather than everything.

    STATUS is the field to watch: 472 of 676 drivers are OUT, which in
    DrvStatus means TERMINATED, not "out on the road". Pass active_only=True
    for the current roster (204 drivers), or status=["USE"] for the 97 actually
    on the road right now. The other codes are AVL (available) and PLN
    (planned). Every result carries a plain `terminated` boolean so this is
    hard to misread.

    DATES. hire_date and termination_date come back null where TMW stored a
    placeholder rather than a real date — a current driver has 2049-12-31 on
    file, which is not a real termination. terminated_after / terminated_before
    only match genuine dates, so "who left this year" cannot sweep in the
    active roster.

    The driver_code this returns is the code legheader stores, so it feeds
    straight into search_stops(drivers=[...]) to see where someone has been,
    or truck_plan for what is ahead of them.

    PERSONAL DATA IS NOT AVAILABLE through this tool. The underlying table
    holds social security numbers, dates of birth, licence numbers, home
    addresses and gender markers; none of those are returned or searchable, by
    design. Work contact details (phone, email) and the licence state and class
    are included. If a question needs an excluded field, it has to be answered
    outside this server.

    Returns {"drivers": [...], "count": n, "truncated": bool}. Three rows carry
    no name at all and sort last; none of them appears on a real trip.
    """
    return db.search_drivers(
        name=name,
        limit=limit,
        drivers=drivers,
        other_ids=other_ids,
        status=status,
        active_only=active_only,
        terminated_only=terminated_only,
        trucks=trucks,
        team_leaders=team_leaders,
        terminals=terminals,
        fleets=fleets,
        divisions=divisions,
        domiciles=domiciles,
        companies=companies,
        license_states=license_states,
        license_classes=license_classes,
        cities=cities,
        states=states,
        hired_after=hired_after,
        hired_before=hired_before,
        terminated_after=terminated_after,
        terminated_before=terminated_before,
        available_after=available_after,
        available_before=available_before,
        trainers=trainers,
        trainees=trainees,
    )


@mcp.tool()
def search_stops(
    scope: str = "stop",
    orders: list[int] | None = None,
    legs: list[int] | None = None,
    movements: list[int] | None = None,
    stops: list[int] | None = None,
    stop_types: list[str] | None = None,
    events: list[str] | None = None,
    stop_status: list[str] | None = None,
    departure_status: list[str] | None = None,
    unarrived: bool = False,
    undeparted: bool = False,
    arrived_after: str | None = None,
    arrived_before: str | None = None,
    departed_after: str | None = None,
    departed_before: str | None = None,
    appt_after: str | None = None,
    appt_before: str | None = None,
    appt_latest_after: str | None = None,
    appt_latest_before: str | None = None,
    firm_appt: bool = False,
    late_arrival: bool = False,
    companies: list[str] | None = None,
    cities: list[int] | None = None,
    states: list[str] | None = None,
    zips: list[str] | None = None,
    drivers: list[str] | None = None,
    trucks: list[str] | None = None,
    carriers: list[str] | None = None,
    trailers: list[str] | None = None,
    reference_numbers: list[str] | None = None,
    reference_types: list[str] | None = None,
    billto: list[str] | None = None,
    shipper: list[str] | None = None,
    consignee: list[str] | None = None,
    status: list[str] | None = None,
    invoice_status: list[str] | None = None,
    revtype1: list[str] | None = None,
    revtype2: list[str] | None = None,
    revtype3: list[str] | None = None,
    revtype4: list[str] | None = None,
    started_after: str | None = None,
    started_before: str | None = None,
    completed_after: str | None = None,
    completed_before: str | None = None,
    order_numbers: list[str] | None = None,
    origin_company: list[str] | None = None,
    dest_company: list[str] | None = None,
    origin_city: list[int] | None = None,
    dest_city: list[int] | None = None,
    origin_state: list[str] | None = None,
    dest_state: list[str] | None = None,
    min_charge: float | None = None,
    limit: int = 200,
) -> dict:
    """Search stops by identity, status, date, appointment, location, driver, or truck.

    At least one filter is required. Dates are ISO strings ('2025-06-01') and the
    *_before bounds are exclusive. All list filters match any of the given values.

    SCOPE decides what the matches stand for, and choosing wrong quietly changes
    the answer:

    - scope="stop" (default) returns the matching stops themselves, newest first.
      limit bounds the number of stops.
    - scope="movement" returns EVERY stop on every movement a match belongs to -
      the whole trip, the Trip Folder view. limit bounds MOVEMENTS, not rows.

    Three quarters of movements in this database carry stops from more than one
    order, so scope="movement" normally returns stops belonging to other orders
    too: the co-loaded freight sharing the trailer. Use it to answer "what else
    is on this truck" or "show me the whole trip". Use scope="stop" to answer
    "which stops match these conditions".

    The three lookups this replaces:
      every stop on an order's trips -> search_stops(orders=[...], scope="movement")
      stops on specific legs         -> search_stops(legs=[...])
      stops in specific movements    -> search_stops(movements=[...])

    STOP vs ORDER fields. stop_status and departure_status are the stop's; plain
    status and invoice_status are the ORDER's, as are billto, shipper, consignee,
    revtype1-4 and started_after/before, so you can ask for stops on a customer's
    orders without a second query. stop_status is DNE (arrived), NON or OPN;
    departure_status DNE means the driver has left. unarrived and undeparted are
    the shorthands for "not there yet" and "still there".

    stop_types are PUP (pickup), DRP (delivery) and NONE (an in-transit copy on a
    DLT/HLT/BMT stop - most rows, rarely what you want). events are the finer
    codes: LUL, DLT, HLT, XDU, XDL and so on.

    DATES. arrival_date and departure_date hold actual times once the driver has
    been there and expected times before, so one date range spans history and
    plan. appt_after/appt_before bound the earliest appointment time
    (stp_schdtearliest); appt_latest_* bound the late end of the window.
    late_arrival=True keeps only stops that arrived after their window closed.

    cities are numeric city codes from find_city_codes, not names. states are
    two-letter. companies are cmp_id facility codes. These describe where THIS
    STOP is; origin_city / dest_state / origin_company and the rest describe the
    ORDER's endpoints, so stops=TX with origin_state=IL finds the Texas stops of
    loads that started in Illinois.

    drivers matches either seat, so a team driver's stops are found whichever
    seat they held. reference_numbers searches customer paperwork - pair it with
    reference_types, where the common types are 'B/L #', 'LOAD #', 'P/U #',
    'REF' and 'CUSBRK'.

    Two filters cannot use an index and scan 1.6M rows if used alone - trailers
    and late_arrival. Pair either with a date or status filter.

    Stops with ord_hdrnumber 0 are not an order: they are empty equipment moves
    (BMT, DMT, DLT, HLT, RTP) on a movement's empty legs, and scope="movement"
    includes them as trip context with ord_number and order_status NULL.

    Returns {"stops": [...], "count": n, "movements": n, "truncated": bool,
    "scope": str}, ordered by trip: movements by their first arrival, then
    chronologically within each. When truncated is true more matched than were
    returned - narrow the filters or raise limit rather than treating the result
    as complete.
    """
    return db.search_stops(
        scope=scope,
        limit=limit,
        orders=orders,
        legs=legs,
        movements=movements,
        stops=stops,
        stop_types=stop_types,
        events=events,
        stop_status=stop_status,
        departure_status=departure_status,
        unarrived=unarrived,
        undeparted=undeparted,
        arrived_after=arrived_after,
        arrived_before=arrived_before,
        departed_after=departed_after,
        departed_before=departed_before,
        appt_after=appt_after,
        appt_before=appt_before,
        appt_latest_after=appt_latest_after,
        appt_latest_before=appt_latest_before,
        firm_appt=firm_appt,
        late_arrival=late_arrival,
        companies=companies,
        cities=cities,
        states=states,
        zips=zips,
        drivers=drivers,
        trucks=trucks,
        carriers=carriers,
        trailers=trailers,
        reference_numbers=reference_numbers,
        reference_types=reference_types,
        billto=billto,
        shipper=shipper,
        consignee=consignee,
        status=status,
        invoice_status=invoice_status,
        revtype1=revtype1,
        revtype2=revtype2,
        revtype3=revtype3,
        revtype4=revtype4,
        started_after=started_after,
        started_before=started_before,
        completed_after=completed_after,
        completed_before=completed_before,
        order_numbers=order_numbers,
        origin_company=origin_company,
        dest_company=dest_company,
        origin_city=origin_city,
        dest_city=dest_city,
        origin_state=origin_state,
        dest_state=dest_state,
        min_charge=min_charge,
    )


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


@mcp.tool()
def get_order_freight(orders: list[str], stop_type: str = "DRP") -> list[dict]:
    """Get the freight lines (commodity, weight, count, temperature, dimensions) for orders.

    The same freight is written onto pickup, delivery and in-transit stops. On an
    order with more than one pickup or delivery those copies can disagree, and the
    DRP (delivery) copies are the authoritative record — so DRP is the default.

    stop_type: "DRP" (deliveries, the default and the source of truth), "PUP"
    (pickups), "NONE" (in-transit stops), or "ANY" for every copy.

    Use summarize_order_freight instead when you want per-order totals rather than
    individual lines.
    """
    return db.get_order_freight(orders, stop_type)


@mcp.tool()
def summarize_order_freight(orders: list[str]) -> list[dict]:
    """Get per-order freight totals, with the pickup figures alongside for comparison.

    Returns one row per order: total weight, pieces, temperature range, largest
    dimensions and the commodity codes, all taken from the authoritative DRP
    (delivery) stops.

    Every measure is normalised before aggregating: weight in POUNDS, dimensions
    in INCHES, temperatures in FAHRENHEIT. A MIN across mixed C and F rows, or a
    MAX across FET and INS, would otherwise be meaningless. mixed_weight_units
    says whether more than one weight unit contributed. Counts
    are NOT converted, and mixed_count_units true means `pieces` adds unlike units
    (PCS, PLT, COIL), so report it as unreliable rather than as a total.

    Also returns pup_weight and pup_pieces from the pickup stops and an in_sync
    flag. When in_sync is false the pickup and delivery records disagree, which
    happens on orders with several pickups or deliveries — report the DRP figure
    as the real one, and mention the discrepancy rather than hiding it.

    Pair this with search_orders: search first, then pass the returned
    ord_hdrnumber values here to attach freight to them.
    """
    return db.summarize_order_freight(orders)


@mcp.tool()
def search_freight(
    stop_type: str = "DRP",
    weight_basis: str = "LBS",
    dimension_basis: str = "INS",
    temp_basis: str = "F",
    weight_unit: list[str] | None = None,
    count_unit: list[str] | None = None,
    dimension_unit: list[str] | None = None,
    temp_unit: list[str] | None = None,
    commodities: list[str] | None = None,
    commodity_class: list[str] | None = None,
    description: str | None = None,
    orders: list[str] | None = None,
    stops: list[str] | None = None,
    freight_numbers: list[str] | None = None,
    min_weight: float | None = None,
    max_weight: float | None = None,
    min_count: float | None = None,
    temp_controlled: bool | None = None,
    min_temp: float | None = None,
    max_temp: float | None = None,
    has_dimensions: bool | None = None,
    min_length: float | None = None,
    min_width: float | None = None,
    min_height: float | None = None,
    started_after: str | None = None,
    started_before: str | None = None,
    completed_after: str | None = None,
    completed_before: str | None = None,
    billto: list[str] | None = None,
    shipper: list[str] | None = None,
    consignee: list[str] | None = None,
    status: list[str] | None = None,
    revtype1: list[str] | None = None,
    revtype2: list[str] | None = None,
    origin_state: list[str] | None = None,
    dest_state: list[str] | None = None,
    origin_city: list[int] | None = None,
    dest_city: list[int] | None = None,
    limit: int = 200,
) -> dict:
    """Search freight lines by commodity, weight, temperature, dimensions, and order.

    At least one filter is required. Order-level filters (dates, billto, shipper,
    consignee, status, revenue types, origin and destination) work here too, so
    "oversize freight for this customer last quarter" is a single call.

    stop_type defaults to "DRP" — the delivery copies, which are the authoritative
    record. Use "PUP" for pickups, "NONE" for in-transit stops, or "ANY" for all.
    Most freight rows sit on in-transit stops, so searching "ANY" returns several
    copies of the same freight.

    temp_controlled true returns only freight with a temperature set; has_dimensions
    true returns only freight with length, width or height recorded (oversize loads).
    description matches with SQL LIKE, so wrap it in % wildcards. It is a freeform
    user-editable field — filter on commodities for anything reliable.

    WEIGHT AND UNITS. Weight is stored per row with its own unit. min_weight and
    max_weight are compared after normalising every row to pounds, and weight_basis
    says which unit your threshold is in: min_weight=10000 with weight_basis="KGS"
    means 10,000 kg and matches a 22,046 lb row. Only KGS is converted; TON and MTN
    are mislabelled pounds in this data and are treated as pounds. Each result
    carries the stored weight with its weight_unit, plus weight_lbs.

    Use weight_unit to restrict to rows stored in a given unit instead, e.g.
    weight_unit=["KGS"].

    DIMENSIONS. min_length / min_width / min_height compare after normalising
    every row to inches, and dimension_basis says which unit your threshold is in:
    "INS", "FET", "MTR", "YRD" or "CM". min_length=40 with dimension_basis="FET"
    means 40 feet and matches a 480 inch row. Length, width and height each carry
    their own unit, and each is converted against its own. The unit labels here are
    reliable - 97% of FET lengths are 60 or under, so they really are feet - and the
    undocumented unit "N" behaves as inches. Results carry length_in, width_in and
    height_in beside the stored values. Use dimension_unit to scope by stored unit.

    TEMPERATURE. min_temp / max_temp compare after normalising to Fahrenheit, and
    temp_basis is "F" or "C". min_temp=0 with temp_basis="C" means freezing, not
    0 F, and matches rows at 32 F or above. Results carry low_temp_f and
    high_temp_f. Use temp_unit to scope by stored unit.

    COUNTS ARE NOT CONVERTED. PCS, PLT, COIL and CAS have no fixed ratio, so a
    min_count threshold spans unlike units. Pair it with count_unit, e.g.
    count_unit=["PLT"], for a comparison that means something.

    The stored weights contain bad data: 87 rows exceed 1,000,000 lbs and the
    largest is 505 billion. Treat extreme values as suspect rather than real.

    Returns {"freight": [...], "count": n, "truncated": bool, "stop_type": str}
    plus the basis actually used for each measure. When truncated is true, more
    rows matched than were returned.
    """
    return db.search_freight(
        stop_type=stop_type,
        weight_basis=weight_basis,
        dimension_basis=dimension_basis,
        temp_basis=temp_basis,
        limit=limit,
        weight_unit=weight_unit,
        count_unit=count_unit,
        dimension_unit=dimension_unit,
        temp_unit=temp_unit,
        commodities=commodities,
        commodity_class=commodity_class,
        description=description,
        orders=orders,
        stops=stops,
        freight_numbers=freight_numbers,
        min_weight=min_weight,
        max_weight=max_weight,
        min_count=min_count,
        temp_controlled=temp_controlled,
        min_temp=min_temp,
        max_temp=max_temp,
        has_dimensions=has_dimensions,
        min_length=min_length,
        min_width=min_width,
        min_height=min_height,
        started_after=started_after,
        started_before=started_before,
        completed_after=completed_after,
        completed_before=completed_before,
        billto=billto,
        shipper=shipper,
        consignee=consignee,
        status=status,
        revtype1=revtype1,
        revtype2=revtype2,
        origin_state=origin_state,
        dest_state=dest_state,
        origin_city=origin_city,
        dest_city=dest_city,
    )

class BearerAuthMiddleware:
    """Require `Authorization: Bearer <key>` on every HTTP request.

    Keys are labelled in TMW_MCP_API_KEYS so the log names who called. Each
    tester gets their own key: one can then be revoked without rotating the
    others, and the access log attributes requests to a person.
    """

    def __init__(self, app, api_keys: dict[str, str], trusted_proxies=()):
        self.app = app
        # Reverse the mapping — lookup is by presented secret, not by label.
        self._by_secret = {secret: label for label, secret in api_keys.items()}
        self.trusted_proxies = trusted_proxies

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
            ip = client_ip(scope, self.trusted_proxies)
            reason = "no_key" if not token else "invalid_key"
            # One stable line per failure, with the IP in a fixed position, so
            # a fail2ban <HOST> capture stays valid as other fields change.
            auth_logger.warning(
                "authentication failed from %s reason=%s path=%s",
                ip, reason, scope.get("path"),
            )
            logger.warning("rejected request from %s (%s)", ip, reason)
            from starlette.responses import JSONResponse

            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        logger.info(
            "authenticated %s from %s -> %s",
            label, client_ip(scope, self.trusted_proxies), scope.get("path"),
        )
        scope.setdefault("state", {})["api_key_label"] = label
        await self.app(scope, receive, send)


def run_http(settings) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    if settings.auth_log:
        configure_auth_log(settings.auth_log)
        logger.info("auth failures logged to %s", settings.auth_log)
        if not settings.trusted_proxies:
            logger.warning(
                "TMW_MCP_AUTH_LOG is set but TMW_MCP_TRUSTED_PROXIES is empty — "
                "behind a reverse proxy the logged address will be the proxy, "
                "not the client, and fail2ban would ban the proxy."
            )

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
        app = BearerAuthMiddleware(app, settings.api_keys, settings.trusted_proxies)
        logger.info(
            "bearer auth enabled for %d key(s): %s",
            len(settings.api_keys),
            ", ".join(sorted(settings.api_keys)),
        )
    else:
        logger.warning("no API keys configured — the server is unauthenticated")

    # proxy_headers=False is deliberate. Uvicorn ships its own forwarded-header
    # handling, trusting 127.0.0.1 by default and rewriting scope["client"] from
    # X-Forwarded-For before any middleware runs. That is a second, looser trust
    # model layered under ours, and it wins because it runs first. Turning it off
    # makes TMW_MCP_TRUSTED_PROXIES the single source of truth.
    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        proxy_headers=False,
    )


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