"""LLM client — single NVIDIA API key for all models (DeepSeek, Nemotron, CodeLlama)."""
import logging
from typing import Optional

from openai import OpenAI

from app.config import settings

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Agent -> model mapping (all hosted on NVIDIA)
# ──────────────────────────────────────────────
AGENT_MODEL_MAP: dict[str, tuple[str, str]] = {
    "manager": ("nvidia", "nvidia/nemotron-3-nano-30b-a3b"),
    "risk_manager": ("nvidia", "nvidia/nemotron-3-nano-30b-a3b"),
    "computer_scientist": ("nvidia", "nvidia/nemotron-3-nano-30b-a3b"),
    "super_trader": ("nvidia", "nvidia/nemotron-3-nano-30b-a3b"),
    "trader_bot_1": ("nvidia", "nvidia/nemotron-3-nano-30b-a3b"),
    "trader_bot_2": ("nvidia", "nvidia/nemotron-3-nano-30b-a3b"),
    "trader_bot_3": ("nvidia", "nvidia/nemotron-3-nano-30b-a3b"),
    "trader_bot_4": ("nvidia", "nvidia/nemotron-3-nano-30b-a3b"),
}


# ──────────────────────────────────────────────
# Single client (one key for everything)
# ──────────────────────────────────────────────
_client: Optional[OpenAI] = None


def get_client() -> Optional[OpenAI]:
    """Return the NVIDIA OpenAI-compatible client, or None if key is missing."""
    global _client
    if _client is not None:
        return _client
    if not settings.NVIDIA_API_KEY:
        return None
    _client = OpenAI(
        api_key=settings.NVIDIA_API_KEY,
        base_url=settings.NVIDIA_BASE_URL,
        timeout=30.0,
    )
    return _client


# ──────────────────────────────────────────────
# Chat completion
# ──────────────────────────────────────────────
def chat_completion(
    provider: str,
    model: str,
    system_prompt: str,
    user_message: str,
    history: list[dict] | None = None,
) -> Optional[str]:
    """Call the LLM and return the assistant message content.

    Args:
        provider: "nvidia" (kept for interface compatibility)
        model: Model identifier (e.g. "nvidia/nemotron-3-nano-30b-a3b")
        system_prompt: System-level instruction
        user_message: User-level prompt
        history: Optional conversation history [{"role":"user","content":"..."}, ...]

    Returns:
        The assistant's reply as a string, or None if both attempts fail.
    """
    client = get_client()
    if client is None:
        logger.warning("No NVIDIA_API_KEY configured. Skipping LLM call.")
        return None

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    for attempt in range(2):  # retry once on failure
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
            )
            return response.choices[0].message.content
        except Exception as exc:
            logger.warning(
                "LLM call failed (model=%s, attempt=%d/2): %s",
                model,
                attempt + 1,
                exc,
            )

    logger.error("Both attempts failed for model=%s", model)
    return None
