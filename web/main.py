"""MG4 Mate — web server."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).parent))
import db_reader
import i18n

app = FastAPI(title="MG4 Mate")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def _localts_filter(ts) -> str:
    """Convert a UTC ISO timestamp string to a local-time ISO string for display."""
    if not ts:
        return str(ts or "")
    try:
        dt = datetime.fromisoformat(str(ts).replace(" ", "T").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz = db_reader._display_tz()
        return (dt.astimezone(tz) if tz else dt.astimezone()).isoformat()
    except Exception:
        return str(ts)


templates.env.filters["localts"] = _localts_filter
templates.env.filters["abs"] = abs


# ── Setup check middleware ────────────────────────────────────────────────────

@app.middleware("http")
async def setup_check(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") or path.startswith("/static/"):
        return await call_next(request)
    if not db_reader.is_setup_complete():
        # The poller marks setup complete after it connects to Home Assistant.
        return RedirectResponse(request.headers.get("x-ingress-path", "") + "/settings")
    return await call_next(request)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _soc_color(soc: float) -> str:
    if soc >= 50: return "#22c55e"
    if soc >= 20: return "#f59e0b"
    return "#ef4444"

def _state_color(pos: dict) -> str:
    if pos.get("charging"): return "text-yellow-400"
    if pos.get("speed_kmh", 0) > 1: return "text-blue-400"
    return "text-green-400"

def _ctx(**kwargs):
    """Add shared helpers + i18n to every template context."""
    lang = db_reader.get_language()
    t = i18n.get_t(lang)
    def state_label(pos: dict) -> str:
        if pos.get("charging"): return t("state_charging")
        if pos.get("speed_kmh", 0) > 1: return t("state_driving")
        return t("state_parked")
    return {**kwargs, "lang": lang, "t": t,
            "soc_color": _soc_color, "state_label": state_label, "state_color": _state_color}


def _addon_option(key: str, default: str = "") -> str:
    path = os.environ.get("ADDON_OPTIONS_PATH", "/data/options.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh).get(key)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default
    return str(value) if value is not None else default


def _ha_command_client():
    from ha_commands import HomeAssistantCommandClient

    vehicle, _ = db_reader.get_vehicle()
    ha_url = db_reader.get_setting("ha_url") or os.environ.get("HA_URL") or _addon_option("HA_URL")
    token = db_reader.get_setting("ha_token") or os.environ.get("HA_TOKEN") or _addon_option("HA_TOKEN")
    prefix = (
        db_reader.get_setting("ha_entity_prefix")
        or os.environ.get("HA_ENTITY_PREFIX")
        or _addon_option("HA_ENTITY_PREFIX")
        or (vehicle or {}).get("vin", "").lower()
    )
    return HomeAssistantCommandClient(ha_url, token, prefix)


def _safe_controls(feedback: dict | None = None) -> dict:
    try:
        controls = _ha_command_client().get_controls()
    except Exception as exc:
        controls = {
            "online": False,
            "error": str(exc),
            "locks": {},
            "switches": {},
            "numbers": {},
            "selects": {},
            "climate": None,
        }
    if feedback:
        controls["feedback"] = feedback
    return controls


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    vehicle, settings = db_reader.get_vehicle()
    status = db_reader.get_latest_status()
    trips = db_reader.get_trips(limit=3)
    charges = db_reader.get_charges(limit=1)
    return templates.TemplateResponse(request, "overview.html", _ctx(
        page="overview", vehicle=vehicle, settings=settings,
        status=status, recent_trips=trips,
        last_charge=charges[0] if charges else None,
    ))


@app.get("/trips", response_class=HTMLResponse)
async def trips_page(request: Request, highlight: int = 0):
    vehicle, _ = db_reader.get_vehicle()
    grouped = db_reader.get_trips_grouped()
    total   = sum(y["count"] for y in grouped)
    return templates.TemplateResponse(request, "trips.html", _ctx(
        page="trips", vehicle=vehicle, grouped=grouped,
        total=total, highlight=highlight,
    ))


@app.get("/trips/{trip_id}", response_class=HTMLResponse)
async def trip_detail(request: Request, trip_id: int):
    vehicle, _ = db_reader.get_vehicle()
    trip = db_reader.get_trip_detail(trip_id)
    if not trip:
        return RedirectResponse(request.headers.get("x-ingress-path", "") + "/trips")
    prev_trip, next_trip = db_reader.get_adjacent_trips(trip_id)
    similar = db_reader.get_similar_trips(trip_id, limit=8, days=365)
    return templates.TemplateResponse(request, "trip_detail.html", _ctx(
        page="trips", vehicle=vehicle, trip=trip,
        prev_trip=prev_trip, next_trip=next_trip,
        similar_trips=similar,
    ))


@app.get("/charges", response_class=HTMLResponse)
async def charges_page(request: Request, highlight: int = 0):
    vehicle, _ = db_reader.get_vehicle()
    grouped = db_reader.get_charges_grouped()
    stats   = db_reader.get_charge_stats()
    prices  = db_reader.get_charge_prices()
    status  = db_reader.get_latest_status()
    total   = sum(y["count"] for y in grouped)
    return templates.TemplateResponse(request, "charges.html", _ctx(
        page="charges", vehicle=vehicle, grouped=grouped,
        stats=stats, total=total, highlight=highlight,
        charge_types=db_reader.CHARGE_TYPES, prices=prices,
        status=status, ac_dc=db_reader.get_ac_dc_stats(),
    ))


@app.get("/statistics", response_class=HTMLResponse)
async def statistics(request: Request):
    vehicle, _ = db_reader.get_vehicle()
    grouped  = db_reader.get_stats_grouped()
    totals   = db_reader.get_stats_summary()
    return templates.TemplateResponse(request, "statistics.html", _ctx(
        page="statistics", vehicle=vehicle,
        grouped=grouped, totals=totals,
    ))


@app.get("/vehicle", response_class=HTMLResponse)
async def vehicle_page(request: Request):
    vehicle, _ = db_reader.get_vehicle()
    return templates.TemplateResponse(request, "vehicle.html", _ctx(
        page="vehicle", vehicle=vehicle,
    ))


@app.get("/controls", response_class=HTMLResponse)
async def controls_page(request: Request):
    vehicle, _ = db_reader.get_vehicle()
    controls = _safe_controls()
    return templates.TemplateResponse(request, "controls.html", _ctx(
        page="controls", vehicle=vehicle, controls=controls, feedback=None,
    ))


@app.get("/api/controls", response_class=HTMLResponse)
async def controls_panel(request: Request):
    controls = _safe_controls()
    return templates.TemplateResponse(request, "partials/controls_panel.html", _ctx(
        controls=controls, feedback=None,
    ))


@app.post("/api/controls/action", response_class=HTMLResponse)
async def controls_action(request: Request):
    form = await request.form()
    action = str(form.get("action", ""))
    entity_id = str(form.get("entity_id", ""))
    client = _ha_command_client()
    feedback = {"ok": True, "message": "Command sent"}
    try:
        if action == "lock":
            client.lock_entity(entity_id)
        elif action == "unlock":
            client.unlock_entity(entity_id)
        elif action == "switch_on":
            client.turn_on_switch(entity_id)
        elif action == "switch_off":
            client.turn_off_switch(entity_id)
        elif action == "climate_on":
            client.turn_on_climate(entity_id)
        elif action == "climate_off":
            client.turn_off_climate(entity_id)
        elif action == "climate_temp":
            client.set_climate_temperature(entity_id, float(form.get("value")))
        elif action == "number":
            client.set_number(entity_id, float(form.get("value")))
        elif action == "select":
            client.select_option(entity_id, str(form.get("value", "")))
        else:
            raise ValueError(f"Unsupported action: {action}")
    except Exception as exc:
        feedback = {"ok": False, "message": str(exc)}
    return templates.TemplateResponse(request, "partials/controls_panel.html", _ctx(
        controls=_safe_controls(feedback), feedback=feedback,
    ))


@app.get("/api/vehicle-status", response_class=HTMLResponse)
async def vehicle_status_api(request: Request):
    status = db_reader.get_latest_status()
    vs = None
    if status:
        vs = {
            "soc": status.get("soc"),
            "range_km": status.get("range_km"),
            "odometer_km": status.get("odometer_km"),
            "gear": status.get("gear"),
            "charging": bool(status.get("charging")),
            "plug_connected": bool(status.get("plug_connected")),
            "remaining_charge_min": status.get("remaining_charge_min"),
            "is_locked": bool(status.get("is_locked")),
            "doors": {
                "fl": bool(status.get("door_fl_open")),
                "fr": bool(status.get("door_fr_open")),
                "rl": bool(status.get("door_rl_open")),
                "rr": bool(status.get("door_rr_open")),
                "trunk": bool(status.get("trunk_open")),
                "bonnet": bool(status.get("bonnet_open")),
            },
            "windows": {
                "any": bool(status.get("windows_open")),
                "sunshade": bool(status.get("sunshade_open")),
            },
            "lights_any": bool(status.get("lights_any")),
            "heading_deg": status.get("heading_deg"),
            "aux_battery_v": status.get("aux_battery_v"),
            "temps": {
                "cabin": status.get("inside_temp"),
                "outside": status.get("outside_temp"),
                "battery": status.get("battery_min_temp"),
            },
            "tyres": {
                "fl": status.get("tyre_fl_bar"),
                "fr": status.get("tyre_fr_bar"),
                "rl": status.get("tyre_rl_bar"),
                "rr": status.get("tyre_rr_bar"),
            },
        }
    return templates.TemplateResponse(request, "partials/vehicle_status.html", _ctx(vs=vs))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    vehicle, settings = db_reader.get_vehicle()
    prices = db_reader.get_charge_prices()
    settings = {**settings, **prices}
    return templates.TemplateResponse(request, "settings.html", _ctx(
        page="settings", vehicle=vehicle, settings=settings,
        charge_types=db_reader.CHARGE_TYPES,
    ))


# ── Charge type update (HTMX) ────────────────────────────────────────────────

@app.post("/api/charges/{charge_id}/type", response_class=HTMLResponse)
async def set_charge_type(request: Request, charge_id: int):
    form = await request.form()
    location_type = form.get("location_type", "HOME")
    charge = db_reader.update_charge_type(charge_id, location_type)
    return templates.TemplateResponse(request, "partials/charge_type_badge.html", {
        "charge": charge,
        "charge_types": db_reader.CHARGE_TYPES,
    })


@app.post("/api/charges/{charge_id}/edit", response_class=HTMLResponse)
async def edit_charge(request: Request, charge_id: int):
    form = await request.form()
    try:
        def _norm(s: str) -> str:
            s = s.strip().replace(" ", "T")
            if len(s) == 16:
                s += ":00"
            return s

        started_at = _norm(str(form.get("started_at", "")))
        ended_at_raw = str(form.get("ended_at", "")).strip()
        ended_at   = _norm(ended_at_raw) if ended_at_raw else None
        start_soc  = float(form.get("start_soc", 0))
        end_soc_raw = str(form.get("end_soc", "")).strip()
        end_soc    = float(end_soc_raw) if end_soc_raw else None
    except (ValueError, TypeError) as exc:
        return HTMLResponse(f'<span class="text-red-400">Invalid data: {exc}</span>')

    result = db_reader.update_charge(charge_id, started_at, ended_at, start_soc, end_soc)
    if not result:
        return HTMLResponse('<span class="text-red-400">Charge not found</span>')

    ingress = request.headers.get("x-ingress-path", "")
    return HTMLResponse("", headers={"HX-Redirect": f"{ingress}/charges?highlight={charge_id}"})


@app.post("/api/settings/prices", response_class=HTMLResponse)
async def save_prices(request: Request):
    form = await request.form()
    for key in ["price_home_kwh", "price_ac_kwh", "price_fast_kwh", "price_hpc_kwh"]:
        val = form.get(key)
        if val:
            try:
                db_reader.update_charge_price(key, float(val))
            except ValueError:
                pass
    return HTMLResponse('<span style="color:#22c55e;font-size:13px">✓ Saved — costs recalculated</span>')


@app.post("/api/history-import", response_class=HTMLResponse)
async def history_import(request: Request):
    form = await request.form()
    try:
        days = max(1, min(int(form.get("days", 7)), 365))
    except (TypeError, ValueError):
        days = 7

    try:
        import sys as _sys
        poller_path = Path(__file__).resolve().parents[1] / "poller"
        if str(poller_path) not in _sys.path:
            _sys.path.insert(0, str(poller_path))
        from db import Database
        from ha_history import HistoryImporter, HomeAssistantHistoryClient

        db = Database(db_reader.DB_PATH)
        vehicle, _ = db_reader.get_vehicle()
        prefix = (
            db_reader.get_setting("ha_entity_prefix")
            or os.environ.get("HA_ENTITY_PREFIX")
            or _addon_option("HA_ENTITY_PREFIX")
            or ((vehicle or {}).get("vin", "").lower())
        )
        ha_url = db_reader.get_setting("ha_url") or os.environ.get("HA_URL") or _addon_option("HA_URL")
        token = db_reader.get_setting("ha_token") or os.environ.get("HA_TOKEN") or _addon_option("HA_TOKEN")
        vehicle_id = db.ensure_vehicle(prefix.upper(), "MG4")
        client = HomeAssistantHistoryClient(ha_url, token, prefix)
        result = HistoryImporter(db, vehicle_id, prefix).repair_days(client, days)
        db.close()
        return HTMLResponse(
            "<span style='color:#22c55e;font-size:13px'>"
            f"✓ Repaired {result['positions_fixed']} positions and {result['trips_filled']} untracked trips "
            f"(real times for {result['trip_times']}); added {result['positions_added']} positions, "
            f"{result['trips']} trips, {result['charges']} charges"
            + (f" · untracked trips not repaired: {sk}" if (sk := {k: v for k, v in (result.get('trips_skipped') or {}).items() if k != 'candidates' and v}) else "")
            + (lambda bp: f" · positions with no SOC: {bp['total']} total, {bp['in_window']} in the last {result['window_days']}d (oldest {bp['oldest']})" if bp and bp.get("total") else "")(result.get("bad_positions"))
            + (f" · ⚠ stages that failed: {'; '.join(f'{k}: {v}' for k, v in result['stage_errors'].items())}" if result.get("stage_errors") else "")
            + (f" · regen added to {result['trips_regen']} trips" if result.get("trips_regen") else "")
            + (f" · estimated from long-term statistics: {result['stats_trips']} trips, {result['stats_positions']} positions" if result.get("stats_trips") or result.get("stats_positions") else "")
            + (f" · long-term statistics unavailable: {result['stats_error']}" if result.get("stats_error") else "")
            + (" · no long-term statistics found for the SOC sensor" if result.get("stats_found") is False else "")
            + (f" · Home Assistant only has data from {result['history_from']}" if result.get("history_from") else "")
            + ("" if result.get("snapshots") else " · ⚠ Home Assistant returned no usable history; check the entity prefix and recorder")
            + "</span>"
        )
    except Exception as exc:
        import html as _html
        import logging as _logging
        _logging.getLogger("mg4_mate_web").exception("History import failed")
        return HTMLResponse(
            f"<span style='color:#ef4444;font-size:13px'>Import failed: {_html.escape(type(exc).__name__)}: "
            f"{_html.escape(str(exc))} — see the container log for the full traceback</span>",
            status_code=500,
        )


# ── HTMX partial ─────────────────────────────────────────────────────────────

@app.get("/api/charging-live", response_class=HTMLResponse)
async def charging_live(request: Request):
    status = db_reader.get_latest_status()
    return templates.TemplateResponse(request, "partials/charging_live.html", _ctx(status=status))


@app.get("/api/status-card", response_class=HTMLResponse)
async def status_card(request: Request):
    status = db_reader.get_latest_status()
    vehicle, _ = db_reader.get_vehicle()
    return templates.TemplateResponse(request, "partials/status_card.html", _ctx(
        status=status, vehicle=vehicle,
    ))


@app.post("/api/settings/trip-merge", response_class=HTMLResponse)
async def save_trip_merge(request: Request):
    form = await request.form()
    val = int(form.get("trip_merge_gap_min", 5))
    val = max(0, min(30, val))
    db_reader.set_setting("trip_merge_gap_min", str(val))
    lang = db_reader.get_language()
    t = i18n.get_t(lang)
    return HTMLResponse(f'<span class="text-green-400">{t("saved")}</span>')


@app.post("/api/trips/{trip_id}/merge", response_class=HTMLResponse)
async def merge_trip(request: Request, trip_id: int):
    form = await request.form()
    target_id = int(form.get("target_id", 0))
    if not target_id:
        return HTMLResponse('<span class="text-red-400">Invalid request</span>')
    # Always keep the earlier trip
    keep_id = min(trip_id, target_id)
    drop_id = max(trip_id, target_id)
    result = db_reader.merge_trips(keep_id, drop_id)
    if not result:
        return HTMLResponse('<span class="text-red-400">Merge failed</span>')
    ingress = request.headers.get("x-ingress-path", "")
    return HTMLResponse("", headers={"HX-Redirect": f"{ingress}/trips/{keep_id}"})


@app.post("/api/poll-settings", response_class=HTMLResponse)
async def poll_settings(request: Request):
    """Save the user-tunable poll cadence (parked / driving seconds). The poller picks
    these up live on its next cycle."""
    form = await request.form()
    try:
        parked = max(10, min(int(form.get("poll_parked", 30)), 600))
        driving = max(10, min(int(form.get("poll_driving", 10)), 60))
    except (ValueError, TypeError):
        return HTMLResponse('<span style="color:#ef4444">Invalid value</span>', status_code=400)
    db_reader.set_setting("poll_parked", str(parked))
    db_reader.set_setting("poll_driving", str(driving))
    return HTMLResponse(f'<span style="color:#22c55e">✓ {parked}s / {driving}s</span>')


_BOOST_DEFAULT_S = 300   # 5 min — covers the "got in the car → started driving" window


@app.api_route("/api/boost", methods=["GET", "POST"])
async def boost(seconds: int = _BOOST_DEFAULT_S):
    """Trigger fast (10s) polling for a window, so the poller catches a trip start that
    would otherwise be missed during deep sleep. Meant to be called when you get in the
    car (e.g. an iPhone Bluetooth shortcut, relayed by HA on the LAN — Mate stays local).
    Coordinated with the poller via settings['boost_until']."""
    import time
    seconds = max(30, min(int(seconds or _BOOST_DEFAULT_S), 1800))
    until = time.time() + seconds
    db_reader.set_setting("boost_until", str(until))
    return {"status": "boost on", "seconds": seconds}


# ── Charts page ───────────────────────────────────────────────────────────────

@app.get("/charts", response_class=HTMLResponse)
async def charts_page(request: Request):
    return templates.TemplateResponse(request, "charts.html", _ctx(page="charts"))


@app.get("/api/charts/soc-history")
async def api_soc_history(days: int = 30):
    return JSONResponse(db_reader.get_soc_history(days))


@app.get("/api/charts/monthly-costs")
async def api_monthly_costs():
    return JSONResponse(db_reader.get_monthly_charge_costs())


@app.get("/api/charts/efficiency-temp")
async def api_efficiency_temp():
    return JSONResponse(db_reader.get_efficiency_vs_temp())


@app.get("/api/charts/trip-paths")
async def api_trip_paths(limit: int = 200):
    return JSONResponse(db_reader.get_trip_paths(limit))


# ── Maintenance page ──────────────────────────────────────────────────────────

@app.get("/maintenance", response_class=HTMLResponse)
async def maintenance_page(request: Request):
    vehicle, _ = db_reader.get_vehicle()
    status      = db_reader.get_latest_status()
    current_km  = (status or {}).get("odometer_km")
    items       = db_reader.get_maintenance_items(current_km)
    logs        = db_reader.get_maintenance_logs(limit=15)
    overdue     = sum(1 for i in items if i["status"] == "overdue")
    upcoming    = sum(1 for i in items if i["status"] == "upcoming")
    return templates.TemplateResponse(request, "maintenance.html", _ctx(
        page="maintenance", vehicle=vehicle,
        items=items, logs=logs,
        overdue=overdue, upcoming=upcoming,
    ))


@app.post("/api/maintenance/{item_id}/log", response_class=HTMLResponse)
async def log_maintenance_api(request: Request, item_id: int):
    form = await request.form()
    try:
        odo_raw  = str(form.get("odometer_km", "")).strip()
        cost_raw = str(form.get("cost", "")).strip()
        odometer_km = float(odo_raw)  if odo_raw  else None
        cost        = float(cost_raw) if cost_raw else None
    except (ValueError, TypeError):
        return HTMLResponse('<span class="text-red-400">Invalid data</span>', status_code=400)

    provider = str(form.get("provider", "")).strip()
    notes    = str(form.get("notes",    "")).strip()

    db_reader.log_maintenance(item_id, odometer_km, cost, provider, notes)

    status     = db_reader.get_latest_status()
    current_km = (status or {}).get("odometer_km")
    items      = db_reader.get_maintenance_items(current_km)
    item       = next((i for i in items if i["id"] == item_id), None)
    if not item:
        return HTMLResponse('<span class="text-red-400">Item not found</span>', status_code=404)
    return templates.TemplateResponse(
        request, "partials/maintenance_item_card.html", _ctx(item=item)
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("WEB_PORT", 4000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
