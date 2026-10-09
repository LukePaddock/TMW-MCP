import threading
from datetime import datetime

import pyodbc

# --- Order search ---------------------------------------------------------
#
# One query serves every combination of filters. Each entry maps a filter name
# to a hardcoded SQL fragment; user values are always parameterized, never
# interpolated. Adding a searchable field is one line here, not a new method.
#
# "list" fragments take {ph} and expand to IN (?, ?, ...).

# orderheader.ord_currency is not a clean code. This database holds CA$
# (259,164 orders), US$ (49,939), UNK (16,969), a stray 'US' spelling (549),
# 141 blanks and 2 rows of 'CDN $' - so 'US' and 'US$' are the same currency
# under two names, as are 'CA$' and 'CDN $'. There is no Currency row in
# labelfile to decode them. _CURRENCY_NORM folds the spellings together so a
# total cannot be split across two names for one currency, and so a filter
# accepts whichever spelling the caller happens to use.
_CURRENCY_ALIASES = {
    "CA$": "CAD", "CDN $": "CAD", "CDN$": "CAD", "CAD": "CAD", "CA": "CAD",
    "US$": "USD", "US": "USD", "USD": "USD",
}
_CURRENCY_NORM = (
    "(CASE LTRIM(RTRIM(COALESCE(oh.ord_currency, ''))) "
    "WHEN 'CA$' THEN 'CAD' WHEN 'CDN $' THEN 'CAD' WHEN 'CDN$' THEN 'CAD' "
    "WHEN 'US$' THEN 'USD' WHEN 'US' THEN 'USD' "
    "WHEN '' THEN 'UNK' ELSE 'UNK' END)"
)


def _norm_currency(values):
    """Map caller-supplied currency spellings onto the canonical codes.

    Accepts 'CA$', 'CAD', 'US$', 'USD' and the rest interchangeably, so a
    filter never misses the 549 rows spelled 'US' instead of 'US$'.
    """
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    out = []
    for v in values:
        key = str(v).strip().upper()
        out.append(_CURRENCY_ALIASES.get(key, key))
    return list(dict.fromkeys(out))


# --- Booking company and agent (ord_revtype1 / ord_revtype4) ---------------
#
# labelfile.userlabelname names the four revenue types: RevType1 'Company',
# RevType2 'Region', RevType3 'TAXABLE', RevType4 'Booking Agent'. RevType1 is
# the sub-company that booked the load - EPT, JSV, TSC, PKS, and CFS, retired
# since 2018. It is exposed as booking_company rather than `company`, because
# `companies` and `*_company` already mean a cmp_id facility everywhere else.
#
# RevType4 was REPURPOSED. It first held a load class - LEGAL (164,395
# orders), VAN (38,633), WIDTH, WEIGHT, HEIGHT, WH, XATA - all now retired and
# last used in 2022. Agent codes ran alongside them from 2003, so both eras
# overlap in time and a date cut-off cannot separate them. Treated as agents,
# LEGAL would be the busiest booking agent in history. These codes are
# therefore never reported as an agent: booking_agent comes back NULL for
# them, while the raw revtype4 still carries the stored code. RevType1 has no
# such history.
_REVTYPE4_LEGACY = ("VAN", "LEGAL", "WIDTH", "WEIGHT", "HEIGHT", "WH", "XATA")
_REVTYPE4_LEGACY_SQL = ", ".join(f"'{c}'" for c in _REVTYPE4_LEGACY)

# The code, or NULL where the order carries a legacy load class instead.
_BOOKING_AGENT = (
    f"(CASE WHEN oh.ord_revtype4 IN ({_REVTYPE4_LEGACY_SQL}) "
    "THEN NULL ELSE oh.ord_revtype4 END)"
)
_BOOKING_AGENT_NAME = (
    f"(CASE WHEN oh.ord_revtype4 IN ({_REVTYPE4_LEGACY_SQL}) "
    "THEN NULL ELSE ba.name END)"
)


def _label_filter(column: str, definition: str, exclude: str | None = None) -> tuple[str, str]:
    """A list filter on a labelfile-coded column that takes the code OR the name.

    Callers naturally pass the name, and codes are truncated to six characters,
    so they often differ: CAMER is CAMERON, JENN is JENNIFER. The collation is
    case-insensitive, so "Jennifer" matches too. `exclude` is a SQL list of
    codes that must never match, whichever way they are spelled.
    """
    excl = f" AND lx.abbr NOT IN ({exclude})" if exclude else ""
    return (
        f"{column} IN ("
        " SELECT lx.abbr FROM labelfile lx"
        f" WHERE lx.labeldefinition = '{definition}'"
        f" AND (lx.abbr IN ({{ph}}) OR lx.name IN ({{ph}})){excl})",
        "list_x2",
    )


_ORDER_FILTERS: dict[str, tuple[str, str]] = {
    "orders":           ("oh.ord_hdrnumber IN ({ph})", "list"),
    "order_numbers":    ("oh.ord_number IN ({ph})", "list"),
    "billto":           ("oh.ord_billto IN ({ph})", "list"),
    "shipper":          ("oh.ord_shipper IN ({ph})", "list"),
    "consignee":        ("oh.ord_consignee IN ({ph})", "list"),
    "status":           ("oh.ord_status IN ({ph})", "list"),
    "invoice_status":   ("oh.ord_invoicestatus IN ({ph})", "list"),
    "revtype1":         ("oh.ord_revtype1 IN ({ph})", "list"),
    "revtype2":         ("oh.ord_revtype2 IN ({ph})", "list"),
    "revtype3":         ("oh.ord_revtype3 IN ({ph})", "list"),
    "revtype4":         ("oh.ord_revtype4 IN ({ph})", "list"),
    # Code or name. Legacy load-class codes never match an agent.
    "booking_company":  _label_filter("oh.ord_revtype1", "RevType1"),
    "booking_agent":    _label_filter("oh.ord_revtype4", "RevType4", _REVTYPE4_LEGACY_SQL),
    "origin_company":   ("oh.ord_originpoint IN ({ph})", "list"),
    "dest_company":     ("oh.ord_destpoint IN ({ph})", "list"),
    "origin_city":      ("oh.ord_origincity IN ({ph})", "list"),
    "dest_city":        ("oh.ord_destcity IN ({ph})", "list"),
    "origin_state":     ("oh.ord_originstate IN ({ph})", "list"),
    "dest_state":       ("oh.ord_deststate IN ({ph})", "list"),
    "started_after":    ("oh.ord_startdate >= ?", "date"),
    "started_before":   ("oh.ord_startdate < ?", "date"),
    "completed_after":  ("oh.ord_completiondate >= ?", "date"),
    "completed_before": ("oh.ord_completiondate < ?", "date"),
    "min_charge":       ("oh.ord_totalcharge >= ?", "scalar"),
    # Compared against the folded code, so any spelling works; search_orders
    # and summarize_orders put the supplied values through _norm_currency.
    "currency":         (f"{_CURRENCY_NORM} IN ({{ph}})", "list"),
}

# group_by name -> (key expression, optional label expression)
_ORDER_GROUPS: dict[str, tuple[str, str | None]] = {
    "revtype1":       ("oh.ord_revtype1", None),
    "revtype2":       ("oh.ord_revtype2", None),
    "revtype3":       ("oh.ord_revtype3", None),
    "revtype4":       ("oh.ord_revtype4", None),
    "booking_company": ("oh.ord_revtype1", "bc.name"),
    # Legacy load-class orders collapse into one NULL group rather than
    # appearing as agents named Legal, Van and so on.
    "booking_agent":  (_BOOKING_AGENT, _BOOKING_AGENT_NAME),
    "status":         ("oh.ord_status", None),
    "invoice_status": ("oh.ord_invoicestatus", None),
    "billto":         ("oh.ord_billto", "b.cmp_name"),
    "shipper":        ("oh.ord_shipper", "sh.cmp_name"),
    "consignee":      ("oh.ord_consignee", "cn.cmp_name"),
    "origin_city":    ("ocity.cty_nmstct", None),
    "dest_city":      ("dcity.cty_nmstct", None),
    "origin_state":   ("oh.ord_originstate", None),
    "dest_state":     ("oh.ord_deststate", None),
    "month":          ("CONVERT(char(7), oh.ord_startdate, 126)", None),
    "year":           ("CONVERT(char(4), oh.ord_startdate, 126)", None),
    "currency":       (_CURRENCY_NORM, None),
}

# --- Freight search ------------------------------------------------------
#
# Freight lines hang off stops, not orders: freightdetail.order_hdrnumber exists
# and is indexed but is NULL on every row in this database, so the link is
# always freightdetail -> stops -> orderheader.
#
# A stop may carry more than one freight row - rare (about 1,024 stops of 1.64M,
# at most 7 rows) but legitimate, e.g. five commodities delivered to one stop.
# Everything here keys on fgt_number and aggregates with SUM/COUNT rather than
# assuming one row per stop. fgt_sequence is not unique within a stop, so every
# ORDER BY ends with fgt_number to keep the order total.
#
# stops.stp_type is 'PUP', 'DRP' or 'NONE'. The same freight is written onto
# load, unload and in-transit stops, and on an order with several pickups or
# drops the PUP and DRP copies can disagree - the DRP rows are treated as the
# authoritative record. Of 1.64M freight rows, 849k sit on NONE stops, so
# filtering by stop type is not optional.

