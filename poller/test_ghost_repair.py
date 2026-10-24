"""Untracked (ghost) trips get odometer/SOC/duration, and old ones are repaired."""
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "poller"))     # repo layout: tests/ next to poller/
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))  # or run from inside poller/

from db import Database  # noqa: E402
from ha_history import backfill_ghost_trip_times  # noqa: E402
from recorder import Recorder  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402


def _make_data(odometer_km=50000.0, soc=80.0):
    """Minimal VehicleData stand-in for a parked car."""
    d = MagicMock()
    d.odometer_km, d.soc = odometer_km, soc
    d.vehicle_state, d.speed_kmh, d.gear = "parked", 0.0, "P"
    d.charging_status, d.plug_connected = 0, False
    d.latitude, d.longitude = -27.47, 153.02
    d.outside_temp, d.inside_temp, d.range_km = 20.0, 22.0, 200.0
    d.climate_target_temp = d.battery_min_temp = None
    d.is_locked = True
    for name in ("climate_on", "climate_cooling", "climate_heating", "climate_defrost", "trunk_open",
                 "windows_open", "sunshade_open", "door_fl_open", "door_fr_open", "door_rl_open",
                 "door_rr_open", "bonnet_open", "lights_dipped", "lights_main", "lights_side"):
        setattr(d, name, False)
    d.remaining_charge_min = d.charge_voltage_v = d.aux_battery_v = d.heading_deg = None
    d.tyre_fl_bar = d.tyre_fr_bar = d.tyre_rl_bar = d.tyre_rr_bar = None
    d.charge_current_a = d.charge_power_kw = 0.0
    d.last_powered_on = d.last_powered_off = None
    return d


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


class GhostRepairTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db = Database(self._tmp.name)
        self.vid = self.db.ensure_vehicle("TESTVIN", "MG4")

    def tearDown(self):
        self.db.close()
        os.unlink(self._tmp.name)

    def _trip(self):
        return self.db._conn.execute("SELECT * FROM trips ORDER BY id DESC LIMIT 1").fetchone()

    def _jump(self, **extra):
        rec = Recorder(self.db, self.vid)
        rec.process(_make_data(odometer_km=1000.0, soc=80.0))
        after = _make_data(odometer_km=1012.0, soc=77.0)
        for k, v in extra.items():
            setattr(after, k, v)
        rec.process(after)

    def test_ghost_trip_uses_car_power_timestamps(self):
        now = datetime.now(timezone.utc)
        on, off = now - timedelta(minutes=40), now - timedelta(minutes=25)
        self._jump(last_powered_on=iso(on), last_powered_off=iso(off))
        t = self._trip()
        self.assertEqual((t["start_odometer_km"], t["end_odometer_km"]), (1000.0, 1012.0))
        self.assertEqual((t["start_soc"], t["end_soc"]), (80.0, 77.0))
        self.assertAlmostEqual(t["duration_min"], 15.0, places=1)
        self.assertEqual(t["started_at"], iso(on))
        self.assertIsNotNone(t["efficiency_kwh_100km"])

    def test_ghost_trip_without_timestamps_still_gets_odometer_and_soc(self):
        self._jump(last_powered_on=None, last_powered_off=None)
        t = self._trip()
        self.assertEqual((t["start_soc"], t["end_soc"]), (80.0, 77.0))
        self.assertEqual(t["end_odometer_km"], 1012.0)
        self.assertIsNone(t["duration_min"])

    def test_backfill_from_positions(self):
        base = datetime.now(timezone.utc) - timedelta(hours=3)
        steps = [(0, 1000.0, 80.0), (30, 1000.0, 80.0), (60, 1012.0, 77.0), (90, 1012.0, 77.0)]
        for minutes, odo, soc in steps:
            self.db.save_position_at(self.vid, _make_data(odometer_km=odo, soc=soc), iso(base + timedelta(minutes=minutes)))
        # old-style ghost trip: distance only, bracketing the poll before the jump
        self.db.create_ghost_trip(self.vid, 12.0, iso(base + timedelta(minutes=30)), iso(base + timedelta(minutes=60)))
        self.assertEqual(self.db.backfill_ghost_trips(self.vid), 1)
        t = self._trip()
        self.assertEqual((t["start_odometer_km"], t["end_odometer_km"]), (1000.0, 1012.0))
        self.assertEqual((t["start_soc"], t["end_soc"]), (80.0, 77.0))
        self.assertIsNotNone(t["efficiency_kwh_100km"])
        self.assertEqual(self.db.backfill_ghost_trips(self.vid), 0)  # idempotent

    def test_backfill_skips_when_positions_do_not_match(self):
        base = datetime.now(timezone.utc) - timedelta(hours=3)
        self.db.save_position_at(self.vid, _make_data(odometer_km=1000.0, soc=80.0), iso(base))
        self.db.save_position_at(self.vid, _make_data(odometer_km=1030.0, soc=70.0), iso(base + timedelta(minutes=30)))
        self.db.create_ghost_trip(self.vid, 12.0, iso(base), iso(base + timedelta(minutes=30)))
        self.assertEqual(self.db.backfill_ghost_trips(self.vid), 0)

    def test_times_from_ha_history(self):
        ended = datetime.now(timezone.utc) - timedelta(hours=2)
        on, off = ended - timedelta(minutes=50), ended - timedelta(minutes=35)
        self.db.create_ghost_trip(self.vid, 12.0, iso(ended - timedelta(seconds=30)), iso(ended))

        class FakeClient:
            _entity_prefix = "car"

            def fetch_entities(self, ids, days):
                return [
                    [{"entity_id": "sensor.car_last_powered_on", "state": iso(on), "last_changed": iso(ended - timedelta(seconds=5))}],
                    [{"entity_id": "sensor.car_last_powered_off", "state": iso(off), "last_changed": iso(ended - timedelta(seconds=5))}],
                ]

        self.assertEqual(backfill_ghost_trip_times(self.db, self.vid, FakeClient()), 1)
        t = self._trip()
        self.assertEqual((t["started_at"], t["ended_at"]), (iso(on), iso(off)))
        self.assertAlmostEqual(t["duration_min"], 15.0, places=1)
        self.assertEqual(backfill_ghost_trip_times(self.db, self.vid, FakeClient()), 0)


if __name__ == "__main__":
    unittest.main()
