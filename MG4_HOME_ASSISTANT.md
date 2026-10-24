# MG4 Home Assistant Source

MG4 Mate uses Home Assistant entities from the SAIC/MG MQTT Gateway.

## Requirements

- Home Assistant reachable from the add-on/container.
- SAIC MQTT Gateway already configured with Home Assistant MQTT discovery enabled.
- A Home Assistant long-lived access token.
- The MG4 entity prefix, usually the lower-case VIN used in entity ids.

Example entity prefix:

```text
lsjwh4097rn111393
```

## Configuration

Set these options in the Home Assistant add-on configuration or as Docker environment variables:

```text
VEHICLE_SOURCE=homeassistant
HA_URL=http://homeassistant.local:8123
HA_TOKEN=<long-lived-access-token>
HA_ENTITY_PREFIX=<mg4-entity-prefix>
```

For a Home Assistant add-on running on the same host, prefer the local HTTP address if available. This avoids public DNS and HTTPS endpoint issues:

```text
HA_URL=http://192.168.x.x:8123
```

## Expected Entities

The adapter reads the current state from Home Assistant and maps these entities into the existing Mate recorder:

```text
sensor.<prefix>_soc
sensor.<prefix>_soc_kwh
sensor.<prefix>_range
sensor.<prefix>_mileage
sensor.<prefix>_vehicle_speed
sensor.<prefix>_power
sensor.<prefix>_voltage
sensor.<prefix>_current
sensor.<prefix>_remaining_charging_time
sensor.<prefix>_total_battery_capacity
sensor.<prefix>_interior_temperature
sensor.<prefix>_exterior_temperature
device_tracker.<prefix>_vehicle_position
binary_sensor.<prefix>_vehicle_running
binary_sensor.<prefix>_battery_charging
binary_sensor.<prefix>_charger_connected
binary_sensor.<prefix>_door_driver
binary_sensor.<prefix>_door_passenger
binary_sensor.<prefix>_door_rear_left
binary_sensor.<prefix>_door_rear_right
binary_sensor.<prefix>_boot
binary_sensor.<prefix>_window_driver
binary_sensor.<prefix>_window_passenger
binary_sensor.<prefix>_window_rear_left
binary_sensor.<prefix>_window_rear_right
lock.<prefix>_doors_lock
```

## Notes

- The SQLite recorder, trips, charges, statistics, and web UI are reused.
- Trip quality depends on the SAIC gateway refresh cadence. Around 30-40 second updates are enough for useful maps and statistics.
- Do not commit Home Assistant tokens. Pass them only through add-on options or environment variables.


## Device-name entities (e.g. MG4 EV Urban)

If your entities look like `sensor.mg_mg4_ev_urban_state_of_charge` rather than `sensor.<vin>_soc`, set
`HA_ENTITY_PREFIX` to the part before the signal name (`mg_mg4_ev_urban`). MG4 Mate tries the SAIC-gateway name
first and then the alternatives listed in `poller/entity_map.py` (and `web/ha_commands.py` for controls).
On startup the poller log shows how many signals have data and which have none.
