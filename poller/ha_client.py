"""Home Assistant vehicle data source for MG/SAIC entities.

Works with the SAIC MQTT Gateway naming (``sensor.<prefix>_soc``) and with
integrations that use device-name entities (``sensor.<prefix>_state_of_charge``).
See entity_map.py for the name mapping.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import entity_map as em
from vehicle_data import VehicleData

log = logging.getLogger(__name__)

StateFetcher = Callable[[], list[dict[str, Any]]]

# How long to trust the list of entities that actually exist before re-checking
DISCOVERY_TTL_S = 1800


@dataclass
class HomeAssistantVehicle:
    vin: str
    car_type: str = "MG4"
    year: int | None = None


class HomeAssistantMateClient:
    """Read MG4 states from Home Assistant and expose the MG4 Mate data shape."""

    def __init__(
        self,
        ha_url: str,
        token: str,
        entity_prefix: str,
        *,
        fetch_states: StateFetcher | None = None,
    ):
        self._ha_url = ha_url.rstrip("/")
        self._token = token
        self._entity_prefix = entity_prefix.lower()
        self._fetch_states = fetch_states or self._fetch_states_from_api
        self._vehicle = HomeAssistantVehicle(vin=self._entity_prefix.upper())
        self.battery_capacity_kwh: float | None = None
        self._entity_ids: list[str] | None = None
        self._entity_ids_at: float = 0.0

    def login(self):
        """Validate that Home Assistant is reachable and cache static vehicle data."""
        states = self._state_map(self._fetch_states())
        self.battery_capacity_kwh = self._float(
            states, "sensor", "total_battery_capacity", default=None
        )
        year = self._float(states, "sensor", "model_year", default=None)
        if year:
            self._vehicle.year = int(year)
        self._log_coverage(states)
        log.info("Home Assistant source ready — VIN: %s model: MG4", self._vehicle.vin)

    def get_ha_timezone(self) -> str | None:
        """Return the IANA timezone name configured in Home Assistant (e.g. 'Europe/Rome')."""
        if not self._ha_url or not self._token:
            return None
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        req = urllib.request.Request(f"{self._ha_url}/api/config", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data.get("time_zone") or None
        except Exception as exc:
            log.warning("Could not fetch HA timezone: %s", exc)
            return None

    def get_status(self) -> VehicleData:
        states = self._state_map(self._fetch_states())
        p = self._entity_prefix

        speed = self._float(states, "sensor", "vehicle_speed")
        running = self._bool(states, "binary_sensor", "vehicle_running")
        charging = em.is_charging(states, p)
        plugged = em.is_plugged(states, p)
        power_kw = abs(em.charge_power_kw(states, p))
        current_a = self._float(states, "sensor", "current")
        voltage_v = self._float(states, "sensor", "voltage")

        position = em.resolve(states, p, "device_tracker", "vehicle_position") or {}
        attrs = position.get("attributes") or {}
        vehicle_state = "driving" if running or speed > 1 else "parked"

        door_suffixes = ("door_driver", "door_passenger", "door_rear_left", "door_rear_right")
        window_suffixes = ("window_driver", "window_passenger", "window_rear_left", "window_rear_right")

        return VehicleData(
            vin=self._vehicle.vin,
            timestamp_ms=self._timestamp_ms(states),
            soc=self._float(states, "sensor", "soc"),
            range_km=self._float(states, "sensor", "range"),
            odometer_km=self._float(states, "sensor", "mileage"),
            speed_kmh=speed,
            gear="D" if vehicle_state == "driving" else "P",
            vehicle_state=vehicle_state,
            charging_status=1 if charging else 0,
            charge_power_kw=round(power_kw, 3),
            latitude=self._attr_float(attrs, "latitude"),
            longitude=self._attr_float(attrs, "longitude"),
            outside_temp=self._float(states, "sensor", "exterior_temperature"),
            inside_temp=self._float(states, "sensor", "interior_temperature"),
            climate_target_temp=self._float(states, "number", "climate_target_temperature"),
            battery_min_temp=0.0,
            is_locked=self._locked(states),
            climate_on=self._climate_on(states),
            climate_cooling=False,
            climate_heating=False,
            climate_defrost=(
                self._bool(states, "switch", "front_defrost")
                or self._bool(states, "switch", "rear_window_defrost")
            ),
            trunk_open=self._bool(states, "binary_sensor", "boot"),
            windows_open=any(self._bool(states, "binary_sensor", s) for s in window_suffixes),
            sunshade_open=False,
            any_door_open=any(
                self._bool(states, "binary_sensor", s) for s in (*door_suffixes, "boot")
            ),
            plug_connected=plugged,
            remaining_charge_min=self._remaining_charge_min(states),
            charge_voltage_v=voltage_v,
            charge_current_a=current_a,
            door_fl_open=self._bool(states, "binary_sensor", "door_driver"),
            door_fr_open=self._bool(states, "binary_sensor", "door_passenger"),
            door_rl_open=self._bool(states, "binary_sensor", "door_rear_left"),
            door_rr_open=self._bool(states, "binary_sensor", "door_rear_right"),
            bonnet_open=self._bool(states, "binary_sensor", "bonnet"),
            aux_battery_v=self._float(states, "sensor", "auxiliary_battery_voltage") or 0.0,
            lights_dipped=self._bool(states, "binary_sensor", "lights_dipped_beam"),
            lights_main=self._bool(states, "binary_sensor", "lights_main_beam"),
            lights_side=self._bool(states, "binary_sensor", "lights_side"),
            heading_deg=self._float(states, "sensor", "heading") or 0.0,
            tyre_fl_bar=em.pressure_bar(states, p, "sensor", "tyres_front_left_pressure") or 0.0,
            tyre_fr_bar=em.pressure_bar(states, p, "sensor", "tyres_front_right_pressure") or 0.0,
            tyre_rl_bar=em.pressure_bar(states, p, "sensor", "tyres_rear_left_pressure") or 0.0,
            tyre_rr_bar=em.pressure_bar(states, p, "sensor", "tyres_rear_right_pressure") or 0.0,
        )

    def close(self):
        pass

    # ── fetching ──────────────────────────────────────────────────────────

    def _fetch_states_from_api(self) -> list[dict[str, Any]]:
        if not self._ha_url or not self._token:
            raise RuntimeError("HA_URL and HA_TOKEN are required for Home Assistant source")

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        entity_ids = self._entity_ids_to_fetch(headers)

        def fetch_one(entity_id: str) -> dict[str, Any]:
            req = urllib.request.Request(
                f"{self._ha_url}/api/states/{entity_id}",
                headers=headers,
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as response:
                    body = response.read().decode("utf-8")
                return json.loads(body)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return {"entity_id": entity_id, "state": "unavailable", "attributes": {}}
                raise

        with ThreadPoolExecutor(max_workers=8) as executor:
            return list(executor.map(fetch_one, entity_ids))

    def _entity_ids_to_fetch(self, headers: dict[str, str]) -> list[str]:
        """Only poll entities that exist, so alias candidates don't cost 404s each cycle."""
        now = time.monotonic()
        if self._entity_ids is not None and now - self._entity_ids_at < DISCOVERY_TTL_S:
            return self._entity_ids

        wanted = em.wanted_entity_ids(self._entity_prefix)
        ids = wanted
        cached_at = now
        try:
            req = urllib.request.Request(f"{self._ha_url}/api/states", headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                registered = {
                    s.get("entity_id")
                    for s in json.loads(response.read().decode("utf-8"))
                    if isinstance(s, dict)
                }
            found = [e for e in wanted if e in registered]
            if found:
                ids = found
                log.info("Discovered %d of %d candidate entities for prefix %r",
                         len(found), len(wanted), self._entity_prefix)
            else:
                log.warning(
                    "No Home Assistant entities matched prefix %r — check HA_ENTITY_PREFIX",
                    self._entity_prefix,
                )
        except Exception as exc:
            log.warning("Entity discovery failed (%s); polling all candidates", exc)
            cached_at = now - DISCOVERY_TTL_S + 60  # retry discovery in a minute

        self._entity_ids = ids
        self._entity_ids_at = cached_at
        return ids

    def _log_coverage(self, states: dict[str, dict[str, Any]]) -> None:
        missing = []
        total = 0
        for domain, suffix in em.CANONICAL_SIGNALS:
            if (domain, suffix) in em.COVERAGE_OPTIONAL:
                continue
            total += 1
            if em.state_of(states, self._entity_prefix, domain, suffix) in em.UNUSABLE:
                missing.append(f"{domain}.{suffix}")
        log.info("Entity coverage: %d/%d signals have data", total - len(missing), total)
        if missing:
            log.info("No data for (shown as empty/off): %s", ", ".join(missing))

    # ── helpers ───────────────────────────────────────────────────────────

    def _state_map(self, states: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        prefix = self._entity_prefix
        return {
            s["entity_id"]: s
            for s in states
            if isinstance(s, dict)
            and isinstance(s.get("entity_id"), str)
            and s["entity_id"].split(".", 1)[-1].startswith(prefix)
        }

    def _entity(self, domain: str, suffix: str) -> str:
        return em.entity_id(self._entity_prefix, domain, suffix)

    def _raw_state(self, states: dict[str, dict[str, Any]], domain: str, suffix: str) -> Any:
        return em.state_of(states, self._entity_prefix, domain, suffix)

    def _float(
        self,
        states: dict[str, dict[str, Any]],
        domain: str,
        suffix: str,
        *,
        default: float | None = 0.0,
    ) -> float | None:
        return em.to_float(self._raw_state(states, domain, suffix), default)

    def _bool(self, states: dict[str, dict[str, Any]], domain: str, suffix: str) -> bool:
        return em.truthy(self._raw_state(states, domain, suffix))

    def _locked(self, states: dict[str, dict[str, Any]]) -> bool:
        return str(self._raw_state(states, "lock", "doors_lock")).lower() == "locked"

    def _climate_on(self, states: dict[str, dict[str, Any]]) -> bool:
        off = {"", "none", "off", "unknown", "unavailable"}
        climate = str(self._raw_state(states, "climate", "vehicle_climate")).lower()
        remote = str(self._raw_state(states, "sensor", "remote_climate_state")).lower()
        return (
            climate not in off
            or remote not in off
            or self._bool(states, "switch", "air_conditioning")
        )

    def _remaining_charge_min(self, states: dict[str, dict[str, Any]]) -> int:
        return em.duration_minutes(states, self._entity_prefix)

    def _timestamp_ms(self, states: dict[str, dict[str, Any]]) -> int:
        newest = max((s.get("last_updated") or "" for s in states.values()), default="")
        if newest:
            try:
                from datetime import datetime

                return int(datetime.fromisoformat(newest.replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                pass
        return int(time.time() * 1000)

    @staticmethod
    def _attr_float(attrs: dict[str, Any], key: str) -> float:
        value = attrs.get(key)
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0
