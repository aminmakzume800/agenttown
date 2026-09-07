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
# Re-measured 2026-09-05 on this key, reasoning traces DISABLED (THINKING_OFF).
# Models retire fast on this tier, so these are the ones verified alive today:
#   nemotron-3.5-lightning-30b    ~0.7 s   fastest, clean structured output
#   nemotron-3-super-120b-a12b    ~2.5 s   best reasoning per second
#   nemotron-3-ultra-550b-a55b    ~3.5 s   deepest analysis
#
# With thinking left ON these took 5-12 s and spent most of the budget narrating
# their own scratchpad, so the flag is not optional.
#
# RETIRED — do not reintroduce without re-probing. Each returns HTTP 410:
#   nvidia/nemotron-3-nano-30b-a3b        end of life 2026-09-01
#   meta/llama-3.1-8b-instruct            end of life 2026-08-26
#   meta/llama-3.3-70b-instruct, meta/llama-4-maverick-17b-128e-instruct,
#   qwen/qwen3-next-80b-a3b-instruct, microsoft/phi-4-multimodal-instruct
#
# Also unavailable on this tier (404): writer/palmyra-fin-70b-32k (finance
# specialist, would be ideal), deepseek-*, mistralai/*, qwen3-32b, gemma-3-*,
# nemotron-3.5-nano, nemotron-nano-9b-v2, llama-4-scout.
# openai/gpt-oss-20b answers but returns empty content, so it is not usable.
SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/nemotron-3-ultra-550b-a55b"
LIGHTNING = "nvidia/nemotron-3.5-lightning-30b-a3b"

# Lightning is now both the fastest and the most reliable at structured output,
# so it fills the slot the retired nano model used to hold.
FAST = LIGHTNING

# Nemotron narrates its reasoning by default. This switch turns that off at the
# chat template, which is far more reliable than stripping it afterwards.
# Models that do not understand the flag ignore it; if one rejects the request
# outright, chat_completion retries without it.
THINKING_OFF = {"chat_template_kwargs": {"thinking": False}}

# Tried in order when the primary model fails. Both are quick and never emit a
# scratchpad, so an outage degrades latency and depth rather than the feature.
# Lightning first: it is both the fastest and, in practice, the most consistently
# available. Super and Ultra are stronger but do go through 503 and timeout
# periods, so they belong behind it rather than in front.
FALLBACK_CHAIN = [LIGHTNING, SUPER, ULTRA]

# Scalping overrides. At a one-minute horizon the price moves while a big model
# is still thinking, so a slower-but-deeper answer is worth less than a fast one:
# by the time Super has replied (~4.7 s) the entry it quoted is often already
# off-market and gets refused. The deterministic risk gates do the real
# protecting and cost nothing, so speed is the right trade here.
# Day and swing keep the stronger models, where seconds do not matter.
SCALP_MODEL_MAP: dict[str, str] = {
    "manager": FAST,          # 0.7 s instead of 2.5 s
    "risk_manager": FAST,
    "super_trader": FAST,     # 0.7 s instead of 3.5 s
    "computer_scientist": FAST,
    "trader_bot_1": FAST,
    "trader_bot_2": FAST,
    "trader_bot_3": FAST,
    "trader_bot_4": FAST,
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
    "trader_bot_1": ("nvidia", FAST),
    "trader_bot_2": ("nvidia", FAST),
    "trader_bot_3": ("nvidia", FAST),
    "trader_bot_4": ("nvidia", FAST),
}


def model_for(agent_key: str, style: str | None = None) -> tuple[str, str]:
    """Which model an agent should use, given the active trading style.

    One place decides this so the chat path and the autopilot never disagree
    about who is answering.
    """
    provider, model = AGENT_MODEL_MAP.get(agent_key, ("nvidia", FAST))
    active = (style or settings.style_name()).lower()
    if active == "scalp":
        model = SCALP_MODEL_MAP.get(agent_key, FAST)
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
    # 30s was long enough to make a single hung model dominate the whole reply.
    # A working model answers in under 5s, so 12s is generous while still failing
    # over to the next candidate quickly.
    _client = OpenAI(
        api_key=settings.NVIDIA_API_KEY,
        base_url=settings.NVIDIA_BASE_URL,
        timeout=float(settings.LLM_TIMEOUT_SEC),
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
            text = str(exc)
            # Retrying the same model is pointless when the fault is the model
            # itself rather than our request. Failing over immediately is the
            # difference between a 2-second answer and a 60-second wait, since
            # each timeout is otherwise paid twice before moving on.
            terminal = ("410" in text or "404" in text or "503" in text
                        or "timed out" in text.lower()
                        or "Gone" in text or "temporarily" in text.lower())
            if terminal or "extra_body" not in kwargs:
                return None

    return None


def probe_models() -> dict:
    """Check every model this app depends on is still served.

    Models on this tier retire on a schedule, and when one goes the agent using
    it simply stops answering — which looks like a broken agent rather than a
    retired model. This turns that into a straight answer.
    """
    client = get_client()
    if client is None:
        return {"ok": False, "error": "No NVIDIA_API_KEY configured.", "models": {}}

    wanted = sorted({m for _, m in AGENT_MODEL_MAP.values()}
                    | set(SCALP_MODEL_MAP.values()) | set(FALLBACK_CHAIN))
    report: dict[str, dict] = {}
    for model in wanted:
        try:
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=5,
                temperature=0.0,
            )
            report[model] = {"alive": True, "detail": "responding"}
        except Exception as exc:
            text = str(exc)
            if "410" in text:
                detail = "RETIRED — end of life, must be replaced"
            elif "404" in text:
                detail = "not available on this tier"
            else:
                detail = text[:160]
            report[model] = {"alive": False, "detail": detail}

    dead = [m for m, r in report.items() if not r["alive"]]
    return {
        "ok": not dead,
        "models": report,
        "dead": dead,
        "used_by": {
            agent: model_for(agent)[1] for agent in AGENT_MODEL_MAP
        },
    }


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
        model: Model identifier (e.g. "nvidia/nemotron-3.5-lightning-30b-a3b")
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
