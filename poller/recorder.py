"""
Recorder: reacts to state machine events to persist trips, charges, and positions.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from db import Database, _now_iso
from state_machine import State, StateMachine, StateEvent, _PARKED_STATES
from vehicle_data import VehicleData

log = logging.getLogger(__name__)


class Recorder:
    def __init__(self, db: Database, vehicle_id: int):
        self._db = db
        self._vehicle_id = vehicle_id
        self._sm = StateMachine()
        self._active_trip_id: Optional[int] = None
        self._active_charge_id: Optional[int] = None
        self._regen_kwh: float = 0.0
        self._max_charge_kw: float = 0.0
        self._started: bool = False
        self._last_process_ts: float = 0.0
        self._last_odo: Optional[float] = None
        self._last_soc: Optional[float] = None

    @property
    def state(self) -> State:
        return self._sm.state

    @property
    def poll_interval(self) -> int:
        return self._sm.poll_interval

    def set_poll_intervals(self, parked: int, driving: int) -> None:
        self._sm.poll_parked = parked
        self._sm.poll_driving = driving

    @staticmethod
    def _within_seconds(iso_ts: str, seconds: int) -> bool:
        try:
            dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() <= seconds
        except Exception:
            return False

    def _resume_or_close(self, data: VehicleData) -> None:
        """At startup, reconcile sessions left open by a previous run (poller/HA
        restart, crash). If the activity is STILL ongoing, RESUME the open session
        instead of closing it — this avoids fragmenting one physical charge/trip into
        multiple DB records. If it's no longer ongoing, close it (crash recovery)."""
        is_charging = data.charging_status > 0 or data.plug_connected
        is_driving  = data.vehicle_state == "driving" or data.speed_kmh > 1

        open_charge = self._db.get_open_charge(self._vehicle_id)
        if open_charge:
            if is_charging:
                self._active_charge_id = open_charge["id"]
                self._max_charge_kw = open_charge["max_power_kw"] or 0.0
                self._sm.state = State.CHARGING
                log.info("Resumed open charge #%d (car still charging)", open_charge["id"])
            else:
                self._db.close_orphan_charges(self._vehicle_id)

        open_trip = self._db.get_open_trip(self._vehicle_id)
        if open_trip:
            if is_driving and not is_charging:
                gap_min = int(self._db.get_setting_value('trip_merge_gap_min', '5'))
                last_pos_at = self._db.get_last_trip_position_time(open_trip["id"])
                ref_ts = last_pos_at or open_trip["started_at"]
                if self._within_seconds(ref_ts, gap_min * 60):
                    self._active_trip_id = open_trip["id"]
                    self._sm.state = State.DRIVING
                    log.info("Resumed open trip #%d (car still driving)", open_trip["id"])
                else:
                    log.warning(
                        "Open trip #%d is stale (last activity: %s) — closing as orphan",
                        open_trip["id"], ref_ts,
                    )
                    self._db.close_orphan_trips(self._vehicle_id)
            else:
                self._db.close_orphan_trips(self._vehicle_id)

    def process(self, data: VehicleData) -> None:
        """Called every poll cycle with fresh vehicle data."""
        now = time.monotonic()
        dt = (now - self._last_process_ts) if self._last_process_ts else 10.0
        self._last_process_ts = now

        if not self._started:
            self._started = True
            self._resume_or_close(data)

        self._db.save_position(self._vehicle_id, data)

        # Ghost trip detection: odometer jumped while parked and no active trip
        if (
            self._sm.state in _PARKED_STATES
            and not self._active_trip_id
            and data.odometer_km
        ):
            if self._last_odo is not None and data.odometer_km - self._last_odo >= 0.5:
                delta = data.odometer_km - self._last_odo
                prev_recorded_at = self._db.get_last_position_recorded_at(self._vehicle_id)
                started_at, ended_at, times_known = self._ghost_window(data, prev_recorded_at)
                self._db.create_ghost_trip(
                    self._vehicle_id, delta, started_at, ended_at,
                    start_odometer_km=self._last_odo, end_odometer_km=data.odometer_km,
                    start_soc=self._last_soc, end_soc=data.soc, times_known=times_known,
                )
                log.warning(
                    "Ghost trip detected: %.1f km (odometer %.1f → %.1f)",
                    delta, self._last_odo, data.odometer_km,
                )
        if data.odometer_km:
            self._last_odo = data.odometer_km
        self._last_soc = data.soc

        events = self._sm.update(data)
        for event in events:
            self._handle_event(event, data)

        # During active trip: record GPS point and accumulate regen.
        # Regen = energy flowing INTO the pack while unplugged. charge_power_kw is now a
        # magnitude (|current×voltage|), so we gate on a clearly-negative charge current.
        # This stays conservative: at worst it counts 0, never mistaking driving
        # discharge for regen.
        if self._sm.state == State.DRIVING and self._active_trip_id:
            self._db.add_trip_position(self._active_trip_id, data)
            if not data.plug_connected and data.charge_current_a < -3.0:
                self._regen_kwh += data.charge_power_kw * (dt / 3600)

        # During active charge: track peak power (persisted so it survives a restart)
        if self._sm.state == State.CHARGING and self._active_charge_id:
            if data.charge_power_kw > self._max_charge_kw:
                self._max_charge_kw = data.charge_power_kw
                self._db.update_charge_max_power(self._active_charge_id, self._max_charge_kw)

    @staticmethod
    def _ghost_window(data: VehicleData, prev_recorded_at: Optional[str]) -> tuple[str, str, bool]:
        """Start/end for an untracked trip.

        Prefer the car's own power-on/off timestamps (exact); otherwise fall back to
        the previous position and now, which only brackets the trip.
        """
        def parse(value: Optional[str]) -> Optional[datetime]:
            if not value:
                return None
            try:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        on, off = parse(data.last_powered_on), parse(data.last_powered_off)
        now = datetime.now(timezone.utc)
        if (
            on and off and on < off
            and (off - on).total_seconds() <= 12 * 3600
            and (now - off).total_seconds() <= 48 * 3600
            and (off - now).total_seconds() <= 300
        ):
            return on.isoformat(), off.isoformat(), True
        return prev_recorded_at or _now_iso(), _now_iso(), False

    def mark_offline(self) -> None:
        events = self._sm.mark_offline()
        for e in events:
            self._handle_event(e, None)

    def mark_online(self) -> None:
        events = self._sm.mark_online()
        for e in events:
            self._handle_event(e, None)

    def _handle_event(self, event: StateEvent, data: Optional[VehicleData]) -> None:
        frm, to = event.from_state, event.to_state

        if to == State.DRIVING:
            self._regen_kwh = 0.0
            gap_min = int(self._db.get_setting_value('trip_merge_gap_min', '5'))
            recent = self._db.get_recent_ended_trip(self._vehicle_id, gap_min * 60) if gap_min > 0 else None
            if recent:
                self._db.reopen_trip(recent['id'])
                self._active_trip_id = recent['id']
                self._regen_kwh = recent.get('regen_kwh') or 0.0
                log.info("Auto-merged new trip into recent trip #%d (gap < %d min)", recent['id'], gap_min)
            else:
                self._active_trip_id = self._db.create_trip(self._vehicle_id, data)

        elif frm == State.DRIVING and to in _PARKED_STATES:
            if self._active_trip_id and data:
                self._db.finalize_trip(self._active_trip_id, data, self._regen_kwh)
            self._active_trip_id = None
            self._regen_kwh = 0.0

        elif to == State.CHARGING:
            if self._active_trip_id and data:
                # Plug inserted while driving → trip closed immediately, no 20s wait
                self._db.finalize_trip(self._active_trip_id, data, self._regen_kwh)
                self._active_trip_id = None
                self._regen_kwh = 0.0
            self._max_charge_kw = 0.0
            if data:
                self._active_charge_id = self._db.create_charge(self._vehicle_id, data)

        elif frm == State.CHARGING and to in _PARKED_STATES:
            if self._active_charge_id and data:
                self._db.finalize_charge(
                    self._active_charge_id, data,
                    max_power_kw=self._max_charge_kw,
                )
            self._active_charge_id = None
            self._max_charge_kw = 0.0
