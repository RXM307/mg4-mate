"""Entity-name mapping between Home Assistant integrations.

MG4 Mate was written against the SAIC MQTT Gateway, whose entities look like
``sensor.<vin>_soc``. Other integrations expose the same signals under other
names, e.g. ``sensor.mg_mg4_ev_urban_state_of_charge``.

The rest of the poller keeps asking for the "canonical" (SAIC gateway) name.
``resolve()`` tries that name first and then the aliases below, so both kinds of
setup work with the same code and no extra configuration.
"""
from __future__ import annotations

from typing import Any

UNUSABLE = (None, "", "unknown", "unavailable")

# canonical (domain, suffix) -> alternatives tried when the canonical entity is
# missing or has no usable state
ALIASES: dict[tuple[str, str], list[tuple[str, str]]] = {
    ("sensor", "soc"): [("sensor", "state_of_charge")],
    ("sensor", "range"): [("sensor", "electric_range")],
    ("sensor", "vehicle_speed"): [("sensor", "speed")],
    ("sensor", "power"): [("sensor", "charging_power"), ("sensor", "instant_power")],
    ("sensor", "current"): [("sensor", "charging_current")],
    ("sensor", "voltage"): [("sensor", "charging_voltage")],
    ("sensor", "remote_climate_state"): [("sensor", "climate_mode")],
    ("sensor", "auxiliary_battery_voltage"): [("sensor", "ancillary_battery_voltage")],
    ("sensor", "tyres_front_left_pressure"): [("sensor", "tyre_pressure_front_left")],
    ("sensor", "tyres_front_right_pressure"): [("sensor", "tyre_pressure_front_right")],
    ("sensor", "tyres_rear_left_pressure"): [("sensor", "tyre_pressure_rear_left")],
    ("sensor", "tyres_rear_right_pressure"): [("sensor", "tyre_pressure_rear_right")],
    ("binary_sensor", "vehicle_running"): [("binary_sensor", "engine_status")],
    ("binary_sensor", "charger_connected"): [("binary_sensor", "charging_gun_state")],
    ("binary_sensor", "boot"): [("binary_sensor", "boot_status")],
    ("binary_sensor", "bonnet"): [("binary_sensor", "bonnet_status")],
    # Doors/windows: the alternative integration names them by physical position
    ("binary_sensor", "door_driver"): [("binary_sensor", "door_front_left")],
    ("binary_sensor", "door_passenger"): [("binary_sensor", "door_front_right")],
    ("binary_sensor", "window_driver"): [("binary_sensor", "window_front_left")],
    ("binary_sensor", "window_passenger"): [("binary_sensor", "window_front_right")],
    ("binary_sensor", "lights_dipped_beam"): [("binary_sensor", "dipped_beam_status")],
    ("binary_sensor", "lights_main_beam"): [("binary_sensor", "main_beam_status")],
    ("binary_sensor", "lights_side"): [("binary_sensor", "side_light_status")],
    ("lock", "doors_lock"): [("lock", "lock")],
    ("climate", "vehicle_climate"): [("climate", "climate")],
    ("device_tracker", "vehicle_position"): [("device_tracker", "gps_location")],
}

# Every canonical signal the poller reads
CANONICAL_SIGNALS: list[tuple[str, str]] = [
    ("sensor", "soc"),
    ("sensor", "range"),
    ("sensor", "mileage"),
    ("sensor", "vehicle_speed"),
    ("sensor", "power"),
    ("sensor", "current"),
    ("sensor", "voltage"),
    ("sensor", "exterior_temperature"),
    ("sensor", "interior_temperature"),
    ("sensor", "total_battery_capacity"),
    ("sensor", "remaining_charging_time"),
    ("sensor", "remote_climate_state"),
    ("sensor", "auxiliary_battery_voltage"),
    ("sensor", "heading"),
    ("sensor", "tyres_front_left_pressure"),
    ("sensor", "tyres_front_right_pressure"),
    ("sensor", "tyres_rear_left_pressure"),
    ("sensor", "tyres_rear_right_pressure"),
    ("binary_sensor", "vehicle_running"),
    ("binary_sensor", "battery_charging"),
    ("binary_sensor", "charger_connected"),
    ("binary_sensor", "boot"),
    ("binary_sensor", "bonnet"),
    ("binary_sensor", "door_driver"),
    ("binary_sensor", "door_passenger"),
    ("binary_sensor", "door_rear_left"),
    ("binary_sensor", "door_rear_right"),
    ("binary_sensor", "window_driver"),
    ("binary_sensor", "window_passenger"),
    ("binary_sensor", "window_rear_left"),
    ("binary_sensor", "window_rear_right"),
    ("binary_sensor", "lights_dipped_beam"),
    ("binary_sensor", "lights_main_beam"),
    ("binary_sensor", "lights_side"),
    ("lock", "doors_lock"),
    ("climate", "vehicle_climate"),
    ("device_tracker", "vehicle_position"),
]

# Signals with no SAIC-gateway equivalent; used to derive or enrich values
EXTRA_SIGNALS: list[tuple[str, str]] = [
    ("sensor", "charging_status"),
    ("sensor", "model_year"),
    ("sensor", "last_powered_on"),
    ("sensor", "last_powered_off"),
    ("switch", "charging"),
    ("switch", "air_conditioning"),
    ("switch", "front_defrost"),
    ("switch", "rear_window_defrost"),
    ("number", "climate_target_temperature"),
]

