"""Full trade lifecycle on the demo account: ask, enter, manage, exit.

This is the "do a trade and take the profit" test. It runs the real path end to
end on a live MT5 demo account:

  1. Ask the agent for a plan the way a user would in chat
  2. Parse it and run every risk gate
  3. Place the order at the broker
  4. Poll live prices and let the management rules act:
       - stop to break-even once the trade is 1R ahead
       - bank part of the position at the profit trigger
       - trail the remainder
  5. Exit and report the realised P&L

Honest limitation: whether this ends green depends on the market moving in our
favour during the polling window, which no code controls. The target is set
deliberately close so there is a real chance inside a few minutes. The outcome is
reported exactly as it happens, win or lose — a losing run still proves the
machinery, and pretending otherwise would be the only real failure here.

Usage:  python scripts/full_cycle_test.py [SYMBOL] [MINUTES]
        python scripts/full_cycle_test.py EUR/USD 8
"""
from __future__ import annotations

import asyncio
import sys
import time
import traceback
from datetime import datetime, timezone

sys.path.insert(0, ".")

LOG = open("_cycle.log", "w", encoding="utf-8")


def out(s: str = "") -> None:
    LOG.write(str(s) + "\n")
    LOG.flush()
    try:
        print(s)
    except Exception:
        pass


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "EUR/USD"
    minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0

    from app.config import settings
    from app.db import (get_audit, get_open_positions, init_db, realised_pnl)
    from app.memory import init_knowledge_table, init_memory_table
    init_db(); init_memory_table(); init_knowledge_table()

    from app.autopilot import autopilot
    from app.market_data import fx_market_open, get_candles, get_quote
    from app.trading.broker import BrokerError, broker
    from app.trading.execution import router
    from app.trading.indicators import atr
    from app.trading.proposal import parse_proposal
    from app.trading.risk_rules import evaluate_order

    out("=" * 66)
    out("FULL TRADE CYCLE — %s" % symbol)
    out("=" * 66)

    is_open, session = fx_market_open()
    out("market            : %s (%s)" % ("OPEN" if is_open else "CLOSED", session))
    out("execution mode    : %s" % router.mode)
    out("orders enabled    : %s" % settings.BROKER_TRADING_ENABLED)
    if not is_open:
        out("\nMarket is closed — a market order cannot fill. Stopping.")
        return
    if not router.describe()["ready"]:
        out("\nExecution path not ready: %s" % router.describe()["warning"])
        return

    ok, why = broker.is_tradeable(symbol)
    out("symbol tradeable  : %s (%s)" % (ok, why))
    if not ok:
        out("\nStopping: the broker will not accept orders on this symbol.")
        return

    before = broker.account_info()
    out("balance before    : %s %s" % (before["balance"], before["currency"]))
    out()

    # ── 1. Ask the agent, exactly as the chat UI does ──────────
    out("-" * 66)
    out("1. ASKING THE AGENT")
    out("-" * 66)
    from app.agents import ALL_AGENTS
    bot = {"EUR/USD": "trader_bot_1", "XAU/USD": "trader_bot_2",
           "GBP/USD": "trader_bot_3"}.get(symbol, "super_trader")
    ask = (f"Give me a short-term {symbol} trade I can take right now. "
           f"Keep the target close so it can be reached quickly.")
    out("user -> %s: %s" % (bot, ask))
    t0 = time.monotonic()
    reply = ALL_AGENTS[bot].chat(ask)
    out("(%.1fs)" % (time.monotonic() - t0))
    out()
    out(reply)
    out()

    order = parse_proposal(reply)
    if not order:
        out("The reply was not a usable plan, so there is nothing to execute.")
        return

    # A close target so the test can resolve inside the polling window. Real
    # trading would use the rule-derived target; this is a deliberate scalp.
    quote = get_quote(symbol)
    a = atr(get_candles(symbol) or []) or 0.0
    side = order["side"]
    entry_est = float(quote["ask"] if side == "buy" else quote["bid"])
    if a:
        tight = round(a * 0.6, 5)
        order["entry_price"] = round(entry_est, 5)
        order["stop_loss"] = round(entry_est - tight * 2 if side == "buy"
                                   else entry_est + tight * 2, 5)
        order["take_profit"] = round(entry_est + tight if side == "buy"
                                     else entry_est - tight, 5)
    order["size"] = 0.01
    from app.trading.proposal import risk_amount
    order["risk_usd"] = round(risk_amount(symbol, order["entry_price"],
                                          order["stop_loss"], order["size"]), 2)
    out("plan to send: %s %s %s @ %s  SL %s  TP %s  (risk $%s)" % (
        side.upper(), order["size"], symbol, order["entry_price"],
        order["stop_loss"], order["take_profit"], order["risk_usd"]))
    out()

    # ── 2. Risk gates ─────────────────────────────────────────
    out("-" * 66)
    out("2. RISK GATES")
    out("-" * 66)
    approved, checks = evaluate_order(order)
    for c in checks:
        out("   " + c)
    out("verdict: %s" % ("APPROVED" if approved else "REJECTED"))
    if not approved:
        out("\nStopping: the gate refused, so nothing is sent. That is the gate working.")
        return
    out()

    # ── 3. Execute ────────────────────────────────────────────
    out("-" * 66)
    out("3. PLACING THE ORDER")
    out("-" * 66)
    result = router.execute_order(
        agent_key=bot, symbol=symbol, direction=side, size=order["size"],
        entry_price=order["entry_price"], stop_loss=order["stop_loss"],
        take_profit=order["take_profit"], opened_by="user",
    )
    if not result.get("ok"):
        out("REJECTED: %s" % result.get("error") or result.get("message"))
        return
    pid = result["position_id"]
    bpid = result.get("broker_position_id")
    fill = result["entry_price"]
    out("FILLED at %s   MT5 ticket %s" % (fill, bpid))
    out("check your terminal: %s %s %s @ %s" % (
        side.upper(), order["size"], symbol, fill))
    out()

    # ── 4. Monitor and manage ─────────────────────────────────
    out("-" * 66)
    out("4. MONITORING (%.0f minutes) — banking and protecting as it moves" % minutes)
    out("-" * 66)
    risk_dist = abs(fill - order["stop_loss"])
    deadline = time.monotonic() + minutes * 60
    best_r = -99.0
    closed_by = None

    while time.monotonic() < deadline:
        q = get_quote(symbol)
        if not q:
            time.sleep(10); continue
        mark = float(q["bid"] if side == "buy" else q["ask"])
        progress = (mark - fill) if side == "buy" else (fill - mark)
        r = progress / risk_dist if risk_dist else 0.0
        best_r = max(best_r, r)

        # Has the broker already closed it on SL/TP?
        try:
            still = [p for p in broker.positions()
                     if p["broker_position_id"] == str(bpid)]
        except BrokerError:
            still = None
        if still is not None and not still:
            closed_by = "broker (stop or target hit)"
            break

        # Run the real management cycle: bank profit, move stops.
        res = asyncio.run(autopilot.manage_open_positions())
        for act in res.get("actions", []):
            out("   ACTION %s: %s" % (act.get("kind"), act))

        live = [p for p in get_open_positions() if p["id"] == pid]
        size_now = live[0]["size"] if live else 0
        stop_now = live[0].get("stop_loss") if live else None
        out("   %s  mark %-10s  %+.2fR  size %s  stop %s" % (
            datetime.now(timezone.utc).strftime("%H:%M:%S"),
            mark, r, size_now, stop_now))
        if not live:
            closed_by = "management (fully closed)"
            break
        time.sleep(20)

    out()

    # ── 5. Close and report ───────────────────────────────────
    out("-" * 66)
    out("5. RESULT")
    out("-" * 66)
    remaining = [p for p in get_open_positions() if p["id"] == pid]
    if remaining:
        pos = remaining[0]
        q = get_quote(symbol)
        exit_px = float(q["bid"] if side == "buy" else q["ask"])
        est = realised_pnl(symbol=symbol, direction=side, size=float(pos["size"]),
                           entry_price=fill, exit_price=exit_px)
        out("closing the remainder at market (%s)" % exit_px)
        closed = router.close_one(pid, exit_price=exit_px, pnl=est)
        out("   %s" % closed)
        closed_by = closed_by or "test end"

    banked = 0.0
    for rec in get_audit(limit=40):
        if rec["action_type"] == "partial_close" and pid[:8] in str(rec["metadata"]):
            out("banked earlier: %s" % rec["detail"])
    for rec in get_audit(action_type="partial_close", limit=10):
        if pid[:8] in str(rec["metadata"]):
            try:
                banked += float(str(rec["detail"]).split("for ")[1].split(" ")[0])
            except Exception:
                pass

    after = broker.account_info()
    out()
    out("closed by         : %s" % closed_by)
    out("best excursion    : %+.2fR" % best_r)
    out("balance before    : %s" % before["balance"])
    out("balance after     : %s" % after["balance"])
    net = float(after["balance"]) - float(before["balance"])
    out("NET ON THE ACCOUNT: %+.2f %s" % (net, after["currency"]))
    out()
    if net > 0:
        out("Green. The cycle ran and the account ended up.")
    elif net == 0:
        out("Flat. The mechanics worked; price did not move enough either way.")
    else:
        out("Red. The machinery worked correctly and this trade lost — which is")
        out("what a real system does a meaningful fraction of the time. The point")
        out("proven here is the pipeline, not that any single trade wins.")
    out()
    out("broker positions still open: %d" % len(broker.positions()))
    out("local book still open      : %d" % len(get_open_positions()))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        out("ABORTED"); out(traceback.format_exc())
    finally:
        LOG.close()
