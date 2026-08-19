"""Order execution router — one interface, three destinations.

TRADING_MODE decides where an approved order actually goes:

  paper   simulated fill written straight to SQLite. No broker, no network,
          no money. This is the default and what the demo runs on.
  broker  a real market order on your MT5 account through the MetaApi cloud
          bridge. Works identically on macOS, Linux and Windows. This is the
          path to use with a Trading.com demo account.
  live    the local MetaTrader 5 terminal via the MetaTrader5 package.
          Windows only, kept for anyone already set up that way.

In broker mode SQLite stays the local book of record: every real fill is
mirrored into the positions table with the broker's ticket alongside it. The
risk gates read that table, so they keep working unchanged, and sync() reconciles
the two whenever the broker closes something on its own.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config import settings
from app.db import (
    close_all_positions,
    close_position,
    get_open_positions,
    insert_position,
    log_event,
    realised_pnl,
)
from app.trading.broker import BrokerError, broker

logger = logging.getLogger(__name__)


class ExecutionRouter:
    """Sends orders wherever TRADING_MODE points, and reports honestly."""

    # ── mode ────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return settings.TRADING_MODE

    @property
    def is_paper(self) -> bool:
        return self.mode not in ("broker", "live")

    @property
    def is_broker(self) -> bool:
        return self.mode == "broker"

    @property
    def is_live(self) -> bool:
        """Local-terminal MT5 mode. Distinct from broker mode."""
        return self.mode == "live"

    @property
    def uses_real_money(self) -> bool:
        """True when fills leave this process for an actual account.

        A demo account is still 'real' by this definition: the orders are real
        orders on a real server, they just settle in play money.
        """
        return self.is_broker or self.is_live

    @property
    def live_adapter(self):
        """Local MT5 adapter, or None when unavailable (e.g. on a Mac)."""
        if not self.is_live:
            return None
        try:
            from app.trading.mt5_live import MT5_AVAILABLE, mt5_live

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

    def describe(self) -> dict:
        """What the UI shows so nobody has to guess where orders are going."""
        report = {
            "mode": self.mode,
            "uses_real_money": self.uses_real_money,
            "destination": {
                "paper": "Simulated locally — nothing is sent to a broker",
                "broker": "Live MT5 account via the MetaApi cloud bridge",
                "live": "Local MetaTrader 5 terminal (Windows only)",
            }.get(self.mode, "Simulated locally"),
            "ready": True,
            "warning": None,
        }
        if self.is_broker:
            report["ready"] = broker.trading_enabled
            if not broker.is_configured:
                report["warning"] = (
                    "TRADING_MODE=broker but METAAPI_TOKEN / METAAPI_ACCOUNT_ID "
                    "are missing — orders will be refused."
                )
            elif not broker.trading_enabled:
                report["warning"] = (
                    "Broker is connected read-only. Set BROKER_TRADING_ENABLED=true "
                    "to allow orders."
                )
        elif self.is_live:
            report["ready"] = self.live_adapter is not None
            if not report["ready"]:
                report["warning"] = (
                    "TRADING_MODE=live needs MetaTrader 5 running on Windows. "
                    "On macOS or Linux use TRADING_MODE=broker instead."
                )
        return report

    # ── entries ─────────────────────────────────────────────

    def execute_order(
        self,
        agent_key: str,
        symbol: str,
        direction: str,
        size: float,
        entry_price: float,
        stop_loss: float = 0,
        take_profit: float = 0,
        opened_by: str = "user",
    ) -> dict:
        """Place one order. Returns a result dict with ok set either way."""
        if self.is_broker:
            return self._execute_broker(
                agent_key, symbol, direction, size, entry_price,
                stop_loss, take_profit, opened_by,
            )

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
                    metadata=f"order_id={result.get('order_id')}, sl={stop_loss}, tp={take_profit}",
                )
            return result

        if self.is_live:
            # Asked for live but the terminal is not there. Refusing is the only
            # honest answer — silently papering the fill would be a lie.
            return {
                "ok": False,
                "status": "live_unavailable",
                "error": (
                    "MetaTrader 5 terminal is not available. On macOS or Linux "
                    "set TRADING_MODE=broker to trade through the cloud bridge."
                ),
                "mode": "live",
            }

        return self._execute_paper(
            agent_key, symbol, direction, size, entry_price,
            stop_loss, take_profit, opened_by,
        )

    def _execute_paper(
        self, agent_key, symbol, direction, size, entry_price,
        stop_loss, take_profit, opened_by,
    ) -> dict:
        # Fill at the live crossing price, not at whatever the agent proposed.
        # A paper fill that ignores the market is a simulation of nothing, and
        # ignoring the spread flatters every short-term trade.
        from app.market_data import entry_price_for

        requested = entry_price
        live = entry_price_for(symbol, direction)
        if live:
            entry_price = live

        position_id = insert_position(
            agent_key=agent_key,
            symbol=symbol,
            direction=direction,
            size=size,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            opened_by=opened_by,
        )
        log_event(
            agent_key=agent_key,
            action_type="trade_executed",
            detail=f"{direction} {size} {symbol} @ {entry_price}",
            metadata=f"position_id={position_id}, sl={stop_loss}, tp={take_profit}, "
                     f"mode=paper, by={opened_by}, requested={requested}",
        )
        return {
            "ok": True,
            "position_id": position_id,
            "symbol": symbol,
            "direction": direction,
            "size": size,
            "entry_price": entry_price,
            "requested_price": requested,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "status": "filled_simulated",
            "mode": "paper",
        }

    def _execute_broker(
        self, agent_key, symbol, direction, size, entry_price,
        stop_loss, take_profit, opened_by,
    ) -> dict:
        try:
            result = broker.place_market_order(
                canonical_symbol=symbol,
                side=direction,
                volume=size,
                stop_loss=stop_loss,
                take_profit=take_profit,
                comment=f"AT {agent_key}",
            )
        except BrokerError as exc:
            log_event(
                agent_key=agent_key,
                action_type="trade_rejected",
                detail=f"Broker refused {direction} {size} {symbol}",
                metadata=str(exc)[:300],
            )
            return {"ok": False, "status": "broker_error", "error": str(exc), "mode": "broker"}

        if not result.get("ok"):
            log_event(
                agent_key=agent_key,
                action_type="trade_rejected",
                detail=f"Broker rejected {direction} {size} {symbol}",
                metadata=f"code={result.get('code')} {result.get('message')}",
            )
            return {**result, "mode": "broker", "status": result.get("status") or "rejected"}

        broker_pid = result.get("broker_position_id")
        fill_price = self._fill_price(broker_pid) or entry_price

        position_id = insert_position(
            agent_key=agent_key,
            symbol=symbol,
            direction=direction,
            size=size,
            entry_price=fill_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            opened_by=opened_by,
            broker_position_id=broker_pid,
            broker_symbol=result.get("broker_symbol"),
        )
        log_event(
            agent_key=agent_key,
            action_type="trade_executed_BROKER",
            detail=f"{direction} {size} {symbol} filled @ {fill_price}",
            metadata=f"position_id={position_id}, broker_position={broker_pid}, "
                     f"order={result.get('order_id')}, sl={stop_loss}, tp={take_profit}, "
                     f"by={opened_by}",
        )
        return {
            **result,
            "position_id": position_id,
            "entry_price": fill_price,
            "requested_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "status": "filled_broker",
            "mode": "broker",
        }

    @staticmethod
    def _fill_price(broker_pid: Optional[str]) -> Optional[float]:
        """The price the broker actually filled at, if it can be read back."""
        if not broker_pid:
            return None
        try:
            for pos in broker.positions():
                if pos["broker_position_id"] == str(broker_pid):
                    return pos["entry_price"] or None
        except BrokerError:
            pass
        return None

    # ── reads ───────────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        """Open positions. In broker mode this reconciles first."""
        if self.is_broker:
            try:
                self.sync()
            except Exception as exc:
                logger.warning("Broker sync failed: %s", exc)
        adapter = self.live_adapter
        if adapter:
            return adapter.get_open_positions()
        return get_open_positions()

    # ── exits ───────────────────────────────────────────────

    def close_one(self, position_id: str, exit_price: float, pnl: float) -> dict:
        """Close a single position, at the broker too when there is one.

        exit_price and pnl are the local estimate. If the position is backed by
        a real broker ticket, the broker's settled numbers replace them, because
        those are the ones the account is actually credited with.
        """
        row = next((p for p in get_open_positions() if p.get("id") == position_id), None)
        if not row:
            return {"ok": False, "error": "Open position not found."}
        broker_pid = row.get("broker_position_id")
        at_broker = False

        if self.is_broker and broker_pid:
            try:
                result = broker.close_position(broker_pid)
                if not result.get("ok"):
                    logger.warning("Broker refused to close %s: %s", broker_pid, result)
                    return {
                        "ok": False,
                        "error": result.get("message") or "Broker refused to close the position.",
                        "code": result.get("code"),
                    }
                at_broker = True
                outcome = broker.position_outcome(broker_pid)
                if outcome:
                    exit_price = outcome["exit_price"] or exit_price
                    pnl = outcome["pnl"]
            except BrokerError as exc:
                logger.warning("Broker close failed for %s: %s", broker_pid, exc)
                return {"ok": False, "error": str(exc)}

        if not close_position(position_id, exit_price, pnl):
            return {"ok": False, "error": "Position could not be closed locally."}

        log_event(
            agent_key="system",
            action_type="position_closed",
            detail=f"{row['symbol']} {row.get('direction')} {row['size']} closed @ {exit_price}",
            metadata=f"position={position_id} pnl={pnl} mode={self.mode} "
                     f"broker_position={broker_pid or '-'}",
        )
        return {
            "ok": True,
            "position_id": position_id,
            "exit_price": exit_price,
            "pnl": pnl,
            "closed_at_broker": at_broker,
        }

    def emergency_close_all(self) -> int:
        """Kill switch: flatten everything, wherever it lives."""
        if self.is_broker:
            flattened = 0
            try:
                result = broker.close_all()
                flattened = result.get("count", 0)
                if result.get("failed"):
                    log_event(
                        agent_key="system",
                        action_type="kill_switch_BROKER",
                        detail=f"{len(result['failed'])} broker position(s) would not close",
                        metadata=str(result["failed"]),
                    )
            except BrokerError as exc:
                log_event(
                    agent_key="system",
                    action_type="kill_switch_BROKER",
                    detail="Broker flatten failed — local book closed anyway",
                    metadata=str(exc)[:300],
                )
            count = self._close_local_at_market()
            log_event(
                agent_key="system",
                action_type="kill_switch_BROKER",
                detail=f"Flattened {flattened} broker position(s), closed {count} local row(s)",
            )
            return max(flattened, count)

        adapter = self.live_adapter
        if adapter:
            count = adapter.close_all_positions()
            log_event(
                agent_key="system",
                action_type="kill_switch_LIVE",
                detail=f"Emergency closed {count} live positions",
            )
            return count

        count = close_all_positions()
        log_event(
            agent_key="system",
            action_type="kill_switch",
            detail=f"Emergency closed {count} paper positions",
        )
        return count

    def _close_local_at_market(self) -> int:
        """Mark local rows closed using the broker's numbers where available."""
        count = 0
        for pos in get_open_positions():
            broker_pid = pos.get("broker_position_id")
            exit_price = float(pos["entry_price"])
            pnl = 0.0
            if broker_pid:
                outcome = None
                try:
                    outcome = broker.position_outcome(broker_pid)
                except BrokerError:
                    pass
                if outcome:
                    exit_price = outcome["exit_price"] or exit_price
                    pnl = outcome["pnl"]
            if close_position(pos["id"], exit_price, pnl):
                count += 1
        return count

    # ── reconciliation ──────────────────────────────────────

    def sync(self) -> dict:
        """Make the local book agree with the broker.

        Two things drift. The broker fills a stop or target on its own, so a row
        we think is open is actually closed; and someone trades from the MT5 app
        directly, so a real position has no local row. Both are handled: the
        first is closed off with the broker's own P&L, the second is adopted so
        the risk gates can see the exposure they are supposed to be capping.
        """
        if not self.is_broker or not broker.is_configured:
            return {"synced": False, "reason": "not in broker mode"}

        remote = {p["broker_position_id"]: p for p in broker.positions()}
        local = get_open_positions()
        closed, adopted = [], []

        for row in local:
            broker_pid = row.get("broker_position_id")
            if not broker_pid or broker_pid in remote:
                continue
            outcome = broker.position_outcome(broker_pid)
            if outcome:
                exit_price, pnl = outcome["exit_price"], outcome["pnl"]
            else:
                # No deal history yet — price it locally rather than lose the row.
                exit_price = float(row["entry_price"])
                pnl = 0.0
            if close_position(row["id"], exit_price, pnl):
                closed.append({"position_id": row["id"], "pnl": pnl, "exit_price": exit_price})
                log_event(
                    agent_key="system",
                    action_type="position_closed",
                    detail=f"{row['symbol']} {row['direction']} {row['size']} "
                           f"closed at broker @ {exit_price}",
                    metadata=f"position={row['id']} broker_position={broker_pid} "
                             f"pnl={pnl} source=broker_sync",
                )

        known = {r.get("broker_position_id") for r in local if r.get("broker_position_id")}
        for broker_pid, pos in remote.items():
            if broker_pid in known:
                continue
            local_id = insert_position(
                agent_key="external",
                symbol=pos["symbol"],
                direction=pos["direction"],
                size=pos["size"],
                entry_price=pos["entry_price"],
                stop_loss=pos["stop_loss"],
                take_profit=pos["take_profit"],
                opened_by="broker",
                broker_position_id=broker_pid,
                broker_symbol=pos.get("broker_symbol"),
            )
            adopted.append({"position_id": local_id, "broker_position_id": broker_pid})
            log_event(
                agent_key="system",
                action_type="position_adopted",
                detail=f"Adopted broker position {pos['symbol']} {pos['direction']} {pos['size']}",
                metadata=f"position={local_id} broker_position={broker_pid}",
            )

        return {
            "synced": True,
            "open_at_broker": len(remote),
            "closed_locally": closed,
            "adopted": adopted,
        }


# Singleton — one router for the process.
router = ExecutionRouter()
