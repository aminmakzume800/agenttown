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
from app.websocket_manager import ws_manager
from app.config import settings
from app.memory import init_memory_table
from app.market_data import get_current_price, get_market_summary, get_candles

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
        log_event(agent_key="system", action_type="kill_switch_activated", detail=f"Closed {count} positions")
        return {"ok": True, "active": True, "positions_closed": count}
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
