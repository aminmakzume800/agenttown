"""Agent memory — persists conversation history per agent per session."""
import json
import sqlite3
from datetime import datetime
from typing import Optional

from app.config import settings


def _get_conn():
    conn = sqlite3.connect(settings.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_memory_table():
    """Create the memory table if not exists."""
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_key TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_agent ON agent_memory(agent_key)
        """)
        conn.commit()
    finally:
        conn.close()


def save_message(agent_key: str, role: str, content: str):
    """Save a message to agent memory.
    
    Args:
        agent_key: Which agent this belongs to
        role: "user" or "assistant"
        content: The message text
    """
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO agent_memory (agent_key, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (agent_key, role, content, datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_history(agent_key: str, limit: int = 10) -> list[dict]:
    """Get recent conversation history for an agent.
    
    Returns list of {"role": "user"/"assistant", "content": "..."}
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT role, content FROM agent_memory WHERE agent_key = ? ORDER BY id DESC LIMIT ?",
            (agent_key, limit),
        ).fetchall()
        # Reverse to get chronological order
        history = [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]
        return history
    finally:
        conn.close()


def clear_history(agent_key: str):
    """Clear all memory for an agent."""
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM agent_memory WHERE agent_key = ?", (agent_key,))
        conn.commit()
    finally:
        conn.close()


def get_all_memory_stats() -> dict:
    """Get message counts per agent."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT agent_key, COUNT(*) as count FROM agent_memory GROUP BY agent_key"
        ).fetchall()
        return {row["agent_key"]: row["count"] for row in rows}
    finally:
        conn.close()
