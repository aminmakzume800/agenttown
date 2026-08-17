"""Database layer for the trading agent system. SQLite only, raw queries."""

import os
import sqlite3
from datetime import datetime, date
from uuid import uuid4

from app.config import settings


def _get_conn() -> sqlite3.Connection:
    """Return a connection to the SQLite database."""
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables (audit, positions, agents) and seed demo agents."""
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)

    conn = _get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                agent_key TEXT NOT NULL,
                action_type TEXT NOT NULL,
                detail TEXT,
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY,
                agent_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                size REAL NOT NULL,
                entry_price REAL NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                exit_price REAL,
                pnl REAL,
                status TEXT NOT NULL DEFAULT 'open'
            );

            CREATE TABLE IF NOT EXISTS agents (
                agent_key TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit(agent_key);
            CREATE INDEX IF NOT EXISTS idx_audit_action ON audit(action_type);
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_positions_agent ON positions(agent_key);
        """)
        conn.commit()
    finally:
        conn.close()

    ensure_demo_agents()


def log_event(agent_key: str, action_type: str, detail: str = "", metadata: str = "") -> None:
    """Insert an audit record."""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO audit (timestamp, agent_key, action_type, detail, metadata) VALUES (?, ?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), agent_key, action_type, detail, metadata),
        )
        conn.commit()
    finally:
        conn.close()


def get_audit(agent_key: str | None = None, action_type: str | None = None, limit: int = 50) -> list[dict]:
    """Query audit with optional filters (agent_key, action_type, limit)."""
    conn = _get_conn()
    try:
        query = "SELECT * FROM audit WHERE 1=1"
        params: list = []

        if agent_key:
            query += " AND agent_key = ?"
            params.append(agent_key)
        if action_type:
            query += " AND action_type = ?"
            params.append(action_type)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_agents() -> list[dict]:
    """Return all agents as list of dicts."""
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM agents ORDER BY role, agent_key").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def set_agent_status(agent_key: str, status: str) -> bool:
    """Update agent status. Returns True if the agent was found and updated."""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "UPDATE agents SET status = ? WHERE agent_key = ?",
            (status, agent_key),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def ensure_demo_agents() -> None:
    """Seed 8 demo agents if they don't already exist."""
    demo_agents = [
        ("manager", "Manager (Alice)", "manager"),
        ("super_trader", "Super Trader", "super_trader"),
        ("risk_manager", "Risk Manager", "risk_manager"),
        ("computer_scientist", "Computer Scientist (Bob)", "computer_scientist"),
        ("trader_bot_1", "Trader Bot 1 (EUR/USD)", "trader_bot"),
        ("trader_bot_2", "Trader Bot 2 (XAU/USD)", "trader_bot"),
        ("trader_bot_3", "Trader Bot 3 (GBP/USD)", "trader_bot"),
        ("trader_bot_4", "Trader Bot 4 (NAS100)", "trader_bot"),
    ]

    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        for agent_key, display_name, role in demo_agents:
            conn.execute(
                """INSERT OR IGNORE INTO agents (agent_key, display_name, role, status, created_at)
                   VALUES (?, ?, ?, 'active', ?)""",
                (agent_key, display_name, role, now),
            )
        conn.commit()
    finally:
        conn.close()


def insert_position(
    agent_key: str,
    symbol: str,
    direction: str,
    size: float,
    entry_price: float,
) -> str:
    """Record a new trade position. Returns the position ID."""
    position_id = str(uuid4())
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO positions (id, agent_key, symbol, direction, size, entry_price, opened_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open')""",
            (position_id, agent_key, symbol, direction, size, entry_price, datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return position_id


def get_open_positions() -> list[dict]:
    """Get all open positions."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY opened_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def close_position(position_id: str, exit_price: float, pnl: float) -> bool:
    """Mark a position closed with PnL. Returns True if position was found and closed."""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """UPDATE positions SET status = 'closed', closed_at = ?, exit_price = ?, pnl = ?
               WHERE id = ? AND status = 'open'""",
            (datetime.utcnow().isoformat(), exit_price, pnl, position_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def close_all_positions(exit_price_map: dict[str, float] | None = None) -> int:
    """Kill switch: close all open positions. Returns count of positions closed.

    Args:
        exit_price_map: Optional dict mapping position_id -> exit_price.
                        If not provided, positions are closed at entry_price (PnL = 0).
    """
    conn = _get_conn()
    try:
        now = datetime.utcnow().isoformat()
        open_positions = conn.execute(
            "SELECT id, entry_price, direction, size FROM positions WHERE status = 'open'"
        ).fetchall()

        closed_count = 0
        for pos in open_positions:
            pos_id = pos["id"]
            entry_price = pos["entry_price"]

            if exit_price_map and pos_id in exit_price_map:
                exit_price = exit_price_map[pos_id]
                direction = pos["direction"]
                size = pos["size"]
                if direction == "long":
                    pnl = (exit_price - entry_price) * size
                else:
                    pnl = (entry_price - exit_price) * size
            else:
                exit_price = entry_price
                pnl = 0.0

            conn.execute(
                """UPDATE positions SET status = 'closed', closed_at = ?, exit_price = ?, pnl = ?
                   WHERE id = ?""",
                (now, exit_price, pnl, pos_id),
            )
            closed_count += 1

        conn.commit()
        return closed_count
    finally:
        conn.close()


def get_daily_pnl() -> float:
    """Sum today's realized PnL from closed positions."""
    conn = _get_conn()
    try:
        today = date.today().isoformat()
        row = conn.execute(
            """SELECT COALESCE(SUM(pnl), 0.0) as total_pnl
               FROM positions
               WHERE status = 'closed' AND closed_at LIKE ?""",
            (today + "%",),
        ).fetchone()
        return float(row["total_pnl"])
    finally:
        conn.close()
