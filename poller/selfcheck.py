"""Detect a mixed-version install (files copied one by one from different releases).

Typical symptoms it replaces: ``unexpected keyword argument``, ``cannot import name`` or the
poller starting the web server's ``main.py``. Problems are reported in plain words.
"""
from __future__ import annotations

import dataclasses
import importlib
import os

# module -> {attribute: [members it must have]}; an empty list only requires the attribute to exist
REQUIREMENTS: dict[str, dict[str, list[str]]] = {
    "vehicle_data": {"VehicleData": ["battery_power_kw", "last_powered_on", "last_powered_off", "tyre_rr_bar"]},
    "entity_map": {"battery_power_kw": [], "resolve": []},
    "regen": {"RegenTracker": ["add", "regen_kwh"], "regen_is_negative": []},
    "db": {"Database": [
        "backfill_ghost_trips", "position_times", "get_trips_needing_regen", "set_trip_regen",
        "oldest_missing_soc", "update_position_soc_range", "position_repair_stats", "insert_historical_trip",
    ]},
    "ha_client": {"HomeAssistantMateClient": ["get_status"]},
    "ha_history": {"HistoryImporter": ["repair_days"], "iter_history_items": []},
    "ha_stats": {"repair_from_statistics": []},
    "ha_ws": {"HomeAssistantStatistics": ["fetch"]},
    "recorder": {"Recorder": ["_final_regen"]},
}

HINT = ("Copy the whole poller/ and web/ folders from the same release (for example by unzipping "
        "mg4-mate-urban-support.zip over your checkout) and rebuild the image.")


def check_install(poller_dir: str | None = None, web_dir: str | None = None) -> list[str]:
    """Return a list of problems; empty means the files are consistent."""
    poller_dir = poller_dir or os.path.dirname(os.path.abspath(__file__))
    web_dir = web_dir or os.path.join(os.path.dirname(poller_dir), "web")
    problems: list[str] = []

    for folder, needle, label in ((poller_dir, "poller", "poller/main.py"), (web_dir, "web server", "web/main.py")):
        path = os.path.join(folder, "main.py")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            first = fh.readline().strip()
        if needle not in first.lower():
            problems.append(f"{label} is not the {needle} main.py (it starts with {first[:50]!r}) — files were swapped")

    for module, attrs in REQUIREMENTS.items():
        try:
            mod = importlib.import_module(module)
        except Exception as exc:
            problems.append(f"{module}.py is missing or broken: {type(exc).__name__}: {exc}")
            continue
        for attr, members in attrs.items():
            obj = getattr(mod, attr, None)
            if obj is None:
                problems.append(f"{module}.py is outdated: it has no {attr}")
                continue
            have = {f.name for f in dataclasses.fields(obj)} if dataclasses.is_dataclass(obj) else set(dir(obj))
            missing = [m for m in members if m not in have]
            if missing:
                problems.append(f"{module}.py is outdated: {attr} lacks {', '.join(missing)}")
    return problems
