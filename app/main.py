"""Main FastAPI application - all HTTP + WebSocket routes."""
import json
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.db import (
    init_db,
    get_agents as db_get_agents,
    set_agent_status,
    log_event,
    get_audit as db_get_audit,
    get_open_positions,
    get_daily_pnl,
)
from app.agents import ALL_AGENTS
from app.trading.mt5_simulator import simulator
from app.trading.backtest import run_backtest
from app.trading.proposal import parse_proposal
from app.trading.risk_rules import evaluate_order
from app.websocket_manager import ws_manager
from app.config import settings
from app.memory import init_memory_table, get_history, clear_history
from app.market_data import get_current_price, get_market_summary, get_candles
from app.db import close_position as db_close_position

from uuid import uuid4
from datetime import datetime

# --- App Setup ---
app = FastAPI(title="Trading Agent Starter")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Module-level state
kill_switch_active = False


# --- Request Models ---

class MessageRequest(BaseModel):
    agent_key: str
    message: str
    lang: str = "en"


class TerminateRequest(BaseModel):
    agent_key: str


class KillSwitchRequest(BaseModel):
    activate: bool


class ProposeRequest(BaseModel):
    agent_key: str
    text: str


class DecisionRequest(BaseModel):
    proposal_id: str
    approve: bool


class ClosePositionRequest(BaseModel):
    position_id: str


# Pending proposals awaiting a Manager decision, keyed by proposal id.
# In-memory by design: an un-actioned proposal must not survive a restart.
PENDING: dict[str, dict] = {}


# --- Startup ---

@app.on_event("startup")
def startup():
    init_db()
    init_memory_table()


# --- Routes ---

@app.get("/")
def serve_index():
    return FileResponse("app/static/index.html")


@app.get("/agents")
def get_agents():
    """List all agents with their current status."""
    agents = [agent.to_dict() for agent in ALL_AGENTS.values()]
    return {"ok": True, "agents": agents}


@app.post("/agent/message")
def agent_message(req: MessageRequest):
    """Send a message to an agent and get a reply."""
    global kill_switch_active

    agent = ALL_AGENTS.get(req.agent_key)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{req.agent_key}' not found")

    if kill_switch_active:
        return {
            "ok": True,
            "reply": "⚠️ Kill switch is active. All trading halted. Deactivate kill switch to resume.",
            "agent_key": req.agent_key,
        }

    try:
        reply = agent.chat(req.message, req.lang)
        return {"ok": True, "reply": reply, "agent_key": req.agent_key}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/agent/history/{agent_key}")
def agent_history(agent_key: str, limit: int = Query(40)):
    """Saved conversation for one agent, oldest first.

    Backs the chat log so a page reload or server restart does not lose the
    thread — the same rows the agent itself reads back as memory.
    """
    if agent_key not in ALL_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_key}' not found")
    return {"ok": True, "agent_key": agent_key, "messages": get_history(agent_key, limit=limit)}


@app.delete("/agent/history/{agent_key}")
def agent_history_clear(agent_key: str):
    """Wipe one agent's memory and chat history."""
    if agent_key not in ALL_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_key}' not found")
    clear_history(agent_key)
    log_event(agent_key=agent_key, action_type="memory_cleared", detail="History wiped by user")
    return {"ok": True, "agent_key": agent_key}


@app.post("/agent/terminate")
def agent_terminate(req: TerminateRequest):
    """Stop an agent session."""
    if req.agent_key not in ALL_AGENTS:
        raise HTTPException(status_code=404, detail=f"Agent '{req.agent_key}' not found")

    agent = ALL_AGENTS[req.agent_key]
    agent.status = "offline"
    set_agent_status(req.agent_key, "offline")
    log_event(agent_key=req.agent_key, action_type="terminated", detail="Agent session terminated by user")
    return {"ok": True, "agent_key": req.agent_key, "status": "offline"}


