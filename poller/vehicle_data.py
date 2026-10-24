"""Shared vehicle data model consumed by the recorder."""
from dataclasses import dataclass


@dataclass
class VehicleData:
    vin: str
    timestamp_ms: int
    soc: float
    range_km: float
    odometer_km: float
    speed_kmh: float
    gear: str
    vehicle_state: str
    charging_status: int
    charge_power_kw: float
    latitude: float
    longitude: float
    outside_temp: float
    inside_temp: float
    climate_target_temp: float
    battery_min_temp: float
    is_locked: bool
    climate_on: bool
    climate_cooling: bool
    climate_heating: bool
    climate_defrost: bool
    trunk_open: bool
    windows_open: bool
    sunshade_open: bool
    any_door_open: bool
    plug_connected: bool
    remaining_charge_min: int
    charge_voltage_v: float
    charge_current_a: float
    door_fl_open: bool = False
    door_fr_open: bool = False
    door_rl_open: bool = False
    door_rr_open: bool = False
    bonnet_open: bool = False
    aux_battery_v: float = 0.0
    lights_dipped: bool = False
    lights_main: bool = False
    lights_side: bool = False
    heading_deg: float = 0.0
    tyre_fl_bar: float = 0.0
    tyre_fr_bar: float = 0.0
    tyre_rl_bar: float = 0.0
    tyre_rr_bar: float = 0.0
    last_powered_on: str | None = None    # ISO timestamps reported by the car/integration
    last_powered_off: str | None = None

    def fingerprint(self) -> tuple:
        """Compact snapshot of signals that indicate car activity."""
        return (
            self.is_locked,
            round(self.soc),
            round(self.inside_temp),
            self.any_door_open,
            self.charging_status,
            self.plug_connected,
        )
