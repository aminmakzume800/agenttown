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


# ── Durable knowledge ───────────────────────────────────────
# Chat history is a rolling window (the last handful of turns), so anything the
# user says scrolls out of view and is effectively forgotten. Instructions,
# preferences and corrections need to outlive that window, so they are extracted
# and stored separately, then injected into every later prompt.
#
# Scope: a fact saved for one agent is private to it, unless saved against
# "all", which every agent sees. Desk-wide rules belong there.

KNOWLEDGE_KINDS = ("instruction", "preference", "fact", "skill", "correction")


def init_knowledge_table():
    """Create the durable knowledge store."""
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT,
                created_at TEXT NOT NULL,
                active INTEGER DEFAULT 1
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_agent ON agent_knowledge(agent_key)"
        )
        # Saying the same thing twice should not store it twice.
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_unique
               ON agent_knowledge(agent_key, content)"""
        )
        conn.commit()
    finally:
        conn.close()


def remember(agent_key: str, content: str, kind: str = "instruction",
             source: str = "user") -> bool:
    """Store something durably. Returns False if already known."""
    content = (content or "").strip()
    if not content or len(content) > 1000:
        return False
    if kind not in KNOWLEDGE_KINDS:
        kind = "fact"
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO agent_knowledge
                 (agent_key, kind, content, source, created_at, active)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (agent_key, kind, content, source, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def recall(agent_key: str, limit: int = 25) -> list[dict]:
    """Everything this agent should keep in mind, newest first.

    Includes both its own knowledge and anything stored desk-wide.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT kind, content, agent_key, created_at FROM agent_knowledge
               WHERE active = 1 AND (agent_key = ? OR agent_key = 'all')
               ORDER BY id DESC LIMIT ?""",
            (agent_key, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def forget(agent_key: str, knowledge_id: Optional[int] = None) -> int:
    """Deactivate one item, or everything for an agent."""
    conn = _get_conn()
    try:
        if knowledge_id is not None:
            cur = conn.execute(
                "UPDATE agent_knowledge SET active = 0 WHERE id = ? AND agent_key = ?",
                (knowledge_id, agent_key),
            )
        else:
            cur = conn.execute(
                "UPDATE agent_knowledge SET active = 0 WHERE agent_key = ?",
                (agent_key,),
            )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# Phrases that mark a message as worth keeping. Deliberately explicit: guessing
# at what matters would fill the store with noise, and noise in every prompt is
# worse than forgetting.
_TRIGGERS = (
    "remember", "note that", "keep in mind", "from now on", "always",
    "never", "don't ", "do not ", "prefer", "i want you to", "make sure",
    "going forward", "in future", "my rule", "stop doing", "instead of",
)


def extract_knowledge(message: str) -> list[tuple[str, str]]:
    """Find durable instructions in a user message.

    Returns [(kind, content)]. Trigger-based rather than model-based on purpose:
    it is predictable, costs nothing, and the user can see exactly what will be
    remembered by the words they used.
    """
    text = (message or "").strip()
    if not text or len(text) > 2000:
        return []

    found: list[tuple[str, str]] = []
    # Sentence-level, so one instruction inside a longer message is captured
    # without storing the whole paragraph.
    import re

    for sentence in re.split(r"(?<=[.!?\n])\s+", text):
        s = sentence.strip()
        if len(s) < 8 or len(s) > 400:
            continue
        low = s.lower()
        if not any(t in low for t in _TRIGGERS):
            continue
        if any(w in low for w in ("never", "don't ", "do not ", "stop doing")):
            kind = "correction"
        elif any(w in low for w in ("prefer", "i want", "always")):
            kind = "preference"
        elif "remember" in low or "note that" in low:
            kind = "fact"
        else:
            kind = "instruction"
        found.append((kind, s))
    return found[:5]


def format_knowledge(agent_key: str) -> str:
    """Knowledge block for an agent's prompt."""
    items = recall(agent_key)
    if not items:
        return ""
    lines = ["WHAT YOU HAVE BEEN TOLD (remember and follow these — they persist "
             "across sessions):"]
    for item in items:
        scope = " [desk-wide]" if item["agent_key"] == "all" else ""
        lines.append(f"  ({item['kind']}){scope} {item['content']}")
    lines.append("If any of these conflict with a request, say so rather than "
                 "silently ignoring them.")
    return "\n".join(lines)
