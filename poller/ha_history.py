"""Import historical MG4 states from Home Assistant history."""
from __future__ import annotations

import json
import logging
from bisect import bisect_right
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import entity_map as em
from regen import MAX_STEP_S, RegenTracker, record_votes, regen_is_negative
from db import haversine_km
from vehicle_data import VehicleData


log = logging.getLogger(__name__)

MAX_DAYS = 365  # Home Assistant returns whatever it still has; the recorder default is only 10 days

HISTORY_SUFFIXES = {
    "sensor": [
        "soc",
        "range",
        "mileage",
        "vehicle_speed",
        "power",
        "voltage",
        "current",
        "remaining_charging_time",
        "interior_temperature",
        "exterior_temperature",
        "auxiliary_battery_voltage",
        "tyres_front_left_pressure",
        "tyres_front_right_pressure",
        "tyres_rear_left_pressure",
        "tyres_rear_right_pressure",
    ],
    "binary_sensor": [
        "vehicle_running",
        "battery_charging",
        "charger_connected",
        "boot",
        "window_driver",
        "window_passenger",
        "window_rear_left",
        "window_rear_right",
        "door_driver",
        "door_passenger",
        "door_rear_left",
        "door_rear_right",
        "bonnet",
        "lights_dipped_beam",
        "lights_main_beam",
        "lights_side",
    ],
    "lock": ["doors_lock"],
    "device_tracker": ["vehicle_position"],
}

# Extra signals used to derive charging state on integrations without
# battery_charging / charger_connected sensors
HISTORY_EXTRAS = [
    ("sensor", "charging_status"),
    ("switch", "charging"),
    ("sensor", "last_powered_on"),
    ("sensor", "last_powered_off"),
    ("sensor", "instant_power"),
]


@dataclass
class HistoricalSnapshot:
    recorded_at: str
    data: VehicleData


class HomeAssistantHistoryClient:
    def __init__(self, ha_url: str, token: str, entity_prefix: str):
        self._ha_url = ha_url.rstrip("/")
        self._token = token
        self._entity_prefix = entity_prefix.lower()

    def entity_ids(self) -> list[str]:
        signals = [
            (domain, suffix)
            for domain, suffixes in HISTORY_SUFFIXES.items()
            for suffix in suffixes
        ] + HISTORY_EXTRAS
        return em.wanted_entity_ids(self._entity_prefix, signals)

    def fetch_history(self, days: int) -> list[list[dict[str, Any]]]:
        return self.fetch_entities(self.entity_ids(), days)

    def fetch_entities(self, entity_ids: list[str], days: int, chunk: int = 20,
                       slice_days: int = 14) -> list[list[dict[str, Any]]]:
        """Fetch history in time slices (and entity chunks) so a long lookback stays small.

        Home Assistant needs ``end_time`` (it defaults to 1 day after the start), and long
        entity lists can exceed proxy URL limits. Only the fields the importer reads are kept.
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=max(1, min(days, MAX_DAYS)))
        out: list[list[dict[str, Any]]] = []
        slice_start = start
        while slice_start < now:
            slice_end = min(slice_start + timedelta(days=slice_days), now)
            for i in range(0, len(entity_ids), chunk):
                query = urllib.parse.urlencode({
                    "filter_entity_id": ",".join(entity_ids[i:i + chunk]),
                    # NB: do not send minimal_response at all — Home Assistant checks only that the
                    # parameter is present, so "false" would still strip entity ids and attributes
                    "end_time": slice_end.isoformat(),
                })
                req = urllib.request.Request(
                    f"{self._ha_url}/api/history/period/{urllib.parse.quote(slice_start.isoformat())}?{query}",
                    headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=90) as response:
                    for series in json.loads(response.read().decode("utf-8")):
                        out.append(_fill_entity_ids([_slim(item) for item in series if isinstance(item, dict)]))
            slice_start = slice_end
        return out


def _fill_entity_ids(series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Home Assistant may only name the entity on the first state of a series."""
    eid = next((i.get("entity_id") for i in series if i.get("entity_id")), None)
    if eid:
        for item in series:
            if not item.get("entity_id"):
                item["entity_id"] = eid
    return series


