"""Regenerative braking energy from the battery power Home Assistant reports.

Integrations differ on the sign of battery power (positive = discharging, or the reverse),
so the convention is worked out from the data: while driving, far more energy flows out of
the pack than back in, so the sign that dominates is "discharge" and the other is regen.
It can be forced with the ``regen_sign`` setting or ``REGEN_SIGN`` environment variable
("negative" = negative power means regen, "positive" = the opposite).
"""
from __future__ import annotations

import os
from typing import Any, Optional

# A reading is held for at most this long: stale data and poll gaps must not be integrated.
MAX_STEP_S = 120.0


class RegenTracker:
    """Integrates signed battery power (kW) into positive and negative energy (kWh)."""

    def __init__(self) -> None:
        self.pos_kwh = 0.0
        self.neg_kwh = 0.0

    def add(self, power_kw: Any, seconds: float) -> None:
        if not isinstance(power_kw, (int, float)) or isinstance(power_kw, bool) or seconds <= 0:
            return
        hours = min(float(seconds), MAX_STEP_S) / 3600.0
        if power_kw > 0:
            self.pos_kwh += power_kw * hours
        elif power_kw < 0:
            self.neg_kwh += -power_kw * hours

    @property
    def has_data(self) -> bool:
        return bool(self.pos_kwh or self.neg_kwh)

    def regen_kwh(self, regen_is_negative: bool) -> float:
        return self.neg_kwh if regen_is_negative else self.pos_kwh


def forced_convention(db) -> Optional[bool]:
    """True/False when the user fixed the sign convention, None for automatic."""
    raw = (db.get_setting("regen_sign") or os.environ.get("REGEN_SIGN", "")).strip().lower()
    if raw in ("negative", "neg", "-"):
        return True
    if raw in ("positive", "pos", "+"):
        return False
    return None


def regen_is_negative(db, pos_kwh: float = 0.0, neg_kwh: float = 0.0) -> bool:
    """True when negative battery power means regeneration."""
    forced = forced_convention(db)
    if forced is not None:
        return forced
    pos = float(db.get_setting("regen_votes_pos", "0") or 0) + pos_kwh
    neg = float(db.get_setting("regen_votes_neg", "0") or 0) + neg_kwh
    return pos >= neg    # the larger side is discharge, so regen is the other one


def record_votes(db, pos_kwh: float, neg_kwh: float) -> None:
    """Remember the energy seen on each sign so the convention gets steadier over time."""
    if not (pos_kwh or neg_kwh):
        return
    db.set_setting("regen_votes_pos", f"{float(db.get_setting('regen_votes_pos', '0') or 0) + pos_kwh:.4f}")
    db.set_setting("regen_votes_neg", f"{float(db.get_setting('regen_votes_neg', '0') or 0) + neg_kwh:.4f}")
