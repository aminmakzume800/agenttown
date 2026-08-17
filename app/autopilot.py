"""Unattended trading loop — the part that keeps working while you sleep.

Every cycle the autopilot:
  1. checks its own guardrails and stops if any of them are breached
  2. manages open positions, closing any that hit their stop or target
  3. asks each trader bot for a read on its instrument
  4. keeps the single best idea, clamps its size, and runs the risk gate
  5. asks the Manager for a second opinion
  6. either queues the order for a human APPROVE click (the default) or
     places it, then notifies

Every guardrail is deliberately dumb and deterministic. The LLM can only ever
make a trade less likely to happen, never more: an idea has to pass the parser,
the autopilot caps, the risk gate and the Manager before it reaches execution.

Nothing here runs on import. The loop starts only when start() is called,
either by AUTOPILOT_ENABLED at boot or the button in the UI.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app.config import settings
from app.db import (
    close_position,
    get_daily_pnl,
    get_open_positions,
    log_event,
    realised_pnl,
)
from app.llm_client import SUPER, AGENT_MODEL_MAP, chat_completion
from app.market_data import get_candles, get_current_price
from app.trading.broker import BrokerError, broker, http_json
from app.trading.execution import router as execution
from app.trading.proposal import parse_proposal, risk_amount
from app.trading.risk_rules import evaluate_order
from app.websocket_manager import ws_manager

logger = logging.getLogger(__name__)

# Which bot owns which instrument.
SYMBOL_BOT = {
    "EUR/USD": "trader_bot_1",
    "XAU/USD": "trader_bot_2",
    "GBP/USD": "trader_bot_3",
    "NAS100": "trader_bot_4",
}

# Asked of each bot once per cycle. Terse on purpose: the reply is parsed, not
# read, and NO-TRADE has to be an easy answer to give.
SIGNAL_SYSTEM = """You are the {symbol} trader bot in an automated desk. You are being polled by an unattended loop, so there is no human to ask for clarification.

Output ONLY these six lines and nothing else:
SIGNAL: BUY or SELL or NO-TRADE
ENTRY: <price>
SL: <price>
TP: <price>
SIZE: <lots, at most {max_size}>
WHY: <one short sentence>

Rules you must follow:
- Answer NO-TRADE unless the setup is clear. Most polls should be NO-TRADE.
- The stop must sit on the losing side of entry, the target on the winning side.
- Reward must be at least {min_rr} times risk.
- Quote prices at the same precision as the data you were given.
- No reasoning, no markdown, no extra lines."""

MANAGER_SYSTEM = """You are the desk Manager reviewing one order an unattended loop wants to place. The deterministic risk gate has already passed it, so you are the judgement call, not the arithmetic.

Reply with exactly two lines:
VERDICT: APPROVE or REJECT
REASON: <one short sentence>

