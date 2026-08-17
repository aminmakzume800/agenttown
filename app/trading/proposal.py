"""Parse free-text agent replies into structured trade orders.

Agents write proposals in prose. This module extracts a machine-checkable
order from that text so the deterministic risk gate can evaluate it.

Returns None when the text is not a trade proposal — that is the normal case
for most chat messages and must not be treated as an error.
"""
from __future__ import annotations

import re
from typing import Optional

# ── Symbol normalisation ────────────────────────────────────
SYMBOL_ALIASES = {
    "EURUSD": "EUR/USD", "EUR/USD": "EUR/USD", "EUR USD": "EUR/USD",
    "XAUUSD": "XAU/USD", "XAU/USD": "XAU/USD", "GOLD": "XAU/USD",
    "GBPUSD": "GBP/USD", "GBP/USD": "GBP/USD", "GBP USD": "GBP/USD",
    "NAS100": "NAS100", "NASDAQ": "NAS100", "US100": "NAS100", "NDX": "NAS100",
}

# Contract size per 1.0 lot — used to convert price distance into USD risk.
CONTRACT_SIZE = {
    "EUR/USD": 100_000,
    "GBP/USD": 100_000,
    "XAU/USD": 100,
    "NAS100": 20,
}

# Sane price bands to reject hallucinated levels (e.g. gold at 1.08).
PRICE_BANDS = {
    "EUR/USD": (0.5, 2.5),
    "GBP/USD": (0.5, 2.5),
    "XAU/USD": (500.0, 10_000.0),
    "NAS100": (5_000.0, 60_000.0),
}

_NUM = r"([0-9]+(?:[.,][0-9]+)?)"


def _num(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _find_symbol(text: str) -> Optional[str]:
    upper = text.upper()
    # Longest alias first so "EUR/USD" wins over a bare "EUR".
    for alias in sorted(SYMBOL_ALIASES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(alias) + r"\b", upper):
            return SYMBOL_ALIASES[alias]
    return None


def _find_side(text: str) -> Optional[str]:
    upper = text.upper()
    buy = re.search(r"\b(BUY|LONG)\b", upper)
    sell = re.search(r"\b(SELL|SHORT)\b", upper)
    if buy and sell:
        # Whichever is stated first is the directive; the other is usually
        # commentary ("... rather than selling").
        return "buy" if buy.start() < sell.start() else "sell"
    if buy:
        return "buy"
    if sell:
        return "sell"
    return None


def _first_match(text: str, patterns: list[str]) -> Optional[float]:
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            v = _num(m.group(1))
            if v is not None:
                return v
    return None


def _find_entry(text: str) -> Optional[float]:
    return _first_match(text, [
        r"entry\s*(?:price)?\s*[:=@]?\s*" + _NUM,
        r"@\s*" + _NUM,
        r"\bat\s+" + _NUM,
        r"\benter\s+(?:at\s+)?" + _NUM,
    ])


def _find_sl(text: str) -> Optional[float]:
    return _first_match(text, [
        r"stop\s*[-\s]?loss\s*[:=@]?\s*" + _NUM,
        r"\bSL\s*[:=@]?\s*" + _NUM,
        r"\bstop\s*[:=@]?\s*" + _NUM,
    ])


def _find_tp(text: str) -> Optional[float]:
    return _first_match(text, [
        r"take\s*[-\s]?profit\s*[:=@]?\s*" + _NUM,
        r"\bTP\s*[:=@]?\s*" + _NUM,
        r"\btarget\s*[:=@]?\s*" + _NUM,
    ])


def _find_size(text: str) -> Optional[float]:
    return _first_match(text, [
        r"(?:size|volume)\s*[:=]?\s*" + _NUM,
        _NUM + r"\s*lots?\b",
        r"\blots?\s*[:=]?\s*" + _NUM,
    ])


def _in_band(symbol: str, price: float) -> bool:
    lo, hi = PRICE_BANDS.get(symbol, (0.0, float("inf")))
    return lo <= price <= hi


def risk_amount(symbol: str, entry: float, stop_loss: float, size: float) -> float:
    """USD at risk if the stop is hit, based on contract size."""
    mult = CONTRACT_SIZE.get(symbol, 100_000)
    return abs(entry - stop_loss) * mult * size


def parse_proposal(text: str) -> Optional[dict]:
    """Extract a trade order from agent prose.

    Returns a dict with symbol, side, entry_price, stop_loss, take_profit,
    size, risk_usd and rr — or None if the text is not a usable proposal.

    A proposal is only accepted when symbol, side, entry and stop loss are all
    present: without a stop there is no risk to check, so it is not tradeable.
    """
    if not text or len(text) > 8000:
        return None

    symbol = _find_symbol(text)
    side = _find_side(text)
    if not symbol or not side:
        return None

    entry = _find_entry(text)
    stop_loss = _find_sl(text)
    if entry is None or stop_loss is None:
        return None

    if not _in_band(symbol, entry) or not _in_band(symbol, stop_loss):
        return None

    # Stop must sit on the losing side of entry, otherwise the parse is wrong.
    if side == "buy" and stop_loss >= entry:
        return None
    if side == "sell" and stop_loss <= entry:
        return None

    take_profit = _find_tp(text)
    if take_profit is not None and not _in_band(symbol, take_profit):
        take_profit = None

    size = _find_size(text)
    if size is None or size <= 0:
        size = 0.01  # smallest sane default; risk gate still applies
    size = round(min(size, 100.0), 2)

    risk = risk_amount(symbol, entry, stop_loss, size)

    rr = None
    if take_profit is not None:
        reward = abs(take_profit - entry)
        risk_dist = abs(entry - stop_loss)
        if risk_dist > 0:
            rr = round(reward / risk_dist, 2)

    return {
        "symbol": symbol,
        "side": side,
        "entry_price": round(entry, 5),
        "stop_loss": round(stop_loss, 5),
        "take_profit": round(take_profit, 5) if take_profit is not None else 0.0,
        "size": size,
        "risk_usd": round(risk, 2),
        "rr": rr,
    }
