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


def _build_where(filters: dict) -> tuple[str, list]:
    """Turn a dict of filters into a WHERE clause and its parameters.

    Unknown filter names raise rather than being silently ignored, so a
    mistyped filter can never widen the result set.
    """
    clauses: list[str] = []
    params: list = []

    for name, value in filters.items():
        if value is None:
            continue
        spec = _ORDER_FILTERS.get(name)
        if spec is None:
            valid = ", ".join(sorted(_ORDER_FILTERS))
            raise ValueError(f"Unknown filter {name!r}. Valid filters: {valid}")
        fragment, kind = spec

        if kind == "list":
            values = [value] if isinstance(value, (str, int)) else list(value)
            if not values:
                continue
            clauses.append(fragment.format(ph=", ".join("?" * len(values))))
            params.extend(values)
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
