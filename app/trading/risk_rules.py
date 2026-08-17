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


def check_news_blackout() -> tuple[bool, str]:
    """Check if a news blackout is currently active.
    
    For MVP, this always returns True (no blackout).
    In production, this would check an economic calendar.
    
    Returns (passed, reason).
    """
    # MVP: No news calendar integration yet
    return True, "No news blackout active (MVP: calendar not integrated)."


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