# Not worth reporting as "missing" in the startup coverage log
COVERAGE_OPTIONAL = {
    ("binary_sensor", "battery_charging"),  # derived from charging_status
    ("sensor", "heading"),                  # not exposed by some integrations
    ("sensor", "total_battery_capacity"),   # set capacity in Settings instead
}


def entity_id(prefix: str, domain: str, suffix: str) -> str:
    return f"{domain}.{prefix}_{suffix}"


def candidates(domain: str, suffix: str) -> list[tuple[str, str]]:
    return [(domain, suffix), *ALIASES.get((domain, suffix), [])]


def wanted_entity_ids(prefix: str, signals: list[tuple[str, str]] | None = None) -> list[str]:
    """All entity ids worth fetching (canonical + aliases + extras), de-duplicated."""
    seen: dict[str, None] = {}
    for domain, suffix in (signals if signals is not None else CANONICAL_SIGNALS + EXTRA_SIGNALS):
        for d, s in candidates(domain, suffix):
            seen.setdefault(entity_id(prefix, d, s), None)
    return list(seen)


def resolve(states: dict[str, dict[str, Any]], prefix: str, domain: str, suffix: str) -> dict[str, Any] | None:
    """Return the first candidate entity that has a usable state.

    Falls back to the first candidate that exists at all (so callers can still see
    'unavailable'), or None when nothing matches.
    """
    first_seen = None
    for d, s in candidates(domain, suffix):
        item = states.get(entity_id(prefix, d, s))
        if not item:
            continue
        if first_seen is None:
            first_seen = item
        if item.get("state") not in UNUSABLE:
            return item
    return first_seen


def state_of(states: dict[str, dict[str, Any]], prefix: str, domain: str, suffix: str) -> Any:
    item = resolve(states, prefix, domain, suffix)
    return None if item is None else item.get("state")


def to_float(value: Any, default: float | None = None) -> float | None:
    if value in UNUSABLE:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def truthy(value: Any) -> bool:
    return str(value).lower() in {"on", "true", "1", "locked"}


def _unit(item: dict[str, Any] | None) -> str:
    return str(((item or {}).get("attributes") or {}).get("unit_of_measurement") or "").strip().lower()


def pressure_bar(states: dict[str, dict[str, Any]], prefix: str, domain: str, suffix: str) -> float | None:
    item = resolve(states, prefix, domain, suffix)
    value = to_float((item or {}).get("state"))
    if value is None:
        return None
    unit = _unit(item)
    if unit == "kpa":
        return value / 100.0
    if unit == "psi":
        return value * 0.0689476
    if unit == "pa":
        return value / 100000.0
    return value


def charge_power_kw(states: dict[str, dict[str, Any]], prefix: str) -> float:
    """Signed charge/drive power in kW (callers take abs() where needed)."""
    item = resolve(states, prefix, "sensor", "power")
    value = to_float((item or {}).get("state"), 0.0) or 0.0
    if _unit(item) == "w":
        value /= 1000.0
    return value


def duration_minutes(states: dict[str, dict[str, Any]], prefix: str) -> int:
    """Remaining charge time in minutes. Unit-aware; unitless values are seconds
    (the SAIC gateway convention)."""
    item = resolve(states, prefix, "sensor", "remaining_charging_time")
    value = to_float((item or {}).get("state"), 0.0) or 0.0
    unit = _unit(item)
    if unit in {"min", "mins", "minute", "minutes"}:
        return int(value)
    if unit in {"h", "hr", "hrs", "hour", "hours"}:
        return int(value * 60)
    return int(value / 60)


_NOT_CHARGING = (
    "not charging", "not_charging", "unplug", "disconnect", "stopp", "finish",
    "complete", "paused", "fault", "error", "idle",
)
_NOT_PLUGGED = ("unplug", "disconnect", "not connected", "not charging", "not_charging")


def _charging_status(states: dict[str, dict[str, Any]], prefix: str) -> str | None:
    value = state_of(states, prefix, "sensor", "charging_status")
    return None if value in UNUSABLE else str(value).strip().lower()


def is_plugged(states: dict[str, dict[str, Any]], prefix: str) -> bool:
    gun = state_of(states, prefix, "binary_sensor", "charger_connected")
    if str(gun).lower() in {"on", "off"}:
        return str(gun).lower() == "on"
    status = _charging_status(states, prefix)
    if status is None or any(k in status for k in _NOT_PLUGGED):
        return False
    return any(k in status for k in ("plug", "connect", "charging", "complete", "finish", "full"))


def is_charging(states: dict[str, dict[str, Any]], prefix: str) -> bool:
    direct = state_of(states, prefix, "binary_sensor", "battery_charging")
    if direct not in UNUSABLE:
        return truthy(direct)
    status = _charging_status(states, prefix)
    if status is not None:
        if any(k in status for k in _NOT_CHARGING):
            return False
        if "charging" in status:
            return True
    if truthy(state_of(states, prefix, "switch", "charging")):
        return True
    return is_plugged(states, prefix) and abs(charge_power_kw(states, prefix)) > 0.05
