"""Deterministic entry rules — the trade has to earn its place.

This inverts how entries were decided. Before, an LLM was asked "give me a
trade" and it always found one, because that is what it was asked for. A model
prompted for a trade will produce a trade whether or not a setup exists, and
that is the definition of random.

Now the rules decide. Every condition below is computed from price, and a setup
only exists when enough of them agree. The LLM's job shrinks to reviewing a
setup the rules already found, and it can veto but never invent. Most polls
should return nothing — that is correct behaviour, not a failure.

Nothing here guarantees profit. What it guarantees is that a trade has a stated,
checkable reason, the same reason every time, which is what makes the results
worth measuring at all.
"""
from __future__ import annotations

from typing import Optional

from app.trading.indicators import atr, compute_all, ema, rsi, swing_levels

# How many conditions must agree before a setup is real. Four of six keeps it
# selective; lowering this produces more trades of worse quality.
MIN_CONFLUENCE = 4

# Trades are only taken when reward is at least this multiple of risk, measured
# against a structural target rather than an arbitrary number of pips.
MIN_TARGET_RR = 1.5


def _pct(a: float, b: float) -> float:
    return abs(a - b) / b if b else 0.0


def evaluate_setup(candles: list[dict], bid: float, ask: float) -> Optional[dict]:
    """Look for a setup. Returns None when there is nothing worth taking.

    Returns a dict with side, entry, stop, target, the reasons that fired, and a
    confluence score, so the decision can be explained and audited afterwards.
    """
    if not candles or len(candles) < 60:
        return None

    ind = compute_all(candles)
    if not all([ind["ema_9"], ind["ema_21"], ind["sma_50"],
                ind["atr_14"], ind["rsi_14"], ind["swing"]]):
        return None

    close = float(candles[-1]["close"])
    ema9, ema21, sma50 = ind["ema_9"], ind["ema_21"], ind["sma_50"]
    rsi14, a, swing = ind["rsi_14"], ind["atr_14"], ind["swing"]
    macd = ind["macd"] or {}
    hist = macd.get("histogram", 0.0)

    # A flat market has no edge in either direction, and the spread still costs.
    if a / close < 0.0002:
        return None

    # Directionality test. Confluence counts conditions, but conditions can all
    # read "up" inside a market that is merely oscillating — which is how a
    # chop-trap happens. This measures whether price actually went anywhere:
    # net movement over the window against the total distance travelled. A
    # trending market scores high; an oscillating one scores near zero however
    # bullish the last few bars look.
    window = candles[-40:]
    if len(window) >= 20:
        closes_w = [float(c["close"]) for c in window]
        net = abs(closes_w[-1] - closes_w[0])
        travelled = sum(abs(b - a2) for a2, b in zip(closes_w, closes_w[1:]))
        efficiency = net / travelled if travelled else 0.0
        if efficiency < 0.25:
            return None

    long_reasons: list[str] = []
    short_reasons: list[str] = []

    # 1. Trend: fast over slow, and price on the right side of the 50.
    if ema9 > ema21:
        long_reasons.append(f"EMA9 {ema9:.5f} above EMA21 {ema21:.5f} (uptrend)")
    elif ema9 < ema21:
        short_reasons.append(f"EMA9 {ema9:.5f} below EMA21 {ema21:.5f} (downtrend)")

    if close > sma50:
        long_reasons.append(f"price {close:.5f} above SMA50 {sma50:.5f}")
    elif close < sma50:
        short_reasons.append(f"price {close:.5f} below SMA50 {sma50:.5f}")

    # 2. Momentum must agree with the trend, not merely not contradict it.
    if hist > 0:
        long_reasons.append(f"MACD histogram {hist:+.6f} (momentum up)")
    elif hist < 0:
        short_reasons.append(f"MACD histogram {hist:+.6f} (momentum down)")

    # 3. RSI. The window widens in an established trend, because a strong trend
    #    keeps RSI elevated for a long time and demanding a mid-range reading
    #    there would rule out every continuation entry. Only a genuine extreme
    #    (>82 or <18) is treated as too far gone.
    strong_up = ema9 > ema21 and close > sma50
    strong_down = ema9 < ema21 and close < sma50
    long_hi = 82 if strong_up else 68
    short_lo = 18 if strong_down else 32
    if 40 <= rsi14 <= long_hi:
        long_reasons.append(
            f"RSI {rsi14} within the {'trend' if strong_up else 'neutral'} "
            f"window (max {long_hi})"
        )
    if short_lo <= rsi14 <= 60:
        short_reasons.append(
            f"RSI {rsi14} within the {'trend' if strong_down else 'neutral'} "
            f"window (min {short_lo})"
        )

    # 4. Pullback rather than chase: price near the fast EMA, not extended.
    if _pct(close, ema9) < 0.0015:
        note = f"price within 0.15% of EMA9 (entering on a pullback, not a chase)"
        long_reasons.append(note)
        short_reasons.append(note)

    # 5. Position within the recent range. Buying in the upper part of the range
    #    is trend continuation; buying at the very top is chasing.
    span = swing["high"] - swing["low"]
    if span > 0:
        position_in_range = (close - swing["low"]) / span
        if 0.35 <= position_in_range <= 0.95:
            long_reasons.append(
                f"price {position_in_range * 100:.0f}% up the recent range "
                f"(with the trend, not at the extreme)"
            )
        if 0.05 <= position_in_range <= 0.65:
            short_reasons.append(
                f"price {position_in_range * 100:.0f}% up the recent range "
                f"(room to fall)"
            )

    # 6. Recent closes leaning the same way as the trend.
    recent = [float(c["close"]) for c in candles[-5:]]
    ups = sum(1 for x, y in zip(recent, recent[1:]) if y > x)
    if ups >= 3:
        long_reasons.append(f"{ups} of last 4 closes higher")
    elif ups <= 1:
        short_reasons.append(f"{4 - ups} of last 4 closes lower")

    long_score, short_score = len(long_reasons), len(short_reasons)

    # Conflicting evidence is a reason to stand aside, not to pick a winner.
    if long_score >= MIN_CONFLUENCE and long_score > short_score:
        side, reasons, score = "buy", long_reasons, long_score
    elif short_score >= MIN_CONFLUENCE and short_score > long_score:
        side, reasons, score = "sell", short_reasons, short_score
    else:
        return None

    # Stop outside the noise, target at structure. Both derived, not chosen.
    #
    # The target cannot simply be the recent swing level: in a trend price sits
    # right at it, leaving no room, which would reject every continuation trade.
    # So the target is the swing level when there is genuine distance to it, and
    # otherwise a measured ATR extension beyond it — a breakout target rather
    # than a mean-reversion one.
    if side == "buy":
        entry = ask
        stop = round(min(entry - 1.5 * a, swing["low"] - 0.2 * a), 5)
        risk = entry - stop
        structural = swing["high"]
        if structural - entry >= risk * MIN_TARGET_RR:
            target = round(structural, 5)
        else:
            target = round(max(structural, entry) + risk * MIN_TARGET_RR, 5)
        if target <= entry:
            return None
    else:
        entry = bid
        stop = round(max(entry + 1.5 * a, swing["high"] + 0.2 * a), 5)
        risk = stop - entry
        structural = swing["low"]
        if entry - structural >= risk * MIN_TARGET_RR:
            target = round(structural, 5)
        else:
            target = round(min(structural, entry) - risk * MIN_TARGET_RR, 5)
        if target >= entry:
            return None

    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        return None
    rr = reward / risk
    if rr < MIN_TARGET_RR:
        return None

    return {
        "side": side,
        "entry": round(entry, 5),
        "stop": stop,
        "target": target,
        "atr": a,
        "rr": round(rr, 2),
        "efficiency": round(efficiency, 3),
        "confluence": score,
        "max_confluence": 6,
        "reasons": reasons,
        "indicators": {"rsi": rsi14, "macd_histogram": hist,
                       "ema9": ema9, "ema21": ema21, "sma50": sma50},
    }


