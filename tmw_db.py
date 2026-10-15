import locale
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
def _fold_currency(column: str) -> str:
    """SQL folding a stored currency label to CAD / USD / UNK."""
    return (
        f"(CASE LTRIM(RTRIM(COALESCE({column}, ''))) "
        "WHEN 'CA$' THEN 'CAD' WHEN 'CDN $' THEN 'CAD' WHEN 'CDN$' THEN 'CAD' "
        "WHEN 'US$' THEN 'USD' WHEN 'US' THEN 'USD' "
        "ELSE 'UNK' END)"
    )


_CURRENCY_NORM = _fold_currency("oh.ord_currency")


# --- Revenue ----------------------------------------------------------------
#
# orderheader.ord_totalcharge is NOT revenue. Audited over five years of
# completed, invoiced orders (87,493), it captured 66.8% of what was invoiced
# in CAD and 51.2% in USD:
#
#   - 37,184 invoiced orders (42%) carry ord_totalcharge = 0 - those customers
#     are rated at invoicing, never on the order - hiding 71M of billing.
#   - When a wrong invoice is credited and rebilled, the order keeps the
#     ORIGINAL amount on 43% of corrected orders; supplemental invoices are
#     missing from it on a quarter.
#   - 15 orders hold typo totals (up to 47,250,000 on a 3,500 load) that would
#     add 204M of revenue that was never billed.
#
# Revenue is therefore read from the billing lines (invoicedetail), which
# exist for the whole life of an order: before invoicing they sit with
# ivh_hdrnumber = 0 ("pending"), and invoicing moves them onto the invoice.
# Per order:
#
#   1. Any live invoice (status <> 'CAN')  -> the net of ALL its live invoices:
#      original + credits + rebills + supplementals. Credits are negative and
#      reverse their target exactly (966 of 967), so the sum is the final
#      billed amount. Pending lines are ignored once invoiced: 57 of the 102
#      left behind on invoiced orders duplicate an invoiced line.
#   2. Not billable - CAN cancelled, QTE quote, MST master template -> 0.
#      Without this, an unfiltered summary counted 17.6M of cancelled orders,
#      3.2M of quotes and a 1.5M typo on a cancelled order as revenue. ICO
#      (Cancelled Billable) IS billable and falls through.
#   3. Pending lines                       -> their sum; this matches
#      ord_totalcharge on 99% of uninvoiced rated orders.
#   4. Invoiced, but every invoice CANCELLED -> 0. The bill was withdrawn; 212
#      completed orders, one carrying a 23.2M typo in ord_totalcharge that a
#      fallback would have resurrected.
#   5. Otherwise                           -> ord_totalcharge, flagged 'order'.
#
# Tax is excluded from revenue and reported separately. It is identified by
# chargetype.cht_basis = 'TAX', NOT by ivd_type = 'TAX': the GST code is typed
# 'LI' on 26,783 lines (3.7M), and testing ivd_type made 7,844 orders look 12%
# under-billed when the gap was 13% HST.
#
# Currency comes from the invoice header; invoicedetail.cur_code is NULL on
# most lines and 'US' on thousands of CA$ lines. An UNK invoice currency falls
# back to the order's. 130 orders were invoiced in two currencies - a
# currency correction, credited and rebilled - and on 129 every currency but
# one nets to zero, so zero-net currency rows are dropped where the order has
# another.

_IS_TAX = "(CASE WHEN rch.cht_basis = 'TAX' THEN 1 ELSE 0 END)"
_NOT_BILLABLE = ("CAN", "QTE", "MST")
_NOT_BILLABLE_SQL = ", ".join(f"'{s}'" for s in _NOT_BILLABLE)

# Billing-line rows for the orders in CTE `{src}`, which must expose
# ord_hdrnumber, ord_cur (the order's folded currency) and ord_status. One row
# per (order, currency, source) with pretax revenue and tax. Steps 2, 4 and 5
# of the rule apply to orders with no row here, and are the caller's.
_REVENUE_LINES = """
    SELECT {src}.ord_hdrnumber,
           COALESCE(NULLIF({inv_cur}, 'UNK'), {src}.ord_cur) AS cur,
           'invoiced' AS src,
           SUM(CASE WHEN {is_tax} = 1 THEN 0 ELSE rd.ivd_charge END) AS rev,
           SUM(CASE WHEN {is_tax} = 1 THEN rd.ivd_charge ELSE 0 END) AS tax
    FROM {src}
    JOIN invoiceheader rih ON rih.ord_hdrnumber = {src}.ord_hdrnumber
        AND rih.ivh_invoicestatus <> 'CAN'
    JOIN invoicedetail rd ON rd.ivh_hdrnumber = rih.ivh_hdrnumber
    LEFT JOIN chargetype rch ON rch.cht_itemcode = rd.cht_itemcode
    GROUP BY {src}.ord_hdrnumber, COALESCE(NULLIF({inv_cur}, 'UNK'), {src}.ord_cur)
    UNION ALL
    SELECT {src}.ord_hdrnumber, {src}.ord_cur, 'pending',
           SUM(CASE WHEN {is_tax} = 1 THEN 0 ELSE rd.ivd_charge END),
           SUM(CASE WHEN {is_tax} = 1 THEN rd.ivd_charge ELSE 0 END)
    FROM {src}
    JOIN invoicedetail rd ON rd.ord_hdrnumber = {src}.ord_hdrnumber
        AND COALESCE(rd.ivh_hdrnumber, 0) = 0
    LEFT JOIN chargetype rch ON rch.cht_itemcode = rd.cht_itemcode
    WHERE NOT EXISTS (SELECT 1 FROM invoiceheader x
                      WHERE x.ord_hdrnumber = {src}.ord_hdrnumber
                        AND x.ivh_invoicestatus <> 'CAN')
      AND {src}.ord_status NOT IN ({not_billable})
    GROUP BY {src}.ord_hdrnumber, {src}.ord_cur
"""


def _revenue_lines(src: str) -> str:
    return _REVENUE_LINES.format(
        src=src, inv_cur=_fold_currency("rih.ivh_currency"), is_tax=_IS_TAX,
        not_billable=_NOT_BILLABLE_SQL)


# Steps 2, 4 and 5 for an order with no billing-line row: (revenue, source).
_REVENUE_FALLBACK = (
    f"CASE WHEN oh.ord_status IN ({_NOT_BILLABLE_SQL}) THEN 'not_billable'"
    " WHEN EXISTS (SELECT 1 FROM invoiceheader fi WHERE fi.ord_hdrnumber = oh.ord_hdrnumber)"
    " THEN 'invoice_cancelled' ELSE 'order' END"
)