# Weight is stored per row with its own unit and is not normalised. Only KGS is
# converted: TON and MTN are mislabelled pounds in this database - the largest TON
# values are ~61,460, which as short tons would be 123 million lbs - so they, and
# rows with an unknown or missing unit, are treated as pounds. 574,797 LBS rows
# and 8,832 KGS rows are 99.97% of all rows carrying a weight.
_KG_TO_LBS = 2.20462
_WEIGHT_BASES = ("LBS", "KGS")

# Normalises fgt_weight to pounds. Wrapping the column makes the predicate
# non-sargable, which costs nothing here: freightdetail has no index on
# fgt_weight.
_WEIGHT_LBS = (
    "(CASE WHEN LTRIM(RTRIM(f.fgt_weightunit)) = 'KGS' "
    f"THEN f.fgt_weight * {_KG_TO_LBS} ELSE f.fgt_weight END)"
)


def _to_lbs(value, basis: str):
    """Convert a caller-supplied weight threshold into pounds."""
    if value is None:
        return None
    if basis == "KGS":
        return value * _KG_TO_LBS
    return value


# Dimensions are normalised to INCHES. Unlike the weight units, these labels are
# mostly trustworthy: 97% of FET lengths are 60 or under (real feet), 90% of INS
# and 85% of N lengths fall in the 61-700 range (real inches), and MTR averages
# 6.34m x 2.76m x 2.80m, which is an ordinary load. 'N' is treated as inches - it
# behaves exactly like INS and is almost certainly a truncated "IN".
#
# Length, width and height each carry their own unit column, and 168 rows
# disagree between them, so every dimension is converted against its own unit
# rather than against the length unit.
_INCHES_PER = {"INS": 1.0, "N": 1.0, "FET": 12.0, "YRD": 36.0, "MTR": 39.3701, "CM": 0.393701}
_DIMENSION_BASES = tuple(_INCHES_PER)


def _dim_inches(value_col: str, unit_col: str) -> str:
    """SQL expression normalising one dimension column to inches."""
    whens = " ".join(
        f"WHEN '{unit}' THEN {value_col} * {factor}"
        for unit, factor in _INCHES_PER.items()
        if factor != 1.0
    )
    # INS, N, UNK, blank and NULL fall through as inches: the unlabelled rows
    # average about 130, which is inches-like, not feet-like.
    return f"(CASE LTRIM(RTRIM(COALESCE({unit_col}, ''))) {whens} ELSE {value_col} END)"


def _to_inches(value, basis: str):
    """Convert a caller-supplied dimension threshold into inches."""
    if value is None:
        return None
    return value * _INCHES_PER[basis]


# Temperature is normalised to FAHRENHEIT. F is the dominant unit (5,176 rows)
# and C is genuine (-20..28). Rows with no unit are treated as F, matching their
# range. The conversion is affine, not a scale factor, so thresholds go through
# _to_fahrenheit rather than being multiplied.
_TEMP_BASES = ("F", "C")


def _temp_f(value_col: str) -> str:
    """SQL expression normalising a temperature column to Fahrenheit."""
    return (
        "(CASE WHEN LTRIM(RTRIM(COALESCE(f.fgt_tempunit, ''))) = 'C' "
        f"THEN {value_col} * 9.0 / 5.0 + 32 ELSE {value_col} END)"
    )


def _to_fahrenheit(value, basis: str):
    """Convert a caller-supplied temperature threshold into Fahrenheit."""
    if value is None:
        return None
    if basis == "C":
        return value * 9.0 / 5.0 + 32
    return value


_FREIGHT_FILTERS: dict[str, tuple[str, str]] = {
    "freight_numbers":  ("f.fgt_number IN ({ph})", "list"),
    "stops":            ("f.stp_number IN ({ph})", "list"),
    "commodities":      ("f.cmd_code IN ({ph})", "list"),
    "commodity_class":  ("cm.cmd_class IN ({ph})", "list"),
    "description":      ("f.fgt_description LIKE ?", "scalar"),
    # Compared in pounds. search_freight converts the threshold first, so a
    # caller can express it in kilograms via weight_basis.
    "min_weight":       (f"{_WEIGHT_LBS} >= ?", "scalar"),
    "max_weight":       (f"{_WEIGHT_LBS} <= ?", "scalar"),
    "weight_unit":      ("LTRIM(RTRIM(f.fgt_weightunit)) IN ({ph})", "list"),
    # Counts are NOT converted - PCS, PLT, COIL and CAS have no fixed ratio -
    # so filter by unit instead of hoping a threshold means one thing.
    "count_unit":       ("LTRIM(RTRIM(f.fgt_countunit)) IN ({ph})", "list"),
    "min_count":        ("f.fgt_count >= ?", "scalar"),
    # Compared in inches; search_freight converts the threshold per
    # dimension_basis first.
    "min_length":       (f"{_dim_inches('f.fgt_length', 'f.fgt_lengthunit')} >= ?", "scalar"),
    "min_width":        (f"{_dim_inches('f.fgt_width', 'f.fgt_widthunit')} >= ?", "scalar"),
    "min_height":       (f"{_dim_inches('f.fgt_height', 'f.fgt_heightunit')} >= ?", "scalar"),
    "dimension_unit":   ("LTRIM(RTRIM(f.fgt_lengthunit)) IN ({ph})", "list"),
    # Compared in Fahrenheit; converted per temp_basis.
    "max_temp":         (f"{_temp_f('f.fgt_hitemp')} <= ?", "scalar"),
    "min_temp":         (f"{_temp_f('f.fgt_lowtemp')} >= ?", "scalar"),
    "temp_unit":        ("LTRIM(RTRIM(f.fgt_tempunit)) IN ({ph})", "list"),
    # Flags take no parameter - the fragment is the whole predicate.
    "temp_controlled":  ("(f.fgt_lowtemp IS NOT NULL OR f.fgt_hitemp IS NOT NULL)", "flag"),
    "has_dimensions":   ("(f.fgt_length > 0 OR f.fgt_width > 0 OR f.fgt_height > 0)", "flag"),
}

_FREIGHT_FROM = """
    FROM freightdetail f
    JOIN stops s           ON s.stp_number = f.stp_number
    LEFT JOIN orderheader oh ON oh.ord_hdrnumber = s.ord_hdrnumber
    LEFT JOIN commodity cm ON cm.cmd_code = f.cmd_code
    LEFT JOIN city cty     ON cty.cty_code = s.stp_city
    LEFT JOIN company c    ON c.cmp_id = s.cmp_id
"""

_ORDER_FROM = """
    FROM orderheader oh
    LEFT JOIN city ocity  ON ocity.cty_code = oh.ord_origincity
    LEFT JOIN city dcity  ON dcity.cty_code = oh.ord_destcity
    LEFT JOIN company b   ON b.cmp_id = oh.ord_billto
    LEFT JOIN company sh  ON sh.cmp_id = oh.ord_shipper
    LEFT JOIN company cn  ON cn.cmp_id = oh.ord_consignee
    LEFT JOIN labelfile bc ON bc.abbr = oh.ord_revtype1
        AND bc.labeldefinition = 'RevType1'
    LEFT JOIN labelfile ba ON ba.abbr = oh.ord_revtype4
        AND ba.labeldefinition = 'RevType4'
"""


