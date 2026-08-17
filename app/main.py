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
from app.trading.execution import router as execution
from app.trading.broker import BrokerError, broker
from app.trading.backtest import run_backtest
from app.trading.proposal import parse_proposal
from app.trading.risk_rules import evaluate_order
from app.websocket_manager import ws_manager
from app.config import settings
from app.memory import init_memory_table, get_history, clear_history
from app.market_data import get_current_price, get_market_summary, get_candles
from app.db import close_position as db_close_position, realised_pnl
from app.autopilot import autopilot
from app.tts import synthesize as tts_synthesize, is_available as tts_available
from fastapi.responses import Response

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


class TTSRequest(BaseModel):
    text: str
    agent_key: str = ""
    lang: str = "en"


class BrokerConfigRequest(BaseModel):
    """Broker credentials entered from the UI. All optional — only what is
    sent is changed. token/account_id/region come from app.metaapi.cloud."""
    token: Optional[str] = None
    account_id: Optional[str] = None
    region: Optional[str] = None
    mode: Optional[str] = None
    trading_enabled: Optional[bool] = None


class AutopilotConfigRequest(BaseModel):
    """Runtime cap changes. Every field optional — only what is sent is applied."""
    interval_sec: Optional[int] = None
    max_trades_per_hour: Optional[int] = None
    max_trades_per_day: Optional[int] = None
    max_size: Optional[float] = None
    min_rr: Optional[float] = None
    require_approval: Optional[bool] = None
    allow_live: Optional[bool] = None
    halt_drawdown: Optional[float] = None
    symbols: Optional[list[str]] = None


# Pending proposals awaiting a Manager decision, keyed by proposal id.
# In-memory by design: an un-actioned proposal must not survive a restart.
PENDING: dict[str, dict] = {}


# --- Startup ---

def engage_kill_switch(reason: str = "activated by user") -> dict:
    """Halt everything: flatten the book, void pending orders, stop the loop.

    Shared by the /kill-switch route and the autopilot's own drawdown halt so
    both paths leave the system in exactly the same state.
    """
    global kill_switch_active
    kill_switch_active = True
    closed = execution.emergency_close_all()
    dropped = len(PENDING)
    PENDING.clear()
    if autopilot.running:
        autopilot.stop(f"kill switch — {reason}")
    log_event(
        agent_key="system",
        action_type="kill_switch_activated",
        detail=f"{reason}. Closed {closed} positions, voided {dropped} pending proposal(s)",
    )
    return {"positions_closed": closed, "proposals_voided": dropped}


@app.on_event("startup")
async def startup():
    init_db()
    init_memory_table()

    autopilot.bind(
        pending_store=PENDING,
        kill_switch_get=lambda: kill_switch_active,
        kill_switch_set=engage_kill_switch,
    )
    # Only auto-starts when the operator asked for it in .env. Default is off:
    # a fresh checkout must never begin trading on its own.
    if settings.AUTOPILOT_ENABLED:
        autopilot.start()


@app.on_event("shutdown")
async def shutdown():
    autopilot.stop("server shutting down")


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
        result = engage_kill_switch("activated by user")
        return {"ok": True, "active": True, **result}
    else:
        kill_switch_active = False
        log_event(agent_key="system", action_type="kill_switch_deactivated", detail="Kill switch deactivated")
        return {"ok": True, "active": False, "positions_closed": 0}


@app.get("/trades")
def get_trades():
    """List open trading positions.

    In broker mode this reconciles against the account first, so a stop the
    broker filled overnight shows as closed rather than lingering as open.
    """
    positions = execution.get_positions()
    return {"ok": True, "positions": positions, "count": len(positions), "mode": execution.mode}


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
    """System health, trading mode and where orders are actually routed."""
    positions = get_open_positions()
    pnl = get_daily_pnl()
    return {
        "ok": True,
        "trading_mode": settings.TRADING_MODE,
        "execution": execution.describe(),
        "kill_switch_active": kill_switch_active,
        "open_positions": len(positions),
        "daily_pnl": pnl,
        "autopilot_running": autopilot.running,
    }


# --- Broker bridge (MT5 on any OS) ---

@app.get("/broker/status")
def broker_status():
    """Connection, account and symbol mapping in one call.

    Safe to hit before trading is enabled — it only reads, so it is the right
    way to confirm a Trading.com demo login works before risking anything.
    """
    return {"ok": True, **broker.health()}


@app.get("/broker/config")
def broker_config_get():
    """Current broker config for the settings form. Token is redacted."""
    return {"ok": True, **settings.broker_config_snapshot()}


@app.post("/broker/config")
def broker_config_set(req: BrokerConfigRequest):
    """Save broker credentials from the UI and write them to .env.

    After saving, the symbol cache is cleared and a fresh read-only status is
    returned so the form can immediately show whether the connection works —
    without the caller having to enable trading first.
    """
    snapshot = settings.apply_broker_config(
        token=req.token,
        account_id=req.account_id,
        region=req.region,
        mode=req.mode,
        trading_enabled=req.trading_enabled,
        persist=True,
    )
    broker.reset_cache()
    log_event(
        agent_key="system",
        action_type="broker_config",
        detail="Broker credentials updated from UI",
        metadata=f"mode={snapshot['mode']} account={snapshot['account_id']} "
                 f"region={snapshot['region']} trading_enabled={snapshot['trading_enabled']} "
                 f"token_set={snapshot['token_set']}",
    )
    # health() only reads, so it is safe to run before trading is enabled.
    return {"ok": True, "config": snapshot, "status": broker.health()}