@app.post("/kill-switch")
def kill_switch(req: KillSwitchRequest):
    """Activate or deactivate the emergency kill switch."""
    global kill_switch_active

    if req.activate:
        kill_switch_active = True
        count = simulator.emergency_close_all()
        dropped = len(PENDING)
        PENDING.clear()
        log_event(
            agent_key="system",
            action_type="kill_switch_activated",
            detail=f"Closed {count} positions, voided {dropped} pending proposal(s)",
        )
        return {"ok": True, "active": True, "positions_closed": count, "proposals_voided": dropped}
    else:
        kill_switch_active = False
        log_event(agent_key="system", action_type="kill_switch_deactivated", detail="Kill switch deactivated")
        return {"ok": True, "active": False, "positions_closed": 0}


@app.get("/trades")
def get_trades():
    """List open trading positions."""
    positions = get_open_positions()
    return {"ok": True, "positions": positions, "count": len(positions)}


@app.get("/audit")
def get_audit(
    agent_key: Optional[str] = Query(None),
    action_type: Optional[str] = Query(None),
    limit: int = Query(50),
):
    """Query the audit trail."""
    records = db_get_audit(agent_key=agent_key, action_type=action_type, limit=limit)
    return {"ok": True, "records": records, "count": len(records)}


@app.get("/status")
def get_status():
    """System health and trading mode."""
    positions = get_open_positions()
    pnl = get_daily_pnl()
    return {
        "ok": True,
        "trading_mode": settings.TRADING_MODE,
        "kill_switch_active": kill_switch_active,
        "open_positions": len(positions),
        "daily_pnl": pnl,
    }


# --- Trade Pipeline ---

@app.post("/trade/propose")
def trade_propose(req: ProposeRequest):
    """Parse an agent reply into an order and run the risk gate.

    Returns is_proposal=False when the text is just conversation — that is the
    normal case and not an error.
    """
    order = parse_proposal(req.text)
    if not order:
        return {"ok": True, "is_proposal": False}

    approved, checks = evaluate_order(order)

    # The kill switch overrides every other verdict.
    if kill_switch_active:
        approved = False
        checks.insert(0, "✗ Kill switch is active — all new risk is blocked.")

    pid = str(uuid4())
    PENDING[pid] = {
        "proposal_id": pid,
        "agent_key": req.agent_key,
        "order": order,
        "risk_approved": approved,
        "checks": checks,
        "created_at": datetime.utcnow().isoformat(),
        "status": "pending" if approved else "rejected_by_risk",
    }

    log_event(
        agent_key=req.agent_key,
        action_type="trade_proposed",
        detail=f"{order['side']} {order['size']} {order['symbol']} @ {order['entry_price']} "
               f"SL {order['stop_loss']} TP {order['take_profit']}",
        metadata=f"proposal={pid} risk=${order['risk_usd']} approved={approved}",
    )
    log_event(
        agent_key="risk_manager",
        action_type="risk_assessment",
        detail=f"proposal={pid} verdict={'APPROVED' if approved else 'REJECTED'}",
        metadata=" | ".join(checks),
    )

    return {
        "ok": True,
        "is_proposal": True,
        "proposal_id": pid,
        "order": order,
        "risk_approved": approved,
        "checks": checks,
        "trading_mode": settings.TRADING_MODE,
    }


@app.post("/trade/decide")
def trade_decide(req: DecisionRequest):
    """Manager approves or rejects a pending proposal. Approval executes it."""
    global kill_switch_active

    p = PENDING.get(req.proposal_id)
    if not p:
        raise HTTPException(status_code=404, detail="Proposal not found or already actioned.")

    if not req.approve:
        p["status"] = "rejected_by_manager"
        log_event(
            agent_key="manager",
            action_type="trade_rejected",
            detail=f"proposal={req.proposal_id} rejected by manager",
        )
        PENDING.pop(req.proposal_id, None)
        return {"ok": True, "executed": False, "status": "rejected_by_manager"}

    if not p["risk_approved"]:
        raise HTTPException(status_code=403, detail="Risk Manager vetoed this proposal — it cannot be executed.")

    if kill_switch_active:
        raise HTTPException(status_code=403, detail="Kill switch is active — execution blocked.")

    # Re-run the gate at execution time: the book may have changed since the
    # proposal was raised.
    order = p["order"]
    approved_now, checks_now = evaluate_order(order)
    if not approved_now:
        p["status"] = "stale_risk"
        log_event(
            agent_key="risk_manager",
            action_type="risk_assessment",
            detail=f"proposal={req.proposal_id} re-check FAILED at execution",
            metadata=" | ".join(checks_now),
        )
        PENDING.pop(req.proposal_id, None)
        return {"ok": False, "executed": False, "status": "stale_risk", "checks": checks_now}

    result = simulator.execute_order(
        agent_key=p["agent_key"],
        symbol=order["symbol"],
        direction=order["side"],
        size=order["size"],
        entry_price=order["entry_price"],
        stop_loss=order["stop_loss"],
        take_profit=order["take_profit"],
    )

    log_event(
        agent_key="manager",
        action_type="trade_approved",
        detail=f"proposal={req.proposal_id} approved and sent to execution",
        metadata=f"mode={settings.TRADING_MODE} result_ok={result.get('ok')}",
    )

    PENDING.pop(req.proposal_id, None)
    return {
        "ok": bool(result.get("ok")),
        "executed": bool(result.get("ok")),
        "status": "executed" if result.get("ok") else "execution_failed",
        "result": result,
        "mode": settings.TRADING_MODE,
    }