# --- Driver search --------------------------------------------------------
#
# manpowerprofile is only 676 rows, which changes the calculus that governs
# every other search here. search_orders refuses a `LIKE` on city names because
# it would discard an index seek across 326k rows; on 676 rows a full scan is
# free, so `name` does substring matching and callers never have to know a
# driver code to find a driver.
#
# mpp_id is the driver code that legheader.lgh_driver1 / lgh_driver2 carry, and
# it matches for all 594 distinct drivers that appear on a leg, so the code this
# returns feeds straight into search_stops(drivers=[...]).
#
# WHAT IS DELIBERATELY NOT SELECTED. This table holds real personal data:
# 555 full-length SSNs, 651 dates of birth, 646 licence numbers, 626 home
# addresses and 381 gender markers. None of those columns appear in the output
# or in a filter, because every field a tool returns lands in a model's context
# and travels to whatever client is connected. Work contact details
# (mpp_currentphone, mpp_email) and the licence STATE and CLASS are included -
# they are operational, not identifying. The pay columns are excluded too,
# though they happen to be zero throughout this database.
_DRIVER_FILTERS: dict[str, tuple[str, str]] = {
    # Identity. `drivers` takes the mpp_id codes; `name` is handled separately
    # because one search term expands to one LIKE per whitespace token.
    "drivers":        ("m.mpp_id IN ({ph})", "list"),
    "other_ids":      ("m.mpp_otherid IN ({ph})", "list"),
    # Status: AVL available, PLN planned, USE on the road, OUT terminated.
    # 472 of 676 are OUT, so active_only is usually what a caller wants.
    "status":         ("m.mpp_status IN ({ph})", "list"),
    "active_only":    ("m.mpp_status <> 'OUT'", "flag"),
    "terminated_only": ("m.mpp_status = 'OUT'", "flag"),
    # Assignment
    "trucks":         ("m.mpp_tractornumber IN ({ph})", "list"),
    "team_leaders":   ("m.mpp_teamleader IN ({ph})", "list"),
    "terminals":      ("m.mpp_terminal IN ({ph})", "list"),
    "fleets":         ("m.mpp_fleet IN ({ph})", "list"),
    "divisions":      ("m.mpp_division IN ({ph})", "list"),
    "domiciles":      ("m.mpp_domicile IN ({ph})", "list"),
    "companies":      ("m.mpp_company IN ({ph})", "list"),
    # Licence - state and class only, never the number
    "license_states": ("m.mpp_licensestate IN ({ph})", "list"),
    "license_classes": ("LTRIM(RTRIM(m.mpp_licenseclass)) IN ({ph})", "list"),
    # Where the driver is based
    "cities":         ("m.mpp_city IN ({ph})", "list"),
    "states":         ("m.mpp_state IN ({ph})", "list"),
    # Employment dates. The *_before bounds are exclusive, as everywhere else.
    "hired_after":    ("m.mpp_hiredate >= ?", "date"),
    "hired_before":   ("m.mpp_hiredate < ?", "date"),
    # A real termination date only; the 2049-12-31 sentinel that marks a current
    # driver is excluded so "terminated this year" cannot sweep in the active
    # roster.
    "terminated_after":  (
        "(m.mpp_terminationdt >= ? AND m.mpp_terminationdt < '2040-01-01')", "date"),
    "terminated_before": (
        "(m.mpp_terminationdt < ? AND m.mpp_terminationdt > '1950-01-02')", "date"),
    # Dispatch availability
    "available_after":  ("m.mpp_avl_date >= ?", "date"),
    "available_before": ("m.mpp_avl_date < ?", "date"),
    "trainers":       ("m.mpp_trainer = 'Y'", "flag"),
    "trainees":       ("m.mpp_trainee = 'Y'", "flag"),
}

# manpowerprofile is 676 rows, so TMW_MAX_SEARCH_ROWS (default 200, sized for
# the 326k-row order table) is the wrong ceiling here - the active roster alone
# is 204, so "list the active drivers" would truncate by four. This cap lets a
# caller ask for the whole table and nothing larger.
_DRIVER_MAX_ROWS = 1000

_DRIVER_FROM = """
    FROM manpowerprofile m
    LEFT JOIN city dcty   ON dcty.cty_code = m.mpp_city
    LEFT JOIN labelfile ls ON ls.abbr = m.mpp_status
        AND ls.labeldefinition = 'DrvStatus'
    LEFT JOIN labelfile lt ON lt.abbr = m.mpp_teamleader
        AND lt.labeldefinition = 'TeamLeader'
"""

# TMW writes 2049-12-31 into mpp_terminationdt for a driver who has not left,
# and 1950-01-01 where a date is simply unknown. Both are stored as real
# datetimes, so reporting them raw would have every current driver "terminated"
# in 2049 and 15 of them hired in 1950. Verified: all 204 non-OUT drivers carry
# the future sentinel and all 463 OUT drivers carry a genuine date.
_DATE_SENTINEL_LOW = datetime(1950, 1, 2)
_DATE_SENTINEL_HIGH = datetime(2040, 1, 1)


def _real_date(value):
    """Return a stored date as a string, or None if it is a TMW sentinel."""
    if value is None:
        return None
    if value < _DATE_SENTINEL_LOW or value > _DATE_SENTINEL_HIGH:
        return None
    return str(value)


def _like_term(term: str) -> str:
    """Wrap a user term for a substring LIKE, neutralising its wildcards.

    An unescaped '%' or '_' in a search term silently changes what matches, and
    '[' opens a character class in T-SQL. The fragments that use this pair it
    with ESCAPE '\\'.
    """
    for ch in ("\\", "%", "_", "["):
        term = term.replace(ch, "\\" + ch)
    return f"%{term}%"


# --- Stop search ----------------------------------------------------------
#
# One query serves every stop lookup, the way _ORDER_FILTERS serves order
# search. Filtering in SQL is right here for the same reason: `stops` is
# indexed for almost exactly these predicates - sk_stp_ordnum (ord_hdrnumber),
# dk_lghnum (lgh_number), dk_mov (mov_number), dk_stp_type, sk_stp_arrvdt
# (stp_arrivaldate), dk_stops_sch_seq (stp_schdtearliest), dk_stpdetstatus
# (stp_status + stp_departure_status), dk_cmparrival (cmp_id), ix_stp_city,
# sk_stops_stp_refnum (stp_reftype + stp_refnum), ix_stops_HLT (stp_event) -
# and on legheader dk_lgh_driver1, ix_lh_dr2_outst_stdt, dk_tractor and
# dk_lgh_carrier_enddate cover the people and equipment filters.
#
# Two exceptions, both deliberate:
#   - `trailers` reads stops.trl_id, which carries no index, so it scans 1.6M
#     rows alone. Pair it with a date or status filter.
#   - `late_arrival` compares two columns, so no index can serve it. Same advice.
_STOP_FILTERS: dict[str, tuple[str, str]] = {
    # Identity
    "stops":             ("s.stp_number IN ({ph})", "list"),
    "orders":            ("s.ord_hdrnumber IN ({ph})", "list"),
    "legs":              ("s.lgh_number IN ({ph})", "list"),
    "movements":         ("s.mov_number IN ({ph})", "list"),
    # Classification
    "stop_types":        ("s.stp_type IN ({ph})", "list"),
    "events":            ("s.stp_event IN ({ph})", "list"),
    # Progress. Named stop_status / departure_status because plain `status` and
    # `invoice_status` are the ORDER's, inherited from _ORDER_FILTERS.
    "stop_status":       ("s.stp_status IN ({ph})", "list"),
    "departure_status":  ("s.stp_departure_status IN ({ph})", "list"),
    "unarrived":         ("s.stp_status <> 'DNE'", "flag"),
    "undeparted":        ("s.stp_departure_status <> 'DNE'", "flag"),
    # Actual times. These are expected times until the driver has been there,
    # so a date range spans planned and historical stops alike.
    "arrived_after":     ("s.stp_arrivaldate >= ?", "date"),
    "arrived_before":    ("s.stp_arrivaldate < ?", "date"),
    "departed_after":    ("s.stp_departuredate >= ?", "date"),
    "departed_before":   ("s.stp_departuredate < ?", "date"),
    # Appointment window
    "appt_after":        ("s.stp_schdtearliest >= ?", "date"),
    "appt_before":       ("s.stp_schdtearliest < ?", "date"),
    "appt_latest_after": ("s.stp_schdtlatest >= ?", "date"),
    "appt_latest_before": ("s.stp_schdtlatest < ?", "date"),
    "firm_appt":         ("s.stp_firm_appt_flag = 'Y'", "flag"),
    "late_arrival":      ("s.stp_arrivaldate > s.stp_schdtlatest", "flag"),
    # Where
    "companies":         ("s.cmp_id IN ({ph})", "list"),
    "cities":            ("s.stp_city IN ({ph})", "list"),
    "states":            ("s.stp_state IN ({ph})", "list"),
    "zips":              ("s.stp_zipcode IN ({ph})", "list"),
    # Who and what pulled it. `drivers` matches either seat, which is what
    # "this driver's stops" means to a dispatcher. Written as two seeks UNIONed
    # rather than `lgh_driver1 IN (..) OR lgh_driver2 IN (..)`: the OR form
    # cannot use dk_lgh_driver1 and ix_lh_dr2_outst_stdt at once and scanned
    # legheader for 3.1s, where this returns in milliseconds.
    "drivers":           (
        "s.lgh_number IN ("
        " SELECT d1.lgh_number FROM legheader d1 WHERE d1.lgh_driver1 IN ({ph})"
        " UNION"
        " SELECT d2.lgh_number FROM legheader d2 WHERE d2.lgh_driver2 IN ({ph}))",
        "list_x2",
    ),
    "trucks":            ("lh.lgh_tractor IN ({ph})", "list"),
    "carriers":          ("lh.lgh_carrier IN ({ph})", "list"),
    "trailers":          ("s.trl_id IN ({ph})", "list"),
    # Customer paperwork: B/L #, LOAD #, P/U #, REF, CUSBRK are the common types
    "reference_numbers": ("s.stp_refnum IN ({ph})", "list"),
    "reference_types":   ("s.stp_reftype IN ({ph})", "list"),
}

# Narrow FROM for the key-picking half of the deferred join: only the tables a
# filter can reference. The output columns are joined on afterwards.
_STOP_FILTER_FROM = """
    FROM stops s
    LEFT JOIN legheader lh   ON lh.lgh_number = s.lgh_number
    LEFT JOIN orderheader oh ON oh.ord_hdrnumber = s.ord_hdrnumber
"""

