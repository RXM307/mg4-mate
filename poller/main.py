"""MG4 Mate — vehicle data poller."""
import json
import logging
import os
import time

from db import Database
from recorder import Recorder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mg4_mate")


def _addon_option(key: str, default: str = "") -> str:
    """Read a Home Assistant add-on option without making Docker users depend on it."""
    path = os.environ.get("ADDON_OPTIONS_PATH", "/data/options.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh).get(key)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default
    return str(value) if value is not None else default


def _setting_env_option(db: "Database", db_key: str, env_key: str, default: str = "") -> str:
    return db.get_setting(db_key) or os.environ.get(env_key) or _addon_option(env_key, default)


def vehicle_source(db: "Database") -> str:
    return _setting_env_option(db, "vehicle_source", "VEHICLE_SOURCE", "homeassistant").lower()


def load_ha_config(db: "Database") -> dict:
    return {
        "ha_url": _setting_env_option(db, "ha_url", "HA_URL"),
        "token": _setting_env_option(db, "ha_token", "HA_TOKEN"),
        "entity_prefix": _setting_env_option(
            db, "ha_entity_prefix", "HA_ENTITY_PREFIX",
            _setting_env_option(db, "mg4_entity_prefix", "MG4_ENTITY_PREFIX"),
        ),
    }


def main():
    db_path = os.environ.get("DB_PATH", "mg4_mate.db")
    log.info("Starting MG4 Mate poller")

    db = Database(db_path)
    source = vehicle_source(db)

    if source != "homeassistant":
        raise RuntimeError("MG4 Mate supports VEHICLE_SOURCE=homeassistant")

    from ha_client import HomeAssistantMateClient

    cfg = load_ha_config(db)
    log.info("Using Home Assistant vehicle source: %s", cfg["ha_url"])
    client = HomeAssistantMateClient(**cfg)

    client.login()
    # Vehicle is known after login; register it in the DB
    v = client._vehicle
    vehicle_id = db.ensure_vehicle(v.vin, v.car_type, getattr(v, "year", None))

    capacity = getattr(client, "battery_capacity_kwh", None)
    if capacity:
        db.set_battery_capacity(capacity)
    db.set_setting("vehicle_source", "homeassistant")

    tz = client.get_ha_timezone()
    if tz:
        db.set_setting("display_timezone", tz)
        log.info("Display timezone: %s", tz)

    db.mark_setup_complete()

    # Repair untracked trips recorded by older versions (no SOC/odometer, 0 min).
    try:
        db.backfill_ghost_trips(vehicle_id)
        if cfg["ha_url"] and cfg["token"]:
            from ha_history import HomeAssistantHistoryClient, backfill_ghost_trip_times
            n = backfill_ghost_trip_times(
                db, vehicle_id,
                HomeAssistantHistoryClient(cfg["ha_url"], cfg["token"], cfg["entity_prefix"]),
            )
            if n:
                log.info("Set real start/end on %d untracked trips from HA history", n)
    except Exception as exc:  # never block polling on a repair step
        log.warning("Untracked-trip repair skipped: %s", exc)

    # Crash/restart recovery for open trips/charges happens on the first poll inside
    # Recorder._resume_or_close(), which RESUMES a still-ongoing session (avoiding
    # fragmentation) and only closes it if the activity has actually ended.
    recorder = Recorder(db, vehicle_id)

    log.info("Polling VIN %s (vehicle_id=%d)", v.vin, vehicle_id)

    while True:
        try:
            # Apply user-tunable poll cadence (Settings) live, each cycle
            try:
                recorder.set_poll_intervals(
                    int(db.get_setting("poll_parked", "30") or 30),
                    int(db.get_setting("poll_driving", "10") or 10),
                )
            except (TypeError, ValueError):
                pass
            data = client.get_status()
            recorder.process(data)
            interval = recorder.poll_interval
            # Boost window (set via POST /api/boost, e.g. an iPhone BT shortcut relayed
            # by HA when you get in the car): poll fast so we catch the trip start that
            # deep sleep would otherwise miss. Only matters while still parked — once
            # DRIVING the state machine already polls at 10s.
            try:
                boost_until = float(db.get_setting("boost_until", "0") or 0)
            except (TypeError, ValueError):
                boost_until = 0.0
            boosting = time.time() < boost_until and interval > 10
            if boosting:
                interval = 10
            log.info(
                "SOC %.1f%% | Range %d km | Speed %.0f km/h | State: %-8s | Gear: %s | Next poll: %ds%s",
                data.soc, data.range_km, data.speed_kmh,
                recorder.state.value, data.gear, interval,
                " (boost)" if boosting else "",
            )
            recorder.mark_online()
        except KeyboardInterrupt:
            log.info("Stopped by user")
            break
        except Exception as exc:
            log.error("Poll error: %s", exc)
            recorder.mark_offline()
            interval = recorder.poll_interval

        # Interruptible sleep: while parked we may be sleeping for minutes, so check the
        # boost flag every few seconds and wake immediately if one is requested.
        deadline = time.time() + interval
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(5.0, remaining))
            if interval > 10:
                try:
                    if float(db.get_setting("boost_until", "0") or 0) > time.time():
                        break   # boost just requested → poll now
                except (TypeError, ValueError):
                    pass

    client.close()
    db.close()


if __name__ == "__main__":
    main()