Reject if the plan is incoherent, the level makes no sense against the quoted price, or the desk is already carrying enough risk. When in doubt, reject: a missed trade costs nothing."""


class Autopilot:
    """Owns the background loop and everything the UI needs to see about it."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self.running = False
        self.halted = False
        self.halt_reason = ""
        self.cycles = 0
        self.last_cycle_at: Optional[str] = None
        self.next_cycle_at: Optional[str] = None
        self.last_error: str = ""

        # Rolling activity feed for the UI. Bounded so a long night cannot
        # grow the process without limit.
        self.feed: deque[dict] = deque(maxlen=200)

        # Execution timestamps, used for the per-hour and per-day caps.
        self._fills: list[datetime] = []

        # Runtime overrides from POST /autopilot/config, so the caps can be
        # tightened without an env edit and restart.
        self.overrides: dict[str, Any] = {}

        # Wired by main at startup — the autopilot does not own the pending
        # queue or the kill switch, it borrows them.
        self._pending: Optional[dict] = None
        self._kill_get: Callable[[], bool] = lambda: False
        self._kill_set: Callable[[str], None] = lambda reason: None

    # ── wiring ──────────────────────────────────────────────

    def bind(
        self,
        pending_store: dict,
        kill_switch_get: Callable[[], bool],
        kill_switch_set: Callable[[str], None],
    ) -> None:
        """Hand the loop the shared state it needs from the app."""
        self._pending = pending_store
        self._kill_get = kill_switch_get
        self._kill_set = kill_switch_set

    # ── settings, with runtime overrides layered on top ─────

    def cfg(self, name: str) -> Any:
        if name in self.overrides:
            return self.overrides[name]
        return getattr(settings, name)

    def config_snapshot(self) -> dict:
        keys = (
            "AUTOPILOT_INTERVAL_SEC",
            "AUTOPILOT_SYMBOLS",
            "AUTOPILOT_MAX_TRADES_PER_HOUR",
            "AUTOPILOT_MAX_TRADES_PER_DAY",
            "AUTOPILOT_MAX_SIZE",
            "AUTOPILOT_MIN_RR",
            "AUTOPILOT_REQUIRE_APPROVAL",
            "AUTOPILOT_ALLOW_LIVE",
            "AUTOPILOT_HALT_DRAWDOWN",
        )
        return {k: self.cfg(k) for k in keys}

    def status(self) -> dict:
        return {
            "running": self.running,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "cycles": self.cycles,
            "last_cycle_at": self.last_cycle_at,
            "next_cycle_at": self.next_cycle_at,
            "last_error": self.last_error,
            "fills_last_hour": len(self._recent_fills(hours=1)),
            "fills_today": len(self._recent_fills(hours=24)),
            "trading_mode": settings.TRADING_MODE,
            "execution": execution.describe(),
            "will_place_orders": self._will_place_orders(),
            "notifications": bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID),
            "config": self.config_snapshot(),
        }

    def _will_place_orders(self) -> bool:
        """True only when the loop is actually allowed to fill on its own.

        Any mode that reaches a real account — broker or local terminal, demo or
        not — additionally needs AUTOPILOT_ALLOW_LIVE. Paper needs only that
        approval is switched off.
        """
        if self.cfg("AUTOPILOT_REQUIRE_APPROVAL"):
            return False
        if execution.uses_real_money and not self.cfg("AUTOPILOT_ALLOW_LIVE"):
            return False
        return True

    # ── activity feed ───────────────────────────────────────

    def _note(self, level: str, message: str, data: Optional[dict] = None) -> dict:
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": level,          # info | trade | warn | halt
            "message": message,
            "data": data or {},
        }
        self.feed.appendleft(entry)
        logger.info("[autopilot] %s: %s", level, message)
        return entry

    async def _emit(self, level: str, message: str, data: Optional[dict] = None) -> None:
        """Record an event and push it to any open browser."""
        entry = self._note(level, message, data)
        try:
            await ws_manager.broadcast({"type": "autopilot", "entry": entry})
        except Exception:
            pass  # a dead socket must never stop the loop

    # ── lifecycle ───────────────────────────────────────────

    def start(self) -> dict:
        if self.running:
            return {"ok": True, "already_running": True, **self.status()}
        self.halted = False
        self.halt_reason = ""
        self.last_error = ""
        self.running = True
        self._task = asyncio.create_task(self._run())
        self._note(
            "info",
            "Autopilot started in %s mode, %s"
            % (
                settings.TRADING_MODE,
                "placing orders itself" if self._will_place_orders()
                else "queueing orders for approval",
            ),
        )
        log_event(
            agent_key="autopilot",
            action_type="autopilot_started",
            detail=f"mode={settings.TRADING_MODE} auto_execute={self._will_place_orders()}",
            metadata=str(self.config_snapshot()),
        )
        self._notify(
            "Autopilot ON (%s). %s."
            % (
                settings.TRADING_MODE,
                "Will place orders" if self._will_place_orders() else "Will ask before placing",
            )
        )
        return {"ok": True, "already_running": False, **self.status()}

    def stop(self, reason: str = "stopped by user") -> dict:
        was = self.running
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self.next_cycle_at = None
        if was:
            self._note("warn", f"Autopilot stopped: {reason}")
            log_event(agent_key="autopilot", action_type="autopilot_stopped", detail=reason)
            self._notify(f"Autopilot OFF: {reason}")
        return {"ok": True, "was_running": was, **self.status()}

    async def _run(self) -> None:
        """The loop. Any single cycle may fail without taking the loop down."""
        try:
            while self.running:
                try:
                    await self._cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = str(exc)[:300]
                    await self._emit("warn", f"Cycle failed: {self.last_error}")

                self.cycles += 1
                self.last_cycle_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                wait = max(30, int(self.cfg("AUTOPILOT_INTERVAL_SEC")))
                self.next_cycle_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=wait)
                ).isoformat(timespec="seconds")
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            self.next_cycle_at = None

    # ── guardrails ──────────────────────────────────────────

    def _recent_fills(self, hours: int) -> list[datetime]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        self._fills = [t for t in self._fills if t > datetime.now(timezone.utc) - timedelta(hours=24)]
        return [t for t in self._fills if t > cutoff]

    @staticmethod
    def fx_market_open(now: Optional[datetime] = None) -> tuple[bool, str]:
        """Rough FX session gate in UTC.

        The spot market runs from Sunday 21:00 to Friday 22:00. Trading a
        weekend gap is exactly the kind of thing an unattended loop should not
        do, so those hours are simply skipped.
        """
        now = now or datetime.now(timezone.utc)
        wd = now.weekday()  # Mon 0 ... Sun 6
        if wd == 5:
            return False, "Saturday — FX market closed"
        if wd == 6 and now.hour < 21:
            return False, "Sunday before 21:00 UTC — FX market closed"
        if wd == 4 and now.hour >= 22:
            return False, "Friday after 22:00 UTC — FX market closed"
        return True, "Market open"

    async def _guards_ok(self) -> bool:
        """Every reason to do nothing this cycle, checked before any API call."""
        if self._kill_get():
            await self._emit("warn", "Kill switch is active — skipping cycle")
            return False

        loss = abs(min(get_daily_pnl(), 0.0))
        limit = float(self.cfg("AUTOPILOT_HALT_DRAWDOWN"))
        if loss >= limit:
            await self._halt(
                f"Daily loss ${loss:.2f} reached the ${limit:.2f} halt limit"
            )
            return False

        per_hour = int(self.cfg("AUTOPILOT_MAX_TRADES_PER_HOUR"))
        if len(self._recent_fills(hours=1)) >= per_hour:
            await self._emit("info", f"Hourly cap reached ({per_hour}/h) — holding")
            return False

        per_day = int(self.cfg("AUTOPILOT_MAX_TRADES_PER_DAY"))
        if len(self._recent_fills(hours=24)) >= per_day:
            await self._emit("info", f"Daily cap reached ({per_day}/day) — holding")
            return False

        if execution.uses_real_money and not self.cfg("AUTOPILOT_ALLOW_LIVE"):
            await self._emit(
                "warn",
                f"TRADING_MODE is {settings.TRADING_MODE} but AUTOPILOT_ALLOW_LIVE "
                "is off — proposals will be queued, never placed",
            )

        return True

    async def _halt(self, reason: str) -> None:
        """Stop trading and pull the kill switch. Deliberately loud."""
        self.halted = True
        self.halt_reason = reason
        await self._emit("halt", f"HALTED: {reason}", {"reason": reason})
        log_event(agent_key="autopilot", action_type="autopilot_halted", detail=reason)
        try:
            self._kill_set(f"autopilot halt: {reason}")
        except Exception as exc:
            logger.error("Kill switch failed during halt: %s", exc)
        self._notify(f"AUTOPILOT HALTED — {reason}. Positions closed, trading stopped.")
        self.stop(f"halted — {reason}")

    # ── notifications ───────────────────────────────────────

    def _notify(self, text: str) -> None:
        """Best-effort Telegram push. Silent no-op when unconfigured.

        This is how you find out what happened overnight without leaving the
        browser open, so a failure here is logged and swallowed rather than
        allowed to interrupt trading.
        """
        token = settings.TELEGRAM_BOT_TOKEN
        chat = settings.TELEGRAM_CHAT_ID
        if not token or not chat:
            return
        try:
            http_json(
                "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                headers={"Accept": "application/json"},
                body={"chat_id": chat, "text": f"[Agent Town] {text}"},
                timeout=8.0,
            )
        except Exception as exc:
            logger.warning("Telegram notify failed: %s", exc)

    # ── exits ───────────────────────────────────────────────

    async def _manage_exits(self) -> None:
        """Close any open position whose stop or target has been reached.

        This is the half people forget. An entry with nothing watching the exit
        is not automation, it is an open-ended bet.

        In broker mode the stop and target were sent with the order, so the
        broker's own server closes them — even if this process is not running.
        There the job is reconciliation, not enforcement.
        """
        if execution.is_broker:
            await self._reconcile_broker()
            return

        positions = get_open_positions()
        if not positions:
            return

        prices: dict[str, float] = {}
        for symbol in {p["symbol"] for p in positions}:
            tick = await asyncio.to_thread(get_current_price, symbol)
            if tick and tick.get("last"):
                prices[symbol] = float(tick["last"])

        for pos in positions:
            price = prices.get(pos["symbol"])
            if price is None:
                continue

            sl = float(pos.get("stop_loss") or 0)
            tp = float(pos.get("take_profit") or 0)
            is_buy = str(pos.get("direction", "")).lower() in ("buy", "long")

            hit = ""
            if sl > 0 and ((is_buy and price <= sl) or (not is_buy and price >= sl)):
                hit = "stop loss"
            elif tp > 0 and ((is_buy and price >= tp) or (not is_buy and price <= tp)):
                hit = "take profit"
            if not hit:
                continue

            pnl = realised_pnl(
                symbol=pos["symbol"],
                direction=pos["direction"],
                size=pos["size"],
                entry_price=pos["entry_price"],
                exit_price=price,
            )
            if not close_position(pos["id"], price, pnl):
                continue

            log_event(
                agent_key="autopilot",
                action_type="position_closed",
                detail=f"{pos['symbol']} {pos['direction']} {pos['size']} closed @ {price} ({hit})",
                metadata=f"position={pos['id']} pnl={pnl} trigger={hit}",
            )
            await self._emit(
                "trade",
                f"{hit.title()} hit — closed {pos['symbol']} {pos['direction']} "
                f"{pos['size']} at {price} for {pnl:+.2f} USD",
                {"position_id": pos["id"], "pnl": pnl, "trigger": hit},
            )
            self._notify(
                f"{hit.title()} on {pos['symbol']} {pos['direction']} {pos['size']} "
                f"@ {price} — P&L {pnl:+.2f} USD"
            )

    async def _reconcile_broker(self) -> None:
        """Pull the broker's view of the book into the local one and report it."""
        try:
            result = await asyncio.to_thread(execution.sync)
        except Exception as exc:
            await self._emit("warn", f"Broker sync failed: {str(exc)[:140]}")
            return
        if not result.get("synced"):
            return

        for closed in result.get("closed_locally", []):
            pnl = closed["pnl"]
            await self._emit(
                "trade",
                f"Broker closed a position at {closed['exit_price']} "
                f"for {pnl:+.2f} USD",
                closed,
            )
            self._notify(f"Broker closed a position — P&L {pnl:+.2f} USD")

        for adopted in result.get("adopted", []):
            await self._emit(
                "info",
                "Adopted a position opened outside the app — it now counts "
                "against the risk limits",
                adopted,
            )

    # ── idea generation ─────────────────────────────────────

    @staticmethod
    def _quote(symbol: str) -> Optional[dict]:
        """Best available price for a symbol.

        In broker mode the broker's own quote is used, because that is the price
        an order will actually fill at; reasoning off a free data feed and then
        trading somewhere else invites entries that are stale on arrival. Falls
        back to the public feed if the bridge is unreachable.
        """
        if execution.is_broker and broker.is_configured:
            try:
                quote = broker.price(symbol)
            except BrokerError:
                quote = None
            if quote:
                return {
                    "last": quote["last"],
                    "high": None,
                    "low": None,
                    "open": None,
                    "source": "broker",
                }
        tick = get_current_price(symbol)
        if tick:
            tick["source"] = "public"
        return tick

    async def _ask_bot(self, symbol: str) -> Optional[dict]:
        """Poll one bot and return a parsed order, or None for no setup."""
        tick = await asyncio.to_thread(self._quote, symbol)
        if not tick or not tick.get("last"):
            await self._emit("warn", f"No price for {symbol} — skipped")
            return None

        candles = await asyncio.to_thread(get_candles, symbol, "1h", 12) or []
        closes = " ".join(str(c["close"]) for c in candles[-8:])

        max_size = float(self.cfg("AUTOPILOT_MAX_SIZE"))
        min_rr = float(self.cfg("AUTOPILOT_MIN_RR"))

        session = ""
        if tick.get("high") and tick.get("low"):
            session = (
                f" Session high {tick['high']}, low {tick['low']}"
                f"{', open ' + str(tick['open']) if tick.get('open') else ''}."
            )

        prompt = (
            f"{symbol} is {tick['last']}.{session}\n"
            f"Last 1H closes, oldest first: {closes or 'unavailable'}.\n"
            f"Risk at most {max_size} lots. Reward must be at least {min_rr}x risk.\n"
            "Give the plan, or NO-TRADE."
        )
        system = SIGNAL_SYSTEM.format(symbol=symbol, max_size=max_size, min_rr=min_rr)
        bot_key = SYMBOL_BOT.get(symbol, "trader_bot_1")
        _, model = AGENT_MODEL_MAP.get(bot_key, ("nvidia", SUPER))

        reply = await asyncio.to_thread(
            chat_completion,
            "nvidia", model, system, prompt, None, 400, 0.2,
        )
        if not reply:
            await self._emit("warn", f"{symbol}: no reply from {bot_key}")
            return None

        if "NO-TRADE" in reply.upper() or "NO TRADE" in reply.upper():
            await self._emit("info", f"{symbol}: no setup ({bot_key})")
            return None

        # The bot answers about the symbol it was asked about, so the symbol is
        # supplied rather than hoped for in the text.
        order = parse_proposal(f"SYMBOL: {symbol}\n{reply}")
        if not order:
            await self._emit("info", f"{symbol}: reply was not a usable plan")
            return None

        order["agent_key"] = bot_key
        order["rationale"] = reply.strip()[:400]
        order["market_price"] = float(tick["last"])
        return order

    # ── vetting ─────────────────────────────────────────────

    def _clamp(self, order: dict) -> tuple[bool, str]:
        """Autopilot-only limits, applied before the shared risk gate.

        These are tighter than the desk rules on purpose: a size a human would
        wave through is not necessarily one to place while nobody is watching.
        """
        max_size = float(self.cfg("AUTOPILOT_MAX_SIZE"))
        if order["size"] > max_size:
            order["size"] = max_size
            order["risk_usd"] = round(
                risk_amount(
                    order["symbol"], order["entry_price"], order["stop_loss"], max_size
                ),
                2,
            )

        min_rr = float(self.cfg("AUTOPILOT_MIN_RR"))
        if not order.get("take_profit"):
            return False, "no take profit — unattended entries need a defined target"
        if order.get("rr") is None or order["rr"] < min_rr:
            return False, f"reward:risk {order.get('rr')} is below the {min_rr} minimum"

        # A level far from the live price will never fill, or fills at a price
        # the bot never reasoned about.
        market = order.get("market_price")
        if market:
            drift = abs(order["entry_price"] - market) / market
            if drift > 0.01:
                return False, (
                    f"entry {order['entry_price']} is {drift * 100:.1f}% away "
                    f"from the live {market}"
                )

        return True, "within autopilot limits"

    async def _manager_review(self, order: dict, checks: list[str]) -> tuple[bool, str]:
        """Second opinion from the Manager. A non-answer counts as a reject."""
        book = get_open_positions()
        held = ", ".join(f"{p['symbol']} {p['direction']} {p['size']}" for p in book) or "flat"
        _, model = AGENT_MODEL_MAP.get("manager", ("nvidia", SUPER))

        prompt = (
            f"Proposed order: {order['side'].upper()} {order['size']} {order['symbol']} "
            f"at {order['entry_price']}, stop {order['stop_loss']}, target "
            f"{order['take_profit']}, risk ${order['risk_usd']}, reward:risk {order['rr']}.\n"
            f"Live price: {order.get('market_price')}\n"
            f"Bot rationale: {order.get('rationale', 'n/a')}\n"
            f"Risk gate: {' | '.join(checks)}\n"
            f"Currently held: {held}\n"
            f"Realised P&L today: ${get_daily_pnl():.2f}\n"
            "Approve or reject."
        )
        reply = await asyncio.to_thread(
            chat_completion,
            "nvidia", model, MANAGER_SYSTEM, prompt, None, 200, 0.1,
        )
        if not reply:
            return False, "Manager unreachable — treated as a reject"

        upper = reply.upper()
        approved = "APPROVE" in upper and "REJECT" not in upper.split("REASON")[0]
        reason = reply.strip().replace("\n", " ")[:220]
        return approved, reason

    # ── the cycle ───────────────────────────────────────────

    async def _cycle(self) -> None:
        if not await self._guards_ok():
            return

        # Exits are managed even when the session is closed for new entries.
        await self._manage_exits()

        open_now, why = self.fx_market_open()
        if not open_now:
            await self._emit("info", f"No new entries: {why}")
            return

        symbols = list(self.cfg("AUTOPILOT_SYMBOLS"))
        candidates: list[dict] = []
        for symbol in symbols:
            if not self.running:
                return
            try:
                order = await self._ask_bot(symbol)
            except Exception as exc:
                await self._emit("warn", f"{symbol}: poll failed — {str(exc)[:120]}")
                continue
            if not order:
                continue
            ok, note = self._clamp(order)
            if not ok:
                await self._emit("info", f"{symbol}: rejected — {note}")
                continue
            candidates.append(order)

        if not candidates:
            await self._emit("info", f"Scanned {len(symbols)} markets, nothing worth taking")
            return

        # One entry per cycle at most, and it is the best reward:risk on offer.
        best = max(candidates, key=lambda o: o.get("rr") or 0)
        if len(candidates) > 1:
            await self._emit(
                "info",
                f"{len(candidates)} ideas passed — taking {best['symbol']} "
                f"on the best reward:risk ({best['rr']})",
            )

        approved, checks = await asyncio.to_thread(evaluate_order, best)
        if not approved:
            failed = [c for c in checks if c.startswith("✗")]
            await self._emit(
                "warn",
                f"Risk gate vetoed {best['symbol']}: {'; '.join(failed) or 'unspecified'}",
                {"checks": checks},
            )
            log_event(
                agent_key="risk_manager",
                action_type="risk_assessment",
                detail=f"autopilot proposal on {best['symbol']} REJECTED",
                metadata=" | ".join(checks),
            )
            return

        ok, reason = await self._manager_review(best, checks)
        if not ok:
            await self._emit("info", f"Manager rejected {best['symbol']}: {reason}")
            log_event(
                agent_key="manager",
                action_type="trade_rejected",
                detail=f"autopilot proposal on {best['symbol']} rejected by manager",
                metadata=reason,
            )
            return

        await self._commit(best, checks, reason)

    # ── commit ──────────────────────────────────────────────

    async def _commit(self, order: dict, checks: list[str], manager_note: str) -> None:
        """Queue the order for approval, or place it, depending on config."""
        from uuid import uuid4

        agent_key = order.get("agent_key", "autopilot")
        summary = (
            f"{order['side'].upper()} {order['size']} {order['symbol']} @ "
            f"{order['entry_price']} SL {order['stop_loss']} TP {order['take_profit']} "
            f"(risk ${order['risk_usd']}, RR {order['rr']})"
        )

        log_event(
            agent_key=agent_key,
            action_type="trade_proposed",
            detail=summary,
            metadata=f"source=autopilot manager={manager_note}",
        )

        if not self._will_place_orders():
            # Approval mode: park it in the same queue the chat uses, so the
            # morning review is one APPROVE click in the existing UI.
            pid = str(uuid4())
            if self._pending is not None:
                self._pending[pid] = {
                    "proposal_id": pid,
                    "agent_key": agent_key,
                    "order": order,
                    "risk_approved": True,
                    "checks": checks + [f"✓ Manager: {manager_note}"],
                    "created_at": datetime.utcnow().isoformat(),
                    "status": "pending",
                    "source": "autopilot",
                }
            await self._emit(
                "trade",
                f"Queued for your approval: {summary}",
                {"proposal_id": pid, "order": order, "manager": manager_note},
            )
            self._notify(f"Proposal waiting for approval — {summary}")
            return

        result = await asyncio.to_thread(
            execution.execute_order,
            agent_key,
            order["symbol"],
            order["side"],
            order["size"],
            order["entry_price"],
            order["stop_loss"],
            order["take_profit"],
            "autopilot",
        )

        if not result.get("ok"):
            await self._emit("warn", f"Execution failed for {summary}", {"result": result})
            self._notify(f"Execution FAILED — {summary}")
            return

        self._fills.append(datetime.now(timezone.utc))
        log_event(
            agent_key="autopilot",
            action_type="trade_approved",
            detail=f"autopilot placed {summary}",
            metadata=f"mode={settings.TRADING_MODE} manager={manager_note}",
        )
        await self._emit("trade", f"Placed {summary}", {"order": order, "result": result})
        self._notify(f"Order placed ({settings.TRADING_MODE}) — {summary}")


# Singleton — the app has exactly one autopilot.
autopilot = Autopilot()
