"""Market data module — fetches real prices from free sources.

Uses Yahoo Finance (yfinance) for forex and index data.
No API key needed — completely free.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Symbol mapping: our symbols -> Yahoo Finance tickers
SYMBOL_MAP = {
    "EUR/USD": "EURUSD=X",
    "EURUSD": "EURUSD=X",
    "XAU/USD": "GC=F",
    "XAUUSD": "GC=F",
    "GBP/USD": "GBPUSD=X",
    "GBPUSD": "GBPUSD=X",
    "NAS100": "^IXIC",
}


def get_current_price(symbol: str) -> Optional[dict]:
    """Get current price for a symbol.
    
    Returns dict with: symbol, bid, ask, last, timestamp
    Or None if fetch fails.
    """
    try:
        import yfinance as yf
        ticker_symbol = SYMBOL_MAP.get(symbol.upper(), SYMBOL_MAP.get(symbol, symbol))
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="1d", interval="1m")
        if hist.empty:
            # Try 5m interval as fallback
            hist = ticker.history(period="1d", interval="5m")
        if hist.empty:
            logger.warning("No data for %s (%s)", symbol, ticker_symbol)
            return None
        
        last_row = hist.iloc[-1]
        return {
            "symbol": symbol,
            "last": round(float(last_row["Close"]), 5),
            "high": round(float(last_row["High"]), 5),
            "low": round(float(last_row["Low"]), 5),
            "open": round(float(last_row["Open"]), 5),
            "volume": int(last_row["Volume"]) if last_row["Volume"] > 0 else 0,
            "timestamp": str(hist.index[-1]),
        }
    except ImportError:
        logger.error("yfinance not installed. Run: pip install yfinance")
        return None
    except Exception as e:
        logger.error("Failed to fetch price for %s: %s", symbol, e)
        return None


def get_candles(symbol: str, timeframe: str = "1h", count: int = 20) -> Optional[list]:
    """Get historical candles for analysis.
    
    Args:
        symbol: Trading symbol (e.g., "EURUSD")
        timeframe: "1m", "5m", "15m", "1h", "4h", "1d"
        count: Number of candles to return
    
    Returns list of dicts with: time, open, high, low, close, volume
    """
    try:
        import yfinance as yf
        ticker_symbol = SYMBOL_MAP.get(symbol.upper(), SYMBOL_MAP.get(symbol, symbol))
        
        # Map timeframe to yfinance params
        tf_map = {
            "1m": ("1d", "1m"),
            "5m": ("5d", "5m"),
            "15m": ("5d", "15m"),
            "1h": ("30d", "1h"),
            "4h": ("60d", "1h"),  # yfinance doesn't have 4h, use 1h and aggregate
            "1d": ("365d", "1d"),
        }
        period, interval = tf_map.get(timeframe, ("30d", "1h"))
        
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period=period, interval=interval)
        
        if hist.empty:
            return None
        
        # Take last N candles
        hist = hist.tail(count)
        
        candles = []
        for idx, row in hist.iterrows():
            candles.append({
                "time": str(idx),
                "open": round(float(row["Open"]), 5),
                "high": round(float(row["High"]), 5),
                "low": round(float(row["Low"]), 5),
                "close": round(float(row["Close"]), 5),
                "volume": int(row["Volume"]) if row["Volume"] > 0 else 0,
            })
        
        return candles
    except ImportError:
        logger.error("yfinance not installed. Run: pip install yfinance")
        return None
    except Exception as e:
        logger.error("Failed to fetch candles for %s: %s", symbol, e)
        return None


def get_market_summary() -> dict:
    """Get a quick summary of all tracked markets.
    
    Returns dict with prices for all symbols.
    """
    summary = {}
    for symbol in ["EUR/USD", "XAU/USD", "GBP/USD", "NAS100"]:
        price = get_current_price(symbol)
        if price:
            summary[symbol] = price
        else:
            summary[symbol] = {"symbol": symbol, "last": None, "error": "unavailable"}
    return summary


def format_market_context(symbol: str) -> str:
    """Create a text summary of market data to inject into agent prompts.
    
    This gives agents real price context when proposing trades.
    """
    price = get_current_price(symbol)
    candles = get_candles(symbol, "1h", 10)
    
    if not price:
        return f"[Market data unavailable for {symbol}]"
    
    context = f"LIVE MARKET DATA for {symbol}:\n"
    context += f"  Current price: {price['last']}\n"
    context += f"  Today's high: {price['high']}\n"
    context += f"  Today's low: {price['low']}\n"
    context += f"  Today's open: {price['open']}\n"
    context += f"  Last updated: {price['timestamp']}\n"
    
    if candles and len(candles) >= 5:
        context += f"\n  Recent 1H candles (last 5):\n"
        for c in candles[-5:]:
            context += f"    {c['time']}: O={c['open']} H={c['high']} L={c['low']} C={c['close']}\n"
    
    return context
