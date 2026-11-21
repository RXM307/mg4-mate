"""Print a report on stored data, configuration source, and what Home Assistant history can repair.

Run inside the container:  python /app/poller/diagnose.py
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def q(db, sql, *a):
    return db.execute(sql, a).fetchone()


def main():
    path = os.environ.get("DB_PATH", "mg4_mate.db")
    print(f"Database: {path}", flush=True)
    if not os.path.exists(path):
        print("  !! file does not exist — DB_PATH is wrong or the /data volume is not mounted")
        return
    print(f"  size: {os.path.getsize(path) / 1e6:.1f} MB")
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row

    t = q(db, """SELECT COUNT(*) n, COALESCE(SUM(COALESCE(untracked,0)=1),0) ghost,
                        COALESCE(SUM(start_soc IS NULL OR start_soc<=0 OR end_soc IS NULL OR end_soc<=0),0) no_soc,
                        COALESCE(SUM(duration_min IS NULL),0) no_dur,
                        MIN(started_at) first, MAX(started_at) last FROM trips""")
    print(f"Trips: {t['n']} (untracked {t['ghost']}, missing/0 SOC {t['no_soc']}, no duration {t['no_dur']}); "
          f"{t['first']} → {t['last']}")
    p = q(db, """SELECT COUNT(*) n, COALESCE(SUM(soc IS NULL OR soc<=0),0) bad,
                        MIN(recorded_at) first, MAX(recorded_at) last FROM positions""")
    print(f"Positions: {p['n']} (SOC 0/missing: {p['bad']}); {p['first']} → {p['last']}")
    bad = q(db, "SELECT MIN(recorded_at) a, MAX(recorded_at) b FROM positions WHERE soc IS NULL OR soc<=0")
    print(f"  bad-SOC positions span: {bad['a']} → {bad['b']}")
    c = q(db, "SELECT COUNT(*) n, MIN(started_at) first FROM charges")
    print(f"Charges: {c['n']} (first {c['first']})")
    for k in ("history_repair_v5", "ha_history_last_repair", "ha_history_last_import"):
        r = q(db, "SELECT value FROM settings WHERE key=?", k)
        print(f"Setting {k}: {r['value'] if r else '(not set)'}")

    # where each Home Assistant setting comes from (the Settings page wins over environment)
    for label, key, env in (("HA URL", "ha_url", "HA_URL"), ("HA token", "ha_token", "HA_TOKEN"),
                            ("Entity prefix", "ha_entity_prefix", "HA_ENTITY_PREFIX")):
        r = q(db, "SELECT value FROM settings WHERE key=?", key)
        if r and r["value"]:
            src, value = "Settings page (database)", r["value"]
        elif os.environ.get(env):
            src, value = "environment", os.environ[env]
        else:
            src, value = "NOT SET", ""
        print(f"Config {label}: {src}" + ("" if "token" in label or not value else f" = {value}"))

    try:
        from db import Database
        from ha_history import HomeAssistantHistoryClient, history_to_snapshots, iter_history_items
        from main import load_ha_config

        cfg = load_ha_config(Database(path))
        if not (cfg["ha_url"] and cfg["token"]):
            raise RuntimeError("HA URL/token not configured (Settings page or HA_URL/HA_TOKEN)")
        client = HomeAssistantHistoryClient(cfg["ha_url"], cfg["token"], cfg["entity_prefix"])
        history = client.fetch_history(30)
        events = list(iter_history_items(history))
        stamps = sorted(x for x in (i.get("last_changed") or i.get("last_updated") for i in events) if x)
        snaps = history_to_snapshots(history, cfg["entity_prefix"])
        print(f"\nHome Assistant (prefix {cfg['entity_prefix']}): {len(events)} state changes, "
              f"{len(snaps)} usable snapshots (30-day lookback)")
        if stamps:
            print(f"  history spans {stamps[0]} → {stamps[-1]}")
        ids = sorted({str(i["entity_id"]) for i in events})
        print(f"  {len(ids)} entities returned; SOC series present: "
              f"{any('state_of_charge' in e or e.endswith('_soc') for e in ids)}")
        try:
            from ha_ws import HomeAssistantStatistics
            import entity_map as em
            ids = HomeAssistantStatistics(cfg["ha_url"], cfg["token"]).statistic_ids()
            wanted = [em.entity_id(cfg["entity_prefix"], d, s) for d, s in em.candidates("sensor", "soc")]
            have = [w for w in wanted if w in ids]
            print(f"Long-term statistics (websocket): SOC {'AVAILABLE as ' + have[0] if have else 'NOT FOUND'}; "
                  f"{sum(1 for i in ids if i and cfg['entity_prefix'] in i)} statistics for this car")
        except Exception as exc:
            print(f"Long-term statistics check failed: {exc}")
    except Exception as exc:
        print(f"\nHome Assistant history check skipped/failed: {exc}")
    print(f"\nNow (UTC): {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
