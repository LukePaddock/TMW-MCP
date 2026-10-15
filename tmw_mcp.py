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
You are connected to a TMW Transportation Management System (TMS) database for a trucking group
whose four booking companies bill in Canadian and US dollars. Read this before answering: several fields here do not mean what their names suggest, and the
tools are built around those traps.

## Which tool answers what

| Question | Tool |
|---|---|
| Find orders; an order's rate or revenue | `search_orders` |
| Revenue / volume totals ("how much", "biggest", "by month") | `summarize_orders` - never sum search rows |
| Invoices, credits, rebills, what was billed | `search_invoice` |
| Individual charges (linehaul, fuel, accessorials, tax), billed or not yet billed | `search_invoice_details` |
| Miles - total, loaded, empty, bobtail; active legs | `search_legs` |
| Stops, appointments, where a trip went, what else is on the truck | `search_stops` |
| Any reference number (B/L, PO, PAPS, seal, load #) | `search_reference_numbers` |
| Freight - commodity, weight, temperature, dimensions | `search_freight`, `get_order_freight`, `summarize_order_freight` |
| Drivers | `search_drivers` |
| Truck GPS position / upcoming plan | `truck_location`, `truck_plan` |
| Active trucks with driver and team leader | `get_active_power` |
| City name -> code | `find_city_codes` |
| Booking agents / booking companies | `list_booking_agents`, `list_booking_companies` |

Conventions shared by the search tools: dates are ISO strings ('2026-06-01') and every *_before bound
is exclusive. List filters match any of the values given. At least one filter is required (except
`search_drivers`). `truncated: true` means more matched than were returned - narrow the filters or
use the tool's totals; never present a truncated list as complete. Where a tool returns `totals` or
`totals_by_currency`, those cover EVERY match, not just the returned rows.

## Core data model

**Order** (`ord_hdrnumber`; `ord_number` is the same number as text): the customer's shipment
request. At least one leg; several when the tractor changes mid-trip, several movements when
cross-docked.

**Leg** (`lgh_number`): two or more stops assigned to one tractor. Belongs to exactly one movement;
one leg can carry stops for several orders.

**Movement** (`mov_number`): legs sharing one loaded trailer, the Trip Folder view. The tractor
changes only via Park/Hook - the outgoing leg ends with DLT (Drop Loaded Trailer), the next begins
with HLT (Hook Loaded Trailer). Empty legs may sit at either end with any equipment.

**Cross-dock** (XDU / XDL): a load moves between trailers, so the order spans movements.

**Stops with ord_hdrnumber 0** belong to no order: they are **non-billable stops**. Some are empty
equipment moves (DMT drop empty, BMT begin empty, HMT hook empty), but many are loaded - DLT / HLT
are by definition the drop and hook of a LOADED trailer at a Park/Hook, and route points (RTP),
inspections and scales are mostly loaded too. Never infer a load status from a missing order; read
the stop's own event and load status.

## Status codes - three different vocabularies

- **Order** `status`: CMP completed, AVL, PLN, STD, CAN cancelled, ICO cancelled-but-billable, QTE
  quote, MST master template, PND.
- **Leg** `lgh_outstatus`: AVL available, PLN planned, DSP dispatched, STD started, CMP completed,
  CAN cancelled. Completed legs are CMP, never DNE.
- **Stop**: `stp_status = 'DNE'` means arrived, `stp_departure_status = 'DNE'` means departed.
  Arrival/departure dates are actual once the driver has been there, expected before.
- **Order invoice status** (`invoice_status` on orders): PPD prepared (an invoice exists), AVL
  available for invoicing, PND pending (load not finished), XIN do not invoice.
- **Invoice** status: XFR transferred to accounting, PRN printed, RTP ready to print, HLD / HLA on
  hold, CAN cancelled.

## Money - read this before quoting any figure

**Revenue is not `ord_totalcharge`.** That field is 0 on 42% of invoiced orders (those customers are
rated only at invoicing), keeps the original amount after a billing correction, and holds a few typo
values in the millions. Every money figure these tools report as `revenue` follows one rule:

1. The order has live invoices -> the net of all of them (original + credits + rebills +
   supplementals), before tax. This is what was actually billed.
2. Otherwise, the order is cancelled, a quote or a master template -> 0.
3. Otherwise, pending billing lines (rated, not yet invoiced) -> their sum.
4. Otherwise, it was invoiced but every invoice was cancelled -> 0.
5. Otherwise -> `ord_totalcharge`, flagged `revenue_source: "order"`.

`revenue_source` says which step applied (invoiced / pending / not_billable / invoice_cancelled /
order). `order_total_charge` is shown beside revenue for comparison only - quote `revenue`.

**Tax is excluded from revenue** and reported separately (GST / HST). Invoice `total` includes tax;
`pretax_total` does not.

**Billing corrections.** A wrong invoice is never edited. It is reversed by a credit memo (CRD,
negative, `applies_to` names the invoice it reverses) and replaced by a rebill (RBIL); extra charges
later go on a supplemental (SUPL). Invoice numbers share the order number plus a letter: 346778A,
346778B, 346778C. Sum the chain for the final amount; `corrected` / `supplemental` flags on orders
say a chain exists. Cancelled (CAN) invoices count toward nothing and are hidden by default.

**Currency.** The database mixes Canadian and US dollars (about 84% / 16%) and money is only
additive within one currency. Every money figure carries its currency (CAD, USD or UNK = not
recorded). Never add CAD and USD, and never convert - the stored exchange rates are years stale.
`summarize_orders` therefore splits every group by currency: one logical group can come back as
several rows. Revenue currency comes from the invoice (a correction may rebill in the other
currency), else the order. Wherever a tool offers `currency` or `min_charge` filters, they test
this revenue currency and revenue amount, not the order's stored values.

**Payment** (paid / unpaid / overdue) is not tracked in TMW and cannot be answered here.

## Revenue types and booking

- `revtype1` = **booking company**, the sub-company that booked the load: EPT (Earl Paddock
  Transpo), JSV (J Syvret), TSC (Trafalgar), PKS (Packers); CFS (EP Logistics) retired. Use the
  `booking_company` filter / group_by. Not the same as `companies`, `origin_company`,
  `dest_company`, which are customer facility IDs (cmp_id).
- `revtype2` = Region: LOCAL, INBOUN (Inbound), OUTBOU (Outbound).
- `revtype3` = Taxable: YES, NO.
- `revtype4` = **booking agent**, the person who booked the load. Use `booking_agent`. Older orders
  may hold a retired load-class code here (LEGAL, VAN, WIDTH ...) which is not an agent.
- `booking_company`, `booking_agent` and `trip_types` accept a code or a name (["JENN"] or
  ["Jennifer"]). Codes are truncated to six characters. UNK means not recorded.

## Mileage

`search_legs` sums each leg's stops: every stop records the distance driven to it and whether the
truck was loaded on the way. Miles split into loaded, empty and bobtail (tractor with no trailer -
report it separately, neither loaded nor empty). The leg header's own figure is `header_miles` and
disagrees on about 12% of legs; report `miles`.

## Places, people, equipment

- City filters take numeric codes from `find_city_codes`, not names. States are two-letter.
  Companies are cmp_id facility codes.
- Driver `status` OUT means **terminated**, not "out on the road" (USE = on the road). Use
  `active_only` for current drivers. Driver personal data (SSN, birth date, licence number, home
  address) is not available through any tool.
- `drivers` filters match either seat of a team.

## Freight

Freight is copied onto pickup, delivery and in-transit stops; the delivery (DRP) copy is
authoritative and the default. Weights are normalised to pounds, dimensions to inches, temperatures
to Fahrenheit; piece counts are not convertible across units (PCS, PLT, COIL). Stored weights contain
bad values in the millions - treat extremes as suspect.

## Reference numbers

`search_reference_numbers` searches every reference on orders, stops, freight and invoice lines.
`search_stops(reference_numbers=...)` sees only each stop's primary reference - about half.
The same number on an order, its stop and its invoice line is a copy, not a separate load.
""")
settings = load_settings()
db = TmwDB.from_settings(settings)


@mcp.tool()
def truck_location(trucks: list[str]) -> list[dict]:
    """Get the latest reported location for one or more trucks by their IDs."""
    return db.get_truck_location(trucks)


@mcp.tool()
def search_legs(
    active_only: bool = False,
    leg_status: list[str] | None = None,
    legs: list[int] | None = None,
    movements: list[int] | None = None,
    trucks: list[str] | None = None,
    trailers: list[str] | None = None,
    carriers: list[str] | None = None,
    drivers: list[str] | None = None,
    booking_company: list[str] | None = None,
    trip_types: list[str] | None = None,
    started_after: str | None = None,
    started_before: str | None = None,
    ended_after: str | None = None,
    ended_before: str | None = None,
    start_companies: list[str] | None = None,
    end_companies: list[str] | None = None,
    start_cities: list[int] | None = None,
    end_cities: list[int] | None = None,
    start_states: list[str] | None = None,
    end_states: list[str] | None = None,
    orders: list[int] | None = None,
    order_numbers: list[str] | None = None,
    billto: list[str] | None = None,
    shipper: list[str] | None = None,
    consignee: list[str] | None = None,
    booking_agent: list[str] | None = None,
    limit: int = 200,
) -> dict:
    """Search legs and get their mileage — total, loaded, empty and bobtail.

    The tool for any mileage question: "how many empty miles did truck 101 run
    last month", "loaded percentage for this driver", "what's active right
    now". At least one filter is required. Dates are ISO strings and the
    *_before bounds are exclusive.

    MILES ARE SUMMED FROM THE STOPS, not taken from the leg header. Each stop
    records the distance driven to it and whether the truck was loaded on the
    way, so every leg splits into loaded_miles, empty_miles and bobtail_miles
    (tractor with no trailer — neither loaded nor pulling an empty, so report
    it separately rather than folding it into either), plus a small
    unclassified remainder. loaded_pct is loaded / total.
    header_miles is the leg header's own figure and miles_match says whether it
    agrees; they differ on about 12% of legs, usually with the stops higher.
    Report `miles`.

    TOTALS. `totals` sums every matching leg, not just the ones returned, so
    use it for any aggregate — it stays correct when truncated is true. Rows
    come back newest start date first.

    STATUS. active_only=True gives every leg still in play (AVL, PLN, DSP,
    STD) — this replaces the old get_active_legs. leg_status takes the codes
    directly: AVL available, PLN planned, DSP dispatched, STD started, CMP
    completed, CAN cancelled. Completed is CMP, not DNE. Most AVL legs are
    unassigned loads with no truck yet, and a handful are years old.

    WHO AND WHAT. trucks, trailers (primary trailer) and carriers are the IDs
    on the leg. drivers matches either seat. booking_company takes the code or
    the name (EPT / "Earl Paddock Transpo"). trip_types is the leg's Trip Type,
    code or name: LOCAL, REG (Deck), REEF (Reefer), LIVEST (Livestock), VAN,
    OD, DRA, DRB and others; each row carries trip_type_name.

    WHERE. start_/end_companies are cmp_id facility codes, start_/end_cities
    numeric codes from find_city_codes, start_/end_states two-letter codes.

    ORDERS. orders, order_numbers, billto, shipper, consignee and
    booking_agent match legs that carry a stop of a matching order. A leg can
    carry several orders and an order can span several legs, so each row lists
    every order on it in `orders`. Miles on a shared leg are the leg's, not
    one order's share.

    Returns {"legs": [...], "count": n, "truncated": bool, "totals": {...}}.
    limit can go up to 1000. Use search_stops(legs=[...]) to see the stops
    behind a leg's miles.
    """
    return db.search_legs(
        limit=limit,
        active_only=active_only,
        leg_status=leg_status,
        legs=legs,
        movements=movements,
        trucks=trucks,
        trailers=trailers,
        carriers=carriers,
        drivers=drivers,
        booking_company=booking_company,
        trip_types=trip_types,
        started_after=started_after,
        started_before=started_before,
        ended_after=ended_after,
        ended_before=ended_before,
        start_companies=start_companies,
        end_companies=end_companies,
        start_cities=start_cities,
        end_cities=end_cities,
        start_states=start_states,
        end_states=end_states,
        orders=orders,
        order_numbers=order_numbers,
        billto=billto,
        shipper=shipper,
        consignee=consignee,
        booking_agent=booking_agent,
    )


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
    booking_company: list[str] | None = None,
    booking_agent: list[str] | None = None,
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
    currency: list[str] | None = None,
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
    revtype1-4, booking_company, booking_agent and started_after/before, so you
    can ask for stops on a customer's orders without a second query. stop_status
    is DNE (arrived), NON or OPN;
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
    seat they held. reference_numbers matches only each stop's PRIMARY
    reference - about half of what is filed, and nothing attached to the order
    itself - so to find a load by a reference number, use
    search_reference_numbers instead. Pair this one with reference_types, where
    the common types are 'B/L #', 'LOAD #', 'P/U #', 'REF' and 'CUSBRK'.

    Two filters cannot use an index and scan 1.6M rows if used alone - trailers
    and late_arrival. Pair either with a date or status filter.

    Stops with ord_hdrnumber 0 belong to no order - they are non-billable stops,
    loaded or empty: DLT / HLT (always a loaded trailer), DMT / BMT / HMT empty
    moves, route points, inspections, scales. scope="movement" includes them as
    trip context with ord_number and order_status NULL.

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
        booking_company=booking_company,
        booking_agent=booking_agent,
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
        currency=currency,
    )


@mcp.tool()
def search_reference_numbers(
    reference_numbers: list[str] | None = None,
    reference_contains: str | None = None,
    reference_types: list[str] | None = None,
    attached_to: list[str] | None = None,
    orders: list[int] | None = None,
    order_numbers: list[str] | None = None,
    updated_after: str | None = None,
    updated_before: str | None = None,
    billto: list[str] | None = None,
    shipper: list[str] | None = None,
    consignee: list[str] | None = None,
    status: list[str] | None = None,
    booking_company: list[str] | None = None,
    booking_agent: list[str] | None = None,
    revtype1: list[str] | None = None,
    revtype2: list[str] | None = None,
    started_after: str | None = None,
    started_before: str | None = None,
    completed_after: str | None = None,
    completed_before: str | None = None,
    origin_state: list[str] | None = None,
    dest_state: list[str] | None = None,
    limit: int = 200,
) -> dict:
    """Find reference numbers (B/L, PO, PAPS, seal, load #, ...) and what they belong to.

    This is the place to look up ANY reference number. An order's paperwork is
    filed across several owners — a B/L # on the order itself, a seal or
    appointment on a stop, a PAPS or ACE number on a freight line, a copy on
    the invoice line — and this searches all of them. search_stops'
    reference_numbers only sees each stop's primary reference, about half of
    what is filed, and nothing attached to the order itself.

    Three ways in, combinable with each other and with every order filter:
      a number you have   -> reference_numbers=["C702280"]   (exact, instant)
      part of a number    -> reference_contains="702280"     (substring)
      everything on order -> orders=[364706] or order_numbers=["364706"]
    Narrow with reference_types, e.g. ["B/L #"], ["P/O #"], ["PAPS"], and with
    attached_to, any of "order", "stop", "freight", "invoice". At least one
    filter is required. Matching is case-insensitive. Prefer reference_contains
    when an exact number finds nothing: customers format numbers loosely, and a
    few stored numbers carry leading spaces an exact match cannot see.

    Each result says what the reference is attached_to and carries its
    context: the order (ord_number, billto, booking_company, start date), the
    stop it sits on (stop_event, company, city, arrival, mov_number,
    lgh_number) — directly, or through its freight or invoice line — and the
    freight line (fgt_number, commodity) or invoice line (ivd_number, item).
    reference_type_name decodes the type where TMW has a label; EDI-loaded
    types such as CR, CON, TEL, ST# and OID have none.

    The same number often appears more than once — on the order, its stop,
    and the invoice line that billed it. Those are copies, not separate loads;
    the attached_to field tells them apart.

    A null ord_hdrnumber means the reference sits on a non-billable stop - one
    belonging to no order, such as a DLT drop or a route point - rather than on
    an order; mov_number still places it on the trip.

    Fuel card numbers and money codes are never returned.

    Results are selected NEWEST FIRST, so when truncated is true the most
    recently entered references are the ones returned. They are then grouped by
    order, newest order first, with order-level references ahead of stop,
    freight and invoice ones. Returns {"references": [...], "count": n,
    "truncated": bool}.
    """
    return db.search_reference_numbers(
        limit=limit,
        reference_numbers=reference_numbers,
        reference_contains=reference_contains,
        reference_types=reference_types,
        attached_to=attached_to,
        orders=orders,
        order_numbers=order_numbers,
        updated_after=updated_after,
        updated_before=updated_before,
        billto=billto,
        shipper=shipper,
        consignee=consignee,
        status=status,
        booking_company=booking_company,
        booking_agent=booking_agent,
        revtype1=revtype1,
        revtype2=revtype2,
        started_after=started_after,
        started_before=started_before,
        completed_after=completed_after,
        completed_before=completed_before,
        origin_state=origin_state,
        dest_state=dest_state,
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
def list_booking_agents(include_retired: bool = True) -> list[dict]:
    """List the booking agents — the people who book loads — with their order activity.

    The booking agent is stored in the order's revtype4. Each row carries the
    agent's code (`booking_agent`), their name, whether they are retired, and
    their order_count, first_order and last_order dates across all history.
    last_order can be in the future for loads already booked ahead.

    Either the code or the name works in the booking_agent filter of
    search_orders, summarize_orders, search_stops and search_freight. They
    usually match, but not always — CAMER is CAMERON, JENN is JENNIFER.

    Retired agents are included by default because their orders remain in the
    history; pass include_retired=False for the current team only.
    """
    return db.list_booking_agents(include_retired)


@mcp.tool()
def list_booking_companies(include_retired: bool = True) -> list[dict]:
    """List the sub-companies that book loads, with their order activity.

    The booking company is stored in the order's revtype1. Each row carries the
    code (`booking_company`), its name, whether it is retired, and its
    order_count, first_order and last_order dates across all history.

    Either the code or the name works in the booking_company filter of
    search_orders, summarize_orders, search_stops and search_freight — ["TSC"]
    and ["Trafalgar"] are the same. Combine it with booking_agent to see who
    books for which company; most agents book for one, but some book for several.
    """
    return db.list_booking_companies(include_retired)


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
    booking_company: list[str] | None = None,
    booking_agent: list[str] | None = None,
    origin_company: list[str] | None = None,
    dest_company: list[str] | None = None,
    origin_city: list[int] | None = None,
    dest_city: list[int] | None = None,
    origin_state: list[str] | None = None,
    dest_state: list[str] | None = None,
    min_charge: float | None = None,
    currency: list[str] | None = None,
    orders: list[int] | None = None,
    order_numbers: list[str] | None = None,
    limit: int = 200,
) -> dict:
    """Search orders by date, customer, location, booking and revenue.

    At least one filter is required. Dates are ISO strings ('2025-06-01') and the
    *_before bounds are exclusive. All list filters match any of the given values.

    REVENUE. Each order carries `revenue` - what was actually billed, before tax:
    the net of all its live invoices including credits, rebills and supplementals,
    or its pending billing lines if not yet invoiced. revenue_source says which
    (invoiced / pending / not_billable / invoice_cancelled / order), and
    revenue_currency its currency (CAD, USD, UNK). An order billed in two
    currencies that do not net out has revenue null and revenue_by_currency
    instead. `tax` is reported separately. `invoices` lists the live invoice
    numbers; `corrected` means a credit/rebill chain exists, `supplemental` an
    extra invoice. Use search_invoice / search_invoice_details for the detail.

    order_total_charge is the order's own stored figure, shown for comparison
    only: it is 0 on 42% of invoiced orders and keeps the original amount after
    a correction. Quote `revenue`.

    min_charge compares revenue; currency filters on the revenue currency
    (spellings such as 'CA$', 'CAD', 'US$', 'US' are interchangeable). Both are
    computed per order, so pair them with a date range or customer.

    Locations come in three precisions: origin_company / dest_company are company
    IDs (the exact facility), origin_city / dest_city are numeric city codes from
    find_city_codes, and origin_state / dest_state are two-letter states.

    Order statuses: CMP completed, AVL, PLN, STD, CAN cancelled, ICO cancelled
    billable, QTE quote, MST master template. invoice_status is the ORDER's:
    PPD prepared, AVL available for invoicing, PND pending, XIN do not invoice.

    BOOKING COMPANY AND AGENT. booking_company filters on the sub-company that
    booked the load (revtype1) and booking_agent on the person (revtype4). Both
    accept a code or a name (["JENN"] and ["Jennifer"] are the same). The agent
    fields are null on older orders whose revtype4 holds a retired load class
    (LEGAL, VAN, ...) rather than an agent.

    Returns {"orders": [...], "count": n, "truncated": bool}, newest start date
    first. For any total, use summarize_orders rather than adding rows up.
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
        booking_company=booking_company,
        booking_agent=booking_agent,
        origin_company=origin_company,
        dest_company=dest_company,
        origin_city=origin_city,
        dest_city=dest_city,
        origin_state=origin_state,
        dest_state=dest_state,
        min_charge=min_charge,
        currency=currency,
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
    booking_company: list[str] | None = None,
    booking_agent: list[str] | None = None,
    origin_company: list[str] | None = None,
    dest_company: list[str] | None = None,
    origin_city: list[int] | None = None,
    dest_city: list[int] | None = None,
    origin_state: list[str] | None = None,
    dest_state: list[str] | None = None,
    min_charge: float | None = None,
    currency: list[str] | None = None,
    limit: int = 200,
) -> dict:
    """Aggregate order revenue and volume - the tool for any "how much" question.

    Takes the same filters as search_orders plus a required group_by, one of:
    revtype1, revtype2, revtype3, revtype4, booking_company, booking_agent,
    status, invoice_status, billto, shipper, consignee, origin_city, dest_city,
    origin_state, dest_state, month, year, currency. Months and years are by
    order start date.

    Each row returns order_count, revenue, total_miles, total_weight and
    rev_per_mile, sorted by revenue descending. revenue is what was billed,
    before tax, by the same rule as search_orders, and it is broken into
    invoiced_revenue (on invoices, net of credits and rebills), pending_revenue
    (rated but not yet invoiced) and order_only_revenue (no billing lines at
    all - the order's stored charge, rarely non-zero). tax is reported
    separately. non_revenue_orders counts orders included at 0: cancelled,
    quotes, master templates, and orders whose invoices were all cancelled -
    filter status=["CMP"] to leave them out of order_count.

    EVERY GROUP IS SPLIT BY CURRENCY, because this database mixes Canadian and
    US dollars and money is only additive within one currency. One logical
    group can come back as several rows, each with its own currency (CAD, USD
    or UNK = not recorded). Report each figure with its currency and never add
    rows of different currencies together. Charges are never converted - the
    stored exchange rates are years stale. group_by="currency" gives the totals
    per currency directly.

    group_by="booking_company" and "booking_agent" label each group with its
    name; booking_agent collects orders holding a retired load class instead of
    an agent into one null group, so prefer it over group_by="revtype4".

    A five-year range takes one to two seconds.
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
        booking_company=booking_company,
        booking_agent=booking_agent,
        origin_company=origin_company,
        dest_company=dest_company,
        origin_city=origin_city,
        dest_city=dest_city,
        origin_state=origin_state,
        dest_state=dest_state,
        min_charge=min_charge,
        currency=currency,
    )


@mcp.tool()
def search_invoice(
    invoice_numbers: list[str] | None = None,
    orders: list[int] | None = None,
    order_numbers: list[str] | None = None,
    movements: list[int] | None = None,
    applies_to: list[str] | None = None,
    invoice_types: list[str] | None = None,
    invoice_status: list[str] | None = None,
    include_cancelled: bool = False,
    corrected_only: bool = False,
    billto: list[str] | None = None,
    shipper: list[str] | None = None,
    consignee: list[str] | None = None,
    booking_company: list[str] | None = None,
    booking_agent: list[str] | None = None,
    billed_after: str | None = None,
    billed_before: str | None = None,
    shipped_after: str | None = None,
    shipped_before: str | None = None,
    delivered_after: str | None = None,
    delivered_before: str | None = None,
    currency: list[str] | None = None,
    min_total: float | None = None,
    max_total: float | None = None,
    master_bills: list[int] | None = None,
    limit: int = 200,
) -> dict:
    """Search invoices - what was billed, credited and rebilled.

    At least one filter is required. Dates are ISO strings and *_before bounds
    are exclusive. Each invoice carries its total (including tax), tax,
    pretax_total (revenue), currency, status, bill / ship / delivery dates, the
    bill-to, and its order. The invoice's billto is authoritative for billing -
    it differs from the order's on some loads.

    INVOICE CHAINS. A wrong invoice is never edited: it is reversed and
    replaced. invoice_types are LH original, CRD credit memo (negative;
    applies_to names the invoice it reverses), RBIL rebill, SUPL supplemental
    (extra charges billed later) and MISC. Numbers share the order number plus a
    letter - 346778A, 346778B, 346778C - and results come back grouped by order
    in issue order, so a credit sits under what it reverses. The final billed
    amount of an order is the sum of its chain's pretax_total. corrected_only
    returns every invoice of orders that have been credited or rebilled.

    STATUS. XFR transferred to accounting, PRN printed, RTP ready to print,
    HLD / HLA on hold, CAN cancelled. Cancelled invoices count toward nothing
    and are excluded unless include_cancelled=True or invoice_status names
    them. Payment status is not recorded in TMW.

    booking_company and booking_agent take a code or a name. currency accepts
    any spelling (CAD, CA$, USD, US$). max_total=0 finds credits. master_bills
    filters by master bill number; master_bill_status shows its state.

    totals_by_currency sums EVERY matching invoice (not just those returned):
    invoices, orders, total, tax, pretax_total and credits, one row per
    currency. Never add CAD and USD together.

    Returns {"invoices": [...], "count": n, "truncated": bool,
    "totals_by_currency": [...]}. Use search_invoice_details for the charge
    lines behind an invoice.
    """
    return db.search_invoice(
        include_cancelled=include_cancelled,
        limit=limit,
        invoice_numbers=invoice_numbers,
        orders=orders,
        order_numbers=order_numbers,
        movements=movements,
        applies_to=applies_to,
        invoice_types=invoice_types,
        invoice_status=invoice_status,
        corrected_only=corrected_only,
        billto=billto,
        shipper=shipper,
        consignee=consignee,
        booking_company=booking_company,
        booking_agent=booking_agent,
        billed_after=billed_after,
        billed_before=billed_before,
        shipped_after=shipped_after,
        shipped_before=shipped_before,
        delivered_after=delivered_after,
        delivered_before=delivered_before,
        currency=currency,
        min_total=min_total,
        max_total=max_total,
        master_bills=master_bills,
    )


@mcp.tool()
def search_invoice_details(
    billing_state: str = "active",
    orders: list[int] | None = None,
    order_numbers: list[str] | None = None,
    invoice_numbers: list[str] | None = None,
    invoices: list[int] | None = None,
    lines: list[int] | None = None,
    invoice_types: list[str] | None = None,
    charge_types: list[str] | None = None,
    line_types: list[str] | None = None,
    tax_only: bool = False,
    exclude_tax: bool = False,
    billed_after: str | None = None,
    billed_before: str | None = None,
    stops: list[int] | None = None,
    commodities: list[str] | None = None,
    min_line_charge: float | None = None,
    max_line_charge: float | None = None,
    include_zero_lines: bool = False,
    billto: list[str] | None = None,
    shipper: list[str] | None = None,
    consignee: list[str] | None = None,
    booking_company: list[str] | None = None,
    booking_agent: list[str] | None = None,
    status: list[str] | None = None,
    started_after: str | None = None,
    started_before: str | None = None,
    completed_after: str | None = None,
    completed_before: str | None = None,
    currency: list[str] | None = None,
    limit: int = 200,
) -> dict:
    """Search individual billing lines - linehaul, fuel, accessorials, tax.

    At least one filter is required. Billing lines exist for an order's whole
    life: they are created when the order is rated, sit as "pending" until it
    is invoiced, then move onto the invoice. So this answers both "what was
    billed" and "what is rated but not yet invoiced".

    billing_state: "active" (default) = invoiced + pending; "invoiced";
    "pending"; or "all", which adds inactive lines - those on cancelled invoices
    and stale pending copies left behind on an already-invoiced order. Each
    line reports its billing_state.

    CHARGES. charge_types are codes such as LHF freight flat, LHW by weight, LHD
    by distance, LH default, FUELRV fuel surcharge % of revenue, FUELMI fuel per
    mile, GST / HST2 / GST1 tax, PERMIT, CLEAN, WAIT, STOPS, STOR, ESCORT,
    BOND, TARP; each line carries charge_type_name. line_types are SUB (the
    linehaul), LI (accessorials, fuel and some tax) and TAX. Tax is identified
    by is_tax, NOT by line_type - GST is typed LI. tax_only / exclude_tax
    filter on it. Lines on a credit memo are negative.

    Per-stop lines carrying 0 are hidden unless include_zero_lines=True.

    Order fields (billto, shipper, consignee, booking_company, booking_agent,
    status, started / completed dates, currency) filter through the line's
    order. billed_after / billed_before use the invoice date, so they exclude
    pending lines.

    totals_by_currency sums EVERY matching line: total, tax and pretax_total
    per currency. Never add CAD and USD together.

    Returns {"lines": [...], "count": n, "truncated": bool, "billing_state":
    str, "totals_by_currency": [...]}, grouped by order and invoice in charge
    sequence.
    """
    return db.search_invoice_details(
        billing_state=billing_state,
        include_zero_lines=include_zero_lines,
        limit=limit,
        orders=orders,
        order_numbers=order_numbers,
        invoice_numbers=invoice_numbers,
        invoices=invoices,
        lines=lines,
        invoice_types=invoice_types,
        charge_types=charge_types,
        line_types=line_types,
        tax_only=tax_only,
        exclude_tax=exclude_tax,
        billed_after=billed_after,
        billed_before=billed_before,
        stops=stops,
        commodities=commodities,
        min_line_charge=min_line_charge,
        max_line_charge=max_line_charge,
        billto=billto,
        shipper=shipper,
        consignee=consignee,
        booking_company=booking_company,
        booking_agent=booking_agent,
        status=status,
        started_after=started_after,
        started_before=started_before,
        completed_after=completed_after,
        completed_before=completed_before,
        currency=currency,
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
    booking_company: list[str] | None = None,
    booking_agent: list[str] | None = None,
    origin_state: list[str] | None = None,
    dest_state: list[str] | None = None,
    origin_city: list[int] | None = None,
    dest_city: list[int] | None = None,
    currency: list[str] | None = None,
    limit: int = 200,
) -> dict:
    """Search freight lines by commodity, weight, temperature, dimensions, and order.

    At least one filter is required. Order-level filters (dates, billto, shipper,
    consignee, status, revenue types, booking company and agent, origin and
    destination) work here too, so
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
        booking_company=booking_company,
        booking_agent=booking_agent,
        origin_state=origin_state,
        dest_state=dest_state,
        origin_city=origin_city,
        dest_city=dest_city,
        currency=currency,
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