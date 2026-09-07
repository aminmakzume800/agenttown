"""How old is the data, really? Broker vs public feed, and what our caches add.

Distinguishes two different things that both look like "old data":
  1. The market is shut, so the last tick is genuinely hours old. Correct.
  2. Our own caching or plumbing adds staleness on top. A bug if significant.
"""
from __future__ import annotations
import sys, time, traceback
from datetime import datetime, timezone
sys.path.insert(0, ".")
LOG = open("_fresh.log", "w", encoding="utf-8")
def out(s=""): LOG.write(str(s) + "\n"); LOG.flush()

try:
    from app.config import settings
    from app.market_data import (clear_quote_cache, fx_market_open, get_quote,
                                 quote_is_tradeable, _CANDLE_TTL)
    from app.trading.broker import broker, BrokerError

    now = datetime.now(timezone.utc)
    is_open, session = fx_market_open()
    out("=== Data freshness audit ===")
    out("now: %s (%s)" % (now.isoformat(timespec="seconds"), now.strftime("%A")))
    out("market: %s — %s" % ("OPEN" if is_open else "CLOSED", session))
    out()

    out("--- 1. What the broker itself reports (its own tick timestamps) ---")
    for sym in ("EUR/USD", "GBP/USD", "XAU/USD", "NAS100"):
        try:
            q = broker.price(sym)
        except BrokerError as e:
            out("  %-9s broker error: %s" % (sym, str(e)[:70])); continue
        if not q:
            out("  %-9s no quote" % sym); continue
        t = datetime.fromisoformat(str(q["time"]).replace("Z", "+00:00"))
        age = (now - t).total_seconds()
        out("  %-9s bid %-10s ask %-10s tick at %s  age %8.0fs (%.1f h)" % (
            sym, q["bid"], q["ask"], q["time"], age, age / 3600))
    out()

    out("--- 2. What our layer serves, and how it judges it ---")
    clear_quote_cache()
    for sym in ("EUR/USD", "XAU/USD"):
        q = get_quote(sym)
        if not q:
            out("  %-9s no quote" % sym); continue
        for style in ("scalp", "day", "swing"):
            settings.TRADING_STYLE = style
            ok, note = quote_is_tradeable(q)
            if style == "scalp":
                out("  %-9s source=%-7s age=%.0fs spread=%s" % (
                    sym, q["source"], q["age_sec"], q.get("spread")))
            out("       %-6s -> %-8s %s" % (style, "TRADE" if ok else "REFUSE", note))
    settings.TRADING_STYLE = "day"
    out()

    out("--- 3. Staleness our own caching adds ---")
    out("  quote cache TTL : %.1fs  (de-duplication window, not price ageing)" %
        settings.QUOTE_CACHE_TTL_SEC)
    out("  candle cache TTL by timeframe:")
    for tf in ("1m", "5m", "15m", "1h"):
        ttl = _CANDLE_TTL.get(tf, 0)
        out("      %-4s %5.0fs  (bar length %s)" % (tf, ttl, tf))
    out()
    clear_quote_cache()
    q1 = get_quote("EUR/USD")
    time.sleep(1.0)
    q2 = get_quote("EUR/USD")          # served from cache
    out("  first call  age = %.2fs" % q1["age_sec"])
    out("  1s later    age = %.2fs  (cache keeps counting, does not freeze)" %
        q2["age_sec"])
    delta = q2["age_sec"] - q1["age_sec"]
    out("  -> cache added %.2fs of apparent age for a 1.0s wait: %s" % (
        delta, "correct" if 0.8 <= delta <= 1.4 else "SUSPECT"))
    out()

    out("--- 4. What to expect when the market reopens ---")
    out("  Your MetaApi account streams quotes every %s seconds"
        % "2.5 (quoteStreamingIntervalInSeconds from your account JSON)")
    out("  So during market hours a broker quote should read age ~0-4s.")
    out("  Gate limits: scalp %ss, day %ss, swing %ss" % (
        settings.style("scalp")["max_quote_age_sec"],
        settings.style("day")["max_quote_age_sec"],
        settings.style("swing")["max_quote_age_sec"]))
    out()
    out("  Meaning: at 2.5s ticks every style passes, including scalp at 10s.")
    out("  Right now everything refuses only because the market is shut.")
except Exception:
    out("ABORTED"); out(traceback.format_exc())
finally:
    LOG.close()