# Wide FROM for the output half. `s` is joined by the caller against the
# picked key set, so this lists only the decoration.
_STOP_OUTPUT_JOINS = """
    LEFT JOIN legheader lh       ON lh.lgh_number = s.lgh_number
    LEFT JOIN orderheader oh     ON oh.ord_hdrnumber = s.ord_hdrnumber
    LEFT JOIN company c          ON c.cmp_id = s.cmp_id
    LEFT JOIN city cty           ON cty.cty_code = s.stp_city
    LEFT JOIN eventcodetable ect ON ect.abbr = s.stp_event
    LEFT JOIN labelfile lf       ON lf.abbr = s.stp_type3
        AND lf.labeldefinition = 'StpType3'
"""

# Trip order: movements oldest-first by their earliest arrival, then
# chronological within each. stp_number is the tiebreaker - stops in a leg can
# share an arrival date, and without it which rows land inside TOP could vary
# between identical calls.
_STOP_ORDER = """
    ORDER BY
        MIN(s.stp_arrivaldate) OVER (PARTITION BY s.mov_number),
        s.mov_number,
        s.lgh_number,
        s.stp_arrivaldate,
        s.stp_number
"""


def _coerce_date(value):
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        raise ValueError(
            f"Expected an ISO date such as '2025-01-31', got {value!r}"
        ) from None


def _build_where(filters: dict, spec_table: dict | None = None) -> tuple[str, list]:
    """Turn a dict of filters into a WHERE clause and its parameters.

    `spec_table` defaults to the order filters; freight and stop searches pass
    their own table merged with the order one, so a single assembler serves all
    three and an order filter such as billto composes with any of them.

    Unknown filter names raise rather than being silently ignored, so a
    mistyped filter can never widen the result set.
    """
    table = _ORDER_FILTERS if spec_table is None else spec_table
    clauses: list[str] = []
    params: list = []

    # The currency fragment compares against the folded code, so the caller's
    # spelling has to be folded the same way. Done here rather than in each
    # search, because every one of them inherits this filter and two of them
    # silently returned nothing when it was not.
    if filters.get("currency") is not None:
        filters = {**filters, "currency": _norm_currency(filters["currency"])}

    for name, value in filters.items():
        if value is None:
            continue
        spec = table.get(name)
        if spec is None:
            valid = ", ".join(sorted(table))
            raise ValueError(f"Unknown filter {name!r}. Valid filters: {valid}")
        fragment, kind = spec

        if kind in ("list", "list_x2"):
            values = [value] if isinstance(value, (str, int)) else list(value)
            if not values:
                continue
            # str.format fills every {ph} in the fragment, so a two-column OR
            # needs its values bound twice over.
            clauses.append(fragment.format(ph=", ".join("?" * len(values))))
            params.extend(values)
            if kind == "list_x2":
                params.extend(values)
        elif kind == "flag":
            # Only a true value applies the predicate; false means "don't care".
            if value:
                clauses.append(fragment)
        elif kind == "date":
            clauses.append(fragment)
            params.append(_coerce_date(value))
        else:
            clauses.append(fragment)
            params.append(value)

    if not clauses:
        raise ValueError(
            "At least one filter is required — an unfiltered search would scan "
            "every row in the table. If you did pass one, check its spelling "
            "against the tool's parameters: an unrecognised name is dropped by "
            "the schema before it reaches this check."
        )
    return " AND ".join(clauses), params



