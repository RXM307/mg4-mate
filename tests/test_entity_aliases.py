"""Device-name entity layout (e.g. MG4 EV Urban) alongside the SAIC-gateway layout."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "poller"))
sys.path.insert(0, str(ROOT / "web"))

import ha_history  # noqa: E402
from ha_client import HomeAssistantMateClient  # noqa: E402
from ha_commands import HomeAssistantCommandClient  # noqa: E402

P = "mg_mg4_ev_urban"


def st(eid, value, **attrs):
    return {"entity_id": eid, "state": value, "attributes": attrs,
            "last_updated": "2026-10-22T12:14:17+00:00"}


def urban_states(**over):
    vals = {
        "sensor.state_of_charge": "92.9", "sensor.soc_stateful": "unknown",
        "sensor.electric_range": "426.0", "sensor.mileage": "6188.0", "sensor.speed": "0.0",
        "sensor.exterior_temperature": "0.0", "sensor.interior_temperature": "21.0",
        "sensor.ancillary_battery_voltage": "12.48", "sensor.charging_power": "0",
        "sensor.charging_current": "0", "sensor.charging_voltage": "0",
        "sensor.remaining_charging_time": "0", "sensor.charging_status": "Unplugged",
        "sensor.total_battery_capacity": "unknown", "sensor.climate_mode": "off",
        "binary_sensor.engine_status": "off", "binary_sensor.charging_gun_state": "off",
        "binary_sensor.boot_status": "off", "binary_sensor.bonnet_status": "off",
        "binary_sensor.door_front_left": "off", "binary_sensor.door_front_right": "on",
        "binary_sensor.door_rear_left": "off", "binary_sensor.door_rear_right": "off",
        "binary_sensor.window_front_left": "off", "binary_sensor.window_front_right": "off",
        "binary_sensor.window_rear_left": "off", "binary_sensor.window_rear_right": "off",
        "binary_sensor.dipped_beam_status": "on", "lock.lock": "locked", "climate.climate": "off",
        "switch.charging": "off", "switch.air_conditioning": "off",
        "number.climate_target_temperature": "22.0",
    }
    vals.update(over)
    out = [st(f"{k.split('.', 1)[0]}.{P}_{k.split('.', 1)[1]}", v) for k, v in vals.items()]
    for pos, v in (("front_left", "2.44"), ("front_right", "2.44"), ("rear_left", "2.4"), ("rear_right", "2.44")):
        out.append(st(f"sensor.{P}_tyre_pressure_{pos}", v, unit_of_measurement="bar"))
    out.append(st(f"device_tracker.{P}_gps_location", "home", latitude=-27.47, longitude=153.02))
    return out


def client(states):
    c = HomeAssistantMateClient("http://x", "t", P, fetch_states=lambda: states)
    c.login()
    return c


class UrbanPollerTest(unittest.TestCase):
    def test_parked(self):
        d = client(urban_states()).get_status()
        self.assertEqual((d.soc, d.range_km, d.odometer_km), (92.9, 426.0, 6188.0))
        self.assertTrue(d.is_locked)
        self.assertTrue(d.door_fr_open and not d.door_fl_open and d.any_door_open)
        self.assertEqual((d.tyre_fl_bar, d.tyre_rl_bar), (2.44, 2.4))
        self.assertEqual(d.aux_battery_v, 12.48)
        self.assertEqual(d.climate_target_temp, 22.0)
        self.assertEqual((d.latitude, d.longitude), (-27.47, 153.02))
        self.assertEqual((d.charging_status, d.plug_connected), (0, False))

    def test_charging_is_derived_from_status_and_gun(self):
        d = client(urban_states(**{
            "sensor.charging_status": "Charging", "binary_sensor.charging_gun_state": "on",
            "sensor.charging_power": "7.2", "sensor.remaining_charging_time": "95",
        })).get_status()
        self.assertEqual((d.charging_status, d.plug_connected, d.charge_power_kw), (1, True, 7.2))
        self.assertEqual(d.remaining_charge_min, 1)

    def test_plugged_but_finished_is_not_charging(self):
        d = client(urban_states(**{
            "sensor.charging_status": "Charging complete", "binary_sensor.charging_gun_state": "on",
        })).get_status()
        self.assertEqual((d.charging_status, d.plug_connected), (0, True))

    def test_driving(self):
        d = client(urban_states(**{"sensor.speed": "62", "binary_sensor.engine_status": "on"})).get_status()
        self.assertEqual(d.vehicle_state, "driving")

    def test_history_snapshot(self):
        rows = [{**s, "last_updated": "2026-10-22T05:10:00+00:00"} for s in urban_states(**{"sensor.speed": "40"})]
        snaps = ha_history.history_to_snapshots([rows], P)
        self.assertEqual((len(snaps), snaps[0].data.soc, snaps[0].data.speed_kmh), (1, 92.9, 40.0))

    def test_history_requests_alias_entities(self):
        ids = ha_history.HomeAssistantHistoryClient("http://x", "t", P).entity_ids()
        self.assertIn(f"sensor.{P}_state_of_charge", ids)
        self.assertIn(f"binary_sensor.{P}_charging_gun_state", ids)


class UrbanControlsTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        states = urban_states(**{
            "switch.front_defrost": "off", "switch.rear_window_defrost": "off",
            "switch.charging_port_lock": "off", "select.charging_current_limit": "Max",
        })
        states.append(st(f"number.{P}_target_soc", "100", min=50, max=100, step=10))
        states.append(st(f"select.{P}_heated_seat_front_left_level", "Off", options=["Off", "Low", "High"]))
        self.client = HomeAssistantCommandClient(
            "http://x", "t", P, fetch_states=lambda: states,
            call_service=lambda d, s, p: self.calls.append((d, s, p)) or {},
        )

    def test_discovers_controls(self):
        c = self.client.get_controls()
        self.assertEqual(c["locks"]["doors"]["entity_id"], f"lock.{P}_lock")
        self.assertEqual(c["climate"]["entity_id"], f"climate.{P}_climate")
        self.assertEqual(c["climate"]["temperature"], 22.0)
        self.assertEqual(c["switches"]["front_defroster"]["entity_id"], f"switch.{P}_front_defrost")
        self.assertEqual(c["switches"]["rear_defroster"]["entity_id"], f"switch.{P}_rear_window_defrost")
        self.assertEqual(c["switches"]["charging"]["entity_id"], f"switch.{P}_charging")
        self.assertEqual(c["cable_lock_switch"]["entity_id"], f"switch.{P}_charging_port_lock")
        self.assertEqual(c["numbers"]["target_soc"]["max"], 100)
        self.assertEqual(c["selects"]["heated_seat_left"]["options"], ["Off", "Low", "High"])

    def test_temperature_uses_number_entity(self):
        self.client.set_climate_temperature(f"climate.{P}_climate", 24)
        self.assertEqual(self.calls, [("number", "set_value", {"entity_id": f"number.{P}_climate_target_temperature", "value": 24})])


if __name__ == "__main__":
    unittest.main()