def iter_history_items(history: list[list[dict[str, Any]]]):
    """All state items in a fetched history, each with its entity_id filled in."""
    for series in history:
        series = [i for i in series if isinstance(i, dict)]
        eid = next((i.get("entity_id") for i in series if i.get("entity_id")), None)
        for item in series:
            if not item.get("entity_id") and eid:
                item = {**item, "entity_id": eid}
            if item.get("entity_id"):
                yield item


def _slim(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") or {}
    return {
        "entity_id": item.get("entity_id"),
        "state": item.get("state"),
        "last_changed": item.get("last_changed"),
        "last_updated": item.get("last_updated"),
        "attributes": {k: attrs[k] for k in ("unit_of_measurement", "latitude", "longitude") if k in attrs},
    }


def history_to_snapshots(history: list[list[dict[str, Any]]], entity_prefix: str) -> list[HistoricalSnapshot]:
    prefix = entity_prefix.lower()
    events: list[dict[str, Any]] = list(iter_history_items(history))
    events.sort(key=lambda item: item.get("last_updated") or item.get("last_changed") or "")

    latest: dict[str, dict[str, Any]] = {}
    snapshots: list[HistoricalSnapshot] = []
    seen: set[str] = set()
    idx = 0
    while idx < len(events):
        item = events[idx]
        ts = item.get("last_updated") or item.get("last_changed")
        if not ts:
            idx += 1
            continue
        while idx < len(events):
            grouped = events[idx]
            grouped_ts = grouped.get("last_updated") or grouped.get("last_changed")
            if grouped_ts != ts:
                break
            latest[grouped["entity_id"]] = grouped
            idx += 1
        data = _vehicle_data(latest, prefix, ts)
        if not data:
            continue
        recorded_at = _normalize_ts(ts)
        if recorded_at in seen:
            continue
        seen.add(recorded_at)
        snapshots.append(HistoricalSnapshot(recorded_at=recorded_at, data=data))
    return snapshots


def backfill_ghost_trip_times(
    db, vehicle_id: int, client: "HomeAssistantHistoryClient", days: int = 30,
    history: list[list[dict[str, Any]]] | None = None,
) -> int:
    """Give untracked trips their real start/end using the car's power-on/off sensors.

    Home Assistant keeps the history of ``last_powered_on`` / ``last_powered_off``; the
    value current when an untracked trip was detected is that trip's real start and end.
    Only trips still inside Home Assistant's history retention can be fixed. Idempotent.
    """
    trips = db.get_ghost_trips_without_duration(vehicle_id)
    if not trips:
        return 0
    prefix = client._entity_prefix
    on_id = f"sensor.{prefix}_last_powered_on"
    off_id = f"sensor.{prefix}_last_powered_off"
    series: dict[str, list[tuple[datetime, datetime]]] = {on_id: [], off_id: []}
    items = history if history is not None else client.fetch_entities([on_id, off_id], days)
    for item in iter_history_items(items):
        if item.get("entity_id") not in series:
            continue
        ts = item.get("last_changed") or item.get("last_updated")
        try:
            value = _dt(str(item.get("state")))
        except ValueError:
            continue
        if ts:
            series[item["entity_id"]].append((_dt(ts), value))
    for rows in series.values():
        rows.sort(key=lambda r: r[0])

    def value_at(entity_id: str, when: datetime) -> datetime | None:
        current = None
        for changed, value in series[entity_id]:
            if changed > when:
                break
            current = value
        return current

    fixed = 0
    for trip in trips:
        try:
            ended = _dt(trip["ended_at"])
        except (TypeError, ValueError):
            continue
        ref = ended + timedelta(seconds=90)
        on, off = value_at(on_id, ref), value_at(off_id, ref)
        if not on or not off or on >= off:
            continue
        if (off - on).total_seconds() > 12 * 3600 or abs((off - ended).total_seconds()) > 3 * 3600:
            continue
        db.update_ghost_trip_times(
            trip["id"], on.isoformat(), off.isoformat(), (off - on).total_seconds() / 60
        )
        fixed += 1
    return fixed


class HistoryImporter:
    def __init__(self, db, vehicle_id: int, entity_prefix: str):
        self._db = db
        self._vehicle_id = vehicle_id
        self._entity_prefix = entity_prefix.lower()

    def import_history(self, history: list[list[dict[str, Any]]]) -> dict[str, int]:
        snapshots = history_to_snapshots(history, self._entity_prefix)
        inserted: list[HistoricalSnapshot] = []
        for snapshot in snapshots:
            if self._db.position_exists(self._vehicle_id, snapshot.recorded_at):
                continue
            self._db.save_position_at(self._vehicle_id, snapshot.data, snapshot.recorded_at)
            inserted.append(snapshot)
        trips = self._import_trips(inserted)
        charges = self._import_charges(inserted)
        self._db.set_setting("ha_history_last_import", datetime.now(timezone.utc).isoformat())
        return {"positions": len(inserted), "trips": trips, "charges": charges}

    def import_days(self, client: HomeAssistantHistoryClient, days: int) -> dict[str, int]:
        return self.import_history(client.fetch_history(days))

    def _import_trips(self, snapshots: list[HistoricalSnapshot], *, dedupe: bool = False) -> int:
        imported = 0
        active: list[HistoricalSnapshot] = []
        for snapshot in snapshots + [None]:
            driving = bool(snapshot and (snapshot.data.vehicle_state == "driving" or snapshot.data.speed_kmh > 1))
            if driving:
                active.append(snapshot)
                continue
            if len(active) >= 2:
                imported += self._save_trip(active, dedupe=dedupe)
            active = []
        return imported

    def _save_trip(self, segment: list[HistoricalSnapshot], *, dedupe: bool = False) -> int:
        gps = [s for s in segment if s.data.latitude and s.data.longitude]
        if len(gps) < 2:
            return 0
        distance = sum(
            haversine_km(
                gps[i].data.latitude, gps[i].data.longitude,
                gps[i + 1].data.latitude, gps[i + 1].data.longitude,
            )
            for i in range(len(gps) - 1)
        )
        if distance < 0.2:
            return 0
        start = segment[0]
        end = segment[-1]
        if dedupe and self._db.trip_overlaps(self._vehicle_id, start.recorded_at, end.recorded_at):
            return 0
        duration = (_dt(end.recorded_at) - _dt(start.recorded_at)).total_seconds() / 60
        energy = max((start.data.soc - end.data.soc) / 100.0 * self._db.get_battery_capacity(), 0)
        efficiency = (energy / distance * 100) if distance > 0.5 else None
        tracker = RegenTracker()
        for a, b in zip(segment, segment[1:]):
            tracker.add(a.data.battery_power_kw, (_dt(b.recorded_at) - _dt(a.recorded_at)).total_seconds())
        regen = tracker.regen_kwh(regen_is_negative(self._db, tracker.pos_kwh, tracker.neg_kwh)) if tracker.has_data else 0.0
        self._db.insert_historical_trip(
            vehicle_id=self._vehicle_id,
            started_at=start.recorded_at,
            ended_at=end.recorded_at,
            start_lat=start.data.latitude, start_lon=start.data.longitude,
            end_lat=end.data.latitude, end_lon=end.data.longitude,
            distance_km=round(distance, 3),
            start_soc=start.data.soc, end_soc=end.data.soc,
            start_odometer_km=start.data.odometer_km, end_odometer_km=end.data.odometer_km,
            duration_min=round(duration, 1),
            efficiency_kwh_100km=round(efficiency, 2) if efficiency else None,
            regen_kwh=regen,
            gps_points=[
                {"recorded_at": p.recorded_at, "latitude": p.data.latitude,
                 "longitude": p.data.longitude, "speed_kmh": p.data.speed_kmh,
                 "soc": p.data.soc}
                for p in gps
            ],
        )
        return 1

    def _import_charges(self, snapshots: list[HistoricalSnapshot], *, dedupe: bool = False) -> int:
        imported = 0
        active: list[HistoricalSnapshot] = []
        before: HistoricalSnapshot | None = None
        prev: HistoricalSnapshot | None = None
        for snapshot in snapshots + [None]:
            charging = bool(snapshot and (snapshot.data.charging_status > 0 or snapshot.data.plug_connected))
            if charging:
                if not active:
                    before = prev
                active.append(snapshot)
            else:
                if len(active) >= 2:
                    imported += self._save_charge(active, before, snapshot, dedupe=dedupe)
                active, before = [], None
            prev = snapshot
        return imported

    def _save_charge(
        self,
        segment: list[HistoricalSnapshot],
        before: HistoricalSnapshot | None = None,
        after: HistoricalSnapshot | None = None,
        *,
        dedupe: bool = False,
    ) -> int:
        start = segment[0]
        end = segment[-1]
        start_soc, end_soc = start.data.soc, end.data.soc
        started_at, ended_at = start.recorded_at, end.recorded_at
        if dedupe:
            # The car reports the plug-in on its next refresh, by which time SOC has
            # already risen: take the reading from just before, like live charges do.
            if before and 0 < before.data.soc < start_soc:
                start_soc, started_at = before.data.soc, before.recorded_at
            end_soc = max(s.data.soc for s in segment)
            if after and after.data.soc > end_soc:
                end_soc = after.data.soc
            if self._db.charge_overlaps(self._vehicle_id, started_at, ended_at):
                return 0
        if end_soc <= start_soc:
            return 0
        max_power = max((s.data.charge_power_kw or 0) for s in segment)
        duration = (_dt(ended_at) - _dt(started_at)).total_seconds() / 60
        energy = (end_soc - start_soc) / 100.0 * self._db.get_battery_capacity()
        self._db.insert_historical_charge(
            vehicle_id=self._vehicle_id,
            started_at=started_at,
            ended_at=ended_at,
            start_soc=start_soc, end_soc=end_soc,
            energy_added_kwh=round(energy, 3),
            duration_min=round(duration, 1),
            latitude=start.data.latitude, longitude=start.data.longitude,
            charge_type="DC" if max_power > 11 else "AC",
            max_power_kw=round(max_power, 2),
        )
        return 1

    # ── Repair of data recorded before the entity names matched ──────────

    def repair_days(self, client: HomeAssistantHistoryClient, days: int, stats_source=None) -> dict[str, Any]:
        """Repair old data from Home Assistant's recorder history.

        * positions stored with SOC 0/NULL get SOC, odometer, range, doors, tyres… from HA
        * positions missing entirely (poller down) are added; existing ones are never duplicated
        * untracked trips get odometer/SOC/efficiency, then real start/end from the power sensors
        * trips and charges HA saw but the poller never recorded are imported, skipping any
          that overlap something already stored
        Safe to run repeatedly. Only reaches back as far as Home Assistant keeps history.
        """
        history = client.fetch_history(days)
        errors: dict[str, str] = {}

        def stage(name: str, fn, default=0):
            """Run one repair stage; a failure is logged and reported but doesn't stop the others."""
            try:
                return fn()
            except Exception as exc:
                log.exception("History repair stage %r failed", name)
                errors[name] = f"{type(exc).__name__}: {exc}"[:200]
                try:
                    self._db._conn.rollback()
                except Exception:
                    pass
                return default

        snapshots = stage("snapshots", lambda: history_to_snapshots(history, self._entity_prefix), [])
        events = list(iter_history_items(history))
        stamps = sorted(t for t in (i.get("last_changed") or i.get("last_updated") for i in events) if t)
        window_start = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, MAX_DAYS)))
        since_iso = window_start.isoformat()
        result: dict[str, Any] = {
            "positions_fixed": 0, "positions_added": 0, "trips_filled": 0,
            "trip_times": 0, "trips": 0, "charges": 0,
            "ha_states": len(events), "snapshots": len(snapshots),
            "window_days": max(1, min(days, MAX_DAYS)),
            # only set when Home Assistant has nothing older than this inside the window
            "history_from": (stamps[0][:10] if stamps and _dt(stamps[0]) > window_start + timedelta(days=1) else ""),
        }
        result["bad_positions"] = stage(
            "stats", lambda: self._db.position_repair_stats(self._vehicle_id, since_iso), {})
        if not snapshots:
            log.warning(
                "Home Assistant returned %d states (%d usable snapshots) for prefix %r over %d days — "
                "check HA_ENTITY_PREFIX and that the recorder keeps these entities",
                len(events), len(snapshots), self._entity_prefix, days)
        if snapshots:
            result.update(stage("positions", lambda: self._repair_positions(snapshots, days), {}))
        result["trips_filled"] = stage("untracked trips", lambda: self._db.backfill_ghost_trips(self._vehicle_id))
        result["trips_skipped"] = dict(getattr(self._db, "backfill_stats", {}))
        result["trip_times"] = stage(
            "trip times", lambda: backfill_ghost_trip_times(self._db, self._vehicle_id, client, days, history=history))
        result["trips_regen"] = stage("regen", lambda: self._repair_trip_regen(history, days))
        if snapshots:
            result["trips"] = stage("trips import", lambda: self._import_trips(snapshots, dedupe=True))
            result["charges"] = stage("charges import", lambda: self._import_charges(snapshots, dedupe=True))
        # Whatever Home Assistant's detailed history no longer holds, estimate from its long-term
        # (hourly) statistics, which it keeps indefinitely.
        if stats_source is None and getattr(client, "_ha_url", None) and getattr(client, "_token", None):
            from ha_ws import HomeAssistantStatistics
            stats_source = HomeAssistantStatistics(client._ha_url, client._token)
        if stats_source is not None:
            try:
                from ha_stats import repair_from_statistics
                result.update(repair_from_statistics(self._db, self._vehicle_id, self._entity_prefix, stats_source))
            except Exception as exc:  # websocket blocked, no statistics, …: report, don't fail the repair
                result["stats_error"] = str(exc)[:160]
                log.warning("Long-term statistics repair skipped: %s", exc)
        if errors:
            result["stage_errors"] = errors
        self._db.set_setting("ha_history_last_repair", datetime.now(timezone.utc).isoformat())
        return result

    def _repair_trip_regen(self, history: list[list[dict[str, Any]]], days: int) -> int:
        """Give recorded trips their regen from the history of the battery-power sensor.

        Regen used to read charger-side values that stay 0 while driving, so every trip has 0.
        Trips are integrated from ``instant_power`` samples; the sign convention is decided
        across all of them at once (energy out of the pack dominates), unless forced.
        """
        sensor = f"sensor.{self._entity_prefix}_instant_power"
        samples: list[tuple[datetime, float]] = []
        for item in iter_history_items(history):
            if item.get("entity_id") != sensor:
                continue
            ts = item.get("last_changed") or item.get("last_updated")
            kw = em.to_float(item.get("state"))
            if ts is None or kw is None:
                continue
            if str((item.get("attributes") or {}).get("unit_of_measurement") or "").strip().lower() == "w":
                kw /= 1000.0
            samples.append((_dt(ts), kw))
        if not samples:
            return 0
        samples.sort(key=lambda x: x[0])
        times = [t for t, _ in samples]

        since = (datetime.now(timezone.utc) - timedelta(days=max(1, min(days, MAX_DAYS)))).isoformat()
        trackers: list[tuple[int, RegenTracker]] = []
        for trip in self._db.get_trips_needing_regen(self._vehicle_id, since):
            start, end = _dt(trip["started_at"]), _dt(trip["ended_at"])
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            idx = bisect_right(times, start) - 1            # last sample at or before the start
            carry = None
            if idx >= 0 and (start - times[idx]).total_seconds() <= MAX_STEP_S:
                carry = samples[idx][1]                      # reading still current when the trip began
            inside = [(t, kw) for t, kw in samples[idx + 1:bisect_right(times, end)]]
            if carry is None and not inside:
                continue                                     # no samples during this trip
            tracker, cursor, value = RegenTracker(), start, carry
            for t, kw in inside:
                if value is not None:
                    tracker.add(value, (t - cursor).total_seconds())
                cursor, value = t, kw
            if value is not None:
                tracker.add(value, (end - cursor).total_seconds())
            if tracker.has_data:
                trackers.append((trip["id"], tracker))
        if not trackers:
            return 0
        pos = sum(t.pos_kwh for _, t in trackers)
        neg = sum(t.neg_kwh for _, t in trackers)
        negative = regen_is_negative(self._db, pos, neg)
        updated = 0
        for trip_id, tracker in trackers:
            kwh = tracker.regen_kwh(negative)
            if kwh > 0:
                self._db.set_trip_regen(trip_id, kwh)
                updated += 1
        record_votes(self._db, pos, neg)
        self._db._conn.commit()
        return updated

    def _repair_positions(self, snapshots: list[HistoricalSnapshot], days: int) -> dict[str, int]:
        since = (datetime.now(timezone.utc) - timedelta(days=max(1, min(days, MAX_DAYS)))).isoformat()
        times = [_dt(s.recorded_at) for s in snapshots]
        fixed = 0
        for n, row in enumerate(self._db.get_positions_needing_repair(self._vehicle_id, since), 1):
            try:
                when = _dt(row["recorded_at"])
            except (TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            idx = bisect_right(times, when) - 1
            if idx < 0:
                continue  # earlier than anything Home Assistant still has
            self._db.repair_position(row["id"], snapshots[idx].data)
            fixed += 1
            if n % 2000 == 0:
                self._db._conn.commit()   # short transactions: never starve the live poller
        self._db._conn.commit()

        # add positions only where none exist within 90 s — checked in memory, not one query per snapshot
        existing = self._db.position_times(self._vehicle_id, iso_minus(snapshots[0].recorded_at, 120))
        added = 0
        for n, snap in enumerate(snapshots, 1):
            ts = _dt(snap.recorded_at).timestamp()
            i = bisect_right(existing, ts)
            near = (i > 0 and ts - existing[i - 1] <= 90) or (i < len(existing) and existing[i] - ts <= 90)
            if near:
                continue
            self._db.save_position_at(self._vehicle_id, snap.data, snap.recorded_at)
            existing.insert(i, ts)
            added += 1
            if n % 2000 == 0:
                self._db._conn.commit()
        self._db._conn.commit()
        return {"positions_fixed": fixed, "positions_added": added}


def iso_minus(iso: str, seconds: int) -> str:
    return (_dt(iso) - timedelta(seconds=seconds)).isoformat()


def _vehicle_data(states: dict[str, dict[str, Any]], prefix: str, ts: str) -> VehicleData | None:
    soc = _float(_state(states, "sensor", prefix, "soc"))
    odometer = _float(_state(states, "sensor", prefix, "mileage"))
    speed = _float(_state(states, "sensor", prefix, "vehicle_speed"), 0.0)
    if soc is None or odometer is None:
        return None

    charging = em.is_charging(states, prefix)
    plugged = em.is_plugged(states, prefix)
    running = _bool(_state(states, "binary_sensor", prefix, "vehicle_running"))
    power = abs(em.charge_power_kw(states, prefix))
    current = _float(_state(states, "sensor", prefix, "current"), 0.0) or 0.0
    voltage = _float(_state(states, "sensor", prefix, "voltage"), 0.0) or 0.0
    tracker = em.resolve(states, prefix, "device_tracker", "vehicle_position") or {}
    attrs = tracker.get("attributes") or {}
    vehicle_state = "driving" if running or speed > 1 else "parked"
    windows = any(_bool(_state(states, "binary_sensor", prefix, suffix)) for suffix in (
        "window_driver", "window_passenger", "window_rear_left", "window_rear_right"
    ))

    doors = {
        suffix: _bool(_state(states, "binary_sensor", prefix, suffix))
        for suffix in ("door_driver", "door_passenger", "door_rear_left", "door_rear_right")
    }
    return VehicleData(
        vin=prefix.upper(),
        timestamp_ms=int(_dt(ts).timestamp() * 1000),
        soc=soc,
        range_km=_float(_state(states, "sensor", prefix, "range"), 0.0) or 0.0,
        odometer_km=odometer,
        speed_kmh=speed or 0.0,
        gear="D" if vehicle_state == "driving" else "P",
        vehicle_state=vehicle_state,
        charging_status=1 if charging else 0,
        charge_power_kw=round(power, 3),
        latitude=_float(attrs.get("latitude"), 0.0) or 0.0,
        longitude=_float(attrs.get("longitude"), 0.0) or 0.0,
        outside_temp=_float(_state(states, "sensor", prefix, "exterior_temperature"), 0.0) or 0.0,
        inside_temp=_float(_state(states, "sensor", prefix, "interior_temperature"), 0.0) or 0.0,
        climate_target_temp=0.0,
        battery_min_temp=0.0,
        is_locked=str(_state(states, "lock", prefix, "doors_lock")).lower() == "locked",
        climate_on=False,
        climate_cooling=False,
        climate_heating=False,
        climate_defrost=False,
        trunk_open=_bool(_state(states, "binary_sensor", prefix, "boot")),
        windows_open=windows,
        sunshade_open=False,
        any_door_open=any(doors.values()),
        plug_connected=plugged,
        remaining_charge_min=em.duration_minutes(states, prefix),
        charge_voltage_v=voltage,
        charge_current_a=current,
        door_fl_open=doors["door_driver"],
        door_fr_open=doors["door_passenger"],
        door_rl_open=doors["door_rear_left"],
        door_rr_open=doors["door_rear_right"],
        bonnet_open=_bool(_state(states, "binary_sensor", prefix, "bonnet")),
        aux_battery_v=_float(_state(states, "sensor", prefix, "auxiliary_battery_voltage"), 0.0) or 0.0,
        lights_dipped=_bool(_state(states, "binary_sensor", prefix, "lights_dipped_beam")),
        lights_main=_bool(_state(states, "binary_sensor", prefix, "lights_main_beam")),
        lights_side=_bool(_state(states, "binary_sensor", prefix, "lights_side")),
        tyre_fl_bar=em.pressure_bar(states, prefix, "sensor", "tyres_front_left_pressure") or 0.0,
        tyre_fr_bar=em.pressure_bar(states, prefix, "sensor", "tyres_front_right_pressure") or 0.0,
        tyre_rl_bar=em.pressure_bar(states, prefix, "sensor", "tyres_rear_left_pressure") or 0.0,
        tyre_rr_bar=em.pressure_bar(states, prefix, "sensor", "tyres_rear_right_pressure") or 0.0,
        battery_power_kw=em.battery_power_kw(states, prefix),
        last_powered_on=_usable(_state(states, "sensor", prefix, "last_powered_on")),
        last_powered_off=_usable(_state(states, "sensor", prefix, "last_powered_off")),
    )


def _state(states: dict[str, dict[str, Any]], domain: str, prefix: str, suffix: str) -> Any:
    return em.state_of(states, prefix, domain, suffix)


def _usable(value: Any) -> str | None:
    return None if value in em.UNUSABLE else str(value)


def _bool(value: Any) -> bool:
    return str(value).lower() in {"on", "true", "1", "locked"}


def _float(value: Any, default: float | None = None) -> float | None:
    if value in (None, "", "unknown", "unavailable"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalize_ts(value: str) -> str:
    return _dt(value).isoformat()
