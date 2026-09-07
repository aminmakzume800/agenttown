"""Managing open positions — the half that decides whether a win is kept.

Entering well is not enough. A trade that goes 2R in profit and is then allowed
to return to the stop is a loss, and it was a winning trade the whole way. Two
rules handle that, both mechanical:

  Break-even: once price has moved one risk-unit in favour, the stop goes to
  entry (plus a small buffer for spread). From that point the trade cannot lose.

  Trailing: beyond that, the stop follows at a fixed ATR distance, giving the
  move room to breathe while locking in progress.

Both only ever move the stop in the protective direction. A stop that can loosen
is not a stop, so widening is refused outright.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def plan_stop_move(
    position: dict,
    current_price: float,
    atr_value: Optional[float] = None,
) -> Optional[dict]:
    """Where the stop should be now, or None to leave it alone.

    Returns {new_stop, reason, kind} where kind is 'break_even' or 'trail'.
    """
    entry = float(position["entry_price"])
    stop = float(position.get("stop_loss") or 0)
    if stop <= 0:
        return None

    is_long = str(position.get("direction", "")).lower() in ("buy", "long")
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    progress = (current_price - entry) if is_long else (entry - current_price)
    if progress <= 0:
        return None                      # nothing to protect yet

    r_multiple = progress / risk
    trail_distance = (atr_value or risk) * float(settings.TRAIL_ATR_MULT)
    buffer = risk * 0.05                 # small allowance for spread

    # Same rounding tolerance as plan_profit_take, for the same reason.
    if r_multiple >= float(settings.TRAIL_START_R) - 1e-6:
        candidate = (current_price - trail_distance if is_long
                     else current_price + trail_distance)
        kind, reason = "trail", (
            f"{r_multiple:.1f}R in profit — trailing the stop "
            f"{trail_distance:.5f} behind price"
        )
    # Before that, the first job is to remove the risk of a loss.
    elif r_multiple >= float(settings.BREAK_EVEN_R) - 1e-6:
        candidate = (entry + buffer) if is_long else (entry - buffer)
        kind, reason = "break_even", (
            f"{r_multiple:.1f}R in profit — stop to entry, the trade can no "
            f"longer lose"
        )
    else:
        return None

    candidate = round(candidate, 5)

    # Only ever tighten. A stop that can move away from price is not protection.
    if is_long and candidate <= stop:
        return None
    if not is_long and candidate >= stop:
        return None

    # Never place the stop the wrong side of the market.
    if is_long and candidate >= current_price:
        return None
    if not is_long and candidate <= current_price:
        return None

    return {"new_stop": candidate, "reason": reason, "kind": kind,
            "r_multiple": round(r_multiple, 2)}


def plan_profit_take(
    position: dict,
    current_price: float,
    already_banked: bool = False,
) -> Optional[dict]:
    """Should part of this position be closed to bank profit?

    Reasoning: a target sitting far away is often never reached, and giving back
    an open profit of 2R because price stalled is the most avoidable loss there
    is. Taking half off at a set multiple locks in a real gain, and the balance
    then runs with a break-even or trailing stop — so the remainder cannot lose.

    Returns {fraction, reason, r_multiple} or None. Only fires once per position.
    """
    if already_banked:
        return None

    entry = float(position["entry_price"])
    stop = float(position.get("stop_loss") or 0)
    size = float(position.get("size") or 0)
    if stop <= 0 or size <= 0:
        return None

    is_long = str(position.get("direction", "")).lower() in ("buy", "long")
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    progress = (current_price - entry) if is_long else (entry - current_price)
    r_multiple = progress / risk
    trigger = float(settings.PARTIAL_TAKE_R)
    # Small tolerance: prices are rounded to 5 decimals, so a genuine 1.5R can
    # compute as 1.4999 and would otherwise be missed at the exact trigger.
    if r_multiple < trigger - 1e-6:
        return None

    fraction = float(settings.PARTIAL_TAKE_FRACTION)
    if fraction <= 0 or fraction >= 1:
        return None

    return {
        "fraction": fraction,
        "r_multiple": round(r_multiple, 2),
        "reason": (
            f"{r_multiple:.1f}R in profit — banking {int(fraction * 100)}% and "
            f"letting the rest run with a protected stop"
        ),
    }


def should_time_exit(position: dict, bars_held: int) -> tuple[bool, str]:
    """Close a trade that has gone nowhere.

    A position that has neither worked nor failed is still consuming margin and
    exposure while the reason for entering it decays. Intraday styles should not
    carry a stalled trade indefinitely.
    """
    limit = int(settings.MAX_BARS_IN_TRADE)
    if limit <= 0:
        return False, "Time-based exit disabled."
    if bars_held >= limit:
        return True, (
            f"held {bars_held} bars without reaching target or stop — the setup "
            f"has expired, closing to free up risk"
        )
    return False, f"held {bars_held}/{limit} bars."
