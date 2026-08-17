"""LLM client — single NVIDIA API key for all models (DeepSeek, Nemotron, CodeLlama)."""
import logging
from typing import Optional

from openai import OpenAI

from app.config import settings

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Agent -> model mapping (all hosted on NVIDIA)
# ──────────────────────────────────────────────
# Model assignment, verified working on the free NVIDIA tier.
#
# Measured latency on this key:
#   nemotron-3-nano-30b-a3b       ~1.0 s   fastest
#   nemotron-3-super-120b-a12b    ~1.7 s   best reasoning per second
#   nemotron-3.5-lightning-30b    ~1.8 s
#   nemotron-3-ultra-550b-a55b    ~5.0 s   deepest, worth it for analysis
#
# Checked and NOT usable on this tier (404 or multi-minute timeouts):
#   writer/palmyra-fin-70b-32k, moonshotai/kimi-k2.6,
#   mistralai/codestral-22b, deepseek-v4-flash-0731,
#   openai/gpt-oss-120b, z-ai/glm-5.2
SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/nemotron-3-ultra-550b-a55b"
NANO = "nvidia/nemotron-3-nano-30b-a3b"
LIGHTNING = "nvidia/nemotron-3.5-lightning-30b-a3b"

AGENT_MODEL_MAP: dict[str, tuple[str, str]] = {
    # Decisions that gate money get the strongest reasoning model.
    "manager": ("nvidia", SUPER),
    "risk_manager": ("nvidia", SUPER),
    # Deepest market analysis — the extra seconds are acceptable here.
    "super_trader": ("nvidia", ULTRA),
    # Code and log analysis: fast, and Lightning handles structure well.
    "computer_scientist": ("nvidia", LIGHTNING),
    # Bots fire often and only need a quick read, so favour latency.
    "trader_bot_1": ("nvidia", NANO),
    "trader_bot_2": ("nvidia", NANO),
    "trader_bot_3": ("nvidia", NANO),
    "trader_bot_4": ("nvidia", NANO),
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
import re

# The Nemotron 3 family sometimes emits its scratchpad before the answer
# ("We need to answer in one sentence…", "Here's a thinking process:").
# That is internal deliberation, not a reply, so it is cut before display.
_TRACE_BLOCKS = re.compile(
    r"<(think|thinking|reasoning)>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TRACE_OPENERS = (
    "here's a thinking process", "here is a thinking process",
    "we need to answer", "we need to respond", "we should answer",
    "the user asks", "the user is asking", "the user wants",
    "let me think through", "let's think through",
    "okay, let's break", "first, i need to",
)


def strip_reasoning(text: Optional[str]) -> Optional[str]:
    """Remove leaked chain-of-thought so only the reply reaches the user."""
    if not text:
        return text

    out = _TRACE_BLOCKS.sub("", text).strip()

    # If it opens with a scratchpad line, drop everything up to the first
    # real paragraph break — but only when a substantive body remains.
    lowered = out.lower()
    if any(lowered.startswith(op) for op in _TRACE_OPENERS):
        parts = re.split(r"\n\s*\n", out, maxsplit=1)
        if len(parts) == 2 and len(parts[1].strip()) > 40:
            out = parts[1].strip()

    # Some models mark the hand-off explicitly.
    for marker in ("**Answer:**", "Answer:", "**Response:**", "Final answer:"):
        idx = out.rfind(marker)
        if idx != -1 and len(out) - idx > 30:
            out = out[idx + len(marker):].strip()
            break

    return out or text


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
            return strip_reasoning(response.choices[0].message.content)
        except Exception as exc:
            logger.warning(
                "LLM call failed (model=%s, attempt=%d/2): %s",
                model,
                attempt + 1,
                exc,
            )

    logger.error("Both attempts failed for model=%s", model)
    return None