class TmwDB:
    def __init__(
        self,
        server: str,
        database: str,
        driver: str = "SQL Server Native Client 11.0",
        user: str | None = None,
        password: str | None = None,
        timeout: int = 30,
        encrypt: str | None = None,
        trust_server_certificate: str | None = None,
        checkcall_lookback_days: int = 30,
        max_search_rows: int = 200,
    ):
        self.server = server
        self.database = database
        self.driver = driver
        self.user = user
        self.password = password
        self.timeout = timeout
        self.encrypt = encrypt
        self.trust_server_certificate = trust_server_certificate
        self.checkcall_lookback_days = checkcall_lookback_days
        self.max_search_rows = max_search_rows
        self._local = threading.local()

    @classmethod
    def from_settings(cls, settings) -> "TmwDB":
        """Build a TmwDB from a config.Settings instance."""
        return cls(
            server=settings.db_server,
            database=settings.db_database,
            driver=settings.db_driver,
            user=settings.db_user,
            password=settings.db_password,
            timeout=settings.db_timeout,
            encrypt=settings.db_encrypt,
            trust_server_certificate=settings.db_trust_server_certificate,
            checkcall_lookback_days=settings.checkcall_lookback_days,
            max_search_rows=settings.max_search_rows,
        )

    @property
    def conn(self):
        """This thread's connection, or None if it has not opened one yet.

        pyodbc.threadsafety is 1 — connections cannot be shared between threads
        — and the streamable-http transport dispatches sync tool functions to a
        thread pool, so each worker thread gets its own. ODBC connection pooling
        (pyodbc.pooling, on by default) makes the extra connects cheap.
        """
        return getattr(self._local, "conn", None)

    @conn.setter
    def conn(self, value):
        self._local.conn = value

    def connect(self):
        conn_str = (
            f"DRIVER={{{self.driver}}};"
            f"SERVER={self.server};"
            f"DATABASE={self.database};"
        )
        if self.user and self.password:
            conn_str += f"UID={self.user};PWD={self.password};"
        else:
            conn_str += "Trusted_Connection=yes;"

        # ODBC Driver 18 (what the Linux container uses) defaults to
        # Encrypt=yes and verifies the certificate, so a SQL Server with a
        # self-signed or AD-issued cert is refused unless one of these is set.
        # The Windows Native Client 11 path leaves both unset and is unchanged.
        if self.encrypt:
            conn_str += f"Encrypt={self.encrypt};"
        if self.trust_server_certificate:
            conn_str += f"TrustServerCertificate={self.trust_server_certificate};"

        self.conn = pyodbc.connect(conn_str, timeout=self.timeout)

    def close(self):
        """Close this thread's connection. Other threads keep theirs."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def _stop_row_to_dict(self, row) -> dict:
        return {
            "stp_number": row[0],
            "mov_number": row[1],
            "lgh_number": row[2],
            "ord_hdrnumber": row[3],
            "ord_number": row[4].strip() if row[4] else None,
            "order_status": row[5],
            "stp_type": row[6],
            "stp_event": row[7],
            "stp_event_name": row[8],
            "stp_status": row[9],
            "stp_departure_status": row[10],
            "arrival_date": str(row[11]) if row[11] else None,
            "departure_date": str(row[12]) if row[12] else None,
            "appt_earliest": str(row[13]) if row[13] else None,
            "appt_latest": str(row[14]) if row[14] else None,
            "driver": row[15],
            "codriver": row[16],
            "truck": row[17],
            "carrier": row[18],
            "trailer": row[19].strip() if row[19] else None,
            "cmp_id": row[20],
            "cmp_name": row[21],
            "address": row[22],
            "city_state": row[23],
            "state": row[24],
            "zip": row[25],
            "appt_type": row[26],
            "sequence": row[27],
            "reference_type": row[28],
            "reference_number": row[29].strip() if row[29] else None,
        }

    def _fetch_all_objects(self, cursor, cls):
        return [cls(*row) for row in cursor.fetchall()]

    def get_truck_location(self, truck_ids: list[str]) -> list[dict]:
        if not truck_ids:
            return []

        if not self.conn:
            self.connect()

        placeholders = ", ".join("?" * len(truck_ids))
        cursor = self.conn.cursor()
        cursor.execute(f"""
            WITH ranked AS (
                SELECT
                    c.ckc_tractor,
                    ROUND(c.ckc_latseconds / 3600.0, 6) AS Lat,
                    ROUND(ABS(c.ckc_longseconds) / -3600.0, 6) AS Lon,
                    c.ckc_date,
                    c.ckc_comment,
                    ROW_NUMBER() OVER (
                        PARTITION BY c.ckc_tractor
                        ORDER BY c.ckc_date DESC
                    ) AS rn
                FROM checkcall c
                WHERE c.ckc_tractor IN ({placeholders})
                  AND c.ckc_date >= DATEADD(DAY, ?, GETDATE())
            )
            SELECT ckc_tractor, Lat, Lon, ckc_date, ckc_comment
            FROM ranked
            WHERE rn = 1
        """, [*truck_ids, -self.checkcall_lookback_days])

        return [
            {"truck": row[0], "lat": row[1], "lon": row[2], "date": str(row[3]), "comment": row[4]}
            for row in cursor.fetchall()
        ]

    def get_active_legs(self) -> list[dict]:
        if not self.conn:
            self.connect()

        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT
                lh.lgh_number,
                lh.lgh_outstatus,
                NULLIF(lh.lgh_tractor, 'UNKNOWN') AS lgh_tractor,
                NULLIF(lh.lgh_primary_trailer, 'UNKNOWN') AS lgh_primary_trailer,
                NULLIF(lh.lgh_carrier, 'UNKNOWN') AS lgh_carrier,
                lh.lgh_startdate,
                lh.lgh_enddate,
                lh.lgh_startcty_nmstct AS StartCity,
                lh.lgh_endcty_nmstct AS EndCity,
                lh.lgh_miles,
                (
                    SELECT STRING_AGG(ord_hdrnumber, ',')
                    FROM (
                        SELECT DISTINCT s.ord_hdrnumber
                        FROM stops s
                        WHERE s.lgh_number = lh.lgh_number
                          AND s.ord_hdrnumber != 0
                    ) deduped
                ) AS orders
            FROM legheader lh
            WHERE lh.lgh_outstatus IN ('AVL', 'STD', 'PLN')
        """)

        return [
            {
                "lgh_number": row[0],
                "status": row[1],
                "tractor": row[2],
                "trailer": row[3],
                "carrier": row[4],
                "start_date": str(row[5]) if row[5] else None,
                "end_date": str(row[6]) if row[6] else None,
                "start_city": row[7],
                "end_city": row[8],
                "miles": row[9],
                "orders": row[10],
            }
            for row in cursor.fetchall()
        ]

    _STOP_SELECT = """
        s.stp_number,
        s.mov_number,
        s.lgh_number,
        s.ord_hdrnumber,
        oh.ord_number,
        oh.ord_status,
        s.stp_type,
        s.stp_event,
        ect.name AS StpEventName,
        s.stp_status,
        s.stp_departure_status,
        s.stp_arrivaldate,
        s.stp_departuredate,
        s.stp_schdtearliest AS AppointmentEarliest,
        s.stp_schdtlatest AS AppointmentLatest,
        lh.lgh_driver1,
        lh.lgh_driver2,
        lh.lgh_tractor,
        lh.lgh_carrier,
        s.trl_id,
        s.cmp_id,
        c.cmp_name,
        c.cmp_address1,
        cty.cty_nmstct,
        s.stp_state,
        s.stp_zipcode,
        lf.name AS Appt,
        s.stp_sequence,
        s.stp_reftype,
        s.stp_refnum
    """

    def search_stops(
        self,
        scope: str = "stop",
        limit: int | None = None,
        **filters,
    ) -> dict:
        """Search stops by any indexed field, optionally widening to whole trips.

        `scope` decides what the matched stops stand for, and it matters:

        - "stop" returns the matching stops themselves. `limit` bounds rows.
        - "movement" returns every stop on every movement a match belongs to,
          which is the Trip Folder view. `limit` bounds MOVEMENTS, not rows.

        The distinction is not cosmetic. 267,192 of 344,345 movements in this
        database carry stops from more than one order, so for roughly three
        orders in four, scope="movement" returns stops the order does not own -
        the co-loaded freight that shares the trailer. Asking for order 373 with
        scope="stop" yields its 2 stops; with scope="movement" it yields 7.
        Neither is wrong, but only one answers "what else is on this truck".

        Movement scope also surfaces stops with ord_hdrnumber = 0, which is not
        an order - there is no row 0 in orderheader. Those 790k stops are empty
        equipment events (BMT, DMT, DLT, HLT, RTP) on the empty legs a movement
        can carry at either end, so they are real trip context, and ord_number
        and order_status come back NULL for them.
        """
        if not self.conn:
            self.connect()

        scope = (scope or "stop").lower()
        if scope not in ("stop", "movement"):
            raise ValueError(f"scope must be 'stop' or 'movement', got {scope!r}")

        limit = min(limit or self.max_search_rows, self.max_search_rows)
        # Stop filters win on `orders`: s.ord_hdrnumber seeks sk_stp_ordnum
        # directly, where oh.ord_hdrnumber would go through the join. Every
        # other name is distinct, so order filters such as billto, revtype1 or
        # started_after compose freely with stop filters.
        merged = {**_ORDER_FILTERS, **_STOP_FILTERS}
        where, params = _build_where(filters, merged)

        cursor = self.conn.cursor()

        if scope == "movement":
            # Pick the distinct movements the filters hit, then widen. Fan-out
            # is bounded in practice: 4.8 stops per movement on average, 84 at
            # the worst, and only 199 movements exceed 20.
            cursor.execute(f"""
                WITH matched AS (
                    SELECT DISTINCT TOP (?) s.mov_number
                    {_STOP_FILTER_FROM}
                    WHERE {where}
                    ORDER BY s.mov_number DESC
                )
                SELECT {self._STOP_SELECT}
                FROM matched
                JOIN stops s ON s.mov_number = matched.mov_number
                {_STOP_OUTPUT_JOINS}
                {_STOP_ORDER}
            """, [limit + 1, *params])
            rows = cursor.fetchall()
            movements = {r[1] for r in rows}
            truncated = len(movements) > limit
            if truncated:
                # Drop the overflow movement whole rather than truncating it
                # mid-trip, which would look like a short trip.
                keep = set(sorted(movements, reverse=True)[:limit])
                rows = [r for r in rows if r[1] in keep]
                movements = keep
            return {
                "stops": [self._stop_row_to_dict(r) for r in rows],
                "count": len(rows),
                "movements": len(movements),
                "truncated": truncated,
                "scope": scope,
            }

        # Deferred join, for the same reason search_freight uses one: the 30
        # output columns across six tables in the same query as the TOP/ORDER BY
        # cost the optimiser the ordered scan the row goal allows. Picking
        # stp_number first, then widening over at most `limit` rows, keeps it.
        # Selection takes the most recent matches; presentation is trip order.
        cursor.execute(f"""
            WITH picked AS (
                SELECT TOP (?) s.stp_number
                {_STOP_FILTER_FROM}
                WHERE {where}
                ORDER BY s.stp_arrivaldate DESC, s.stp_number DESC
            )
            SELECT {self._STOP_SELECT}
            FROM picked
            JOIN stops s ON s.stp_number = picked.stp_number
            {_STOP_OUTPUT_JOINS}
            {_STOP_ORDER}
        """, [limit + 1, *params])

        rows = cursor.fetchall()
        truncated = len(rows) > limit
        return {
            "stops": [self._stop_row_to_dict(r) for r in rows[:limit]],
            "count": min(len(rows), limit),
            "movements": len({r[1] for r in rows[:limit]}),
            "truncated": truncated,
            "scope": scope,
        }

    def get_active_power(self) -> list[dict]:
        if not self.conn:
            self.connect()

        cursor = self.conn.cursor()
        cursor.execute("""
            WITH RankedLegs AS (
                SELECT
                    lh.lgh_tractor,
                    mpp.mpp_teamleader,
                    mpp.mpp_id,
                    ROW_NUMBER() OVER (PARTITION BY lh.lgh_tractor ORDER BY lh.lgh_enddate DESC) AS rn
                FROM legheader lh
                LEFT JOIN manpowerprofile mpp ON mpp.mpp_id = lh.lgh_driver1
                LEFT JOIN tractorprofile tp ON tp.trc_number = lh.lgh_tractor
                WHERE lh.lgh_enddate > DATEADD(YEAR, -1, GETDATE())
                    AND lh.lgh_tractor <> 'UNKNOWN'
                    AND tp.trc_retiredate > GETDATE()
                    AND mpp.mpp_terminationdt > GETDATE()
            )
            SELECT lgh_tractor, mpp_teamleader, mpp_id
            FROM RankedLegs
            WHERE rn = 1
        """)

        return [
            {"tractor": row[0], "team_leader": row[1], "driver_id": row[2]}
            for row in cursor.fetchall()
        ]

    def get_truck_plan(self, truck_ids: list[str]) -> list[dict]:
        if not truck_ids:
            return []

        if not self.conn:
            self.connect()

        placeholders = ", ".join("?" * len(truck_ids))
        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT
                s.mov_number,
                s.lgh_number,
                s.ord_hdrnumber,
                s.stp_event,
                ect.name AS StpEventName,
                s.stp_status,
                s.stp_departure_status,
                s.stp_arrivaldate,
                s.stp_departuredate,
                s.stp_schdtearliest AS AppointmentEarliest,
                s.stp_schdtlatest AS AppointmentLatest,
                lh.lgh_driver1,
                lh.lgh_tractor,
                lh.lgh_carrier,
                s.trl_id,
                s.cmp_id,
                c.cmp_address1,
                cty.cty_nmstct,
                lf.name AS Appt
            FROM stops s
            INNER JOIN legheader lh ON lh.lgh_number = s.lgh_number
                AND lh.lgh_tractor IN ({placeholders})
                AND lh.lgh_outstatus IN ('PLN', 'STD')
            LEFT JOIN company c ON c.cmp_id = s.cmp_id
            LEFT JOIN city cty ON cty.cty_code = s.stp_city
            LEFT JOIN labelfile lf ON lf.abbr = s.stp_type3
                AND lf.labeldefinition = 'StpType3'
            LEFT JOIN eventcodetable ect ON ect.abbr = s.stp_event
            ORDER BY
                lh.lgh_tractor,
                MIN(s.stp_arrivaldate) OVER (PARTITION BY s.mov_number),
                s.mov_number,
                s.lgh_number,
                s.stp_arrivaldate
        """, truck_ids)

        return [
            {
                "mov_number": row[0],
                "lgh_number": row[1],
                "ord_hdrnumber": row[2],
                "stp_event": row[3],
                "stp_event_name": row[4],
                "stp_status": row[5],
                "stp_departure_status": row[6],
                "arrival_date": str(row[7]) if row[7] else None,
                "departure_date": str(row[8]) if row[8] else None,
                "appt_earliest": str(row[9]) if row[9] else None,
                "appt_latest": str(row[10]) if row[10] else None,
                "driver": row[11],
                "truck": row[12],
                "carrier": row[13],
                "trailer": row[14],
                "cmp_id": row[15],
                "address": row[16],
                "city_state": row[17],
                "appt_type": row[18],
            }
            for row in cursor.fetchall()
        ]

    def resolve_cities(self, name: str, state: str | None = None, limit: int = 25) -> list[dict]:
        if not self.conn:
            self.connect()

        sql = """
            SELECT TOP (?) cty_code, cty_name, cty_state, cty_nmstct
            FROM city
            WHERE cty_name LIKE ?
        """
        params: list = [limit, f"{name}%"]
        if state:
            sql += " AND cty_state = ?"
            params.append(state)
        sql += " ORDER BY LEN(cty_name), cty_name"

        cursor = self.conn.cursor()
        cursor.execute(sql, params)

        return [
            {"cty_code": row[0], "city": row[1], "state": row[2], "name_state": row[3]}
            for row in cursor.fetchall()
        ]

    def _list_labels(
        self, column: str, definition: str, key: str,
        exclude: str | None, include_retired: bool,
    ) -> list[dict]:
        """Every labelfile code under `definition`, with its order activity.

        UNK is always left out - it means "not recorded", not a value. Retired
        codes are kept by default because their orders remain in the history.
        `column` is a hardcoded orderheader column, never caller input.
        """
        if not self.conn:
            self.connect()

        clauses = ["lf.labeldefinition = ?", "lf.abbr <> 'UNK'"]
        if exclude:
            clauses.append(f"lf.abbr NOT IN ({exclude})")
        if not include_retired:
            clauses.append("COALESCE(lf.retired, 'N') <> 'Y'")

        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT
                lf.abbr,
                lf.name,
                lf.retired,
                COUNT(oh.ord_hdrnumber),
                -- 1950-01-01 is TMW's unknown-date placeholder, not an order.
                MIN(CASE WHEN oh.ord_startdate > '1950-01-02' THEN oh.ord_startdate END),
                MAX(oh.ord_startdate)
            FROM labelfile lf
            LEFT JOIN orderheader oh ON oh.{column} = lf.abbr
            WHERE {" AND ".join(clauses)}
            GROUP BY lf.abbr, lf.name, lf.retired, lf.code
            ORDER BY lf.code
        """, [definition])

        return [
            {
                key: row[0],
                "name": row[1],
                "retired": row[2] == "Y",
                "order_count": row[3],
                "first_order": str(row[4]) if row[4] else None,
                "last_order": str(row[5]) if row[5] else None,
            }
            for row in cursor.fetchall()
        ]

    def list_booking_agents(self, include_retired: bool = True) -> list[dict]:
        """Booking agents (RevType4), excluding the legacy load-class codes."""
        return self._list_labels(
            "ord_revtype4", "RevType4", "booking_agent",
            _REVTYPE4_LEGACY_SQL, include_retired,
        )

    def list_booking_companies(self, include_retired: bool = True) -> list[dict]:
        """Booking sub-companies (RevType1)."""
        return self._list_labels(
            "ord_revtype1", "RevType1", "booking_company", None, include_retired,
        )

    _DRIVER_SELECT = """
        m.mpp_id,
        m.mpp_lastfirst,
        m.mpp_firstname,
        m.mpp_lastname,
        m.mpp_status,
        ls.name AS StatusName,
        m.mpp_hiredate,
        m.mpp_terminationdt,
        m.mpp_tractornumber,
        m.mpp_teamleader,
        lt.name AS TeamLeaderName,
        m.mpp_terminal,
        m.mpp_fleet,
        m.mpp_division,
        m.mpp_domicile,
        m.mpp_company,
        m.mpp_licensestate,
        m.mpp_licenseclass,
        m.mpp_currentphone,
        m.mpp_email,
        dcty.cty_nmstct,
        m.mpp_state,
        m.mpp_avl_date,
        m.mpp_avl_cmp_id,
        m.mpp_next_event,
        m.mpp_next_state,
        m.mpp_otherid,
        m.mpp_trainer,
        m.mpp_trainee
    """

    def _driver_row_to_dict(self, row) -> dict:
        return {
            "driver_code": row[0].strip() if row[0] else None,
            "name": row[1],
            "first_name": row[2],
            "last_name": row[3],
            "status": row[4],
            "status_name": row[5],
            # OUT is 'Terminated' in DrvStatus, and is 70% of the table.
            "terminated": row[4] == "OUT",
            "hire_date": _real_date(row[6]),
            "termination_date": _real_date(row[7]),
            "truck": row[8].strip() if row[8] else None,
            "team_leader": row[9],
            "team_leader_name": row[10],
            "terminal": row[11],
            "fleet": row[12],
            "division": row[13],
            "domicile": row[14],
            "company": row[15],
            "license_state": row[16],
            "license_class": row[17].strip() if row[17] else None,
            "phone": row[18].strip() if row[18] else None,
            "email": row[19].strip() if row[19] else None,
            "city": row[20],
            "state": row[21],
            "available_from": _real_date(row[22]),
            "available_at": row[23].strip() if row[23] else None,
            "next_event": row[24].strip() if row[24] else None,
            "next_state": row[25],
            "other_id": row[26].strip() if row[26] else None,
            "trainer": row[27] == "Y",
            "trainee": row[28] == "Y",
        }

    def search_drivers(
        self,
        name: str | None = None,
        limit: int | None = None,
        **filters,
    ) -> dict:
        """Search drivers in manpowerprofile by code, name, status or assignment.

        Unlike the order, stop and freight searches, no filter is required:
        manpowerprofile is 676 rows, so listing it is cheap and an unfiltered
        call is a legitimate "who are our drivers". For the same reason `name`
        can do substring matching, which the larger tables cannot afford.

        `name` is split on whitespace and commas and every token must appear in
        `mpp_lastfirst` ('LASTNAME,FIRSTNAME'), so "abdel mohamed" and
        "mohamed abdel" both find ABDELWAHAB,MOHAMED. Terms are escaped, so a
        '%' in the input matches a literal percent sign.

        The returned `driver_code` is the mpp_id that legheader carries, so it
        feeds directly into search_stops(drivers=[...]) and truck_plan.

        Three rows (CU, UNKOWN, WESC02) carry no name. None of them appears on
        a leg, so they sort last rather than leading every unfiltered result.

        Personal data is not returned - see the note on _DRIVER_FILTERS.
        """
        if not self.conn:
            self.connect()

        limit = min(limit or self.max_search_rows, _DRIVER_MAX_ROWS)

        clauses: list[str] = []
        params: list = []
        if filters and any(v is not None and v is not False for v in filters.values()):
            where, params = _build_where(filters, _DRIVER_FILTERS)
            clauses.append(where)

        if name and name.strip():
            # One LIKE per token, ANDed: every token must appear somewhere in
            # 'LASTNAME,FIRSTNAME', which makes word order irrelevant.
            for token in name.replace(",", " ").split():
                clauses.append("m.mpp_lastfirst LIKE ? ESCAPE '\\'")
                params.append(_like_term(token.upper()))

        where = " AND ".join(clauses) if clauses else "1 = 1"

        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT TOP (?)
                {self._DRIVER_SELECT}
            {_DRIVER_FROM}
            WHERE {where}
            -- Three rows (CU, UNKOWN, WESC02) have no name at all, so
            -- mpp_lastfirst is ',' and sorts ahead of every real driver. None
            -- of them appears on a leg, so they go last rather than leading
            -- every unfiltered result.
            ORDER BY CASE WHEN LTRIM(RTRIM(m.mpp_lastfirst)) IN ('', ',')
                          THEN 1 ELSE 0 END,
                     m.mpp_lastfirst
        """, [limit + 1, *params])

        rows = cursor.fetchall()
        return {
            "drivers": [self._driver_row_to_dict(r) for r in rows[:limit]],
            "count": min(len(rows), limit),
            "truncated": len(rows) > limit,
        }

    def search_orders(self, limit: int | None = None, **filters) -> dict:
        if not self.conn:
            self.connect()

        limit = min(limit or self.max_search_rows, self.max_search_rows)
        where, params = _build_where(filters)

        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT TOP (?)
                oh.ord_hdrnumber,
                oh.ord_number,
                oh.ord_status,
                oh.ord_invoicestatus,
                oh.ord_billto,
                b.cmp_name AS billto_name,
                oh.ord_shipper,
                sh.cmp_name AS shipper_name,
                oh.ord_consignee,
                cn.cmp_name AS consignee_name,
                oh.ord_revtype1,
                oh.ord_revtype2,
                oh.ord_startdate,
                oh.ord_completiondate,
                ocity.cty_nmstct AS origin,
                dcity.cty_nmstct AS destination,
                oh.ord_totalmiles,
                oh.ord_totalweight,
                oh.ord_totalcharge,
                oh.ord_currency,
                {_CURRENCY_NORM},
                oh.mov_number,
                oh.ord_revtype3,
                oh.ord_revtype4,
                {_BOOKING_AGENT},
                {_BOOKING_AGENT_NAME},
                bc.name
            {_ORDER_FROM}
            WHERE {where}
            -- ord_hdrnumber breaks ties: orders can share a start time, and
            -- without it which rows land inside TOP could vary between calls.
            ORDER BY oh.ord_startdate DESC, oh.ord_hdrnumber DESC
        """, [limit + 1, *params])

        rows = cursor.fetchall()
        truncated = len(rows) > limit

        return {
            "orders": [
                {
                    "ord_hdrnumber": row[0],
                    "ord_number": row[1].strip() if row[1] else None,
                    "status": row[2],
                    "invoice_status": row[3],
                    "billto": row[4],
                    "billto_name": row[5],
                    "shipper": row[6],
                    "shipper_name": row[7],
                    "consignee": row[8],
                    "consignee_name": row[9],
                    "revtype1": row[10],
                    # revtype1 decoded: the sub-company that booked the load.
                    "booking_company_name": row[26],
                    "revtype2": row[11],
                    "revtype3": row[22],
                    "revtype4": row[23],
                    # revtype4 decoded. NULL where the order predates the
                    # field's use for agents and holds a load class instead.
                    "booking_agent": row[24],
                    "booking_agent_name": row[25],
                    "start_date": str(row[12]) if row[12] else None,
                    "completion_date": str(row[13]) if row[13] else None,
                    "origin": row[14],
                    "destination": row[15],
                    "miles": row[16],
                    "weight": row[17],
                    "total_charge": row[18],
                    # Both spellings: the stored label, and the folded code to
                    # compare or group on. A charge means nothing without it -
                    # this database is 84% CA$ and 16% US$.
                    "currency": (row[19] or "").strip() or None,
                    "currency_code": row[20],
                    "mov_number": row[21],
                }
                for row in rows[:limit]
            ],
            "count": min(len(rows), limit),
            "truncated": truncated,
        }

    def summarize_orders(self, group_by: str, limit: int | None = None, **filters) -> dict:
        """Aggregate order totals by one grouping key, split by currency.

        The currency is always part of the group key (except when grouping by
        currency itself, which would key on it twice), so a `total_charge` is
        never a sum of mixed Canadian and US dollars. One logical group can
        therefore come back as several rows, one per currency it contains.
        """
        if not self.conn:
            self.connect()

        spec = _ORDER_GROUPS.get(group_by)
        if spec is None:
            valid = ", ".join(sorted(_ORDER_GROUPS))
            raise ValueError(f"Unknown group_by {group_by!r}. Valid values: {valid}")
        key_expr, label_expr = spec

        limit = min(limit or self.max_search_rows, self.max_search_rows)
        where, params = _build_where(filters)

        label_select = f"{label_expr} AS label," if label_expr else "NULL AS label,"
        group_cols = f"{key_expr}, {label_expr}" if label_expr else key_expr

        # Money is only additive within one currency. Every _ORDER_GROUPS key
        # spans several here - the five busiest revtype1 values mix 3 to 5 -
        # so the currency is part of the key and each total is unambiguous.
        # Grouping by currency alone would otherwise key on it twice.
        if group_by != "currency":
            group_cols = f"{group_cols}, {_CURRENCY_NORM}"

        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT TOP (?)
                {key_expr} AS grp,
                {label_select}
                {_CURRENCY_NORM} AS currency,
                COUNT(*) AS order_count,
                SUM(oh.ord_totalcharge) AS total_charge,
                SUM(oh.ord_totalmiles) AS total_miles,
                SUM(oh.ord_totalweight) AS total_weight,
                SUM(oh.ord_totalcharge) / NULLIF(SUM(oh.ord_totalmiles), 0) AS rev_per_mile
            {_ORDER_FROM}
            WHERE {where}
            GROUP BY {group_cols}
            ORDER BY SUM(oh.ord_totalcharge) DESC
        """, [limit + 1, *params])

        rows = cursor.fetchall()
        truncated = len(rows) > limit

        return {
            "group_by": group_by,
            "split_by_currency": group_by != "currency",
            "groups": [
                {
                    "group": row[0],
                    "label": row[1],
                    # Every money figure on this row is in this currency, and
                    # rows for the same group in another currency are separate.
                    "currency": row[2],
                    "order_count": row[3],
                    "total_charge": row[4],
                    "total_miles": row[5],
                    "total_weight": row[6],
                    "rev_per_mile": round(row[7], 3) or 0.0 if row[7] is not None else None,
                }
                for row in rows[:limit]
            ],
            "count": min(len(rows), limit),
            "truncated": truncated,
        }

    def _freight_row_to_dict(self, row) -> dict:
        return {
            "fgt_number": row[0],
            "ord_hdrnumber": row[1],
            "stp_number": row[2],
            "stop_type": (row[3] or "").strip() or None,
            "stp_event": (row[4] or "").strip() or None,
            "stop_city": row[5],
            "cmp_id": row[6],
            "company_name": row[7],
            "cmd_code": (row[8] or "").strip() or None,
            "commodity_name": row[9],
            "commodity_class": (row[10] or "").strip() or None,
            "description": row[11],
            "weight": row[12],
            "weight_unit": (row[13] or "").strip() or None,
            # Same weight in pounds, so rows with mixed units are comparable.
            "weight_lbs": (
                round(row[12] * _KG_TO_LBS, 2)
                if row[12] is not None and (row[13] or "").strip().upper() == "KGS"
                else row[12]
            ),
            "count": row[14],
            "count_unit": (row[15] or "").strip() or None,
            "volume": row[16],
            "volume_unit": (row[17] or "").strip() or None,
            "low_temp": row[18],
            "high_temp": row[19],
            "temp_unit": (row[20] or "").strip() or None,
            "length": row[21],
            "width": row[22],
            "height": row[23],
            "dimension_unit": (row[24] or "").strip() or None,
            "sequence": row[25],
            # Normalised alongside the stored values, so rows with different
            # units are comparable without the caller doing arithmetic.
            "length_in": round(row[26], 2) if row[26] is not None else None,
            "width_in": round(row[27], 2) if row[27] is not None else None,
            "height_in": round(row[28], 2) if row[28] is not None else None,
            "low_temp_f": round(row[29], 1) if row[29] is not None else None,
            "high_temp_f": round(row[30], 1) if row[30] is not None else None,
        }

    _FREIGHT_SELECT = f"""
        f.fgt_number,
        s.ord_hdrnumber,
        f.stp_number,
        s.stp_type,
        s.stp_event,
        cty.cty_nmstct AS stop_city,
        s.cmp_id,
        c.cmp_name,
        f.cmd_code,
        cm.cmd_name,
        cm.cmd_class,
        f.fgt_description,
        f.fgt_weight,
        f.fgt_weightunit,
        f.fgt_count,
        f.fgt_countunit,
        f.fgt_volume,
        f.fgt_volumeunit,
        f.fgt_lowtemp,
        f.fgt_hitemp,
        f.fgt_tempunit,
        f.fgt_length,
        f.fgt_width,
        f.fgt_height,
        f.fgt_lengthunit,
        f.fgt_sequence,
        {_dim_inches('f.fgt_length', 'f.fgt_lengthunit')},
        {_dim_inches('f.fgt_width', 'f.fgt_widthunit')},
        {_dim_inches('f.fgt_height', 'f.fgt_heightunit')},
        {_temp_f('f.fgt_lowtemp')},
        {_temp_f('f.fgt_hitemp')}
    """

    def search_freight(
        self,
        stop_type: str | None = "DRP",
        weight_basis: str = "LBS",
        dimension_basis: str = "INS",
        temp_basis: str = "F",
        limit: int | None = None,
        **filters,
    ) -> dict:
        """Search freight lines, defaulting to the authoritative DRP copies.

        Accepts freight filters and, because orderheader is joined as `oh`, every
        order filter too - so "oversize freight for this customer last quarter"
        is a single query. A `stop_type` of None or "ANY" searches every copy.

        Thresholds are compared against normalised values, and the three `*_basis`
        arguments say which unit each threshold is expressed in:

        - weight in pounds; weight_basis "LBS" or "KGS"
        - dimensions in inches; dimension_basis "INS", "FET", "MTR", "YRD" or "CM"
        - temperature in Fahrenheit; temp_basis "F" or "C"

        So min_weight=10000 with weight_basis="KGS" means 10,000 kg and matches a
        22,046 lb row, and min_length=40 with dimension_basis="FET" means 40 feet
        and matches a 480 inch row.
        """
        if not self.conn:
            self.connect()

        basis = (weight_basis or "LBS").upper()
        if basis not in _WEIGHT_BASES:
            raise ValueError(
                f"weight_basis must be one of {', '.join(_WEIGHT_BASES)}, got {weight_basis!r}"
            )
        for bound in ("min_weight", "max_weight"):
            if filters.get(bound) is not None:
                filters[bound] = _to_lbs(filters[bound], basis)

        dim_basis = (dimension_basis or "INS").upper()
        if dim_basis not in _DIMENSION_BASES:
            raise ValueError(
                f"dimension_basis must be one of {', '.join(_DIMENSION_BASES)}, "
                f"got {dimension_basis!r}"
            )
        for bound in ("min_length", "min_width", "min_height"):
            if filters.get(bound) is not None:
                filters[bound] = _to_inches(filters[bound], dim_basis)

        t_basis = (temp_basis or "F").upper()
        if t_basis not in _TEMP_BASES:
            raise ValueError(
                f"temp_basis must be one of {', '.join(_TEMP_BASES)}, got {temp_basis!r}"
            )
        for bound in ("min_temp", "max_temp"):
            if filters.get(bound) is not None:
                filters[bound] = _to_fahrenheit(filters[bound], t_basis)

        limit = min(limit or self.max_search_rows, self.max_search_rows)
        merged = {**_FREIGHT_FILTERS, **_ORDER_FILTERS}
        where, params = _build_where(filters, merged)

        if stop_type and stop_type.upper() != "ANY":
            where = f"({where}) AND s.stp_type = ?"
            params = [*params, stop_type.upper()]

        # Deferred join. Selecting all 26 columns across five tables in the same
        # query as the TOP/ORDER BY makes the optimiser abandon the cheap ordered
        # scan that the row goal allows, and a filter such as min_weight plus a
        # date range then took 24s. Picking the keys with a narrow projection
        # first, then widening over at most `limit` rows, is ~400ms for the same
        # result - a 56x difference measured on this data.
        cursor = self.conn.cursor()
        cursor.execute(f"""
            WITH picked AS (
                SELECT TOP (?) f.fgt_number
                {_FREIGHT_FROM}
                WHERE {where}
                ORDER BY s.ord_hdrnumber DESC, f.stp_number, f.fgt_sequence, f.fgt_number
            )
            SELECT
                {self._FREIGHT_SELECT}
            FROM picked
            JOIN freightdetail f   ON f.fgt_number = picked.fgt_number
            JOIN stops s           ON s.stp_number = f.stp_number
            LEFT JOIN commodity cm ON cm.cmd_code = f.cmd_code
            LEFT JOIN city cty     ON cty.cty_code = s.stp_city
            LEFT JOIN company c    ON c.cmp_id = s.cmp_id
            ORDER BY s.ord_hdrnumber DESC, f.stp_number, f.fgt_sequence, f.fgt_number
        """, [limit + 1, *params])

        rows = cursor.fetchall()
        return {
            "freight": [self._freight_row_to_dict(r) for r in rows[:limit]],
            "count": min(len(rows), limit),
            "truncated": len(rows) > limit,
            "stop_type": (stop_type or "ANY").upper(),
            "weight_compared_in": "LBS",
            "weight_basis": basis,
            "dimensions_compared_in": "INS",
            "dimension_basis": dim_basis,
            "temp_compared_in": "F",
            "temp_basis": t_basis,
        }

    def get_order_freight(
        self, order_ids: list[str], stop_type: str | None = "DRP"
    ) -> list[dict]:
        """Every freight line for the given orders, DRP copies by default."""
        if not order_ids:
            return []

        if not self.conn:
            self.connect()

        placeholders = ", ".join("?" * len(order_ids))
        params: list = list(order_ids)
        clause = ""
        if stop_type and stop_type.upper() != "ANY":
            clause = " AND s.stp_type = ?"
            params.append(stop_type.upper())

        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT
                {self._FREIGHT_SELECT}
            {_FREIGHT_FROM}
            WHERE s.ord_hdrnumber IN ({placeholders}){clause}
            ORDER BY s.ord_hdrnumber, s.stp_type DESC, f.stp_number, f.fgt_sequence, f.fgt_number
        """, params)

        return [self._freight_row_to_dict(r) for r in cursor.fetchall()]

    def summarize_order_freight(self, order_ids: list[str]) -> list[dict]:
        """Per-order freight totals for the PUP and DRP copies, and whether they agree.

        On an order with more than one pickup or drop the two copies can drift.
        DRP is the authoritative figure; `in_sync` flags where PUP disagrees, so a
        discrepancy is visible rather than silently hidden behind one number.
        """
        if not order_ids:
            return []

        if not self.conn:
            self.connect()

        placeholders = ", ".join("?" * len(order_ids))
        cursor = self.conn.cursor()
        cursor.execute(f"""
            WITH per_class AS (
                SELECT
                    s.ord_hdrnumber,
                    s.stp_type,
                    COUNT(*)                     AS lines,
                    COUNT(DISTINCT s.stp_number) AS stops,
                    -- Summed in pounds; a raw SUM would add KGS to LBS.
                    SUM({_WEIGHT_LBS})           AS weight,
                    SUM(f.fgt_count)             AS pieces,
                    COUNT(DISTINCT NULLIF(LTRIM(RTRIM(f.fgt_weightunit)), '')) AS weight_units,
                    COUNT(DISTINCT NULLIF(LTRIM(RTRIM(f.fgt_countunit)), ''))  AS count_units,
                    -- Normalised before aggregating: a MIN across mixed C and
                    -- F rows, or a MAX across FET and INS, is meaningless.
                    MIN({_temp_f('f.fgt_lowtemp')})  AS low_temp,
                    MAX({_temp_f('f.fgt_hitemp')})   AS high_temp,
                    MAX({_dim_inches('f.fgt_length', 'f.fgt_lengthunit')}) AS max_length,
                    MAX({_dim_inches('f.fgt_width', 'f.fgt_widthunit')})   AS max_width,
                    MAX({_dim_inches('f.fgt_height', 'f.fgt_heightunit')}) AS max_height
                FROM freightdetail f
                JOIN stops s ON s.stp_number = f.stp_number
                WHERE s.ord_hdrnumber IN ({placeholders})
                  AND s.stp_type IN ('PUP', 'DRP')
                GROUP BY s.ord_hdrnumber, s.stp_type
            ),
            commodities AS (
                SELECT s3.ord_hdrnumber,
                       STRING_AGG(cmd, ',') AS cmd_list
                FROM (
                    SELECT DISTINCT s2.ord_hdrnumber,
                           LTRIM(RTRIM(f2.cmd_code)) AS cmd
                    FROM freightdetail f2
                    JOIN stops s2 ON s2.stp_number = f2.stp_number
                    WHERE s2.ord_hdrnumber IN ({placeholders})
                      AND s2.stp_type = 'DRP'
                      AND f2.cmd_code IS NOT NULL
                ) s3
                GROUP BY s3.ord_hdrnumber
            )
            SELECT
                COALESCE(d.ord_hdrnumber, p.ord_hdrnumber) AS ord_hdrnumber,
                d.lines, d.stops, d.weight, d.pieces,
                d.low_temp, d.high_temp, d.max_length, d.max_width, d.max_height,
                p.lines, p.stops, p.weight, p.pieces,
                cd.cmd_list,
                d.weight_units, d.count_units
            FROM (SELECT * FROM per_class WHERE stp_type = 'DRP') d
            FULL OUTER JOIN (SELECT * FROM per_class WHERE stp_type = 'PUP') p
                ON p.ord_hdrnumber = d.ord_hdrnumber
            LEFT JOIN commodities cd
                ON cd.ord_hdrnumber = COALESCE(d.ord_hdrnumber, p.ord_hdrnumber)
            ORDER BY 1
        """, [*order_ids, *order_ids])

        out = []
        for row in cursor.fetchall():
            drp_weight, pup_weight = row[3], row[12]
            drp_pieces, pup_pieces = row[4], row[13]
            out.append({
                "ord_hdrnumber": row[0],
                # DRP is the authoritative copy.
                "drp_lines": row[1],
                "drp_stops": row[2],
                "weight": drp_weight,
                "pieces": drp_pieces,
                "low_temp": round(row[5], 1) if row[5] is not None else None,
                "high_temp": round(row[6], 1) if row[6] is not None else None,
                "temp_unit": "F",
                "max_length": round(row[7], 2) if row[7] is not None else None,
                "max_width": round(row[8], 2) if row[8] is not None else None,
                "max_height": round(row[9], 2) if row[9] is not None else None,
                "dimension_unit": "INS",
                "commodities": row[14],
                # Weight is normalised to pounds before summing. Counts are not
                # converted, so a true mixed_count_units means `pieces` adds
                # unlike units (PCS to PLT to COIL) and should not be trusted
                # as a single figure.
                "weight_unit": "LBS",
                "mixed_weight_units": (row[15] or 0) > 1,
                "mixed_count_units": (row[16] or 0) > 1,
                # PUP copy, for comparison only.
                "pup_lines": row[10],
                "pup_stops": row[11],
                "pup_weight": pup_weight,
                "pup_pieces": pup_pieces,
                "in_sync": (drp_weight == pup_weight and drp_pieces == pup_pieces),
            })
        return out