# Scalar forms for WHERE clauses, usable in any query that aliases
# orderheader as `oh` - order, stop, freight, leg and reference searches all
# inherit the filters built on them.
#
# Revenue currency: the latest live invoice's (a currency correction rebills
# in the right one), else the order's.
_REVENUE_CURRENCY = (
    "COALESCE((SELECT TOP 1 NULLIF(" + _fold_currency("ci.ivh_currency") + ", 'UNK')"
    " FROM invoiceheader ci WHERE ci.ord_hdrnumber = oh.ord_hdrnumber"
    " AND ci.ivh_invoicestatus <> 'CAN'"
    " ORDER BY ci.ivh_billdate DESC, ci.ivh_hdrnumber DESC), " + _CURRENCY_NORM + ")"
)
# Pretax revenue by the three-step rule above, summed across currencies (only
# one order in five years has two that do not net to zero).
_REVENUE_AMOUNT = (
    "COALESCE(CASE WHEN EXISTS (SELECT 1 FROM invoiceheader vi"
    " WHERE vi.ord_hdrnumber = oh.ord_hdrnumber AND vi.ivh_invoicestatus <> 'CAN')"
    " THEN (SELECT COALESCE(SUM(vd.ivd_charge), 0) FROM invoiceheader vi"
    " JOIN invoicedetail vd ON vd.ivh_hdrnumber = vi.ivh_hdrnumber"
    " LEFT JOIN chargetype vc ON vc.cht_itemcode = vd.cht_itemcode"
    " WHERE vi.ord_hdrnumber = oh.ord_hdrnumber AND vi.ivh_invoicestatus <> 'CAN'"
    " AND COALESCE(vc.cht_basis, '') <> 'TAX')"
    f" WHEN oh.ord_status IN ({_NOT_BILLABLE_SQL}) THEN 0"
    " ELSE (SELECT SUM(vd.ivd_charge) FROM invoicedetail vd"
    " LEFT JOIN chargetype vc ON vc.cht_itemcode = vd.cht_itemcode"
    " WHERE vd.ord_hdrnumber = oh.ord_hdrnumber AND COALESCE(vd.ivh_hdrnumber, 0) = 0"
    " AND COALESCE(vc.cht_basis, '') <> 'TAX') END,"
    " CASE WHEN EXISTS (SELECT 1 FROM invoiceheader vi WHERE vi.ord_hdrnumber = oh.ord_hdrnumber)"
    " THEN 0 ELSE oh.ord_totalcharge END)"
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
    # Both compare REVENUE (see "Revenue" above), not ord_totalcharge, which
    # is 0 on 42% of invoiced orders. Correlated subqueries, so pair them with
    # an indexed filter such as a date range.
    "min_charge":       (f"{_REVENUE_AMOUNT} >= ?", "scalar"),
    # Folded code, so any spelling works; _build_where normalises the input.
    "currency":         (f"{_REVENUE_CURRENCY} IN ({{ph}})", "list"),
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


def _money(value):
    """Round a money column for output; None stays None."""
    return round(float(value), 2) if value is not None else None


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


# --- Reference number search ------------------------------------------------
#
# referencenumber (1.2M rows) is polymorphic: ref_table names the owning table
# and ref_tablekey its key, with ord_hdrnumber carried alongside. A single
# order spreads its paperwork across several owners - a B/L # on the order, a
# seal on a stop, a PAPS number on a freight line - so this is the only place
# every reference for an order can be seen together.
#
# The stop's own stp_refnum holds just its PRIMARY reference: 293k of the 562k
# stop references match it, and none of the 529k order-level ones are on a
# stop at all. search_stops(reference_numbers=...) therefore misses about half
# of what is filed; this search does not.
#
# Only the four order-related owners are exposed. paydetail,
# fuelcardrequestlog, legheader and invoiceheader rows carry no order, the
# newest dates from 2023, and they hold driver pay notes and fuel card numbers.
# TCH* types (TCH card numbers, card IDs and money codes - spendable fuel card
# credentials) are excluded wherever they are attached, including the three
# money codes filed against orders.
_REFERENCE_OWNERS = {
    "order": "orderheader",
    "stop": "stops",
    "freight": "freightdetail",
    "invoice": "invoicedetail",
}
_REFERENCE_OWNER_OF = {table: name for name, table in _REFERENCE_OWNERS.items()}
_REFERENCE_SCOPE = (
    "r.ref_table IN ('orderheader', 'stops', 'freightdetail', 'invoicedetail')"
    " AND r.ref_type NOT LIKE 'TCH%'"
    # 15.5k rows hold a blank or whitespace-only number - no information.
    " AND LTRIM(RTRIM(r.ref_number)) <> ''"
)

_REFERENCE_FILTERS: dict[str, tuple[str, str]] = {
    # Exact match seeks sk_ref_num (ref_number, ref_type) in ~2ms. The caller's
    # values are stripped first; SQL Server ignores trailing spaces but not
    # leading ones, and 1,367 stored numbers carry leading spaces - use
    # reference_contains to find those.
    "reference_numbers":  ("r.ref_number IN ({ph})", "list"),
    # Substring match. No index serves a leading wildcard, but the scan of the
    # narrow index is ~240ms across all 1.2M rows, so it is allowed alone.
    "reference_contains": ("r.ref_number LIKE ? ESCAPE '\\'", "scalar"),
    "reference_types":    ("r.ref_type IN ({ph})", "list"),
    # Translated from order/stop/freight/invoice to table names by the method.
    "attached_to":        ("r.ref_table IN ({ph})", "list"),
    # Overrides the order filter of the same name: r.ord_hdrnumber seeks
    # dk_ord_hdrnumber directly instead of going through the join.
    "orders":             ("r.ord_hdrnumber IN ({ph})", "list"),
    "updated_after":      ("r.last_updatedate >= ?", "date"),
    "updated_before":     ("r.last_updatedate < ?", "date"),
}

# Narrow FROM for the key-picking half. orderheader is joined only so order
# filters (billto, booking_company, started_after, ...) can apply; with none
# supplied the optimiser drops the join, since ord_hdrnumber is its key.
_REFERENCE_FILTER_FROM = """
    FROM referencenumber r
    LEFT JOIN orderheader oh ON oh.ord_hdrnumber = r.ord_hdrnumber
"""

# Wide half. Each owner resolves to a stop where it has one: a stop reference
# is the stop, a freight line sits on a stop, and 90% of invoice lines name
# the stop they bill for. That gives every row its trip context.
_REFERENCE_OUTPUT_JOINS = """
    LEFT JOIN orderheader oh  ON oh.ord_hdrnumber = r.ord_hdrnumber
    LEFT JOIN company b       ON b.cmp_id = oh.ord_billto
    LEFT JOIN labelfile bc    ON bc.abbr = oh.ord_revtype1
        AND bc.labeldefinition = 'RevType1'
    LEFT JOIN labelfile rt    ON rt.abbr = r.ref_type
        AND rt.labeldefinition = 'ReferenceNumbers'
    LEFT JOIN freightdetail f ON r.ref_table = 'freightdetail'
        AND f.fgt_number = r.ref_tablekey
    LEFT JOIN invoicedetail i ON r.ref_table = 'invoicedetail'
        AND i.ivd_number = r.ref_tablekey
    LEFT JOIN stops s         ON s.stp_number = CASE r.ref_table
        WHEN 'stops' THEN r.ref_tablekey
        WHEN 'freightdetail' THEN f.stp_number
        WHEN 'invoicedetail' THEN NULLIF(i.stp_number, 0)
    END
    LEFT JOIN company c       ON c.cmp_id = s.cmp_id
    LEFT JOIN city cty        ON cty.cty_code = s.stp_city
"""


# --- Leg search ---------------------------------------------------------------
#
# Mileage is taken from the STOPS, not legheader. Each stop's stp_lgh_mileage
# is the distance driven from the previous stop on the leg TO this one, and its
# stp_loadstatus describes that same segment: a pickup (LLD) carries 'MT'
# because the truck ran empty to reach it, the delivery (LUL) 'LD'. The first
# stop's mileage is NULL. Summing by load status therefore splits a leg into
# loaded, empty and bobtail miles, which legheader cannot: lgh_mtmiles and
# lgh_actualmiles are unpopulated on every 2025+ leg.
#
# lgh_miles agrees with the stop sum on 88% of completed 2025+ legs. Where it
# does not, the stops are usually the larger (3,312 legs, by 94 miles on
# average); the header is larger on 2,689, mostly where the stops carry no
# mileage at all. Both are returned, with miles_match, so a disagreement is
# visible rather than hidden.
#
# stp_loadstatus: LD loaded, MT empty, BT bobtail (tractor with no trailer -
# its own cost category, neither loaded nor pulling an empty), and NON / UND /
# NULL, which carry almost no mileage and are reported as unclassified. -1 is
# a "not computed" sentinel on 1,250 stops and is excluded. A NULL after the
# first stop is normally a zero-distance stop - a pickup at the same place as
# the previous one - not missing data.
#
# Stops order within a leg by stp_mfh_sequence, which matches arrival order on
# all but one of 302k recent stops. stp_lgh_sequence does NOT: it disagrees on
# 222k of them and repeats within a leg 78,840 times.
_LEG_MILES = """
    SELECT
        COUNT(*) AS stop_count,
        SUM(CASE WHEN ms.stp_lgh_mileage > 0 THEN ms.stp_lgh_mileage ELSE 0 END) AS miles,
        SUM(CASE WHEN ms.stp_lgh_mileage > 0 AND ms.stp_loadstatus = 'LD'
                 THEN ms.stp_lgh_mileage ELSE 0 END) AS loaded_miles,
        SUM(CASE WHEN ms.stp_lgh_mileage > 0 AND ms.stp_loadstatus = 'MT'
                 THEN ms.stp_lgh_mileage ELSE 0 END) AS empty_miles,
        SUM(CASE WHEN ms.stp_lgh_mileage > 0 AND ms.stp_loadstatus = 'BT'
                 THEN ms.stp_lgh_mileage ELSE 0 END) AS bobtail_miles
    FROM stops ms
    WHERE ms.lgh_number = lh.lgh_number
"""

# Leg outstatus. Completed legs are CMP, not DNE - DNE is a STOP status.
_LEG_ACTIVE = ("AVL", "PLN", "DSP", "STD")
_LEG_ACTIVE_SQL = ", ".join(f"'{s}'" for s in _LEG_ACTIVE)

_LEG_FILTERS: dict[str, tuple[str, str]] = {
    "legs":            ("lh.lgh_number IN ({ph})", "list"),
    "movements":       ("lh.mov_number IN ({ph})", "list"),
    # AVL available, PLN planned, DSP dispatched, STD started, CMP completed,
    # CAN cancelled.
    "leg_status":      ("lh.lgh_outstatus IN ({ph})", "list"),
    "active_only":     (f"lh.lgh_outstatus IN ({_LEG_ACTIVE_SQL})", "flag"),
    "trucks":          ("lh.lgh_tractor IN ({ph})", "list"),
    "trailers":        ("lh.lgh_primary_trailer IN ({ph})", "list"),
    "carriers":        ("lh.lgh_carrier IN ({ph})", "list"),
    # Either seat, as two seeks UNIONed - see "drivers" in _STOP_FILTERS for
    # why this is not an OR.
    "drivers":         (
        "lh.lgh_number IN ("
        " SELECT d1.lgh_number FROM legheader d1 WHERE d1.lgh_driver1 IN ({ph})"
        " UNION"
        " SELECT d2.lgh_number FROM legheader d2 WHERE d2.lgh_driver2 IN ({ph}))",
        "list_x2",
    ),
    # lgh_class1 is the booked order's revtype1 on 99.9% of legs.
    "booking_company": _label_filter("lh.lgh_class1", "RevType1"),
    # lgh_type1 is labelled 'Trip Type': LOCAL, REG (Deck), REEF, LIVEST, VAN...
    "trip_types":      _label_filter("lh.lgh_type1", "LghType1"),
    "started_after":   ("lh.lgh_startdate >= ?", "date"),
    "started_before":  ("lh.lgh_startdate < ?", "date"),
    "ended_after":     ("lh.lgh_enddate >= ?", "date"),
    "ended_before":    ("lh.lgh_enddate < ?", "date"),
    "start_companies": ("lh.cmp_id_start IN ({ph})", "list"),
    "end_companies":   ("lh.cmp_id_end IN ({ph})", "list"),
    "start_cities":    ("lh.lgh_startcity IN ({ph})", "list"),
    "end_cities":      ("lh.lgh_endcity IN ({ph})", "list"),
    "start_states":    ("lh.lgh_startstate IN ({ph})", "list"),
    "end_states":      ("lh.lgh_endstate IN ({ph})", "list"),
}

# Order filters reach a leg through its stops, because a leg can carry several
# orders and an order can span several legs - legheader.ord_hdrnumber names
# just one of them. "The leg carries a matching order" is the meaning. Kept to
# the filters that identify orders; dates and booking company have leg-level
# equivalents above.
_LEG_ORDER_FILTERS: dict[str, tuple[str, str]] = {
    "orders":          ("s.ord_hdrnumber IN ({ph})", "list"),
    "order_numbers":   _ORDER_FILTERS["order_numbers"],
    "billto":          _ORDER_FILTERS["billto"],
    "shipper":         _ORDER_FILTERS["shipper"],
    "consignee":       _ORDER_FILTERS["consignee"],
    "booking_agent":   _ORDER_FILTERS["booking_agent"],
}

# 498 legs are active, more than TMW_MAX_SEARCH_ROWS' default of 200, and
# active_only replaces get_active_legs, which returned them all. Same reasoning
# as _DRIVER_MAX_ROWS.
_LEG_MAX_ROWS = 1000


# --- Invoice search -----------------------------------------------------------
#
# invoiceheader (322k) holds one row per invoice; invoicedetail (1.0M) its
# lines. A wrong invoice is never edited or deleted. It is reversed by a
# credit memo and replaced, so an order's billing is a CHAIN of invoices
# whose numbers share the order's and add a letter: 346778A (LH) ->
# 346778B (CRD, ivh_applyto = 346778A) -> 346778C (RBIL). Summing every live
# invoice in the chain gives what was finally billed.
#
# ivh_definition, five years of invoices:
#   LH    87,753  original invoice
#   CRD      967  credit memo - negative, reverses ivh_applyto exactly
#   RBIL     932  rebill issued after a credit
#   SUPL     452  supplemental - extra charges billed later
#   MISC       3  miscellaneous
# Status (labelfile InvoiceStatus): XFR transferred to accounting, PRN
# printed, RTP ready to print, HLD / HLA on hold, CAN cancelled. CAN invoices
# are excluded by default; they count toward nothing.
#
# ivh_totalcharge equals the sum of the invoice's lines and INCLUDES TAX. Tax
# is identified by chargetype.cht_basis = 'TAX', never ivd_type (see
# "Revenue"). ivh_billto differs from the order's billto on 1,633 invoices -
# for billing questions the invoice's is authoritative. Payment columns
# (ivh_totalpaid, ivh_pay_status, ...) are empty on every row: payment is not
# tracked in TMW, so it is not exposed.
_INVOICE_TYPES = {
    "LH": "Original invoice",
    "CRD": "Credit memo",
    "RBIL": "Rebill",
    "SUPL": "Supplemental",
    "MISC": "Miscellaneous",
}
_INVOICE_CURRENCY = _fold_currency("ih.ivh_currency")

_INVOICE_FILTERS: dict[str, tuple[str, str]] = {
    "invoice_numbers":  ("ih.ivh_invoicenumber IN ({ph})", "list"),
    "invoices":         ("ih.ivh_hdrnumber IN ({ph})", "list"),
    "orders":           ("ih.ord_hdrnumber IN ({ph})", "list"),
    "order_numbers":    ("ih.ord_number IN ({ph})", "list"),
    "movements":        ("ih.mov_number IN ({ph})", "list"),
    "applies_to":       ("ih.ivh_applyto IN ({ph})", "list"),
    "invoice_types":    ("ih.ivh_definition IN ({ph})", "list"),
    "invoice_status":   ("ih.ivh_invoicestatus IN ({ph})", "list"),
    # Every invoice of an order that has been credited or rebilled.
    "corrected_only":   (
        "ih.ord_hdrnumber IN (SELECT co.ord_hdrnumber FROM invoiceheader co"
        " WHERE co.ivh_definition IN ('CRD', 'RBIL') AND co.ivh_invoicestatus <> 'CAN')",
        "flag",
    ),
    "billto":           ("ih.ivh_billto IN ({ph})", "list"),
    "shipper":          ("ih.ivh_shipper IN ({ph})", "list"),
    "consignee":        ("ih.ivh_consignee IN ({ph})", "list"),
    "booking_company":  _label_filter("ih.ivh_revtype1", "RevType1"),
    "booking_agent":    _label_filter("ih.ivh_revtype4", "RevType4", _REVTYPE4_LEGACY_SQL),
    "billed_after":     ("ih.ivh_billdate >= ?", "date"),
    "billed_before":    ("ih.ivh_billdate < ?", "date"),
    "shipped_after":    ("ih.ivh_shipdate >= ?", "date"),
    "shipped_before":   ("ih.ivh_shipdate < ?", "date"),
    "delivered_after":  ("ih.ivh_deliverydate >= ?", "date"),
    "delivered_before": ("ih.ivh_deliverydate < ?", "date"),
    "currency":         (f"{_INVOICE_CURRENCY} IN ({{ph}})", "list"),
    # Includes tax; credits are negative, so max_total=0 finds them.
    "min_total":        ("ih.ivh_totalcharge >= ?", "scalar"),
    "max_total":        ("ih.ivh_totalcharge <= ?", "scalar"),
    "master_bills":     ("ih.ivh_mbnumber IN ({ph})", "list"),
}

# --- Invoice line search --------------------------------------------------------
#
# Billing lines exist for an order's whole life. Before invoicing they sit in
# invoicedetail with ivh_hdrnumber = 0 ("pending") and match ord_totalcharge
# on 99% of rated orders; invoicing moves them onto the invoice. So one search
# covers both, and each line carries its billing_state:
#
#   invoiced    - on a live invoice
#   pending     - not yet invoiced
#   cancelled   - on a cancelled invoice (inactive)
#   superseded  - left pending on an order that has since been invoiced; 57
#                 of these 102 lines duplicate an invoiced line (inactive)
#
# ivd_type: SUB the linehaul, LI accessorials and fuel, TAX some taxes (GST is
# typed LI - trust is_tax), DRP / PUP per-stop lines that almost always carry
# 0 and are hidden unless include_zero_lines.
_LINE_STATE = (
    "(CASE WHEN COALESCE(d.ivh_hdrnumber, 0) = 0 THEN"
    " CASE WHEN EXISTS (SELECT 1 FROM invoiceheader sx WHERE sx.ord_hdrnumber = d.ord_hdrnumber"
    " AND sx.ivh_invoicestatus <> 'CAN') THEN 'superseded' ELSE 'pending' END"
    " WHEN ih.ivh_invoicestatus = 'CAN' THEN 'cancelled' ELSE 'invoiced' END)"
)
_LINE_CURRENCY = (
    f"COALESCE(NULLIF({_INVOICE_CURRENCY}, 'UNK'), {_CURRENCY_NORM})"
)

_DETAIL_FILTERS: dict[str, tuple[str, str]] = {
    "lines":            ("d.ivd_number IN ({ph})", "list"),
    "invoice_numbers":  ("ih.ivh_invoicenumber IN ({ph})", "list"),
    "invoices":         ("d.ivh_hdrnumber IN ({ph})", "list"),
    # Overrides the order filter: seeks invoicedetail's own ord_hdrnumber
    # index, and reaches pending lines, which have no header.
    "orders":           ("d.ord_hdrnumber IN ({ph})", "list"),
    "invoice_types":    ("ih.ivh_definition IN ({ph})", "list"),
    "charge_types":     ("d.cht_itemcode IN ({ph})", "list"),
    "line_types":       ("d.ivd_type IN ({ph})", "list"),
    "tax_only":         ("ch.cht_basis = 'TAX'", "flag"),
    "exclude_tax":      ("COALESCE(ch.cht_basis, '') <> 'TAX'", "flag"),
    "billed_after":     ("ih.ivh_billdate >= ?", "date"),
    "billed_before":    ("ih.ivh_billdate < ?", "date"),
    "stops":            ("d.stp_number IN ({ph})", "list"),
    "commodities":      ("d.cmd_code IN ({ph})", "list"),
    "min_line_charge":  ("d.ivd_charge >= ?", "scalar"),
    "max_line_charge":  ("d.ivd_charge <= ?", "scalar"),
}

_DETAIL_FROM = """
    FROM invoicedetail d
    LEFT JOIN invoiceheader ih ON ih.ivh_hdrnumber = d.ivh_hdrnumber
    LEFT JOIN orderheader oh   ON oh.ord_hdrnumber = d.ord_hdrnumber
    LEFT JOIN chargetype ch    ON ch.cht_itemcode = d.cht_itemcode
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
        # Bind str parameters as varchar, not pyodbc's default nvarchar. The
        # TMW columns are varchar under a SQL collation, where comparing one to
        # an nvarchar parameter converts the COLUMN on every row and cannot
        # seek: an exact reference number lookup took 240ms instead of 1ms,
        # and a substring search 2.7s instead of 0.6s.
        #
        # The bytes must be in the encoding the DRIVER assumes for narrow
        # character data, which differs by platform: Native Client on Windows
        # uses the ANSI code page (cp1252 here), while msodbcsql18 on Linux
        # uses the process locale (UTF-8 in the python:slim image). Either way
        # the driver then converts to the server's code page. A hardcoded
        # cp1252 would send 'é' as 0xE9, which the Linux driver reads as broken
        # UTF-8. locale.getencoding() reports the right one on both, and,
        # unlike getpreferredencoding(), is not overridden by Python's UTF-8
        # mode.
        self.conn.setencoding(encoding=locale.getencoding())

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

    def search_legs(self, limit: int | None = None, **filters) -> dict:
        """Search legs, with loaded / empty / bobtail miles summed from their stops.

        Leg filters apply to legheader directly. Order filters (orders,
        billto, booking_agent, ...) match legs that carry a stop of a matching
        order. At least one filter is required; active_only reproduces the
        old get_active_legs.

        Returns the newest `limit` legs by start date, plus `totals` summed
        over EVERY matching leg, so an aggregate question is answered
        correctly even when the row list is truncated.
        """
        if not self.conn:
            self.connect()

        def supplied(d):
            return {k: v for k, v in d.items() if v is not None and v is not False}

        unknown = set(filters) - set(_LEG_FILTERS) - set(_LEG_ORDER_FILTERS)
        if unknown:
            valid = ", ".join(sorted({**_LEG_FILTERS, **_LEG_ORDER_FILTERS}))
            raise ValueError(f"Unknown filter {sorted(unknown)[0]!r}. Valid filters: {valid}")

        leg_f = supplied({k: v for k, v in filters.items() if k in _LEG_FILTERS})
        order_f = supplied({k: v for k, v in filters.items() if k in _LEG_ORDER_FILTERS})

        clauses: list[str] = []
        params: list = []
        if leg_f:
            where, p = _build_where(leg_f, _LEG_FILTERS)
            clauses.append(where)
            params.extend(p)
        if order_f:
            where, p = _build_where(order_f, _LEG_ORDER_FILTERS)
            clauses.append(
                "lh.lgh_number IN (SELECT s.lgh_number FROM stops s"
                " JOIN orderheader oh ON oh.ord_hdrnumber = s.ord_hdrnumber"
                f" WHERE {where})"
            )
            params.extend(p)
        if not clauses:
            # Same wording as _build_where, for the same reason.
            _build_where({}, _LEG_FILTERS)
        where = " AND ".join(clauses)

        limit = min(limit or self.max_search_rows, _LEG_MAX_ROWS)
        cursor = self.conn.cursor()

        # Deferred join, as elsewhere: pick lgh_numbers narrowly, newest first,
        # then sum each picked leg's stops - a seek on dk_lghnum per leg.
        cursor.execute(f"""
            WITH picked AS (
                SELECT TOP (?) lh.lgh_number
                FROM legheader lh
                WHERE {where}
                ORDER BY lh.lgh_startdate DESC, lh.lgh_number DESC
            )
            SELECT
                lh.lgh_number,
                lh.mov_number,
                lh.lgh_outstatus,
                NULLIF(lh.lgh_tractor, 'UNKNOWN'),
                NULLIF(lh.lgh_driver1, 'UNKNOWN'),
                NULLIF(lh.lgh_driver2, 'UNKNOWN'),
                NULLIF(lh.lgh_carrier, 'UNKNOWN'),
                NULLIF(lh.lgh_primary_trailer, 'UNKNOWN'),
                lh.lgh_startdate,
                lh.lgh_enddate,
                lh.cmp_id_start,
                lh.lgh_startcty_nmstct,
                lh.lgh_startstate,
                lh.cmp_id_end,
                lh.lgh_endcty_nmstct,
                lh.lgh_endstate,
                lh.lgh_class1,
                lh.lgh_type1,
                tt.name,
                lh.lgh_miles,
                m.stop_count,
                m.miles,
                m.loaded_miles,
                m.empty_miles,
                m.bobtail_miles,
                (
                    SELECT STRING_AGG(o.ord_hdrnumber, ',')
                    FROM (SELECT DISTINCT os.ord_hdrnumber FROM stops os
                          WHERE os.lgh_number = lh.lgh_number
                            AND os.ord_hdrnumber <> 0) o
                )
            FROM picked
            JOIN legheader lh ON lh.lgh_number = picked.lgh_number
            OUTER APPLY ({_LEG_MILES}) m
            LEFT JOIN labelfile tt ON tt.abbr = lh.lgh_type1
                AND tt.labeldefinition = 'LghType1'
            ORDER BY lh.lgh_startdate DESC, lh.lgh_number DESC
        """, [limit + 1, *params])
        rows = cursor.fetchall()

        # Totals over every matching leg, not just the returned page.
        cursor.execute(f"""
            SELECT
                COUNT(*),
                SUM(m.miles),
                SUM(m.loaded_miles),
                SUM(m.empty_miles),
                SUM(m.bobtail_miles),
                SUM(CAST(lh.lgh_miles AS bigint))
            FROM legheader lh
            OUTER APPLY ({_LEG_MILES}) m
            WHERE {where}
        """, params)
        t = cursor.fetchone()

        return {
            "legs": [self._leg_row_to_dict(r) for r in rows[:limit]],
            "count": min(len(rows), limit),
            "truncated": len(rows) > limit,
            "totals": self._miles_breakdown(
                t[1], t[2], t[3], t[4], {"legs": t[0], "header_miles": t[5]}),
        }

    @staticmethod
    def _miles_breakdown(miles, loaded, empty, bobtail, extra: dict) -> dict:
        miles, loaded, empty, bobtail = (int(v or 0) for v in (miles, loaded, empty, bobtail))
        return {
            **extra,
            "miles": miles,
            "loaded_miles": loaded,
            "empty_miles": empty,
            "bobtail_miles": bobtail,
            # NON / UND / no load status: a few thousand miles in all history.
            "unclassified_miles": miles - loaded - empty - bobtail,
            "loaded_pct": round(100.0 * loaded / miles, 1) if miles else None,
        }

    def _leg_row_to_dict(self, row) -> dict:
        def text(value):
            return value.strip() if isinstance(value, str) and value.strip() else None

        out = {
            "lgh_number": row[0],
            "mov_number": row[1],
            "status": row[2],
            "truck": text(row[3]),
            "driver": text(row[4]),
            "codriver": text(row[5]),
            "carrier": text(row[6]),
            "trailer": text(row[7]),
            "start_date": str(row[8]) if row[8] else None,
            "end_date": str(row[9]) if row[9] else None,
            "start_company": text(row[10]),
            "start_city": row[11],
            "start_state": text(row[12]),
            "end_company": text(row[13]),
            "end_city": row[14],
            "end_state": text(row[15]),
            "booking_company": text(row[16]),
            "trip_type": text(row[17]),
            "trip_type_name": row[18],
            "stop_count": row[20] or 0,
            "orders": [int(o) for o in row[25].split(",")] if row[25] else [],
        }
        out.update(self._miles_breakdown(row[21], row[22], row[23], row[24], {}))
        # legheader's own figure, for comparison. The stop sum above is the
        # one to report; the two disagree on about 12% of completed legs.
        out["header_miles"] = row[19]
        out["miles_match"] = row[19] == out["miles"]
        return out

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
        an order - there is no row 0 in orderheader. Those 790k stops are
        non-billable stops, loaded as well as empty: every DLT is loaded (it is
        the drop half of a Park/Hook) and most route points are, alongside the
        DMT / BMT / HMT empty moves. They are real trip context, and ord_number
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

    def search_reference_numbers(self, limit: int | None = None, **filters) -> dict:
        """Search every reference filed against orders, stops, freight and invoices.

        Each row says what the reference is attached to and carries that
        owner's context: the order, the stop (event, company, city, arrival,
        movement, leg), the freight line, or the invoice line.

        Selection is newest-first by ref_id, so `limit` keeps the most recently
        entered references. Presentation groups them by order, newest order
        first, then order-level references before stop, freight and invoice
        ones.

        Accepts every order filter too, so "B/L numbers on this customer's
        loads last week" is one query.
        """
        if not self.conn:
            self.connect()

        if filters.get("reference_numbers") is not None:
            values = filters["reference_numbers"]
            values = [values] if isinstance(values, str) else values
            filters["reference_numbers"] = [str(v).strip() for v in values]
        if filters.get("reference_contains") is not None:
            term = str(filters["reference_contains"]).strip()
            filters["reference_contains"] = _like_term(term) if term else None
        if filters.get("attached_to") is not None:
            owners = filters["attached_to"]
            owners = [owners] if isinstance(owners, str) else owners
            tables = []
            for owner in owners:
                table = _REFERENCE_OWNERS.get(str(owner).strip().lower())
                if table is None:
                    valid = ", ".join(_REFERENCE_OWNERS)
                    raise ValueError(f"attached_to must be among {valid}, got {owner!r}")
                tables.append(table)
            filters["attached_to"] = tables

        limit = min(limit or self.max_search_rows, self.max_search_rows)
        merged = {**_ORDER_FILTERS, **_REFERENCE_FILTERS}
        where, params = _build_where(filters, merged)

        # Deferred join, as in search_stops and search_freight: pick ref_ids
        # with a narrow projection, newest first - ref_id is the clustered key,
        # so this is a backward scan the row goal can stop early - then widen.
        cursor = self.conn.cursor()
        cursor.execute(f"""
            WITH picked AS (
                SELECT TOP (?) r.ref_id
                {_REFERENCE_FILTER_FROM}
                WHERE {_REFERENCE_SCOPE} AND ({where})
                ORDER BY r.ref_id DESC
            )
            SELECT
                r.ref_id,
                r.ref_type,
                rt.name,
                r.ref_number,
                r.ref_sequence,
                r.ref_table,
                r.ref_tablekey,
                r.ord_hdrnumber,
                oh.ord_number,
                oh.ord_status,
                oh.ord_billto,
                b.cmp_name,
                oh.ord_revtype1,
                bc.name,
                oh.ord_startdate,
                r.last_updatedate,
                s.stp_number,
                s.stp_event,
                s.stp_type,
                s.cmp_id,
                c.cmp_name,
                cty.cty_nmstct,
                s.stp_arrivaldate,
                s.mov_number,
                s.lgh_number,
                f.fgt_number,
                f.cmd_code,
                f.fgt_description,
                i.ivd_number,
                i.cht_itemcode,
                i.ivd_description
            FROM picked
            JOIN referencenumber r ON r.ref_id = picked.ref_id
            {_REFERENCE_OUTPUT_JOINS}
            ORDER BY
                r.ord_hdrnumber DESC,
                CASE r.ref_table WHEN 'orderheader' THEN 0 WHEN 'stops' THEN 1
                                 WHEN 'freightdetail' THEN 2 ELSE 3 END,
                s.stp_arrivaldate,
                r.ref_tablekey,
                r.ref_sequence,
                r.ref_id
        """, [limit + 1, *params])

        rows = cursor.fetchall()
        return {
            "references": [self._reference_row_to_dict(r) for r in rows[:limit]],
            "count": min(len(rows), limit),
            "truncated": len(rows) > limit,
        }

    @staticmethod
    def _reference_row_to_dict(row) -> dict:
        def text(value):
            return value.strip() if isinstance(value, str) and value.strip() else None

        return {
            "ref_id": row[0],
            "reference_type": text(row[1]),
            # NULL for the EDI-loaded types (CR, CON, TEL, ST#, OID, ...),
            # which have no ReferenceNumbers label.
            "reference_type_name": text(row[2]),
            "reference_number": text(row[3]),
            "sequence": row[4],
            "attached_to": _REFERENCE_OWNER_OF.get(row[5], row[5]),
            # ord_hdrnumber 0 is not an order: those references sit on
            # non-billable stops (a DLT drop, a route point) of a trip.
            "ord_hdrnumber": row[7] or None,
            "ord_number": text(row[8]),
            "order_status": row[9],
            "billto": text(row[10]),
            "billto_name": row[11],
            "booking_company": text(row[12]),
            "booking_company_name": row[13],
            "order_start_date": str(row[14]) if row[14] else None,
            "updated": str(row[15]) if row[15] else None,
            # The stop the reference belongs to, directly or through its
            # freight or invoice line.
            "stp_number": row[16],
            "stop_event": text(row[17]),
            "stop_type": text(row[18]),
            "stop_company": text(row[19]),
            "stop_company_name": row[20],
            "stop_city": row[21],
            "stop_arrival": str(row[22]) if row[22] else None,
            "mov_number": row[23],
            "lgh_number": row[24],
            "fgt_number": row[25],
            "commodity": text(row[26]),
            "freight_description": text(row[27]),
            "ivd_number": row[28],
            "invoice_item": text(row[29]),
            "invoice_description": text(row[30]),
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

    def _order_revenue(self, cursor, orders: dict[int, tuple]) -> dict[int, dict]:
        """Revenue, tax and invoice chain for a set of orders.

        `orders` maps ord_hdrnumber to (ord_totalcharge, folded order
        currency, ord_status). Applies the rule documented under "Revenue".
        """
        if not orders:
            return {}
        ids = list(orders)
        ph = ", ".join("?" * len(ids))
        cursor.execute(f"""
            WITH src AS (
                SELECT oh.ord_hdrnumber, {_CURRENCY_NORM} AS ord_cur, oh.ord_status
                FROM orderheader oh WHERE oh.ord_hdrnumber IN ({ph})
            )
            {_revenue_lines("src")}
        """, ids)
        lines: dict[int, list] = {}
        for o, cur, src, rev, tax in cursor.fetchall():
            lines.setdefault(o, []).append((cur, src, float(rev or 0), float(tax or 0)))

        cursor.execute(f"""
            SELECT ord_hdrnumber, LTRIM(RTRIM(ivh_invoicenumber)), ivh_definition,
                   ivh_invoicestatus
            FROM invoiceheader
            WHERE ord_hdrnumber IN ({ph})
            ORDER BY ord_hdrnumber, ivh_hdrnumber
        """, ids)
        chains: dict[int, list] = {}
        cancelled: set[int] = set()
        for o, num, defn, status in cursor.fetchall():
            if status == "CAN":
                cancelled.add(o)
            else:
                chains.setdefault(o, []).append((num, (defn or "").strip()))

        out = {}
        for o, (ord_total, ord_cur, ord_status) in orders.items():
            invs = chains.get(o, [])
            types = {d for _, d in invs}
            info = {
                "invoices": [n for n, _ in invs],
                "corrected": bool(types & {"CRD", "RBIL"}),
                "supplemental": "SUPL" in types,
            }
            ls = lines.get(o)
            if not ls:
                # Steps 2, 4 and 5: nothing billed or pending.
                if ord_status in _NOT_BILLABLE:
                    source, rev = "not_billable", 0.0
                elif o in cancelled:
                    source, rev = "invoice_cancelled", 0.0
                else:
                    source, rev = "order", ord_total
                out[o] = {**info, "revenue": rev, "revenue_currency": ord_cur,
                          "revenue_source": source, "tax": None}
                continue
            by_cur: dict[str, list] = {}
            for cur, _, rev, tax in ls:
                agg = by_cur.setdefault(cur, [0.0, 0.0])
                agg[0] += rev
                agg[1] += tax
            # A currency correction leaves the wrong currency netting to zero.
            if len(by_cur) > 1:
                live = {c: v for c, v in by_cur.items() if abs(v[0]) >= 0.01}
                by_cur = live or by_cur
            source = "invoiced" if any(s == "invoiced" for _, s, _, _ in ls) else "pending"
            if len(by_cur) == 1:
                (cur, (rev, tax)), = by_cur.items()
                out[o] = {**info, "revenue": round(rev, 2), "revenue_currency": cur,
                          "revenue_source": source, "tax": round(tax, 2)}
            else:
                out[o] = {**info, "revenue": None, "revenue_currency": "MIXED",
                          "revenue_by_currency": {c: round(v[0], 2) for c, v in by_cur.items()},
                          "revenue_source": source, "tax": None}
        return out

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
        rows = rows[:limit]
        revenue = self._order_revenue(
            cursor, {r[0]: (float(r[18] or 0), r[20], r[2]) for r in rows})

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
                    # revenue, revenue_currency, revenue_source, tax,
                    # invoices, corrected, supplemental.
                    **revenue[row[0]],
                    # What the order itself says: 0 on 42% of invoiced orders
                    # and stale on corrected ones. For comparison only.
                    "order_total_charge": row[18],
                    "order_currency": row[20],
                    "mov_number": row[21],
                }
                for row in rows
            ],
            "count": len(rows),
            "truncated": truncated,
        }

    def summarize_orders(self, group_by: str, limit: int | None = None, **filters) -> dict:
        """Aggregate order revenue by one grouping key, split by currency.

        Revenue follows the rule under "Revenue": invoices, else pending
        lines, else ord_totalcharge. Each order's revenue is split by its
        invoice currency, so every row is single-currency; one logical group
        can come back as several rows. Grouping by currency keys on the
        revenue currency.
        """
        if not self.conn:
            self.connect()

        spec = _ORDER_GROUPS.get(group_by)
        if spec is None:
            valid = ", ".join(sorted(_ORDER_GROUPS))
            raise ValueError(f"Unknown group_by {group_by!r}. Valid values: {valid}")
        key_expr, label_expr = spec

        limit = min(limit or self.max_search_rows, self.max_search_rows)

        # min_charge and currency are revenue tests. As WHERE clauses they are
        # correlated subqueries per order (12s over a year); here revenue is
        # already computed per order, so apply them to that instead.
        min_charge = filters.pop("min_charge", None)
        currency = _norm_currency(filters.pop("currency", None))
        post, post_params = ["1 = 1"], []
        if min_charge is not None:
            post.append("p.ord_rev >= ?")
            post_params.append(min_charge)
        if currency:
            post.append(f"p.cur IN ({', '.join('?' * len(currency))})")
            post_params.extend(currency)
        where, params = _build_where(filters)

        by_currency = group_by == "currency"
        grp = "p.cur" if by_currency else "p.grp"
        label = "NULL" if by_currency or not label_expr else "p.label"
        # GROUP BY takes real columns only - a constant NULL label is an error.
        group_cols = ", ".join(dict.fromkeys(
            c for c in (grp, label, "p.cur") if c != "NULL"))

        cursor = self.conn.cursor()
        cursor.execute(f"""
            WITH f AS (
                SELECT oh.ord_hdrnumber,
                       {key_expr} AS grp,
                       {label_expr or "NULL"} AS label,
                       {_CURRENCY_NORM} AS ord_cur,
                       oh.ord_status,
                       oh.ord_totalcharge, oh.ord_totalmiles, oh.ord_totalweight
                {_ORDER_FROM}
                WHERE {where}
            ),
            l AS ({_revenue_lines("f")}),
            p AS (
                -- One row per (order, currency). An order with no billing
                -- lines takes step 2, 4 or 5 of the rule: 0 when it is not
                -- billable or its invoices were all cancelled, otherwise
                -- ord_totalcharge in its own currency.
                SELECT f.ord_hdrnumber, f.grp, f.label,
                       COALESCE(l.cur, f.ord_cur) AS cur,
                       CASE WHEN l.ord_hdrnumber IS NOT NULL THEN l.rev
                            WHEN fb.src = 'order' THEN f.ord_totalcharge
                            ELSE 0 END AS rev,
                       COALESCE(l.tax, 0) AS tax,
                       COALESCE(l.src, fb.src) AS src,
                       f.ord_totalmiles, f.ord_totalweight,
                       ROW_NUMBER() OVER (PARTITION BY f.ord_hdrnumber
                                          ORDER BY ABS(COALESCE(l.rev, 0)) DESC) AS rn
                FROM f LEFT JOIN l ON l.ord_hdrnumber = f.ord_hdrnumber
                -- Evaluated only for orders with no billing lines.
                OUTER APPLY (
                    SELECT {_REVENUE_FALLBACK.replace("oh.", "f.")} AS src
                    WHERE l.ord_hdrnumber IS NULL
                ) fb
            ),
            q AS (
                -- Drop a currency that nets to zero beside another (a
                -- currency correction), but keep an order's main row even at
                -- zero so a credited-out order still counts.
                SELECT p.*, SUM(p.rev) OVER (PARTITION BY p.ord_hdrnumber) AS ord_rev
                FROM p
                WHERE p.rn = 1 OR ABS(p.rev) >= 0.01
            )
            SELECT TOP (?)
                {grp} AS grp,
                {label} AS label,
                p.cur,
                COUNT(DISTINCT p.ord_hdrnumber),
                SUM(p.rev),
                SUM(CASE WHEN p.src = 'invoiced' THEN p.rev ELSE 0 END),
                SUM(CASE WHEN p.src = 'pending' THEN p.rev ELSE 0 END),
                SUM(CASE WHEN p.src = 'order' THEN p.rev ELSE 0 END),
                SUM(p.tax),
                -- Miles and weight once per order, on its main currency row.
                SUM(CASE WHEN p.rn = 1 THEN p.ord_totalmiles ELSE 0 END),
                SUM(CASE WHEN p.rn = 1 THEN p.ord_totalweight ELSE 0 END),
                COUNT(DISTINCT CASE WHEN p.src IN ('not_billable', 'invoice_cancelled')
                                    THEN p.ord_hdrnumber END)
            FROM q AS p
            WHERE {" AND ".join(post)}
            GROUP BY {group_cols}
            ORDER BY SUM(p.rev) DESC
        """, [*params, limit + 1, *post_params])

        rows = cursor.fetchall()
        truncated = len(rows) > limit

        def money(v):
            return round(float(v), 2) if v is not None else None

        return {
            "group_by": group_by,
            "split_by_currency": not by_currency,
            "groups": [
                {
                    "group": row[0],
                    "label": row[1],
                    # Every money figure on this row is in this currency, and
                    # rows for the same group in another currency are separate.
                    "currency": row[2],
                    "order_count": row[3],
                    "revenue": money(row[4]),
                    "invoiced_revenue": money(row[5]),
                    "pending_revenue": money(row[6]),
                    "order_only_revenue": money(row[7]),
                    "tax": money(row[8]),
                    "total_miles": row[9],
                    "total_weight": row[10],
                    "rev_per_mile": (round(float(row[4]) / row[9], 3)
                                     if row[9] and row[4] is not None else None),
                    # Counted in order_count at 0 revenue: cancelled orders,
                    # quotes, master templates, and orders whose invoices
                    # were all cancelled.
                    "non_revenue_orders": row[11],
                }
                for row in rows[:limit]
            ],
            "count": min(len(rows), limit),
            "truncated": truncated,
        }

    def search_invoice(
        self,
        include_cancelled: bool = False,
        limit: int | None = None,
        **filters,
    ) -> dict:
        """Search invoice headers, with tax split out and per-currency totals.

        Cancelled invoices are excluded unless include_cancelled is true or
        invoice_status names statuses explicitly. Rows are picked newest bill
        date first and presented as chains - by order, then in issue order -
        so a credit sits directly under the invoice it reverses.
        """
        if not self.conn:
            self.connect()

        limit = min(limit or self.max_search_rows, self.max_search_rows)
        where, params = _build_where(filters, _INVOICE_FILTERS)
        if not include_cancelled and not filters.get("invoice_status"):
            where = f"({where}) AND ih.ivh_invoicestatus <> 'CAN'"

        cursor = self.conn.cursor()
        cursor.execute(f"""
            WITH picked AS (
                SELECT TOP (?) ih.ivh_hdrnumber
                FROM invoiceheader ih
                WHERE {where}
                ORDER BY ih.ivh_billdate DESC, ih.ivh_hdrnumber DESC
            )
            SELECT
                ih.ivh_hdrnumber,
                ih.ivh_invoicenumber,
                ih.ivh_definition,
                ih.ivh_invoicestatus,
                st.name,
                ih.ivh_applyto,
                ih.ord_hdrnumber,
                ih.ord_number,
                ih.mov_number,
                ih.ivh_billto,
                b.cmp_name,
                ih.ivh_shipper,
                ih.ivh_consignee,
                ih.ivh_billdate,
                ih.ivh_shipdate,
                ih.ivh_deliverydate,
                ih.ivh_xferdate,
                ih.ivh_currency,
                {_INVOICE_CURRENCY},
                ih.ivh_totalcharge,
                t.tax,
                t.lines,
                ih.ivh_revtype1,
                ih.ivh_revtype4,
                ih.ivh_mbnumber,
                ih.ivh_mbstatus,
                ih.ivh_remark
            FROM picked
            JOIN invoiceheader ih ON ih.ivh_hdrnumber = picked.ivh_hdrnumber
            LEFT JOIN company b ON b.cmp_id = ih.ivh_billto
            LEFT JOIN labelfile st ON st.abbr = ih.ivh_invoicestatus
                AND st.labeldefinition = 'InvoiceStatus'
            OUTER APPLY (
                SELECT COUNT(*) AS lines,
                       SUM(CASE WHEN tc.cht_basis = 'TAX' THEN td.ivd_charge ELSE 0 END) AS tax
                FROM invoicedetail td
                LEFT JOIN chargetype tc ON tc.cht_itemcode = td.cht_itemcode
                WHERE td.ivh_hdrnumber = ih.ivh_hdrnumber
            ) t
            ORDER BY ih.ord_hdrnumber DESC, ih.ivh_hdrnumber
        """, [limit + 1, *params])
        rows = cursor.fetchall()

        cursor.execute(f"""
            SELECT {_INVOICE_CURRENCY}, COUNT(*), SUM(ih.ivh_totalcharge), SUM(t.tax),
                   SUM(CASE WHEN ih.ivh_definition = 'CRD' THEN ih.ivh_totalcharge ELSE 0 END),
                   COUNT(DISTINCT ih.ord_hdrnumber)
            FROM invoiceheader ih
            OUTER APPLY (
                SELECT SUM(CASE WHEN tc.cht_basis = 'TAX' THEN td.ivd_charge ELSE 0 END) AS tax
                FROM invoicedetail td
                LEFT JOIN chargetype tc ON tc.cht_itemcode = td.cht_itemcode
                WHERE td.ivh_hdrnumber = ih.ivh_hdrnumber
            ) t
            WHERE {where}
            GROUP BY {_INVOICE_CURRENCY}
        """, params)
        totals = [
            {
                "currency": r[0],
                "invoices": r[1],
                "orders": r[5],
                "total": _money(r[2]),
                "tax": _money(r[3]),
                "pretax_total": _money((r[2] or 0) - (r[3] or 0)),
                "credits": _money(r[4]),
            }
            for r in cursor.fetchall()
        ]

        def text(v):
            return v.strip() if isinstance(v, str) and v.strip() else None

        invoices = []
        for r in rows[:limit]:
            defn = text(r[2])
            total, tax = float(r[19] or 0), float(r[20] or 0)
            invoices.append({
                "ivh_hdrnumber": r[0],
                "invoice_number": text(r[1]),
                "invoice_type": defn,
                "invoice_type_name": _INVOICE_TYPES.get(defn),
                "status": text(r[3]),
                "status_name": r[4],
                # The invoice a credit memo reverses; on other types, itself.
                "applies_to": text(r[5]),
                "ord_hdrnumber": r[6],
                "ord_number": text(r[7]),
                "mov_number": r[8],
                "billto": text(r[9]),
                "billto_name": r[10],
                "shipper": text(r[11]),
                "consignee": text(r[12]),
                "bill_date": str(r[13]) if r[13] else None,
                "ship_date": str(r[14]) if r[14] else None,
                "delivery_date": str(r[15]) if r[15] else None,
                "transferred_date": str(r[16]) if r[16] else None,
                "currency": r[18],
                "currency_label": text(r[17]),
                # total includes tax; pretax_total is revenue.
                "total": round(total, 2),
                "tax": round(tax, 2),
                "pretax_total": round(total - tax, 2),
                "line_count": r[21] or 0,
                "booking_company": text(r[22]),
                "booking_agent": text(r[23]),
                "master_bill_number": r[24] or None,
                "master_bill_status": text(r[25]),
                "remark": text(r[26]),
            })
        return {
            "invoices": invoices,
            "count": len(invoices),
            "truncated": len(rows) > limit,
            "totals_by_currency": totals,
        }

    def search_invoice_details(
        self,
        billing_state: str = "active",
        include_zero_lines: bool = False,
        limit: int | None = None,
        **filters,
    ) -> dict:
        """Search billing lines, invoiced or not yet invoiced.

        billing_state: "active" (default - invoiced plus pending), "invoiced",
        "pending", or "all", which adds the inactive cancelled and superseded
        lines. Order filters apply through the line's order.
        """
        if not self.conn:
            self.connect()

        states = {
            "active": ("invoiced", "pending"),
            "invoiced": ("invoiced",),
            "pending": ("pending",),
            "all": ("invoiced", "pending", "cancelled", "superseded"),
        }.get((billing_state or "active").lower())
        if states is None:
            raise ValueError(
                "billing_state must be active, invoiced, pending or all, "
                f"got {billing_state!r}")

        limit = min(limit or self.max_search_rows, self.max_search_rows)
        merged = {**_ORDER_FILTERS, **_DETAIL_FILTERS}
        where, params = _build_where(filters, merged)
        where = f"({where}) AND {_LINE_STATE} IN ({', '.join(repr(s) for s in states)})"
        if not include_zero_lines:
            where += " AND d.ivd_charge <> 0"

        cursor = self.conn.cursor()
        cursor.execute(f"""
            WITH picked AS (
                SELECT TOP (?) d.ivd_number
                {_DETAIL_FROM}
                WHERE {where}
                ORDER BY d.ivd_number DESC
            )
            SELECT
                d.ivd_number,
                d.ivh_hdrnumber,
                ih.ivh_invoicenumber,
                ih.ivh_definition,
                ih.ivh_invoicestatus,
                {_LINE_STATE},
                d.ord_hdrnumber,
                oh.ord_number,
                ih.ivh_billdate,
                {_LINE_CURRENCY},
                d.cht_itemcode,
                ch.cht_description,
                d.ivd_type,
                CASE WHEN ch.cht_basis = 'TAX' THEN 1 ELSE 0 END,
                d.ivd_description,
                d.ivd_quantity,
                d.ivd_unit,
                d.ivd_rate,
                d.ivd_rateunit,
                d.ivd_charge,
                d.ivd_sequence,
                d.stp_number,
                d.cmd_code,
                COALESCE(ih.ivh_billto, oh.ord_billto)
            FROM picked
            JOIN invoicedetail d ON d.ivd_number = picked.ivd_number
            LEFT JOIN invoiceheader ih ON ih.ivh_hdrnumber = d.ivh_hdrnumber
            LEFT JOIN orderheader oh   ON oh.ord_hdrnumber = d.ord_hdrnumber
            LEFT JOIN chargetype ch    ON ch.cht_itemcode = d.cht_itemcode
            ORDER BY d.ord_hdrnumber DESC, COALESCE(NULLIF(d.ivh_hdrnumber, 0), 2147483647),
                     d.ivd_sequence, d.ivd_number
        """, [limit + 1, *params])
        rows = cursor.fetchall()

        cursor.execute(f"""
            SELECT {_LINE_CURRENCY}, COUNT(*), SUM(d.ivd_charge),
                   SUM(CASE WHEN ch.cht_basis = 'TAX' THEN d.ivd_charge ELSE 0 END),
                   COUNT(DISTINCT d.ord_hdrnumber)
            {_DETAIL_FROM}
            WHERE {where}
            GROUP BY {_LINE_CURRENCY}
        """, params)
        totals = [
            {
                "currency": r[0],
                "lines": r[1],
                "orders": r[4],
                "total": _money(r[2]),
                "tax": _money(r[3]),
                "pretax_total": _money((r[2] or 0) - (r[3] or 0)),
            }
            for r in cursor.fetchall()
        ]

        def text(v):
            return v.strip() if isinstance(v, str) and v.strip() else None

        lines = [
            {
                "ivd_number": r[0],
                "ivh_hdrnumber": r[1] or None,
                "invoice_number": text(r[2]),
                "invoice_type": text(r[3]),
                "invoice_status": text(r[4]),
                "billing_state": r[5],
                "ord_hdrnumber": r[6] or None,
                "ord_number": text(r[7]),
                "bill_date": str(r[8]) if r[8] else None,
                "currency": r[9],
                "charge_type": text(r[10]),
                "charge_type_name": r[11],
                "line_type": text(r[12]),
                "is_tax": bool(r[13]),
                "description": text(r[14]),
                "quantity": r[15],
                "unit": text(r[16]),
                "rate": _money(r[17]),
                "rate_unit": text(r[18]),
                "charge": _money(r[19]),
                "sequence": r[20],
                "stp_number": r[21] or None,
                "commodity": text(r[22]),
                "billto": text(r[23]),
            }
            for r in rows[:limit]
        ]
        return {
            "lines": lines,
            "count": len(lines),
            "truncated": len(rows) > limit,
            "billing_state": billing_state,
            "totals_by_currency": totals,
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
