"""Estimate old SOC from Home Assistant's long-term (hourly) statistics.

Home Assistant keeps detailed state history for only ``purge_keep_days`` (10 by default), but
hourly min/mean/max statistics of measurement sensors are kept indefinitely. They are coarser,
so values recovered here are estimates at hourly resolution:

* a trip's start SOC is the highest SOC seen in the hour it started and its end SOC the lowest
  seen in the hour it ended (SOC only falls while driving);
* positions with no SOC get the hourly mean.

Real readings are never overwritten: only SOC <= 0 / missing placeholders are filled.
"""
from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import entity_map as em

log = logging.getLogger(__name__)

HOUR = timedelta(hours=1)


@dataclass
class HourlyStat:
    start: datetime
    end: datetime
    mean: Optional[float]
    low: Optional[float]
    high: Optional[float]


def parse_stat_time(value: Any) -> Optional[datetime]:
    """Home Assistant sends epoch milliseconds (new) or ISO strings (old)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class StatTimeline:
    def __init__(self, rows: list[dict[str, Any]]):
        stats = []
        for r in rows or []:
            start = parse_stat_time(r.get("start"))
            if start is None:
                continue
            end = parse_stat_time(r.get("end")) or start + HOUR
            stats.append(HourlyStat(start, end, r.get("mean"), r.get("min"), r.get("max")))
        stats.sort(key=lambda s: s.start)
        self._stats = stats
        self._starts = [s.start for s in stats]

    def __bool__(self) -> bool:
        return bool(self._stats)

    def at(self, when: datetime) -> Optional[HourlyStat]:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        idx = bisect_right(self._starts, when) - 1
        if idx < 0:
            return None
        stat = self._stats[idx]
        return stat if stat.start <= when < stat.end else None

    def trip_soc(self, started_at: str, ended_at: str) -> Optional[tuple[float, float]]:
        """(start SOC, end SOC) for a trip, or None when the statistics can't say."""
        try:
            began = self.at(parse_stat_time(started_at))
            ended = self.at(parse_stat_time(ended_at))
        except (TypeError, ValueError, AttributeError):
            return None
        if not began or not ended or began.high is None or ended.low is None:
            return None
        start, end = float(began.high), float(ended.low)
        if start <= 0 or end <= 0 or end > start:
            return None   # SOC rose in that hour (charging), so it can't describe a drive
        return round(start, 1), round(end, 1)

    def mean_at(self, when: datetime) -> Optional[float]:
        stat = self.at(when)
        return float(stat.mean) if stat and stat.mean is not None and stat.mean > 0 else None


def _first_rows(data: dict[str, list[dict]], ids: list[str]) -> list[dict]:
    for statistic_id in ids:
        if data.get(statistic_id):
            return data[statistic_id]
    return []


def repair_from_statistics(db, vehicle_id: int, prefix: str, source,
                           now: Optional[datetime] = None) -> dict[str, Any]:
    """Fill SOC (and range) that is still missing after the history repair.

    ``source`` needs ``fetch(statistic_ids, start, end) -> {id: [rows]}``
    (see ha_ws.HomeAssistantStatistics).
    """
    out: dict[str, Any] = {"stats_trips": 0, "stats_positions": 0, "stats_found": False}
    oldest = db.oldest_missing_soc(vehicle_id)
    if not oldest:
        return out
    now = now or datetime.now(timezone.utc)
    start = max(parse_stat_time(oldest) - timedelta(days=1), now - timedelta(days=730))

    soc_ids = [em.entity_id(prefix, d, s) for d, s in em.candidates("sensor", "soc")]
    range_ids = [em.entity_id(prefix, d, s) for d, s in em.candidates("sensor", "range")]
    data = source.fetch(soc_ids + range_ids, start, now)
    soc = StatTimeline(_first_rows(data, soc_ids))
    rng = StatTimeline(_first_rows(data, range_ids))
    out["stats_found"] = bool(soc)
    if not soc:
        log.info("No long-term statistics for %s — is the SOC sensor a measurement sensor?", soc_ids[:2])
        return out

    # trips first: they compare the hour's high and low, which the position pass would flatten
    out["stats_trips"] = db.backfill_ghost_trips(vehicle_id, soc_lookup=soc.trip_soc)

    fixed = 0
    for row in db.get_positions_needing_repair(vehicle_id, "0000"):
        when = parse_stat_time(row["recorded_at"])
        if when is None:
            continue
        mean = soc.mean_at(when)
        if mean is None:
            continue
        db.update_position_soc_range(row["id"], round(mean, 1), rng.mean_at(when))
        fixed += 1
    db.commit()
    out["stats_positions"] = fixed
    log.info("Long-term statistics repair: %d trips, %d positions", out["stats_trips"], fixed)
    return out
