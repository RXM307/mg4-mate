"""Regen from the battery power sensor: live, from history, and the sign convention."""
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "poller"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import entity_map as em  # noqa: E402
from db import Database  # noqa: E402
from ha_history import HistoryImporter  # noqa: E402
from recorder import Recorder  # noqa: E402
from regen import MAX_STEP_S, RegenTracker, regen_is_negative  # noqa: E402
from test_entity_aliases import client, urban_states  # noqa: E402
from test_history_repair import FakeClient, P, ev  # noqa: E402
from test_trip_continuity import _make_data  # noqa: E402


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


class TrackerTest(unittest.TestCase):
    def test_integrates_both_signs(self):
        t = RegenTracker()
        t.add(36.0, 100)      # 1 kWh out
        t.add(-18.0, 100)     # 0.5 kWh in
        self.assertAlmostEqual(t.pos_kwh, 1.0)
        self.assertAlmostEqual(t.neg_kwh, 0.5)

    def test_stale_readings_are_capped_and_junk_ignored(self):
        t = RegenTracker()
        t.add(-36.0, 3600)    # an hour-long gap only counts for MAX_STEP_S
        self.assertAlmostEqual(t.neg_kwh, 36.0 * MAX_STEP_S / 3600)
        for junk in (None, "x", True, float("nan")):
            t.add(junk, 10)
        t.add(-5.0, 0)
        self.assertAlmostEqual(t.neg_kwh, 36.0 * MAX_STEP_S / 3600)

    def test_convention(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(os.path.join(tmp, "t.db"))
            self.assertTrue(regen_is_negative(db, pos_kwh=5.0, neg_kwh=1.0))    # positive dominates -> negative is regen
            self.assertFalse(regen_is_negative(db, pos_kwh=1.0, neg_kwh=5.0))
            db.set_setting("regen_sign", "positive")
            self.assertFalse(regen_is_negative(db, pos_kwh=5.0, neg_kwh=1.0))   # forced
            db.set_setting("regen_sign", "negative")
            self.assertTrue(regen_is_negative(db, pos_kwh=1.0, neg_kwh=5.0))
            db.close()


class EntityTest(unittest.TestCase):
    def test_battery_power_comes_from_instant_power_not_the_charger_sensors(self):
        d = client(urban_states(**{"sensor.instant_power": "-12.5", "sensor.charging_power": "0"})).get_status()
        self.assertEqual(d.battery_power_kw, -12.5)
        self.assertEqual(d.charge_power_kw, 0.0)

    def test_watts_are_converted_and_missing_is_none(self):
        states = {f"sensor.{P}_instant_power": {"state": "-12500", "attributes": {"unit_of_measurement": "W"}}}
        self.assertEqual(em.battery_power_kw(states, P), -12.5)
        self.assertIsNone(em.battery_power_kw({}, P))
        self.assertIsNone(em.battery_power_kw({f"sensor.{P}_instant_power": {"state": "unavailable"}}, P))


class LiveTripTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db = Database(self._tmp.name)
        self.vid = self.db.ensure_vehicle("TESTVIN", "MG4")

    def tearDown(self):
        self.db.close()
        os.unlink(self._tmp.name)

    def drive(self, powers, **extra):
        clock = {"t": 1000.0}
        rec = Recorder(self.db, self.vid)
        with mock.patch("time.monotonic", lambda: clock["t"]):
            def step(**kw):
                clock["t"] += 10
                d = _make_data(**kw)
                for k, v in extra.items():
                    setattr(d, k, v)
                return d

            rec.process(step())                                       # parked
            for p in powers:
                d = step(vehicle_state="driving", speed_kmh=40.0, odometer_km=50001.0, soc=79.0)
                d.battery_power_kw = p
                rec.process(d)
            for _ in range(2):                                        # parked twice ends the trip
                rec.process(step(odometer_km=50003.0, soc=78.0))
        return self.db._conn.execute("SELECT * FROM trips ORDER BY id DESC LIMIT 1").fetchone()

    def test_regen_is_the_minority_sign(self):
        trip = self.drive([20, -15, -15, 25])
        self.assertAlmostEqual(trip["regen_kwh"], 30 * 10 / 3600, places=3)

    def test_works_with_the_opposite_sign_convention(self):
        trip = self.drive([-20, 15, 15, -25])
        self.assertAlmostEqual(trip["regen_kwh"], 30 * 10 / 3600, places=3)

    def test_forced_convention_wins(self):
        self.db.set_setting("regen_sign", "positive")
        trip = self.drive([20, -15, -15, 25])
        self.assertAlmostEqual(trip["regen_kwh"], 45 * 10 / 3600, places=3)

    def test_votes_are_remembered(self):
        self.drive([20, -15, -15, 25])
        self.assertAlmostEqual(float(self.db.get_setting("regen_votes_pos")), 45 * 10 / 3600, places=3)

    def test_saic_gateway_path_is_unchanged(self):
        # unchanged legacy behaviour: 3 driving polls plus the first parked poll before the trip closes
        trip = self.drive([None, None, None], charge_current_a=-5.0, charge_power_kw=10.0)
        self.assertAlmostEqual(trip["regen_kwh"], 10 * 4 * 10 / 3600, places=3)

    def test_no_power_sensor_means_zero_not_an_error(self):
        self.assertEqual(self.drive([None, None])["regen_kwh"], 0)


class HistoryRegenTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db = Database(self._tmp.name)
        self.vid = self.db.ensure_vehicle(P.upper(), "MG4")
        self.t0 = datetime.now(timezone.utc) - timedelta(days=2)

    def tearDown(self):
        self.db.close()
        os.unlink(self._tmp.name)

    def add_trip(self, start, end, regen=0.0):
        self.db._conn.execute(
            "INSERT INTO trips (vehicle_id, started_at, ended_at, distance_km, untracked, regen_kwh) VALUES (?,?,?,?,0,?)",
            (self.vid, iso(start), iso(end), 5.0, regen))
        self.db._conn.commit()

    def history(self):
        s = f"sensor.{P}_instant_power"
        t0 = self.t0
        return [[ev(s, 20, t0), ev(s, -15, t0 + timedelta(seconds=10)),
                 ev(s, 25, t0 + timedelta(seconds=25)), ev(s, 0, t0 + timedelta(seconds=40))]]

    def test_existing_trip_gets_regen(self):
        self.add_trip(self.t0, self.t0 + timedelta(seconds=40))
        res = HistoryImporter(self.db, self.vid, P).repair_days(FakeClient(self.history()), 30)
        self.assertEqual(res["trips_regen"], 1)
        regen = self.db._conn.execute("SELECT regen_kwh FROM trips").fetchone()[0]
        self.assertAlmostEqual(regen, 15 * 15 / 3600, delta=0.001)  # 20 and 25 kW out; -15 kW for 15 s back in

    def test_trips_with_a_regen_value_or_no_samples_are_left_alone(self):
        self.add_trip(self.t0, self.t0 + timedelta(seconds=40), regen=0.5)
        self.add_trip(self.t0 + timedelta(hours=5), self.t0 + timedelta(hours=5, seconds=40))
        res = HistoryImporter(self.db, self.vid, P).repair_days(FakeClient(self.history()), 30)
        self.assertEqual(res["trips_regen"], 0)
        self.assertEqual([r[0] for r in self.db._conn.execute("SELECT regen_kwh FROM trips ORDER BY id")], [0.5, 0.0])

    def test_untracked_trips_are_not_given_regen(self):
        self.db.create_ghost_trip(self.vid, 5.0, iso(self.t0), iso(self.t0 + timedelta(seconds=40)))
        res = HistoryImporter(self.db, self.vid, P).repair_days(FakeClient(self.history()), 30)
        self.assertEqual(res["trips_regen"], 0)


if __name__ == "__main__":
    unittest.main()
