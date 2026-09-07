"""Deterministic risk checking functions. Pure Python, no LLM needed."""
from app.config import settings
from app.db import get_open_positions, get_daily_pnl


def check_daily_drawdown(new_risk_amount: float) -> tuple[bool, str]:
    """Check if adding new_risk_amount would exceed daily drawdown limit.
    
    Returns (passed, reason).
    """
    current_loss = abs(min(get_daily_pnl(), 0))  # Only count losses
    remaining = settings.DAILY_DRAWDOWN_LIMIT - current_loss
    
    if new_risk_amount > remaining:
        return False, f"Daily drawdown limit: used ${current_loss:.2f} of ${settings.DAILY_DRAWDOWN_LIMIT:.2f}. New risk ${new_risk_amount:.2f} exceeds remaining ${remaining:.2f}."
    return True, f"Daily drawdown OK: ${current_loss:.2f} used, ${remaining:.2f} remaining."


def check_position_size(size: float) -> tuple[bool, str]:
    """Check if position size is within maximum allowed.
    
    Returns (passed, reason).
    """
    if size <= 0:
        return False, "Position size must be positive."
    if size > settings.MAX_POSITION_SIZE:
        return False, f"Position size {size} exceeds maximum {settings.MAX_POSITION_SIZE}."
    return True, f"Position size {size} OK (max: {settings.MAX_POSITION_SIZE})."


def check_max_trades() -> tuple[bool, str]:
    """Check if we've reached maximum concurrent open trades.
    
    Returns (passed, reason).
    """
    open_positions = get_open_positions()
    count = len(open_positions)
    
    if count >= settings.MAX_CONCURRENT_TRADES:
        return False, f"Max concurrent trades reached: {count}/{settings.MAX_CONCURRENT_TRADES}."
    return True, f"Open trades OK: {count}/{settings.MAX_CONCURRENT_TRADES}."


def check_news_blackout(symbol: str = "") -> tuple[bool, str]:
    """Refuse to open a position into a high-impact economic release.

    This used to be a stub that always passed, which meant the desk would
    happily buy thirty seconds before Non-Farm Payrolls. Now it checks a real
    schedule of the releases that move these instruments.

    Returns (passed, reason) — passed=False means do not trade.
    """
    from app.news_calendar import news_blackout

    blocked, reason = news_blackout(symbol)
    return (not blocked), reason


def run_all_checks(size: float, risk_amount: float) -> tuple[bool, list[str]]:
    """Run all risk checks and return combined result.
    
    Args:
        size: Position size in lots
        risk_amount: Potential loss amount in USD
    
    Returns:
        (all_passed, list_of_messages) - messages include both passes and failures
    """
    messages = []
    all_passed = True
    
    checks = [
        check_daily_drawdown(risk_amount),
        check_position_size(size),
        check_max_trades(),
        check_news_blackout(),
    ]
    
    for passed, reason in checks:
        messages.append(("✓ " if passed else "✗ ") + reason)
        if not passed:
            all_passed = False
    
    return all_passed, messages


# ── Correlated exposure ─────────────────────────────────────
# Instruments that tend to move together. Opening same-direction risk across a
# group multiplies drawdown, so the group is capped as one bucket.
CORRELATION_GROUPS = [
    {"EUR/USD", "GBP/USD"},   # both USD-quoted majors
]


def check_correlation(symbol: str, side: str) -> tuple[bool, str]:
    """Reject a new position that stacks same-direction correlated exposure."""
    group = next((g for g in CORRELATION_GROUPS if symbol in g), None)
    if not group:
        return True, "No correlated exposure for this symbol."

    peers = [
        p for p in get_open_positions()
        if p.get("symbol") in group
        and p.get("symbol") != symbol
        and str(p.get("direction", "")).lower() == side.lower()
    ]
    if peers:
        names = ", ".join(sorted({p["symbol"] for p in peers}))
        return False, f"Correlated exposure: already {side} on {names}."
    return True, "Correlated exposure OK."


