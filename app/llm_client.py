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
# Measured on this key, with reasoning traces DISABLED (see THINKING_OFF):
#   nemotron-3-nano-30b-a3b       ~1.1 s   fastest, clean structured output
#   nemotron-3.5-lightning-30b    ~1.6 s
#   nemotron-3-super-120b-a12b    ~4.7 s   best reasoning per second
#   nemotron-3-ultra-550b-a55b    ~7 s     deepest, worth it for analysis
#   meta/llama-3.1-8b-instruct    ~0.7 s   needs no thinking flag at all
#   meta/llama-3.1-70b-instruct   ~5.5 s   solid, always well-formed
#
# The same models with thinking left ON took 5-12 s and spent most of the
# budget narrating their own scratchpad, so the flag is not optional.
#
# Checked and NOT usable on this tier (404 / 410 end-of-life / timeout):
#   writer/palmyra-fin-70b-32k (finance specialist, would be ideal),
#   deepseek-ai/deepseek-r1, deepseek-ai/deepseek-v3.1,
#   mistralai/mistral-small-24b-instruct, qwen/qwen2.5-7b-instruct,
#   qwen/qwen3-next-80b-a3b-instruct, microsoft/phi-4-mini-instruct,
#   google/gemma-3-27b-it, openai/gpt-oss-120b, meta/llama-3.3-70b-instruct
SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/nemotron-3-ultra-550b-a55b"
NANO = "nvidia/nemotron-3-nano-30b-a3b"
LIGHTNING = "nvidia/nemotron-3.5-lightning-30b-a3b"
LLAMA_8B = "meta/llama-3.1-8b-instruct"
LLAMA_70B = "meta/llama-3.1-70b-instruct"

# Nemotron narrates its reasoning by default. This switch turns that off at the
# chat template, which is far more reliable than stripping it afterwards.
# Models that do not understand the flag ignore it; if one rejects the request
# outright, chat_completion retries without it.
THINKING_OFF = {"chat_template_kwargs": {"thinking": False}}

# Tried in order when the primary model fails. Both are quick and never emit a
# scratchpad, so an outage degrades latency and depth rather than the feature.
FALLBACK_CHAIN = [LLAMA_8B, NANO]

# Scalping overrides. At a one-minute horizon the price moves while a big model
# is still thinking, so a slower-but-deeper answer is worth less than a fast one:
# by the time Super has replied (~4.7 s) the entry it quoted is often already
# off-market and gets refused. The deterministic risk gates do the real
# protecting and cost nothing, so speed is the right trade here.
# Day and swing keep the stronger models, where seconds do not matter.
SCALP_MODEL_MAP: dict[str, str] = {
    "manager": LLAMA_8B,          # 0.7 s instead of 4.7 s
    "risk_manager": LLAMA_8B,
    "super_trader": NANO,         # 1.1 s instead of 7.0 s
    "computer_scientist": NANO,
    "trader_bot_1": LLAMA_8B,
    "trader_bot_2": LLAMA_8B,
    "trader_bot_3": LLAMA_8B,
    "trader_bot_4": LLAMA_8B,
}

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


def model_for(agent_key: str, style: str | None = None) -> tuple[str, str]:
    """Which model an agent should use, given the active trading style.

    One place decides this so the chat path and the autopilot never disagree
    about who is answering.
    """
    provider, model = AGENT_MODEL_MAP.get(agent_key, ("nvidia", NANO))
    active = (style or settings.style_name()).lower()
    if active == "scalp":
        model = SCALP_MODEL_MAP.get(agent_key, LLAMA_8B)
    return provider, model


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


def _one_call(
    client: OpenAI,
    model: str,
    messages: list[dict],
    max_tokens: Optional[int],
    temperature: float,
    thinking: bool,
) -> Optional[str]:
    """Single completion attempt. Retries once without the thinking flag."""
    kwargs: dict = {"model": model, "messages": messages, "temperature": temperature}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens

    for extra in ({} if thinking else THINKING_OFF, None):
        if extra is None:
            # Second pass: drop the flag entirely in case the model rejected it.
            kwargs.pop("extra_body", None)
        elif extra:
            kwargs["extra_body"] = extra

        try:
            response = client.chat.completions.create(**kwargs)
            return strip_reasoning(response.choices[0].message.content)
        except Exception as exc:
            logger.warning("LLM call failed (model=%s): %s", model, exc)
            if "extra_body" not in kwargs:
                return None  # already the plain attempt, nothing left to vary

    return None


def chat_completion(
    provider: str,
    model: str,
    system_prompt: str,
    user_message: str,
    history: list[dict] | None = None,
    max_tokens: Optional[int] = None,
    temperature: float = 0.7,
    thinking: bool = False,
    fallback: bool = True,
) -> Optional[str]:
    """Call the LLM and return the assistant message content.

    Args:
        provider: "nvidia" (kept for interface compatibility)
        model: Model identifier (e.g. "nvidia/nemotron-3-nano-30b-a3b")
        system_prompt: System-level instruction
        user_message: User-level prompt
        history: Optional conversation history [{"role":"user","content":"..."}]
        max_tokens: Optional output cap
        temperature: Sampling temperature; keep low for trade plans
        thinking: Leave the model's reasoning trace on. Off by default because
            it is 3-10x slower and leaks the scratchpad into the reply.
        fallback: Try FALLBACK_CHAIN if the requested model fails

    Returns:
        The assistant's reply as a string, or None if every attempt fails.
    """
    client = get_client()
    if client is None:
        logger.warning("No NVIDIA_API_KEY configured. Skipping LLM call.")
        return None

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    tried = [model] + ([m for m in FALLBACK_CHAIN if m != model] if fallback else [])
    for candidate in tried:
        result = _one_call(client, candidate, messages, max_tokens, temperature, thinking)
        if result:
            if candidate != model:
                logger.warning("Model %s unavailable, served by %s", model, candidate)
            return result

    logger.error("All models failed (requested=%s)", model)
    return None
