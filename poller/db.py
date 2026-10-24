"""SQLite database layer. Switch DATABASE_URL to postgresql://... for production."""
import logging
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

BATTERY_CAPACITY_DEFAULTS: dict[str, float] = {
    "MG4": 64.0,
}
BATTERY_CAPACITY_FALLBACK = 64.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vehicles (
    id          INTEGER PRIMARY KEY,
    vin         TEXT UNIQUE NOT NULL,
    car_type    TEXT,
    year        INTEGER,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY,
    vehicle_id   INTEGER REFERENCES vehicles(id),
    recorded_at  TEXT NOT NULL,
    latitude     REAL,
    longitude    REAL,
    speed_kmh    REAL,
    odometer_km  REAL,
    soc                 REAL,
    outside_temp        REAL,
    inside_temp         REAL,
    climate_target_temp REAL,
    battery_min_temp    REAL,
    range_km            REAL,
    gear             TEXT,
    charging         INTEGER DEFAULT 0,
    is_locked        INTEGER DEFAULT NULL,
    climate_on       INTEGER DEFAULT NULL,
    climate_cooling  INTEGER DEFAULT NULL,
    climate_heating  INTEGER DEFAULT NULL,
    climate_defrost  INTEGER DEFAULT NULL,
    trunk_open       INTEGER DEFAULT NULL,
    windows_open     INTEGER DEFAULT NULL,
    sunshade_open    INTEGER DEFAULT NULL,
    plug_connected   INTEGER DEFAULT NULL,
    remaining_charge_min INTEGER DEFAULT NULL,
    charge_voltage_v REAL DEFAULT NULL,
    charge_current_a REAL DEFAULT NULL,
    door_fl_open         INTEGER DEFAULT NULL,
    door_fr_open         INTEGER DEFAULT NULL,
    door_rl_open         INTEGER DEFAULT NULL,
    door_rr_open         INTEGER DEFAULT NULL,
    bonnet_open          INTEGER DEFAULT NULL,
    aux_battery_v        REAL DEFAULT NULL,
    lights_any           INTEGER DEFAULT NULL,
    heading_deg          REAL DEFAULT NULL,
    tyre_fl_bar          REAL DEFAULT NULL,
    tyre_fr_bar          REAL DEFAULT NULL,
    tyre_rl_bar          REAL DEFAULT NULL,
    tyre_rr_bar          REAL DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS trips (
    id                   INTEGER PRIMARY KEY,
    vehicle_id           INTEGER REFERENCES vehicles(id),
    started_at           TEXT,
    ended_at             TEXT,
    start_lat            REAL,
    start_lon            REAL,
    end_lat              REAL,
    end_lon              REAL,
    distance_km          REAL,
    start_soc            REAL,
    end_soc              REAL,
    start_odometer_km    REAL,
    end_odometer_km      REAL,
    regen_kwh            REAL DEFAULT 0,
    duration_min         REAL,
    efficiency_kwh_100km REAL
);

CREATE TABLE IF NOT EXISTS trip_positions (
    id          INTEGER PRIMARY KEY,
    trip_id     INTEGER REFERENCES trips(id),
    recorded_at TEXT NOT NULL,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    speed_kmh   REAL,
    soc         REAL
);

CREATE TABLE IF NOT EXISTS charges (
    id               INTEGER PRIMARY KEY,
    vehicle_id       INTEGER REFERENCES vehicles(id),
    started_at       TEXT,
    ended_at         TEXT,
    start_soc        REAL,
    end_soc          REAL,
    energy_added_kwh REAL,
    duration_min     REAL,
    latitude         REAL,
    longitude        REAL,
    charge_type      TEXT DEFAULT 'AC',        -- AC / DC (from power level)
    location_type    TEXT DEFAULT NULL,         -- HOME / AC / FAST / HPC (user-set)
    max_power_kw     REAL,
    cost             REAL
);

CREATE INDEX IF NOT EXISTS idx_positions_vehicle ON positions(vehicle_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_trip_positions_trip ON trip_positions(trip_id);
CREATE INDEX IF NOT EXISTS idx_trips_vehicle ON trips(vehicle_id, started_at);
CREATE INDEX IF NOT EXISTS idx_charges_vehicle ON charges(vehicle_id, started_at);

CREATE TABLE IF NOT EXISTS maintenance_items (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    interval_km     INTEGER,
    interval_months INTEGER,
    trigger_mode    TEXT NOT NULL DEFAULT 'either',
    last_done_km    REAL,
    last_done_date  TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS maintenance_logs (
    id           INTEGER PRIMARY KEY,
    item_id      INTEGER REFERENCES maintenance_items(id) ON DELETE CASCADE,
    done_at      TEXT NOT NULL,
    odometer_km  REAL,
    cost         REAL,
    provider     TEXT,
    notes        TEXT
);

CREATE INDEX IF NOT EXISTS idx_maintenance_logs_item ON maintenance_logs(item_id);
"""

# self.get_battery_capacity() is now stored in settings table, not hardcoded

MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        "ALTER TABLE positions ADD COLUMN climate_target_temp REAL",
        "ALTER TABLE positions ADD COLUMN battery_min_temp REAL",
    ]),
    (2, [
        "ALTER TABLE positions ADD COLUMN is_locked INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN climate_on INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN climate_cooling INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN climate_heating INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN climate_defrost INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN trunk_open INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN windows_open INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN sunshade_open INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN plug_connected INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN remaining_charge_min INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN charge_voltage_v REAL DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN charge_current_a REAL DEFAULT NULL",
    ]),
    (3, [
        "ALTER TABLE positions ADD COLUMN door_fl_open INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN door_fr_open INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN door_rl_open INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN door_rr_open INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN bonnet_open INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN aux_battery_v REAL DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN lights_any INTEGER DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN heading_deg REAL DEFAULT NULL",
    ]),
    (4, [
        "ALTER TABLE positions ADD COLUMN tyre_fl_bar REAL DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN tyre_fr_bar REAL DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN tyre_rl_bar REAL DEFAULT NULL",
        "ALTER TABLE positions ADD COLUMN tyre_rr_bar REAL DEFAULT NULL",
    ]),
    (5, [
        "ALTER TABLE trips ADD COLUMN untracked INTEGER DEFAULT 0",
    ]),
]

# Earlier versions seeded these default items with Italian titles; untouched ones
# are renamed on startup.
_LEGACY_ITALIAN_TITLES = {
    "Rotazione pneumatici": "Tyre rotation",
    "Filtro abitacolo": "Cabin air filter",
    "Liquido freni": "Brake fluid",
    "Controllo pastiglie freno": "Brake pad inspection",
    "Tergicristalli": "Wiper blades",
    "Revisione impianto A/C": "A/C system service",
    "Ispezione liquido raffreddamento": "Coolant inspection",
    "Fluido ponte motore": "Drive unit fluid",
    "Controllo batteria HV": "HV battery check",
    "Tagliando completo": "Full service",
}

_MG4_SCHEDULE: list[tuple[str, int | None, int | None, str]] = [
    ("Tyre rotation",                10_000, 12, "either"),
    ("Cabin air filter",                    20_000, 24, "either"),
    ("Brake fluid",                         None, 24, "months"),
    ("Brake pad inspection",           20_000, 24, "either"),
    ("Wiper blades",                        None, 12, "months"),
    ("A/C system service",                None, 24, "months"),
    ("Coolant inspection",    60_000, 60, "either"),
    ("Drive unit fluid",                 40_000, 48, "either"),
    ("HV battery check",                 None, 12, "months"),
    ("Full service",                  30_000, 24, "either"),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_capacity_for(car_type: str) -> float:
    return BATTERY_CAPACITY_DEFAULTS.get(car_type.upper(), BATTERY_CAPACITY_FALLBACK)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(max(0, a)))


class Database:
    def __init__(self, path: str = "mg4_mate.db"):
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._apply_migrations()
        self._seed_maintenance_items()
        log.info("Database ready: %s", path)

    def _apply_migrations(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, "
            " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        applied = {
            r[0] for r in self._conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        for version, stmts in MIGRATIONS:
            if version in applied:
                continue
            for stmt in stmts:
                try:
                    self._conn.execute(stmt)
                except sqlite3.OperationalError as exc:
                    # Safe: SQLite raises this when the column already exists (pre-migration DB)
                    if "duplicate column" not in str(exc).lower():
                        raise
            self._conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
            )
        self._conn.commit()

    def _seed_maintenance_items(self) -> None:
        for old, new in _LEGACY_ITALIAN_TITLES.items():
            self._conn.execute(
                "UPDATE maintenance_items SET title = ? WHERE title = ?", (new, old)
            )
        self._conn.commit()
        count = self._conn.execute(
            "SELECT COUNT(*) FROM maintenance_items"
        ).fetchone()[0]
        if count > 0:
            return
        for title, interval_km, interval_months, trigger_mode in _MG4_SCHEDULE:
            self._conn.execute(
                "INSERT INTO maintenance_items "
                "(title, interval_km, interval_months, trigger_mode) VALUES (?,?,?,?)",
                (title, interval_km, interval_months, trigger_mode),
            )
        self._conn.commit()
        log.info("Seeded %d MG4 maintenance items", len(_MG4_SCHEDULE))

    # ── Settings ─────────────────────────────────────────────────────────────

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value))
        )
        self._conn.commit()

    def get_battery_capacity(self) -> float:
        return float(self.get_setting("battery_capacity_kwh", str(BATTERY_CAPACITY_FALLBACK)))

    def set_battery_capacity(self, kwh: float) -> None:
        self.set_setting("battery_capacity_kwh", str(kwh))
        log.info("Battery capacity set to %.1f kWh", kwh)

    def is_setup_complete(self) -> bool:
        return self.get_setting("setup_complete") == "1"

    def mark_setup_complete(self) -> None:
        self.set_setting("setup_complete", "1")

    # ── Vehicles ─────────────────────────────────────────────────────────────

    def ensure_vehicle(self, vin: str, car_type: str, year: Optional[int] = None) -> int:
        self._conn.execute(
            "INSERT OR IGNORE INTO vehicles (vin, car_type, year) VALUES (?, ?, ?)",
            (vin, car_type, year),
        )
        self._conn.commit()
        row = self._conn.execute("SELECT id FROM vehicles WHERE vin = ?", (vin,)).fetchone()
        return row["id"]

    def save_position(self, vehicle_id: int, data) -> None:
        self.save_position_at(vehicle_id, data, _now_iso())

    def position_exists(self, vehicle_id: int, recorded_at: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM positions WHERE vehicle_id = ? AND recorded_at = ? LIMIT 1",
            (vehicle_id, recorded_at),
        ).fetchone()
        return row is not None

    def save_position_at(self, vehicle_id: int, data, recorded_at: str) -> None:
        self._conn.execute(
            """INSERT INTO positions
               (vehicle_id, recorded_at, latitude, longitude, speed_kmh, odometer_km,
                soc, outside_temp, inside_temp, climate_target_temp, battery_min_temp,
                range_km, gear, charging, is_locked, climate_on,
                climate_cooling, climate_heating, climate_defrost,
                trunk_open, windows_open, sunshade_open, plug_connected,
                remaining_charge_min, charge_voltage_v, charge_current_a,
                door_fl_open, door_fr_open, door_rl_open, door_rr_open,
                bonnet_open, aux_battery_v, lights_any, heading_deg,
                tyre_fl_bar, tyre_fr_bar, tyre_rl_bar, tyre_rr_bar)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                vehicle_id, recorded_at,
                data.latitude, data.longitude, data.speed_kmh, data.odometer_km,
                data.soc, data.outside_temp, data.inside_temp, data.climate_target_temp,
                data.battery_min_temp, data.range_km, data.gear,
                1 if data.charging_status > 0 else 0,
                1 if data.is_locked else 0,
                1 if data.climate_on else 0,
                1 if data.climate_cooling else 0,
                1 if data.climate_heating else 0,
                1 if data.climate_defrost else 0,
                1 if data.trunk_open else 0,
                1 if data.windows_open else 0,
                1 if data.sunshade_open else 0,
                1 if data.plug_connected else 0,
                data.remaining_charge_min or None,
                data.charge_voltage_v or None,
                data.charge_current_a or None,
                1 if data.door_fl_open else 0,
                1 if data.door_fr_open else 0,
                1 if data.door_rl_open else 0,
                1 if data.door_rr_open else 0,
                1 if data.bonnet_open else 0,
                data.aux_battery_v or None,
                1 if (data.lights_dipped or data.lights_main or data.lights_side) else 0,
                data.heading_deg or None,
                data.tyre_fl_bar or None,
                data.tyre_fr_bar or None,
                data.tyre_rl_bar or None,
                data.tyre_rr_bar or None,
            ),
        )
        self._conn.commit()

    # ── Trip ─────────────────────────────────────────────────────────────────

    def create_trip(self, vehicle_id: int, data) -> int:
        cur = self._conn.execute(
            """INSERT INTO trips (vehicle_id, started_at, start_lat, start_lon,
               start_soc, start_odometer_km)
               VALUES (?,?,?,?,?,?)""",
            (vehicle_id, _now_iso(), data.latitude, data.longitude,
             data.soc, data.odometer_km),
        )
        self._conn.commit()
        trip_id = cur.lastrowid
        log.info("Trip #%d started — SOC %.1f%% @ (%.4f, %.4f)", trip_id, data.soc, data.latitude, data.longitude)
        return trip_id

    def add_trip_position(self, trip_id: int, data) -> None:
        # Skip missing GPS: a (0,0) point draws the route to the Gulf of Guinea and
        # breaks fitBounds on the map. Only record real fixes.
        if not data.latitude or not data.longitude:
            return
        self._conn.execute(
            """INSERT INTO trip_positions (trip_id, recorded_at, latitude, longitude, speed_kmh, soc)
               VALUES (?,?,?,?,?,?)""",
            (trip_id, _now_iso(), data.latitude, data.longitude, data.speed_kmh, data.soc),
        )
        self._conn.commit()

    def finalize_trip(self, trip_id: int, data, regen_kwh: float = 0.0) -> None:
        rows = self._conn.execute(
            "SELECT latitude, longitude FROM trip_positions WHERE trip_id = ? ORDER BY id",
            (trip_id,),
        ).fetchall()

        gps_km = sum(
            haversine_km(rows[i]["latitude"], rows[i]["longitude"],
                         rows[i + 1]["latitude"], rows[i + 1]["longitude"])
            for i in range(len(rows) - 1)
        )

        trip = self._conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()

        # Fallback: use odometer delta when GPS positions are stale/missing (gateway inactive)
        odo_delta = 0.0
        if data.odometer_km and trip["start_odometer_km"]:
            odo_delta = max(0.0, data.odometer_km - trip["start_odometer_km"])
        distance_km = max(gps_km, odo_delta)

        start_soc = trip["start_soc"]
        energy_used_kwh = (start_soc - data.soc) / 100.0 * self.get_battery_capacity()
        efficiency = (energy_used_kwh / distance_km * 100) if distance_km > 0.5 else None

        started_at = datetime.fromisoformat(trip["started_at"])
        duration_min = (datetime.now(timezone.utc) - started_at).total_seconds() / 60

        self._conn.execute(
            """UPDATE trips SET ended_at=?, end_lat=?, end_lon=?, end_soc=?,
               end_odometer_km=?, distance_km=?, duration_min=?,
               efficiency_kwh_100km=?, regen_kwh=?
               WHERE id=?""",
            (_now_iso(), data.latitude, data.longitude, data.soc,
             data.odometer_km, round(distance_km, 3), round(duration_min, 1),
             round(efficiency, 2) if efficiency else None, round(regen_kwh, 3),
             trip_id),
        )
        self._conn.commit()
        log.info(
            "Trip #%d ended — %.1f km | SOC %.1f→%.1f%% | %.0f min | eff %.1f kWh/100km",
            trip_id, distance_km, start_soc, data.soc, duration_min,
            efficiency or 0,
        )

    # ── Charge ───────────────────────────────────────────────────────────────

    def create_charge(self, vehicle_id: int, data) -> int:
        start_soc = data.soc
        start_ts  = _now_iso()
        pre = self._conn.execute(
            """SELECT soc, recorded_at FROM positions
               WHERE vehicle_id=? AND (plug_connected=0 OR plug_connected IS NULL)
               AND recorded_at >= datetime('now', '-2 hours')
               ORDER BY recorded_at DESC LIMIT 1""",
            (vehicle_id,)
        ).fetchone()
        if pre and pre["soc"] is not None and pre["soc"] < data.soc:
            start_soc = pre["soc"]
            start_ts  = pre["recorded_at"]
            log.info(
                "Charge start SOC adjusted %.1f%%→%.1f%% from pre-charge position (%s)",
                data.soc, start_soc, pre["recorded_at"]
            )
        cur = self._conn.execute(
            """INSERT INTO charges (vehicle_id, started_at, start_soc, latitude, longitude)
               VALUES (?,?,?,?,?)""",
            (vehicle_id, start_ts, start_soc, data.latitude, data.longitude),
        )
        self._conn.commit()
        charge_id = cur.lastrowid
        log.info("Charge #%d started — SOC %.1f%%", charge_id, start_soc)
        return charge_id

    def finalize_charge(self, charge_id: int, data, max_power_kw: float = 0.0,
                        price_per_kwh: float = 0.0) -> None:
        charge = self._conn.execute("SELECT * FROM charges WHERE id = ?", (charge_id,)).fetchone()
        start_soc    = charge["start_soc"]
        energy_added = max((data.soc - start_soc) / 100.0 * self.get_battery_capacity(), 0)
        charge_type  = "DC" if max_power_kw > 11 else "AC"
        cost         = round(energy_added * price_per_kwh, 2) if price_per_kwh else None

        started_at   = datetime.fromisoformat(charge["started_at"])
        duration_min = (datetime.now(timezone.utc) - started_at).total_seconds() / 60

        self._conn.execute(
            """UPDATE charges
               SET ended_at=?, end_soc=?, energy_added_kwh=?, duration_min=?,
                   charge_type=?, max_power_kw=?, cost=?
               WHERE id=?""",
            (
                _now_iso(), data.soc, round(energy_added, 3), round(duration_min, 1),
                charge_type, round(max_power_kw, 2), cost,
                charge_id,
            ),
        )
        self._conn.commit()
        log.info(
            "Charge #%d ended — SOC %.1f→%.1f%% | +%.1f kWh | %.0f min | %s | peak %.1f kW",
            charge_id, start_soc, data.soc, energy_added, duration_min,
            charge_type, max_power_kw,
        )

    # ── Historical import (used by ha_history.py) ────────────────────────

    def insert_historical_trip(
        self,
        vehicle_id: int,
        started_at: str,
        ended_at: str,
        start_lat: float, start_lon: float,
        end_lat: float, end_lon: float,
        distance_km: float,
        start_soc: float, end_soc: float,
        start_odometer_km: float, end_odometer_km: float,
        duration_min: float,
        efficiency_kwh_100km: Optional[float],
        gps_points: list,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO trips (vehicle_id, started_at, ended_at, start_lat, start_lon,
               end_lat, end_lon, distance_km, start_soc, end_soc, start_odometer_km,
               end_odometer_km, duration_min, efficiency_kwh_100km, regen_kwh)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (vehicle_id, started_at, ended_at, start_lat, start_lon, end_lat, end_lon,
             distance_km, start_soc, end_soc, start_odometer_km, end_odometer_km,
             duration_min, efficiency_kwh_100km, 0.0),
        )
        trip_id = cur.lastrowid
        for pt in gps_points:
            self._conn.execute(
                """INSERT INTO trip_positions (trip_id, recorded_at, latitude, longitude, speed_kmh, soc)
                   VALUES (?,?,?,?,?,?)""",
                (trip_id, pt["recorded_at"], pt["latitude"], pt["longitude"],
                 pt["speed_kmh"], pt["soc"]),
            )
        self._conn.commit()
        return trip_id

    def insert_historical_charge(
        self,
        vehicle_id: int,
        started_at: str,
        ended_at: str,
        start_soc: float, end_soc: float,
        energy_added_kwh: float,
        duration_min: float,
        latitude: float, longitude: float,
        charge_type: str,
        max_power_kw: float,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO charges (vehicle_id, started_at, ended_at, start_soc, end_soc,
               energy_added_kwh, duration_min, latitude, longitude, charge_type, max_power_kw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (vehicle_id, started_at, ended_at, start_soc, end_soc,
             energy_added_kwh, duration_min, latitude, longitude,
             charge_type, max_power_kw),
        )
        self._conn.commit()
        return cur.lastrowid

    # ── Startup cleanup ───────────────────────────────────────────────────

    def close_orphan_trips(self, vehicle_id: int) -> int:
        """
        Called at poller startup. Finalizes any trip left open by a previous
        crash using the last recorded trip_position as the end point.
        Returns number of trips closed.
        """
        orphans = self._conn.execute(
            "SELECT id, start_soc, start_odometer_km, started_at FROM trips "
            "WHERE vehicle_id = ? AND ended_at IS NULL",
            (vehicle_id,),
        ).fetchall()

        closed = 0
        for trip in orphans:
            trip_id = trip["id"]
            last_pos = self._conn.execute(
                "SELECT * FROM trip_positions WHERE trip_id = ? ORDER BY id DESC LIMIT 1",
                (trip_id,),
            ).fetchone()

            positions = self._conn.execute(
                "SELECT latitude, longitude FROM trip_positions WHERE trip_id = ? ORDER BY id",
                (trip_id,),
            ).fetchall()

            if not last_pos or len(positions) < 2:
                # Not enough data — delete the orphan
                self._conn.execute("DELETE FROM trip_positions WHERE trip_id = ?", (trip_id,))
                self._conn.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
                log.warning("Trip #%d had no usable positions — deleted", trip_id)
            else:
                distance_km = sum(
                    haversine_km(
                        positions[i]["latitude"], positions[i]["longitude"],
                        positions[i + 1]["latitude"], positions[i + 1]["longitude"],
                    )
                    for i in range(len(positions) - 1)
                )
                start_soc = trip["start_soc"] or 0
                end_soc   = float(last_pos["soc"] or start_soc)
                energy    = (start_soc - end_soc) / 100.0 * self.get_battery_capacity()
                efficiency = (energy / distance_km * 100) if distance_km > 0.5 else None

                started_at   = datetime.fromisoformat(trip["started_at"])
                ended_at_iso = last_pos["recorded_at"]
                ended_at_dt  = datetime.fromisoformat(ended_at_iso)
                duration_min = (ended_at_dt - started_at).total_seconds() / 60

                self._conn.execute(
                    """UPDATE trips SET ended_at=?, end_lat=?, end_lon=?, end_soc=?,
                       distance_km=?, duration_min=?, efficiency_kwh_100km=?
                       WHERE id=?""",
                    (
                        ended_at_iso,
                        last_pos["latitude"], last_pos["longitude"], end_soc,
                        round(distance_km, 3), round(duration_min, 1),
                        round(efficiency, 2) if efficiency else None,
                        trip_id,
                    ),
                )
                log.warning(
                    "Trip #%d was open (crash recovery) — closed at last known position "
                    "%.1f km | %.0f min",
                    trip_id, distance_km, duration_min,
                )
            closed += 1

        if orphans:
            self._conn.commit()
        return closed

    def close_orphan_charges(self, vehicle_id: int) -> int:
        """
        Called at poller startup. Finalizes any charge session left open
        using the last recorded position as the end point.
        Returns number of charges closed.
        """
        orphans = self._conn.execute(
            "SELECT id, start_soc, started_at FROM charges "
            "WHERE vehicle_id = ? AND ended_at IS NULL",
            (vehicle_id,),
        ).fetchall()

        closed = 0
        for charge in orphans:
            last_pos = self._conn.execute(
                "SELECT soc, recorded_at FROM positions "
                "WHERE vehicle_id = ? AND recorded_at >= ? ORDER BY id DESC LIMIT 1",
                (vehicle_id, charge["started_at"]),
            ).fetchone()

            end_soc      = float((last_pos["soc"] if last_pos else None) or charge["start_soc"] or 0)
            ended_at_iso = (last_pos["recorded_at"] if last_pos else None) or _now_iso()
            energy_added = max((end_soc - charge["start_soc"]) / 100.0 * self.get_battery_capacity(), 0)

            started_at   = datetime.fromisoformat(charge["started_at"])
            ended_at_dt  = datetime.fromisoformat(ended_at_iso)
            duration_min = (ended_at_dt - started_at).total_seconds() / 60

            self._conn.execute(
                "UPDATE charges SET ended_at=?, end_soc=?, energy_added_kwh=?, duration_min=? WHERE id=?",
                (ended_at_iso, end_soc, round(energy_added, 3), round(duration_min, 1), charge["id"]),
            )
            log.warning(
                "Charge #%d was open (crash recovery) — closed: SOC %.1f→%.1f%% +%.1f kWh",
                charge["id"], charge["start_soc"], end_soc, energy_added,
            )
            closed += 1

        if orphans:
            self._conn.commit()
        return closed

    def get_open_charge(self, vehicle_id: int):
        """Latest charge session left open (ended_at NULL), or None."""
        return self._conn.execute(
            "SELECT id, start_soc, max_power_kw, started_at FROM charges "
            "WHERE vehicle_id = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (vehicle_id,),
        ).fetchone()

    def create_ghost_trip(self, vehicle_id: int, dist_km: float, started_at: str, ended_at: str) -> int:
        """Create a trip record for driving that wasn't captured live (odometer jump while parked)."""
        cur = self._conn.execute(
            """INSERT INTO trips (vehicle_id, started_at, ended_at, distance_km, untracked)
               VALUES (?,?,?,?,1)""",
            (vehicle_id, started_at, ended_at, round(dist_km, 3)),
        )
        self._conn.commit()
        trip_id = cur.lastrowid
        log.info("Ghost trip #%d created — %.1f km (%s → %s)", trip_id, dist_km, started_at, ended_at)
        return trip_id

    def get_last_position_recorded_at(self, vehicle_id: int) -> Optional[str]:
        """Return the second-to-last recorded_at for the given vehicle (used as ghost trip start time)."""
        row = self._conn.execute(
            "SELECT recorded_at FROM positions WHERE vehicle_id=? ORDER BY id DESC LIMIT 1 OFFSET 1",
            (vehicle_id,),
        ).fetchone()
        return row["recorded_at"] if row else None

    def get_open_trip(self, vehicle_id: int):
        """Latest trip left open (ended_at NULL), or None."""
        return self._conn.execute(
            "SELECT id, started_at FROM trips WHERE vehicle_id = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (vehicle_id,),
        ).fetchone()

    def get_last_trip_position_time(self, trip_id: int) -> Optional[str]:
        """Return recorded_at of the most recent GPS point for a trip, or None."""
        row = self._conn.execute(
            "SELECT recorded_at FROM trip_positions WHERE trip_id=? ORDER BY id DESC LIMIT 1",
            (trip_id,),
        ).fetchone()
        return row["recorded_at"] if row else None

    def get_recent_ended_trip(self, vehicle_id: int, within_seconds: int):
        """Return the most recently ended non-ghost trip if it ended within `within_seconds` ago."""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_seconds)).strftime('%Y-%m-%dT%H:%M:%S')
        row = self._conn.execute(
            """SELECT * FROM trips
               WHERE vehicle_id=? AND ended_at IS NOT NULL AND ended_at >= ?
               AND (untracked IS NULL OR untracked = 0)
               ORDER BY ended_at DESC LIMIT 1""",
            (vehicle_id, cutoff)
        ).fetchone()
        return dict(row) if row else None

    def reopen_trip(self, trip_id: int) -> None:
        """Clear finalization fields so the trip is open again for accumulation."""
        self._conn.execute(
            """UPDATE trips
               SET ended_at=NULL, end_soc=NULL, end_odometer_km=NULL,
                   duration_min=NULL, efficiency_kwh_100km=NULL
               WHERE id=?""",
            (trip_id,)
        )
        self._conn.commit()

    def get_setting_value(self, key: str, default: str = "") -> str:
        """Read a settings value (thin wrapper used by recorder)."""
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def update_charge_max_power(self, charge_id: int, max_power_kw: float) -> None:
        """Persist the running peak power so it survives a poller restart mid-charge."""
        self._conn.execute(
            "UPDATE charges SET max_power_kw = ? WHERE id = ?",
            (round(max_power_kw, 2), charge_id),
        )
        self._conn.commit()

    def close(self):
        self._conn.close()