@app.get("/broker/symbols")
def broker_symbols(search: str = Query("", description="Case-insensitive filter")):
    """Instruments the connected account can trade.

    Useful when an index is named something unexpected, so the right value can
    go into BROKER_SYMBOL_NAS100.
    """
    try:
        names = broker.symbols()
    except BrokerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if search:
        needle = search.upper()
        names = [n for n in names if needle in n.upper()]
    return {"ok": True, "count": len(names), "symbols": sorted(names)[:400]}


@app.get("/broker/price/{symbol:path}")
def broker_price(symbol: str):
    """Live broker quote for one of our canonical symbols, e.g. XAU/USD."""
    try:
        quote = broker.price(symbol)
    except BrokerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not quote:
        raise HTTPException(status_code=404, detail=f"Broker has no price for {symbol}.")
    return {"ok": True, "data": quote}


@app.get("/broker/positions")
def broker_positions():
    """Open positions straight from the account, unmediated by our database."""
    try:
        return {"ok": True, "positions": broker.positions()}
    except BrokerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/broker/sync")
def broker_sync():
    """Reconcile the local book against the account.

    Closes local rows the broker has already settled, and adopts positions
    opened elsewhere so they count against the risk limits.
    """
    try:
        return {"ok": True, **execution.sync()}
    except BrokerError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# --- Voice ---

@app.post("/tts")
def tts(req: TTSRequest):
    """Synthesise one chunk of speech with NVIDIA Magpie.

    Returns 503 when unavailable so the browser can fall back to its own
    voice rather than the agent going silent.
    """
    audio = tts_synthesize(req.text, agent_key=req.agent_key, lang=req.lang)
    if not audio:
        raise HTTPException(status_code=503, detail="TTS unavailable — use browser fallback")
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/tts/status")
def tts_status():
    """Whether neural TTS is configured, so the UI can pick its engine."""
    return {"ok": True, "neural": tts_available(), "engine": "nvidia-magpie"}


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
        "execution": execution.describe(),
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

    # A misconfigured broker must fail loudly here rather than quietly falling
    # back to a paper fill that looks identical in the UI.
    destination = execution.describe()
    if not destination["ready"]:
        raise HTTPException(
            status_code=503,
            detail=destination["warning"] or f"Execution mode '{execution.mode}' is not ready.",
        )

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

    result = execution.execute_order(
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
        "error": result.get("error"),
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

    pnl = realised_pnl(
        symbol=pos["symbol"],
        direction=pos.get("direction", ""),
        size=pos["size"],
        entry_price=pos["entry_price"],
        exit_price=exit_price,
    )

    # Goes through the router so a broker-backed position is closed at the
    # broker as well, and the broker's own fill price and P&L win.
    result = execution.close_one(req.position_id, exit_price, pnl)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Close failed."))

    return {**result, "mode": execution.mode}


# --- Autopilot ---

@app.get("/autopilot/status")
def autopilot_status():
    """Everything the UI needs: running state, caps, counters, market session."""
    open_now, session = autopilot.fx_market_open()
    return {
        "ok": True,
        **autopilot.status(),
        "market_open": open_now,
        "session": session,
    }


@app.post("/autopilot/start")
def autopilot_start():
    """Begin unattended operation.

    Refuses while the kill switch is engaged — clearing that is a deliberate
    human act, and starting the loop must not do it silently.
    """
    if kill_switch_active:
        raise HTTPException(
            status_code=409,
            detail="Kill switch is active. Deactivate it before starting the autopilot.",
        )
    return autopilot.start()


@app.post("/autopilot/stop")
def autopilot_stop():
    """Stop unattended operation. Open positions are left untouched."""
    return autopilot.stop("stopped by user")


@app.get("/autopilot/log")
def autopilot_log(limit: int = Query(60)):
    """The activity feed, newest first."""
    entries = list(autopilot.feed)[: max(1, min(limit, 200))]
    return {"ok": True, "entries": entries, "count": len(entries)}


@app.post("/autopilot/config")
def autopilot_config(req: AutopilotConfigRequest):
    """Tighten or loosen the caps while running, without an .env edit."""
    mapping = {
        "interval_sec": "AUTOPILOT_INTERVAL_SEC",
        "max_trades_per_hour": "AUTOPILOT_MAX_TRADES_PER_HOUR",
        "max_trades_per_day": "AUTOPILOT_MAX_TRADES_PER_DAY",
        "max_size": "AUTOPILOT_MAX_SIZE",
        "min_rr": "AUTOPILOT_MIN_RR",
        "require_approval": "AUTOPILOT_REQUIRE_APPROVAL",
        "allow_live": "AUTOPILOT_ALLOW_LIVE",
        "halt_drawdown": "AUTOPILOT_HALT_DRAWDOWN",
        "symbols": "AUTOPILOT_SYMBOLS",
    }
    changed = {}
    for field, key in mapping.items():
        value = getattr(req, field)
        if value is None:
            continue
        autopilot.overrides[key] = value
        changed[key] = value

    if changed:
        log_event(
            agent_key="autopilot",
            action_type="autopilot_config",
            detail="Runtime config updated",
            metadata=str(changed),
        )
    return {"ok": True, "changed": changed, "config": autopilot.config_snapshot()}


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
