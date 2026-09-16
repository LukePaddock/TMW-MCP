import pyodbc


class TmwDB:
    def __init__(
        self,
        server: str,
        database: str,
        driver: str = "SQL Server Native Client 11.0",
        user: str | None = None,
        password: str | None = None,
        timeout: int = 30,
        checkcall_lookback_days: int = 30,
    ):
        self.server = server
        self.database = database
        self.driver = driver
        self.user = user
        self.password = password
        self.timeout = timeout
        self.checkcall_lookback_days = checkcall_lookback_days
        self.conn = None

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
            checkcall_lookback_days=settings.checkcall_lookback_days,
        )

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

        self.conn = pyodbc.connect(conn_str, timeout=self.timeout)

    def close(self):
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
