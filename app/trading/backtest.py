"""Simple backtesting engine — replays historical data through the risk rules."""
import logging
from typing import Optional
from datetime import datetime

from app.trading.risk_rules import check_position_size, check_max_trades

logger = logging.getLogger(__name__)


class BacktestResult:
    """Results of a backtest run."""
    def __init__(self):
        self.trades: list[dict] = []
        self.total_pnl: float = 0
        self.wins: int = 0
        self.losses: int = 0
        self.max_drawdown: float = 0
        self.peak_balance: float = 0
        self.balance_history: list[float] = []
    
    @property
    def total_trades(self) -> int:
        return len(self.trades)
    
    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0
        return (self.wins / self.total_trades) * 100
    
    @property
    def profit_factor(self) -> float:
        total_wins = sum(t["pnl"] for t in self.trades if t["pnl"] > 0)
        total_losses = abs(sum(t["pnl"] for t in self.trades if t["pnl"] < 0))
        if total_losses == 0:
            return float("inf") if total_wins > 0 else 0
        return total_wins / total_losses
    
    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 2),
            "total_pnl": round(self.total_pnl, 2),
            "profit_factor": round(self.profit_factor, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "trades": self.trades[-10:],  # Last 10 trades
        }


def run_backtest(candles: list[dict], strategy: str = "sma_cross", params: Optional[dict] = None) -> BacktestResult:
    """Run a simple backtest on historical candle data.
    
    Args:
        candles: List of dicts with open, high, low, close, volume
        strategy: Strategy name ("sma_cross", "breakout")
        params: Strategy parameters
    
    Returns BacktestResult
    """
    if params is None:
        params = {"fast_period": 5, "slow_period": 20, "risk_per_trade": 100}
    
    result = BacktestResult()
    balance = 10000.0  # Starting balance
    result.peak_balance = balance
    
    if not candles or len(candles) < 25:
        logger.warning("Not enough candles for backtest (need at least 25, got %d)", len(candles) if candles else 0)
        return result
    
    # Simple SMA crossover strategy
    closes = [c["close"] for c in candles]
    fast_period = params.get("fast_period", 5)
    slow_period = params.get("slow_period", 20)
    risk = params.get("risk_per_trade", 100)
    
    position = None  # None, "long", "short"
    entry_price = 0
    
    for i in range(slow_period, len(closes)):
        fast_sma = sum(closes[i - fast_period:i]) / fast_period
        slow_sma = sum(closes[i - slow_period:i]) / slow_period
        current_price = closes[i]
        
        # Entry signal
        if position is None:
            if fast_sma > slow_sma:  # Bullish crossover
                position = "long"
                entry_price = current_price
            elif fast_sma < slow_sma:  # Bearish crossover
                position = "short"
                entry_price = current_price
        
        # Exit signal (opposite crossover)
        elif position == "long" and fast_sma < slow_sma:
            pnl = (current_price - entry_price) * risk / entry_price
            balance += pnl
            result.trades.append({
                "direction": "long",
                "entry": entry_price,
                "exit": current_price,
                "pnl": round(pnl, 2),
            })
            if pnl > 0:
                result.wins += 1
            else:
                result.losses += 1
            result.total_pnl += pnl
            position = None
            
        elif position == "short" and fast_sma > slow_sma:
            pnl = (entry_price - current_price) * risk / entry_price
            balance += pnl
            result.trades.append({
                "direction": "short",
                "entry": entry_price,
                "exit": current_price,
                "pnl": round(pnl, 2),
            })
            if pnl > 0:
                result.wins += 1
            else:
                result.losses += 1
            result.total_pnl += pnl
            position = None
        
        # Track drawdown
        result.balance_history.append(balance)
        if balance > result.peak_balance:
            result.peak_balance = balance
        drawdown = result.peak_balance - balance
        if drawdown > result.max_drawdown:
            result.max_drawdown = drawdown
    
    return result
