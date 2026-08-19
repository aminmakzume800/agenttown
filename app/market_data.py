"""Market data — one price service for the whole app.

Every quote comes from the best source available, in this order:

  1. The broker (MetaApi -> your MT5 account). Real bid and ask, refreshed
     every few seconds. This is the price you actually get filled at, so it is
     used whenever credentials exist — including paper mode, because paper
     trading against a delayed feed teaches you the wrong thing.
  2. Yahoo Finance intraday bars. Free, no key, but a single mid price with no
     spread and a feed delay that varies by instrument.

Every quote carries `source` and `age_sec` so callers can tell how fresh it is
rather than assuming. `quote_is_tradeable()` turns that into a yes/no against
the active trading style: a scalp entry off a 60-second-old price is not a
trade, it is a guess.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Our symbols -> Yahoo Finance tickers.
#   ^NDX is the Nasdaq-100, which is what NAS100 means at a broker.
#   (^IXIC is the ~3000-stock Composite and sits at a different level.)
#   GC=F is gold futures — the closest free proxy for spot XAU/USD.
SYMBOL_MAP = {
    "EUR/USD": "EURUSD=X",
    "EURUSD": "EURUSD=X",
    "XAU/USD": "GC=F",
    "XAUUSD": "GC=F",
    "GBP/USD": "GBPUSD=X",
    "GBPUSD": "GBPUSD=X",
    "NAS100": "^NDX",
}

# Canonical form, so "EURUSD" and "EUR/USD" hit the same cache entry and the
# same broker symbol.
CANONICAL = {
    "EURUSD": "EUR/USD", "EUR/USD": "EUR/USD",
    "GBPUSD": "GBP/USD", "GBP/USD": "GBP/USD",
    "XAUUSD": "XAU/USD", "XAU/USD": "XAU/USD",
    "NAS100": "NAS100",
}


def canonical(symbol: str) -> str:
    return CANONICAL.get(str(symbol).upper().replace(" ", ""), symbol)


# ── quote cache ─────────────────────────────────────────────
# Guards against one agent turn firing the same lookup several times. The TTL is
# deliberately ~1s: this is de-duplication, not caching of old prices.
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _cache_get(symbol: str) -> Optional[dict]:
    ttl = float(settings.QUOTE_CACHE_TTL_SEC)
    if ttl <= 0:
        return None
    with _cache_lock:
        hit = _cache.get(symbol)
    if not hit:
        return None
    stored_at, quote = hit
    if time.time() - stored_at > ttl:
        return None
    fresh = dict(quote)
    # Age has to keep counting while the entry sits in the cache.
    fresh["age_sec"] = round(quote.get("age_sec", 0.0) + (time.time() - stored_at), 2)
    return fresh


def _cache_put(symbol: str, quote: dict) -> None:
    with _cache_lock:
        _cache[symbol] = (time.time(), quote)


def clear_quote_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _candle_cache.clear()


# ── candle cache ────────────────────────────────────────────
# Candles only change when a bar closes, so re-fetching 15-minute bars every few
# seconds is wasted latency on the critical path to a decision. TTL is tied to
# the bar size: roughly a third of a bar, so a new bar is picked up promptly
# without hammering the feed.
_CANDLE_TTL = {
    "1m": 20.0, "2m": 40.0, "5m": 60.0, "15m": 120.0,
    "30m": 240.0, "1h": 600.0, "4h": 900.0, "1d": 1800.0,
}
_candle_cache: dict[tuple[str, str, int], tuple[float, list]] = {}


# ── sources ─────────────────────────────────────────────────

def _from_broker(symbol: str) -> Optional[dict]:
    """Live bid/ask from the connected MT5 account, or None."""
    if not settings.PREFER_BROKER_QUOTES:
        return None
    try:
        from app.trading.broker import BrokerError, broker
    except Exception:
        return None
    if not broker.is_configured:
        return None
    try:
        quote = broker.price(symbol)
    except BrokerError as exc:
        logger.debug("Broker quote unavailable for %s: %s", symbol, exc)
        return None
    except Exception as exc:
        logger.warning("Broker quote failed for %s: %s", symbol, exc)
        return None
    if not quote:
        return None

    age = 0.0
    raw_time = quote.get("time")
    if raw_time:
        try:
            stamp = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            age = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
        except ValueError:
            age = 0.0

    bid, ask = float(quote["bid"]), float(quote["ask"])
    return {
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "last": round((bid + ask) / 2, 5),
        "spread": round(ask - bid, 5),
        "source": "broker",
        "age_sec": round(age, 2),
        "timestamp": str(raw_time or ""),
        "broker_symbol": quote.get("broker_symbol"),
        "high": None,
        "low": None,
        "open": None,
    }


def _from_yahoo(symbol: str) -> Optional[dict]:
    """Most recent intraday bar from Yahoo. Mid price only, no spread."""
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed. Run: pip install yfinance")
        return None

    ticker_symbol = SYMBOL_MAP.get(symbol.upper(), SYMBOL_MAP.get(symbol, symbol))
    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="1d", interval="1m")
        if hist.empty:
            hist = ticker.history(period="5d", interval="5m")
        if hist.empty:
            logger.warning("No data for %s (%s)", symbol, ticker_symbol)
            return None
    except Exception as exc:
        logger.error("Failed to fetch price for %s: %s", symbol, exc)
        return None

    row = hist.iloc[-1]
    stamp = hist.index[-1]
    age = 0.0
    try:
        as_dt = stamp.to_pydatetime()
        if as_dt.tzinfo is None:
            as_dt = as_dt.replace(tzinfo=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - as_dt).total_seconds())
    except Exception:
        age = 0.0

    last = round(float(row["Close"]), 5)
    return {
        "symbol": symbol,
        "bid": last,          # no spread available from this feed
        "ask": last,
        "last": last,
        "spread": 0.0,
        "source": "public",
        "age_sec": round(age, 2),
        "timestamp": str(stamp),
        "high": round(float(row["High"]), 5),
        "low": round(float(row["Low"]), 5),
        "open": round(float(row["Open"]), 5),
        "volume": int(row["Volume"]) if row["Volume"] > 0 else 0,
    }


# ── public API ──────────────────────────────────────────────

def get_quote(symbol: str, allow_cache: bool = True) -> Optional[dict]:
    """Best available quote: broker bid/ask if possible, else the public feed.

    Returns bid, ask, last, spread, source and age_sec — or None if no source
    could answer.
    """
    key = canonical(symbol)
    if allow_cache:
        cached = _cache_get(key)
        if cached:
            return cached

    quote = _from_broker(key) or _from_yahoo(key)
    if quote:
        _cache_put(key, quote)
    return quote


def get_current_price(symbol: str) -> Optional[dict]:
    """Backwards-compatible name. Same payload as get_quote()."""
    return get_quote(symbol)


def entry_price_for(symbol: str, side: str) -> Optional[float]:
    """The side of the book you would actually cross.

    A buy pays the ask and a sell receives the bid. Using the mid for both
    quietly flatters every simulated fill by half the spread, which matters most
    on exactly the short-term trades this is aimed at.
    """
    quote = get_quote(symbol)
    if not quote:
        return None
    if str(side).lower() in ("buy", "long"):
        return float(quote["ask"])
    return float(quote["bid"])


def quote_is_tradeable(quote: Optional[dict], style: str | None = None) -> tuple[bool, str]:
    """Whether a quote is fresh enough to trade on, for the active style."""
    if not quote:
        return False, "No price available."
    profile = settings.style(style)
    limit = float(profile["max_quote_age_sec"])
    age = float(quote.get("age_sec", 0.0))
    if age > limit:
        return False, (
            f"Price is {age:.0f}s old, above the {limit:.0f}s limit for "
            f"{settings.style_name() if style is None else style} trading "
            f"(source: {quote.get('source')})."
        )
    return True, f"Price {age:.1f}s old from {quote.get('source')}."


def get_candles(symbol: str, timeframe: str | None = None, count: int | None = None) -> Optional[list]:
    """Historical candles. Defaults follow the active trading style.

    Scalping reads 1-minute bars, day trading 15-minute, swing hourly — asking a
    scalper to reason off hourly candles is the same mistake as feeding it a
    stale price.
    """
    profile = settings.style()
    timeframe = timeframe or profile["timeframe"]
    count = count or profile["candles"]

    key = (canonical(symbol), timeframe, int(count))
    ttl = _CANDLE_TTL.get(timeframe, 120.0)
    with _cache_lock:
        hit = _candle_cache.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]

    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed. Run: pip install yfinance")
        return None

    ticker_symbol = SYMBOL_MAP.get(symbol.upper(), SYMBOL_MAP.get(symbol, symbol))
    tf_map = {
        "1m": ("1d", "1m"),
        "2m": ("5d", "2m"),
        "5m": ("5d", "5m"),
        "15m": ("5d", "15m"),
        "30m": ("5d", "30m"),
        "1h": ("30d", "1h"),
        "4h": ("60d", "1h"),   # Yahoo has no 4h; hourly is the closest
        "1d": ("365d", "1d"),
    }
    period, interval = tf_map.get(timeframe, ("5d", "15m"))

    try:
        hist = yf.Ticker(ticker_symbol).history(period=period, interval=interval)
        if hist.empty:
            return None
    except Exception as exc:
        logger.error("Failed to fetch candles for %s: %s", symbol, exc)
        return None

    hist = hist.tail(count)
    candles = [
        {
            "time": str(idx),
            "open": round(float(row["Open"]), 5),
            "high": round(float(row["High"]), 5),
            "low": round(float(row["Low"]), 5),
            "close": round(float(row["Close"]), 5),
            "volume": int(row["Volume"]) if row["Volume"] > 0 else 0,
        }
        for idx, row in hist.iterrows()
    ]
    with _cache_lock:
        _candle_cache[key] = (time.time(), candles)
    return candles


def get_market_summary() -> dict:
    """Quotes for all tracked markets, for the ticker panel."""
    summary = {}
    for symbol in ["EUR/USD", "XAU/USD", "GBP/USD", "NAS100"]:
        quote = get_quote(symbol)
        if quote:
            fresh, note = quote_is_tradeable(quote)
            summary[symbol] = {**quote, "tradeable": fresh, "freshness": note}
        else:
            summary[symbol] = {"symbol": symbol, "last": None, "error": "unavailable"}
    return summary


def format_market_context(symbol: str) -> str:
    """Live market block injected into an agent's prompt before it answers.

    Includes the exact bid and ask, the spread, how old the number is and where
    it came from, so the agent quotes levels that are actually reachable.
    """
    quote = get_quote(symbol)
    if not quote:
        return f"[Market data unavailable for {symbol}]"

    profile = settings.style()
    candles = get_candles(symbol)
    fresh, note = quote_is_tradeable(quote)

    lines = [
        f"LIVE MARKET DATA for {symbol}:",
        f"  Bid: {quote['bid']}   Ask: {quote['ask']}   Mid: {quote['last']}",
    ]
    if quote.get("spread"):
        lines.append(f"  Spread: {quote['spread']}")
    lines.append(
        f"  Source: {quote['source']} | age {quote['age_sec']}s | "
        f"{'tradeable' if fresh else 'STALE — do not quote a precise entry'}"
    )
    lines.append(
        f"  Style: {settings.style_name()} ({profile['note']}) — "
        f"buy fills at the ask, sell fills at the bid."
    )

    if candles and len(candles) >= 5:
        lines.append(f"\n  Recent {profile['timeframe']} candles (last 5):")
        for c in candles[-5:]:
            lines.append(
                f"    {c['time']}: O={c['open']} H={c['high']} L={c['low']} C={c['close']}"
            )

    return "\n".join(lines)
