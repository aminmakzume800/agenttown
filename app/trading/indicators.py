"""Technical indicators computed from candles. Pure Python, no dependencies.

Why this exists: the bots used to look at raw closes and write "RSI is neutral"
without anything having computed RSI. That is narration, not analysis — the
number was invented to sound plausible. These functions produce the real values
so the prompt can state facts, and so stops can be sized from measured
volatility instead of a fixed guess.

Everything takes a list of candle dicts (open/high/low/close/volume, oldest
first) and returns None when there is not enough history, rather than a
misleading partial answer.
"""
from __future__ import annotations

from typing import Optional


def _closes(candles: list[dict]) -> list[float]:
    return [float(c["close"]) for c in candles]


def sma(candles: list[dict], period: int = 20) -> Optional[float]:
    """Simple moving average of the close."""
    closes = _closes(candles)
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 6)


def ema(candles: list[dict], period: int = 20) -> Optional[float]:
    """Exponential moving average, seeded with the first SMA."""
    closes = _closes(candles)
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    value = sum(closes[:period]) / period
    for close in closes[period:]:
        value = close * k + value * (1 - k)
    return round(value, 6)


def rsi(candles: list[dict], period: int = 14) -> Optional[float]:
    """Wilder's RSI. Above 70 is overbought, below 30 oversold."""
    closes = _closes(candles)
    if len(closes) < period + 1:
        return None

    gains, losses = [], []
    for prev, curr in zip(closes, closes[1:]):
        change = curr - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder smoothing over the remainder.
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def macd(candles: list[dict], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[dict]:
    """MACD line, signal line and histogram.

    Histogram above zero means momentum is with the buyers.
    """
    closes = _closes(candles)
    if len(closes) < slow + signal:
        return None

    def ema_series(values: list[float], period: int) -> list[float]:
        k = 2.0 / (period + 1)
        out = [sum(values[:period]) / period]
        for v in values[period:]:
            out.append(v * k + out[-1] * (1 - k))
        return out

    fast_line = ema_series(closes, fast)
    slow_line = ema_series(closes, slow)
    # Align: the slow EMA starts later, so trim the fast one to match.
    offset = len(fast_line) - len(slow_line)
    macd_line = [f - s for f, s in zip(fast_line[offset:], slow_line)]
    if len(macd_line) < signal:
        return None
    signal_line = ema_series(macd_line, signal)
    return {
        "macd": round(macd_line[-1], 6),
        "signal": round(signal_line[-1], 6),
        "histogram": round(macd_line[-1] - signal_line[-1], 6),
    }


def atr(candles: list[dict], period: int = 14) -> Optional[float]:
    """Average True Range — how far this instrument actually moves.

    This is the useful one for risk: a stop placed inside one ATR of entry is
    inside the noise and will be taken out by ordinary movement, regardless of
    whether the direction was right.
    """
    if len(candles) < period + 1:
        return None
    trs = []
    for prev, curr in zip(candles, candles[1:]):
        high, low = float(curr["high"]), float(curr["low"])
        prev_close = float(prev["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < period:
        return None
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return round(value, 6)


def swing_levels(candles: list[dict], lookback: int = 20) -> Optional[dict]:
    """Recent high and low — the levels stops and targets actually reference."""
    if not candles:
        return None
    window = candles[-lookback:]
    return {
        "high": round(max(float(c["high"]) for c in window), 6),
        "low": round(min(float(c["low"]) for c in window), 6),
    }


def trend(candles: list[dict]) -> Optional[str]:
    """Plain-language trend read from stacked moving averages."""
    fast, slow = ema(candles, 9), ema(candles, 21)
    if fast is None or slow is None:
        return None
    spread = (fast - slow) / slow if slow else 0
    if spread > 0.0005:
        return "up"
    if spread < -0.0005:
        return "down"
    return "sideways"


def compute_all(candles: list[dict]) -> dict:
    """Every indicator at once, with Nones where history is short."""
    return {
        "rsi_14": rsi(candles),
        "macd": macd(candles),
        "atr_14": atr(candles),
        "ema_9": ema(candles, 9),
        "ema_21": ema(candles, 21),
        "sma_50": sma(candles, 50),
        "swing": swing_levels(candles),
        "trend": trend(candles),
        "candles_used": len(candles),
    }


def format_indicators(candles: list[dict], symbol: str = "") -> str:
    """Indicator block for an agent prompt.

    Values are stated plainly, with the interpretation spelled out, so the model
    reads a measurement instead of guessing at one. The ATR line also gives a
    concrete minimum stop distance, which is the difference between a stop that
    survives noise and one that does not.
    """
    if not candles or len(candles) < 15:
        return ""

    ind = compute_all(candles)
    lines = [f"COMPUTED INDICATORS{' for ' + symbol if symbol else ''} "
             f"(from {ind['candles_used']} candles — these are measured, not estimated):"]

    if ind["rsi_14"] is not None:
        r = ind["rsi_14"]
        read = "overbought" if r > 70 else "oversold" if r < 30 else "neutral"
        lines.append(f"  RSI(14): {r} — {read}")

    if ind["macd"]:
        m = ind["macd"]
        bias = "bullish" if m["histogram"] > 0 else "bearish"
        lines.append(
            f"  MACD: {m['macd']} signal {m['signal']} histogram "
            f"{m['histogram']:+.6f} — momentum {bias}"
        )

    if ind["ema_9"] is not None and ind["ema_21"] is not None:
        lines.append(
            f"  EMA9 {ind['ema_9']} vs EMA21 {ind['ema_21']} — trend {ind['trend']}"
        )
    if ind["sma_50"] is not None:
        lines.append(f"  SMA50: {ind['sma_50']}")

    if ind["swing"]:
        s = ind["swing"]
        lines.append(f"  Recent swing high {s['high']} / low {s['low']}")

    if ind["atr_14"] is not None:
        a = ind["atr_14"]
        lines.append(
            f"  ATR(14): {a} — typical move per candle. Place the stop at least "
            f"{round(a * 1.5, 6)} from entry (1.5x ATR), or ordinary noise will "
            f"close the trade before the idea plays out."
        )

    return "\n".join(lines)