def check_symbol_exposure(symbol: str, size: float) -> tuple[bool, str]:
    """Cap total lots per symbol at the configured max position size."""
    held = sum(
        float(p.get("size", 0))
        for p in get_open_positions()
        if p.get("symbol") == symbol
    )
    total = held + size
    if total > settings.MAX_POSITION_SIZE:
        return False, (
            f"{symbol} exposure {total:.2f} lots exceeds max "
            f"{settings.MAX_POSITION_SIZE:.2f} (already holding {held:.2f})."
        )
    return True, f"{symbol} exposure {total:.2f}/{settings.MAX_POSITION_SIZE:.2f} lots OK."


def check_symbol_tradeable(symbol: str) -> tuple[bool, str]:
    """Will the broker accept an order on this instrument at all?

    Catches the case where a broker streams prices for something it will not let
    you trade. Checked here so the proposal is refused with a clear reason,
    rather than passing every gate and then failing after you click approve.
    """
    from app.trading.execution import router

    if not router.is_broker:
        return True, "Paper mode — no broker symbol restrictions."

    from app.trading.broker import broker

    if not broker.is_configured:
        return True, "Broker not configured — symbol check skipped."
    try:
        ok, reason = broker.is_tradeable(symbol)
    except Exception as exc:
        return True, f"Could not verify symbol with broker ({str(exc)[:60]})."
    return ok, reason


def check_price_freshness(symbol: str, entry_price: float, side: str) -> tuple[bool, str]:
    """Reject an order priced off a stale quote, or too far from the market.

    This is the gate that makes short-term trading honest. Two failure modes it
    catches:

      * the quote itself is old — a scalp entry off a minute-old price is a
        guess, so the age limit tightens with the trading style
      * the entry sits far from the live market — it will either never fill, or
        fill somewhere the agent never reasoned about

    Both limits come from the active style profile, so switching to scalping
    automatically tightens them rather than needing a second set of settings.
    """
    from app.market_data import get_quote, quote_is_tradeable

    profile = settings.style()
    quote = get_quote(symbol)
    fresh, note = quote_is_tradeable(quote)
    if not fresh:
        return False, note

    market = float(quote["ask"] if str(side).lower() in ("buy", "long") else quote["bid"])
    if market <= 0:
        return False, "Live price unavailable — cannot verify the entry."

    drift = abs(float(entry_price) - market) / market
    limit = float(profile["max_entry_drift_pct"])
    if drift > limit:
        return False, (
            f"Entry {entry_price} is {drift * 100:.3f}% from the live {market} "
            f"({quote['source']}), above the {limit * 100:.3f}% limit for "
            f"{settings.style_name()} trading."
        )
    return True, (
        f"Entry within {drift * 100:.3f}% of live {market} "
        f"({quote['source']}, {quote['age_sec']}s old)."
    )


def evaluate_order(order: dict) -> tuple[bool, list[str]]:
    """Run every gate against a parsed order.

    Args:
        order: dict from proposal.parse_proposal — needs symbol, side, size,
               risk_usd.

    Returns:
        (approved, checks) where checks is a list of "✓ …" / "✗ …" lines.
        Approved is True only when every gate passes.
    """
    symbol = order.get("symbol", "")
    side = order.get("side", "buy")
    size = float(order.get("size", 0) or 0)
    risk = float(order.get("risk_usd", 0) or 0)

    entry = float(order.get("entry_price", 0) or 0)

    results = [
        check_symbol_tradeable(symbol),
        check_position_size(size),
        check_daily_drawdown(risk),
        check_max_trades(),
        check_symbol_exposure(symbol, size),
        check_correlation(symbol, side),
        check_price_freshness(symbol, entry, side),
        check_news_blackout(symbol),
    ]

    approved = all(passed for passed, _ in results)
    checks = [("✓ " if passed else "✗ ") + reason for passed, reason in results]
    return approved, checks
