"""LIVE TEST on the demo account: place one 0.01-lot order, verify, then close.

This is the real thing — the order goes to MetaApi, which sends it to the
MetaQuotes-Demo server. It will appear in the MT5 terminal. The account is a
demo, so the money is virtual, and the size is the minimum.

Flow exercised end to end:
  agent-style plan -> parse -> risk gate -> broker execute -> mirror to SQLite
  -> read back from broker -> close at broker -> reconcile P&L

Temporary file.
"""
from __future__ import annotations
import sys, time, traceback
sys.path.insert(0, ".")
LOG = open("_live_trade.log", "w", encoding="utf-8"); F = [0]
def out(s=""): LOG.write(str(s) + "\n"); LOG.flush()
def chk(l, ok, d=""):
    out("[%s] %s%s" % ("PASS" if ok else "FAIL", l, ("  -- " + d) if d else ""))
    if not ok: F[0] += 1

try:
    from app.config import settings
    from app.db import init_db, get_open_positions, get_audit
    from app.memory import init_memory_table
    init_db(); init_memory_table()

    from app.trading.broker import broker, BrokerError
    from app.trading.execution import router
    from app.trading.proposal import parse_proposal
    from app.trading.risk_rules import evaluate_order

    SYMBOL = "EUR/USD"
    SIZE = 0.01          # broker minimum

    out("=== LIVE ORDER TEST on demo account ===")
    out("account %s  mode=%s  trading_enabled=%s" % (
        settings.METAAPI_ACCOUNT_ID[:8] + "...", settings.TRADING_MODE,
        settings.BROKER_TRADING_ENABLED))
    out()

    d = router.describe()
    chk("execution path is ready", d["ready"], str(d.get("warning")))
    chk("path is the broker bridge", d["mode"] == "broker" and d["uses_real_money"])
    chk("account is a demo", broker.is_demo is True, str(broker.is_demo))

    before = broker.account_info()
    out("balance before: %s %s   equity %s" % (
        before["balance"], before["currency"], before["equity"]))
    out("broker positions before: %d" % len(broker.positions()))
    out()

    # 1. Build a plan the way an agent would, priced at the live market.
    q = broker.price(SYMBOL)
    chk("live quote available", bool(q), str(q))
    if not q:
        raise SystemExit
    ask = q["ask"]
    plan = (f"SYMBOL: {SYMBOL}\nSIDE: BUY\nENTRY: {ask}\n"
            f"SL: {round(ask - 0.0050, 5)}\nTP: {round(ask + 0.0100, 5)}\n"
            f"SIZE: {SIZE}\nSmoke test of the execution path.")
    out("--- plan (as an agent would write it) ---")
    out(plan)
    out()

    order = parse_proposal(plan)
    chk("plan parses into an order", bool(order), str(order))

    approved, checks = evaluate_order(order)
    out("--- risk gate ---")
    for c in checks:
        out("  " + c)
    chk("risk gate approves", approved)
    out()

    if not approved:
        out("Stopping: the gate refused, so nothing is sent.")
        raise SystemExit

    # 2. Send it.
    out("--- sending to the broker ---")
    result = router.execute_order(
        agent_key="super_trader", symbol=SYMBOL, direction="buy", size=SIZE,
        entry_price=order["entry_price"], stop_loss=order["stop_loss"],
        take_profit=order["take_profit"], opened_by="user",
    )
    for k in ("ok", "status", "code", "message", "order_id",
              "broker_position_id", "entry_price", "requested_price", "position_id"):
        if k in result:
            out("  %-20s %s" % (k, result[k]))
    chk("broker accepted the order", bool(result.get("ok")),
        str(result.get("error") or result.get("message")))
    if not result.get("ok"):
        out("Stopping: order was not filled.")
        raise SystemExit

    bpid = result.get("broker_position_id")
    lpid = result.get("position_id")
    out()
    chk("broker returned a ticket", bool(bpid), str(bpid))
    chk("mirrored into the local book", bool(lpid), str(lpid))

    # 3. Read it back from the account.
    time.sleep(2)
    remote = broker.positions()
    out("--- open at the broker now: %d ---" % len(remote))
    for p in remote:
        out("  %s" % p)
    mine = [p for p in remote if p["broker_position_id"] == str(bpid)]
    chk("position is visible on the MT5 account", bool(mine))
    if mine:
        p = mine[0]
        chk("stop and target were attached at the broker",
            p["stop_loss"] > 0 and p["take_profit"] > 0,
            "SL %s TP %s" % (p["stop_loss"], p["take_profit"]))
        out("  >>> CHECK YOUR MT5 TERMINAL NOW: %s %s %s @ %s (ticket %s)" % (
            p["direction"].upper(), p["size"], p["broker_symbol"],
            p["entry_price"], bpid))
    out()

    after = broker.account_info()
    out("balance after open: %s   equity %s   margin %s" % (
        after["balance"], after["equity"], after["margin"]))
    chk("margin is now in use (position is real)",
        float(after["margin"] or 0) > 0, "margin=%s" % after["margin"])
    out()

    # 4. Close it.
    out("--- closing at market ---")
    closed = router.close_one(lpid, exit_price=q["bid"], pnl=0.0)
    for k, v in closed.items():
        out("  %-20s %s" % (k, v))
    chk("close succeeded", bool(closed.get("ok")), str(closed.get("error")))
    chk("close went through the broker", bool(closed.get("closed_at_broker")))

    time.sleep(2)
    still = [p for p in broker.positions() if p["broker_position_id"] == str(bpid)]
    chk("position is gone from the account", not still, str(still))
    chk("local book is flat", len(get_open_positions()) == 0,
        str(len(get_open_positions())))

    final = broker.account_info()
    out()
    out("balance final: %s   equity %s   margin %s" % (
        final["balance"], final["equity"], final["margin"]))
    out("realised on the trade: %s USD" % closed.get("pnl"))

    out()
    out("--- audit trail for this test ---")
    for r in reversed(get_audit(limit=8)):
        out("  %-22s %-24s %s" % (r["action_type"], r["agent_key"],
                                  str(r["detail"])[:80]))

    out()
    out("FAILURES: %d" % F[0])
except SystemExit:
    out(); out("FAILURES: %d" % F[0])
except Exception:
    out("ABORTED"); out(traceback.format_exc())
finally:
    LOG.close()
