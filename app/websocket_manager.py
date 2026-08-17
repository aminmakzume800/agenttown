"""WebSocket connection manager for real-time updates."""
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages WebSocket connections and broadcasts events."""

    def __init__(self):
        self.active_connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info("WebSocket connected. Total: %d", len(self.active_connections))

    def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection."""
        self.active_connections.discard(websocket)
        logger.info("WebSocket disconnected. Total: %d", len(self.active_connections))

    async def broadcast(self, event: dict[str, Any]):
        """Send an event to all connected clients."""
        if not self.active_connections:
            return

        message = json.dumps(event)
        dead_connections = set()

        for ws in self.active_connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead_connections.add(ws)

        # Clean up dead connections
        self.active_connections -= dead_connections

    async def broadcast_agent_status(self, agent_key: str, status: str):
        """Broadcast an agent status change."""
        await self.broadcast({
            "type": "agent_status",
            "agent_key": agent_key,
            "status": status,
        })

    async def broadcast_trade_event(self, event_type: str, data: dict):
        """Broadcast a trade-related event."""
        await self.broadcast({
            "type": event_type,
            "data": data,
        })


# Singleton instance
ws_manager = WebSocketManager()
