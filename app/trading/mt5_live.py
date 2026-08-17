"""Real MT5 adapter — sends actual orders to MetaTrader 5.

REQUIREMENTS:
- Windows OS
- MetaTrader 5 installed and running
- pip install MetaTrader5
- A broker demo/live account logged in to MT5

ACTIVATION:
- Set TRADING_MODE=live in .env
- The system will try to import MetaTrader5; if it fails, falls back to paper mode

This module is SAFE by default:
- It does NOT activate unless TRADING_MODE=live
- It logs every action to the audit trail
- Risk Manager still runs before any order
"""
import logging
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Try importing MT5 — only works on Windows with MT5 installed
MT5_AVAILABLE = False
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None
    logger.info("MetaTrader5 package not installed. Live trading unavailable. Install with: pip install MetaTrader5")


class MT5LiveAdapter:
    """Real MetaTrader 5 trading adapter.
    
    Only active when MT5 is installed and TRADING_MODE=live.
    """

    def __init__(self):
        self._connected = False
        self._account_info = None

    def connect(self, login: int = 0, password: str = "", server: str = "") -> bool:
        """Initialize MT5 connection.
        
        If login/password/server provided, logs into specific account.
        Otherwise uses the account already logged in to the MT5 terminal.
        """
        if not MT5_AVAILABLE:
            logger.error("MetaTrader5 package not available. Cannot connect.")
            return False

        if not mt5.initialize():
            logger.error("MT5 initialize() failed: %s", mt5.last_error())
            return False

        # Login to specific account if credentials provided
        if login > 0 and password and server:
            authorized = mt5.login(login=login, password=password, server=server)
            if not authorized:
                logger.error("MT5 login failed: %s", mt5.last_error())
                mt5.shutdown()
                return False

        self._account_info = mt5.account_info()
        if self._account_info is None:
            logger.error("Failed to get account info: %s", mt5.last_error())
            mt5.shutdown()
            return False

        self._connected = True
        logger.info(
            "MT5 connected: account=%d, balance=%.2f, server=%s",
            self._account_info.login,
            self._account_info.balance,
            self._account_info.server,
        )
        return True

    def disconnect(self):
        """Shutdown MT5 connection."""
        if MT5_AVAILABLE and self._connected:
            mt5.shutdown()
            self._connected = False
            logger.info("MT5 disconnected")

    @property
    def is_connected(self) -> bool:
        return self._connected and MT5_AVAILABLE

    def get_account_info(self) -> Optional[dict]:
        """Get current account information."""
        if not self.is_connected:
            return None
        info = mt5.account_info()
        if info is None:
            return None
        return {
            "login": info.login,
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "free_margin": info.margin_free,
            "profit": info.profit,
            "server": info.server,
            "currency": info.currency,
        }

    def get_price(self, symbol: str) -> Optional[dict]:
        """Get current bid/ask price from MT5."""
        if not self.is_connected:
            return None
        tick = mt5.symbol_info_tick(symbol.replace("/", ""))
        if tick is None:
            return None
        return {
            "symbol": symbol,
            "bid": tick.bid,
            "ask": tick.ask,
            "last": tick.last,
            "time": str(datetime.fromtimestamp(tick.time)),
        }

    def execute_order(
        self,
        symbol: str,
        direction: str,
        size: float,
        stop_loss: float = 0,
        take_profit: float = 0,
        comment: str = "AgentTrade",
    ) -> dict:
        """Execute a real market order on MT5.
        
        Args:
            symbol: e.g., "EURUSD" (no slash)
            direction: "buy" or "sell"
            size: Lot size (e.g., 0.01, 0.1, 1.0)
            stop_loss: Price level for stop loss (0 = no SL)
            take_profit: Price level for take profit (0 = no TP)
            comment: Order comment
            
        Returns:
            dict with ok, order_id, details or error
        """
        if not self.is_connected:
            return {"ok": False, "error": "MT5 not connected"}

        # Normalize symbol (remove slash)
        mt5_symbol = symbol.replace("/", "")

        # Check symbol exists
        symbol_info = mt5.symbol_info(mt5_symbol)
        if symbol_info is None:
            return {"ok": False, "error": f"Symbol {mt5_symbol} not found in MT5"}

        # Enable symbol if needed
        if not symbol_info.visible:
            mt5.symbol_select(mt5_symbol, True)

        # Get current price
        tick = mt5.symbol_info_tick(mt5_symbol)
        if tick is None:
            return {"ok": False, "error": f"No tick data for {mt5_symbol}"}

        # Determine order type and price
        if direction.lower() == "buy":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        elif direction.lower() == "sell":
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            return {"ok": False, "error": f"Invalid direction: {direction}"}

        # Build order request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": mt5_symbol,
            "volume": float(size),
            "type": order_type,
            "price": price,
            "deviation": 20,  # Max slippage in points
            "magic": 123456,  # Magic number to identify our orders
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        # Add SL/TP if provided
        if stop_loss > 0:
            request["sl"] = stop_loss
        if take_profit > 0:
            request["tp"] = take_profit

        # Send order
        result = mt5.order_send(request)
        if result is None:
            return {"ok": False, "error": f"Order send failed: {mt5.last_error()}"}

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return {
                "ok": False,
                "error": f"Order rejected: {result.comment} (code={result.retcode})",
            }

        logger.info(
            "MT5 order executed: %s %s %.2f lots @ %.5f, order=%d",
            direction, mt5_symbol, size, result.price, result.order,
        )

        return {
            "ok": True,
            "order_id": result.order,
            "symbol": mt5_symbol,
            "direction": direction,
            "size": size,
            "price": result.price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "status": "filled",
        }

    def close_position(self, ticket: int) -> dict:
        """Close a specific position by ticket number."""
        if not self.is_connected:
            return {"ok": False, "error": "MT5 not connected"}

        position = mt5.positions_get(ticket=ticket)
        if not position:
            return {"ok": False, "error": f"Position {ticket} not found"}

        pos = position[0]
        symbol = pos.symbol
        size = pos.volume

        # Opposite direction to close
        if pos.type == mt5.ORDER_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = mt5.symbol_info_tick(symbol).bid
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = mt5.symbol_info_tick(symbol).ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": size,
            "type": order_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": 123456,
            "comment": "AgentClose",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            return {"ok": False, "error": f"Close failed: {result.comment if result else 'unknown'}"}

        return {"ok": True, "closed_ticket": ticket, "price": result.price}

    def close_all_positions(self) -> int:
        """Emergency: close all open positions."""
        if not self.is_connected:
            return 0

        positions = mt5.positions_get()
        if not positions:
            return 0

        closed = 0
        for pos in positions:
            if pos.magic == 123456:  # Only close our orders
                result = self.close_position(pos.ticket)
                if result.get("ok"):
                    closed += 1

        return closed

    def get_open_positions(self) -> list[dict]:
        """Get all open positions from MT5."""
        if not self.is_connected:
            return []

        positions = mt5.positions_get()
        if not positions:
            return []

        result = []
        for pos in positions:
            if pos.magic == 123456:  # Only our orders
                result.append({
                    "ticket": pos.ticket,
                    "symbol": pos.symbol,
                    "direction": "buy" if pos.type == mt5.ORDER_TYPE_BUY else "sell",
                    "size": pos.volume,
                    "entry_price": pos.price_open,
                    "current_price": pos.price_current,
                    "pnl": pos.profit,
                    "sl": pos.sl,
                    "tp": pos.tp,
                    "opened_at": str(datetime.fromtimestamp(pos.time)),
                })

        return result


# Singleton
mt5_live = MT5LiveAdapter()