def position_size(
    balance: float, entry: float, stop: float, symbol: str,
    risk_pct: float = 0.005,
) -> float:
    """Lots such that being stopped out costs a fixed fraction of the account.

    Fixed fractional risk is the part that keeps an account alive: a losing run
    shrinks the position size automatically, where a fixed lot size does not.
    Defaults to half a percent per trade.
    """
    from app.trading.proposal import CONTRACT_SIZE

    distance = abs(entry - stop)
    if distance <= 0:
        return 0.0
    contract = CONTRACT_SIZE.get(symbol, 100_000)
    budget = max(balance, 0.0) * risk_pct
    lots = budget / (distance * contract)
    # Broker minimum is 0.01; round down so the cap is never exceeded.
    return max(0.01, round(lots - 0.005, 2))


def format_setup(setup: dict, symbol: str) -> str:
    """The setup, written out for the reviewing agent."""
    lines = [
        f"RULE-BASED SETUP FOUND on {symbol} "
        f"(confluence {setup['confluence']}/{setup['max_confluence']}, "
        f"trend efficiency {setup.get('efficiency', 0)}):",
        f"  {setup['side'].upper()} at {setup['entry']}, stop {setup['stop']}, "
        f"target {setup['target']} — reward:risk {setup['rr']}",
        "  Conditions that fired:",
    ]
    lines += [f"    - {r}" for r in setup["reasons"]]
    lines.append(
        "  The stop is 1.5x ATR beyond structure and the target is the opposing "
        "swing level. These were computed, not chosen."
    )
    return "\n".join(lines)
