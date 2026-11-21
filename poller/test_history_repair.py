"""repair_days: fix old SOC/odometer data and import missed charges from HA history."""
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "poller"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from db import Database  # noqa: E402
from ha_history import HistoryImporter  # noqa: E402
from test_ghost_repair import _make_data  # noqa: E402

P = "mg_mg4_ev_urban"


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def ev(eid, state, when, **attrs):
    t = iso(when)
    return {"entity_id": eid, "state": str(state), "attributes": attrs, "last_changed": t, "last_updated": t}


class FakeClient:
    _entity_prefix = P

    def __init__(self, history):
        self._history = history

    def fetch_history(self, days):
        return self._history

    def fetch_entities(self, ids, days):
        return self._history


class HistoryRepairTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db = Database(self._tmp.name)
        self.vid = self.db.ensure_vehicle(P.upper(), "MG4")
        self.t0 = datetime.now(timezone.utc) - timedelta(hours=8)

    def tearDown(self):
        self.db.close()
        os.unlink(self._tmp.name)

    def at(self, minutes):
        return self.t0 + timedelta(minutes=minutes)

    def _history(self, charge=False):
        s = lambda name, domain="sensor": f"{domain}.{P}_{name}"  # noqa: E731
        rows = [
            # parked at 80%, 1000 km; drive of 12 km (powered 40-25 min before refresh at +60); parked at 77%
            ev(s("state_of_charge"), 80, self.at(0)), ev(s("state_of_charge"), 77, self.at(60)),
            ev(s("mileage"), 1000, self.at(0)), ev(s("mileage"), 1012, self.at(60)),
            ev(s("last_powered_on"), iso(self.at(20)), self.at(60)),
            ev(s("last_powered_off"), iso(self.at(35)), self.at(60)),
            ev(s("speed"), 0, self.at(0)),
            ev(s("charging_status"), "Unplugged", self.at(0)),
            ev(s("charging_gun_state", "binary_sensor"), "off", self.at(0)),
        ]
        if charge:
            rows += [
                ev(s("charging_gun_state", "binary_sensor"), "on", self.at(180)),
                ev(s("charging_status"), "Charging", self.at(180)),
                ev(s("state_of_charge"), 85, self.at(180)),
                ev(s("state_of_charge"), 92, self.at(240)),
                ev(s("charging_power"), 7.2, self.at(240), unit_of_measurement="kW"),
                ev(s("charging_gun_state", "binary_sensor"), "off", self.at(300)),
                ev(s("charging_status"), "Unplugged", self.at(300)),
                ev(s("state_of_charge"), 95, self.at(300)),
            ]
        return [rows]

    def _old_positions(self):
        # what the old code stored: SOC 0 because no entity matched, odometer fine
        for minutes, odo in [(0, 1000.0), (30, 1000.0), (60, 1012.0), (90, 1012.0)]:
            self.db.save_position_at(self.vid, _make_data(odometer_km=odo, soc=0.0), iso(self.at(minutes)))

    def test_positions_get_real_soc_and_ghost_trip_is_refixed(self):
        self._old_positions()
        self.db.create_ghost_trip(self.vid, 12.0, iso(self.at(30)), iso(self.at(60)))
        # an earlier run of the position-only backfill wrote placeholder zeros
        self.db._conn.execute("UPDATE trips SET start_soc=0, end_soc=0, start_odometer_km=1000, end_odometer_km=1012")
        self.db._conn.commit()
        res = HistoryImporter(self.db, self.vid, P).repair_days(FakeClient(self._history()), 30)
        socs = [r[0] for r in self.db._conn.execute("SELECT soc FROM positions ORDER BY recorded_at")]
        self.assertEqual(socs, [80.0, 80.0, 77.0, 77.0])
        t = self.db._conn.execute("SELECT * FROM trips").fetchone()
        self.assertEqual((t["start_soc"], t["end_soc"]), (80.0, 77.0))
        self.assertAlmostEqual(t["duration_min"], 15.0, places=1)
        self.assertIsNotNone(t["efficiency_kwh_100km"])
        self.assertEqual((res["positions_fixed"], res["positions_added"]), (4, 0))

    def test_missed_charge_is_imported_once(self):
        self._old_positions()
        imp = HistoryImporter(self.db, self.vid, P)
        self.assertEqual(imp.repair_days(FakeClient(self._history(charge=True)), 30)["charges"], 1)
        c = self.db._conn.execute("SELECT * FROM charges").fetchone()
        self.assertEqual(c["start_soc"], 77.0)          # reading from just before plug-in
        self.assertEqual(c["end_soc"], 95.0)            # final reading after unplug
        self.assertGreater(c["energy_added_kwh"], 0)
        self.assertEqual(imp.repair_days(FakeClient(self._history(charge=True)), 30)["charges"], 0)
        self.assertEqual(self.db._conn.execute("SELECT COUNT(*) FROM charges").fetchone()[0], 1)

    def test_charge_already_recorded_is_not_duplicated(self):
        self.db.insert_historical_charge(self.vid, iso(self.at(185)), iso(self.at(295)), 78.0, 94.0,
                                         10.0, 110.0, 0.0, 0.0, "AC", 7.0)
        res = HistoryImporter(self.db, self.vid, P).repair_days(FakeClient(self._history(charge=True)), 30)
        self.assertEqual(res["charges"], 0)

    def test_positions_only_added_in_gaps(self):
        self.db.save_position_at(self.vid, _make_data(odometer_km=1000.0, soc=80.0), iso(self.at(0)))
        res = HistoryImporter(self.db, self.vid, P).repair_days(FakeClient(self._history()), 30)
        self.assertGreaterEqual(res["positions_added"], 1)
        self.assertEqual(self.db.position_near(self.vid, iso(self.at(0))), True)


if __name__ == "__main__":
    unittest.main()