@app.get("/trade/pending")
def trade_pending():
    """Proposals still awaiting a Manager decision."""
    return {"ok": True, "pending": list(PENDING.values()), "count": len(PENDING)}


@app.post("/trade/close")
def trade_close(req: ClosePositionRequest):
    """Close one open position at the current market price."""
    pos = next((p for p in get_open_positions() if p.get("id") == req.position_id), None)
    if not pos:
        raise HTTPException(status_code=404, detail="Open position not found.")

    tick = get_current_price(pos["symbol"])
    exit_price = tick["last"] if tick and tick.get("last") else pos["entry_price"]

    from app.trading.proposal import CONTRACT_SIZE
    mult = CONTRACT_SIZE.get(pos["symbol"], 100_000)
    diff = exit_price - pos["entry_price"]
    if str(pos.get("direction", "")).lower() in ("sell", "short"):
        diff = -diff
    pnl = round(diff * mult * float(pos["size"]), 2)

    ok = db_close_position(req.position_id, exit_price, pnl)
    if not ok:
        raise HTTPException(status_code=400, detail="Position could not be closed.")

    log_event(
        agent_key="system",
        action_type="position_closed",
        detail=f"{pos['symbol']} {pos.get('direction')} {pos['size']} closed @ {exit_price}",
        metadata=f"position={req.position_id} pnl={pnl}",
    )
    return {"ok": True, "position_id": req.position_id, "exit_price": exit_price, "pnl": pnl}


# --- Market Data Routes ---

@app.get("/market/price/{symbol}")
def market_price(symbol: str):
    """Get current price for a symbol."""
    price = get_current_price(symbol)
    if not price:
        return {"ok": False, "error": f"No data for {symbol}"}
    return {"ok": True, "data": price}


@app.get("/market/summary")
def market_summary():
    """Get prices for all tracked markets."""
    summary = get_market_summary()
    return {"ok": True, "markets": summary}


@app.get("/market/candles/{symbol}")
def market_candles(symbol: str, timeframe: str = Query("1h"), count: int = Query(20)):
    """Get historical candles for a symbol."""
    candles = get_candles(symbol, timeframe, count)
    if not candles:
        return {"ok": False, "error": f"No candle data for {symbol}"}
    return {"ok": True, "symbol": symbol, "timeframe": timeframe, "candles": candles}


@app.post("/backtest")
def backtest_endpoint(symbol: str = Query("EURUSD"), timeframe: str = Query("1h")):
    """Run a backtest on historical data."""
    candles = get_candles(symbol, timeframe, count=100)
    if not candles or len(candles) < 25:
        return {"ok": False, "error": f"Not enough data for backtest on {symbol}"}
    result = run_backtest(candles)
    return {"ok": True, "symbol": symbol, "timeframe": timeframe, "result": result.to_dict()}


# --- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Real-time event stream for the frontend."""
    await ws_manager.connect(ws)
    try:
        # Send initial state
        agents = [agent.to_dict() for agent in ALL_AGENTS.values()]
        await ws.send_text(json.dumps({"type": "init", "agents": agents}))
        # Keep connection alive
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception:
        ws_manager.disconnect(ws)
