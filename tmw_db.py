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
}

# group_by name -> (key expression, optional label expression)
_ORDER_GROUPS: dict[str, tuple[str, str | None]] = {
    "revtype1":       ("oh.ord_revtype1", None),
    "revtype2":       ("oh.ord_revtype2", None),
    "revtype3":       ("oh.ord_revtype3", None),
    "revtype4":       ("oh.ord_revtype4", None),
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
    "min_length":       ("f.fgt_length >= ?", "scalar"),
    "min_width":        ("f.fgt_width >= ?", "scalar"),
    "min_height":       ("f.fgt_height >= ?", "scalar"),
    "max_temp":         ("f.fgt_hitemp <= ?", "scalar"),
    "min_temp":         ("f.fgt_lowtemp >= ?", "scalar"),
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

    `spec_table` defaults to the order filters; freight searches pass the merged
    freight + order table so one assembler serves both.

    Unknown filter names raise rather than being silently ignored, so a
    mistyped filter can never widen the result set.
    """
    table = _ORDER_FILTERS if spec_table is None else spec_table
    clauses: list[str] = []
    params: list = []

    for name, value in filters.items():
        if value is None:
            continue
        spec = table.get(name)
        if spec is None:
            valid = ", ".join(sorted(table))
            raise ValueError(f"Unknown filter {name!r}. Valid filters: {valid}")
        fragment, kind = spec

        if kind == "list":
            values = [value] if isinstance(value, (str, int)) else list(value)
            if not values:
                continue
            clauses.append(fragment.format(ph=", ".join("?" * len(values))))
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
            "every order in the system."
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

    def get_order_stops(self, order_ids: list[str]) -> list[dict]:
        if not order_ids:
            return []

        if not self.conn:
            self.connect()

        placeholders = ", ".join("?" * len(order_ids))
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
            LEFT JOIN legheader lh ON lh.lgh_number = s.lgh_number
            LEFT JOIN company c ON c.cmp_id = s.cmp_id
            LEFT JOIN labelfile lf ON lf.abbr = s.stp_type3
                AND lf.labeldefinition = 'StpType3'
            LEFT JOIN city cty ON cty.cty_code = s.stp_city
            LEFT JOIN eventcodetable ect ON ect.abbr = s.stp_event
            WHERE s.mov_number IN (
                SELECT DISTINCT mov_number FROM stops ss
                WHERE ss.ord_hdrnumber IN ({placeholders})
            )
            ORDER BY
                MIN(s.stp_arrivaldate) OVER (PARTITION BY s.mov_number),
                s.mov_number,
                s.lgh_number,
                s.stp_arrivaldate
        """, order_ids)

        return [self._stop_row_to_dict(row) for row in cursor.fetchall()]

    def get_leg_stops(self, leg_ids: list[str]) -> list[dict]:
        if not leg_ids:
            return []

        if not self.conn:
            self.connect()

        placeholders = ", ".join("?" * len(leg_ids))
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
            LEFT JOIN legheader lh ON lh.lgh_number = s.lgh_number
            LEFT JOIN company c ON c.cmp_id = s.cmp_id
            LEFT JOIN labelfile lf ON lf.abbr = s.stp_type3
                AND lf.labeldefinition = 'StpType3'
            LEFT JOIN city cty ON cty.cty_code = s.stp_city
            LEFT JOIN eventcodetable ect ON ect.abbr = s.stp_event
            WHERE s.lgh_number IN ({placeholders})
            ORDER BY
                MIN(s.stp_arrivaldate) OVER (PARTITION BY s.mov_number),
                s.mov_number,
                s.lgh_number,
                s.stp_arrivaldate
        """, leg_ids)

        return [self._stop_row_to_dict(row) for row in cursor.fetchall()]

    def get_movement_stops(self, mov_ids: list[str]) -> list[dict]:
        if not mov_ids:
            return []

        if not self.conn:
            self.connect()

        placeholders = ", ".join("?" * len(mov_ids))
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
            LEFT JOIN legheader lh ON lh.lgh_number = s.lgh_number
            LEFT JOIN company c ON c.cmp_id = s.cmp_id
            LEFT JOIN labelfile lf ON lf.abbr = s.stp_type3
                AND lf.labeldefinition = 'StpType3'
            LEFT JOIN city cty ON cty.cty_code = s.stp_city
            LEFT JOIN eventcodetable ect ON ect.abbr = s.stp_event
            WHERE s.mov_number IN ({placeholders})
            ORDER BY
                MIN(s.stp_arrivaldate) OVER (PARTITION BY s.mov_number),
                s.mov_number,
                s.lgh_number,
                s.stp_arrivaldate
        """, mov_ids)

        return [self._stop_row_to_dict(row) for row in cursor.fetchall()]

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
                oh.mov_number
            {_ORDER_FROM}
            WHERE {where}
            ORDER BY oh.ord_startdate DESC
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
                    "revtype2": row[11],
                    "start_date": str(row[12]) if row[12] else None,
                    "completion_date": str(row[13]) if row[13] else None,
                    "origin": row[14],
                    "destination": row[15],
                    "miles": row[16],
                    "weight": row[17],
                    "total_charge": row[18],
                    "mov_number": row[19],
                }
                for row in rows[:limit]
            ],
            "count": min(len(rows), limit),
            "truncated": truncated,
        }

    def summarize_orders(self, group_by: str, limit: int | None = None, **filters) -> dict:
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

        cursor = self.conn.cursor()
        cursor.execute(f"""
            SELECT TOP (?)
                {key_expr} AS grp,
                {label_select}
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
            "groups": [
                {
                    "group": row[0],
                    "label": row[1],
                    "order_count": row[2],
                    "total_charge": row[3],
                    "total_miles": row[4],
                    "total_weight": row[5],
                    "rev_per_mile": round(row[6], 3) or 0.0 if row[6] is not None else None,
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
        }

    _FREIGHT_SELECT = """
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
        f.fgt_sequence
    """

    def search_freight(
        self,
        stop_type: str | None = "DRP",
        weight_basis: str = "LBS",
        limit: int | None = None,
        **filters,
    ) -> dict:
        """Search freight lines, defaulting to the authoritative DRP copies.

        Accepts freight filters and, because orderheader is joined as `oh`, every
        order filter too - so "oversize freight for this customer last quarter"
        is a single query. A `stop_type` of None or "ANY" searches every copy.

        `min_weight` / `max_weight` are compared in pounds after normalising each
        row. `weight_basis` says which unit the threshold itself is in, so
        min_weight=10000 with weight_basis="KGS" means 10,000 kg and matches a
        22,046 lb row.
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
                    MIN(f.fgt_lowtemp)           AS low_temp,
                    MAX(f.fgt_hitemp)            AS high_temp,
                    MAX(f.fgt_length)            AS max_length,
                    MAX(f.fgt_width)             AS max_width,
                    MAX(f.fgt_height)            AS max_height
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
                "low_temp": row[5],
                "high_temp": row[6],
                "max_length": row[7],
                "max_width": row[8],
                "max_height": row[9],
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
