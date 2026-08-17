"""Paper-mode trade execution simulator. No real broker connection.

If TRADING_MODE=live and MT5 is available, delegates to mt5_live adapter.
Otherwise uses paper (simulated) execution.
"""
from uuid import uuid4

from app.config import settings
from app.db import insert_position, get_open_positions, close_position, close_all_positions, log_event


class MT5Simulator:
    """Trade execution — routes to paper or live based on config."""

    @property
    def is_live(self) -> bool:
        """Check if live trading is enabled."""
        return settings.TRADING_MODE == "live"

    @property
    def live_adapter(self):
        """Get the live MT5 adapter (lazy import to avoid errors when MT5 not installed)."""
        if not self.is_live:
            return None
        try:
            from app.trading.mt5_live import mt5_live, MT5_AVAILABLE
            if not MT5_AVAILABLE:
                return None
            if not mt5_live.is_connected:
                mt5_live.connect(
                    login=settings.MT5_LOGIN,
                    password=settings.MT5_PASSWORD,
                    server=settings.MT5_SERVER,
                )
            return mt5_live if mt5_live.is_connected else None
        except Exception:
            return None

    def execute_order(self, agent_key: str, symbol: str, direction: str, size: float, entry_price: float, stop_loss: float = 0, take_profit: float = 0) -> dict:
        """Execute a trade order — paper or live."""
        
        # Try live execution first
        adapter = self.live_adapter
        if adapter:
            result = adapter.execute_order(
                symbol=symbol,
                direction=direction,
                size=size,
                stop_loss=stop_loss,
                take_profit=take_profit,
                comment=f"Agent:{agent_key}",
            )
            if result.get("ok"):
                log_event(
                    agent_key=agent_key,
                    action_type="trade_executed_LIVE",
                    detail=f"{direction} {size} {symbol} @ {result['price']}",
                    metadata=f"order_id={result['order_id']}, sl={stop_loss}, tp={take_profit}",
                )
            return result

        # Paper mode (default)
        position_id = insert_position(
            agent_key=agent_key,
            symbol=symbol,
            direction=direction,
            size=size,
            entry_price=entry_price,
        )
        
        log_event(
            agent_key=agent_key,
            action_type="trade_executed",
            detail=f"{direction} {size} {symbol} @ {entry_price}",
            metadata=f"position_id={position_id}, sl={stop_loss}, tp={take_profit}, mode=paper",
        )
        
        return {
            "ok": True,
            "position_id": position_id,
            "symbol": symbol,
            "direction": direction,
            "size": size,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "status": "filled_simulated",
            "mode": "paper",
        }

    def get_positions(self) -> list[dict]:
        """Get all open positions (paper or live)."""
        adapter = self.live_adapter
        if adapter:
            return adapter.get_open_positions()
        return get_open_positions()

    def close_one(self, position_id: str, exit_price: float, pnl: float) -> bool:
        """Close a single position."""
        result = close_position(position_id, exit_price, pnl)
        if result:
            log_event(
                agent_key="system",
                action_type="position_closed",
                detail=f"position={position_id}, exit={exit_price}, pnl={pnl}",
            )
        return result

    def emergency_close_all(self) -> int:
        """Kill switch: close all open positions."""
        # Try live first
        adapter = self.live_adapter
        if adapter:
            count = adapter.close_all_positions()
            log_event(agent_key="system", action_type="kill_switch_LIVE", detail=f"Emergency closed {count} live positions")
            return count

        # Paper mode
        count = close_all_positions()
        log_event(agent_key="system", action_type="kill_switch", detail=f"Emergency closed {count} paper positions")
        return count


# Singleton instance
simulator = MT5Simulator()
