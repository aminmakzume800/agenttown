"""Base agent class that all agents inherit from."""
from typing import Optional

from app.llm_client import AGENT_MODEL_MAP, chat_completion, model_for
from app.db import log_event
from app.memory import save_message, get_history
from app.market_data import format_market_context


def execution_capability() -> str:
    """Tell the agent what this system can actually do with its output.

    Without this, models answer from their training prior — "I am a language
    model, I have no broker connection, here is how to place the order
    yourself". That is simply wrong here: every reply is parsed for a trade
    plan, gated by deterministic risk checks and, on approval, sent to a real
    MT5 account. The prompt has to state that plainly, and state the current
    destination, or the agent talks the user out of a working pipeline.
    """
    from app.trading.execution import router

    where = router.describe()
    live = where["uses_real_money"]

    lines = [
        "EXECUTION CAPABILITY — read this before saying what you cannot do:",
        "You are not a standalone chatbot. You are wired into a live trading system.",
        "Anything you write with a full trade plan is automatically parsed and turned",
        "into a real order ticket. You do NOT need a plugin, an EA, a copier, or a",
        "webhook, and the user does NOT need to place the order by hand.",
        "",
        "What happens to a plan you write:",
        "  1. It is parsed into a structured order (symbol, side, entry, SL, TP, size).",
        "  2. Deterministic risk gates run: size, daily drawdown, open-trade count,",
        "     per-symbol exposure, correlation, and live-price freshness.",
        "  3. If it passes, an APPROVE button appears next to your message.",
        f"  4. On approval the order is sent to: {where['destination']}.",
        "",
        f"Current execution mode: {where['mode'].upper()}"
        + (" — REAL orders on a live MT5 account." if live
           else " — simulated fills, no broker contacted."),
    ]

    # State the credential situation as fact. Without this the model fills the
    # gap from its prior and asserts things like "the bot is in demo mode, still
    # waiting for API keys" — which was never true here and misleads the user
    # into thinking setup is incomplete.
    from app.config import settings as _s

    lines += [
        "",
        "SYSTEM STATE — these are facts, do not contradict them:",
        f"  LLM API key configured: {'YES' if _s.NVIDIA_API_KEY else 'NO'}"
        + (" — you are running on a real model right now."
           if _s.NVIDIA_API_KEY else ""),
        f"  Broker credentials configured: {'YES' if _s.METAAPI_TOKEN else 'NO'}",
        f"  Broker order sending enabled: {'YES' if _s.BROKER_TRADING_ENABLED else 'NO'}",
        f"  Rule-based entry engine active: {'YES' if _s.REQUIRE_RULE_SETUP else 'NO'}",
        "  Never tell the user the desk is 'in demo mode waiting for API keys' when",
        "  the lines above say the keys are configured. Never say a bot cannot trade",
        "  yet because of missing setup. If something really is missing, it is named",
        "  above — quote that, nothing else.",
        "  Do not prefix your reply with your own name in brackets.",
    ]

    if where["mode"] == "broker":
        lines.append(
            "The MT5 bridge IS connected. Never tell the user you have no broker "
            "connection or no API — you do, through this system."
        )
    if where.get("warning"):
        lines.append(f"Caveat right now: {where['warning']}")

    # Prices are NOT fetched here. This block is built on every reply, and
    # pulling four quotes each time cost ~40s before the model was even called.
    # The agents that trade already receive live prices through
    # format_market_context(), so this only needs to state the rule.
    lines += [
        "",
        "PRICES: use only the live figures given below in this prompt. Never quote a",
        "level from memory — training-era prices are thousands of pips stale and any",
        "plan built on one is rejected by the risk gate.",
    ]

    # Whether the market is even open. Cheap: a clock check, no network. Without
    # this the agent proposes a trade at the weekend, the gate refuses it on
    # stale prices, and the user is left with a confusing rejection instead of
    # simply being told the market is shut.
    from app.market_data import fx_market_open

    is_open, session = fx_market_open()
    if not is_open:
        lines += [
            "",
            f"MARKET IS CLOSED right now — {session}.",
            "Say so plainly, in one sentence, as the first thing you say. Spot FX",
            "trades from Sunday 21:00 UTC to Friday 22:00 UTC.",
            "Do not output a trade plan: prices are hours stale and any order would",
            "be refused. You may still discuss analysis, levels to watch on the next",
            "open, or answer general questions.",
        ]
    else:
        lines += ["", f"Market session: open ({session})."]

    lines += [
        "",
        "HOW TO ANSWER A REQUEST TO TRADE OR EXECUTE:",
        "- Never refuse on the grounds that you 'cannot access a terminal or broker'.",
        "  That is false in this system and misleads the user.",
        "- Never give step-by-step instructions for placing the order manually in",
        "  MT4/MT5/TradingView. The system places it.",
        "- Instead, output the plan in this exact shape so it can be parsed:",
        "      SYMBOL: <EUR/USD | XAU/USD | GBP/USD | NAS100>",
        "      SIDE: BUY or SELL",
        "      ENTRY: <price>",
        "      SL: <price>",
        "      TP: <price>",
        "      SIZE: <lots>",
        "  then one or two short sentences of reasoning.",
        "- Quote the entry at the current live price unless the user asked for a",
        "  specific level. A level far from the market will be refused by the gate.",
        "- The stop must sit on the losing side of entry and the target on the",
        "  winning side, or the plan is rejected as unparseable.",
        "- If you genuinely do not want to trade, say NO-TRADE and give the reason.",
        "  That is a valid answer. 'I am unable to' is not.",
        "- You are one of eight agents. The human still clicks APPROVE, so you are",
        "  recommending, not unilaterally firing. Say so if it reassures the user.",
    ]
    return "\n".join(lines)


class BaseAgent:
    """Base class for all trading agents."""

    agent_key: str = ""
    name: str = ""
    role: str = ""
    system_prompt: str = ""
    status: str = "idle"
    # Override in subclasses to inject market data for specific symbols
    market_symbols: list[str] = []

    def __init__(self):
        # Kept for introspection only. The model actually used is resolved per
        # call by model_for(), so a retired model or a style change cannot leave
        # an agent pinned to something that no longer exists.
        self._provider, self._model = model_for(self.agent_key)

    def chat(self, user_message: str, lang: str = "en") -> str:
        """Send a message to the agent and get a response."""
        self.status = "thinking"
        try:
            # Build system prompt with market context if relevant
            prompt = self.system_prompt

            # Every agent is told what the system can really do with its reply.
            # Built fresh each call so a mode change is reflected immediately.
            prompt += "\n\n" + execution_capability()

            # Replies are read aloud, so keep them speakable: no scratchpad,
            # no markdown scaffolding, conversational length.
            prompt += (
                "\n\nOUTPUT RULES:\n"
                "- Reply directly. Never show your reasoning process or restate the question.\n"
                "- Do not open with phrases like 'The user asks' or 'We need to answer'.\n"
                "- Keep it under 120 words unless asked for detail.\n"
                "- Write in plain prose that sounds natural read aloud, EXCEPT for the\n"
                "  SYMBOL/SIDE/ENTRY/SL/TP/SIZE lines of a trade plan, which must be kept\n"
                "  exactly in that line format so the system can parse and place the order.\n"
                "- Otherwise avoid tables, headers and markdown decoration."
            )

            if lang == "bn":
                prompt += "\n\nIMPORTANT: Respond in Bengali (বাংলা)."

            # Market context: live price, computed indicators, and any economic
            # release due. Indicators are calculated rather than guessed, so the
            # agent quotes measurements instead of inventing plausible numbers.
            if self.market_symbols:
                from app.market_data import get_candles
                from app.news_calendar import calendar_context
                from app.trading.indicators import format_indicators

                for sym in self.market_symbols:
                    market_ctx = format_market_context(sym)
                    if "[Market data unavailable" in market_ctx:
                        continue
                    prompt += f"\n\n{market_ctx}"

                    candles = get_candles(sym)
                    if candles:
                        block = format_indicators(candles, sym)
                        if block:
                            prompt += f"\n{block}"

                    events = calendar_context(sym)
                    if events:
                        prompt += f"\n{events}"

            # The agent's own results, so a losing pattern is visible to it.
            from app.learning import format_record

            record = format_record(self.agent_key)
            if record:
                prompt += f"\n\n{record}"

            # Anything the user has told this agent previously. Chat history is a
            # rolling window of a few turns, so instructions given earlier would
            # otherwise scroll out of context and be forgotten.
            from app.memory import format_knowledge

            knowledge = format_knowledge(self.agent_key)
            if knowledge:
                prompt += f"\n\n{knowledge}"

            # Get conversation history for context
            history = get_history(self.agent_key, limit=6)

            # Build messages with history
            messages = [{"role": "system", "content": prompt}]
            for msg in history:
                messages.append(msg)
            messages.append({"role": "user", "content": user_message})

            # Resolved per call, not at startup, so switching to scalping takes
            # effect immediately instead of after a restart.
            provider, model = model_for(self.agent_key)

            response = chat_completion(
                provider=provider,
                model=model,
                system_prompt=prompt,
                user_message=user_message,
                history=history,
            )

            if response is None:
                response = self.get_canned_response(user_message, lang)

            # Save to memory
            save_message(self.agent_key, "user", user_message)
            save_message(self.agent_key, "assistant", response)

            # Durable capture: pull any standing instruction out of what the user
            # said and keep it, so it still applies in a week's time.
            from app.memory import extract_knowledge, remember

            for kind, content in extract_knowledge(user_message):
                if remember(self.agent_key, content, kind=kind, source="chat"):
                    log_event(
                        agent_key=self.agent_key,
                        action_type="knowledge_saved",
                        detail=f"({kind}) {content[:120]}",
                    )

            # Log the interaction
            log_event(
                agent_key=self.agent_key,
                action_type="chat",
                detail=f"user: {user_message[:200]}",
                metadata=f"response: {response[:200]}",
            )

            return response
        finally:
            self.status = "idle"

    def get_canned_response(self, user_message: str, lang: str = "en") -> str:
        """Fallback when the model cannot be reached.

        Distinguishes a missing key from a model outage. Saying "set your API
        keys" when the key is present sends the user chasing a problem that does
        not exist — the far more common cause is a retired model, which is a
        different fix entirely.
        """
        from app.config import settings

        if not settings.NVIDIA_API_KEY:
            if lang == "bn":
                return ("API কী সেট করা নেই। .env ফাইলে NVIDIA_API_KEY যোগ করুন।")
            return ("No LLM API key is configured. Add NVIDIA_API_KEY to .env "
                    "and restart.")

        if lang == "bn":
            return ("মডেলে পৌঁছাতে পারছি না — কী ঠিক আছে, তাই সম্ভবত মডেলটি "
                    "অবসরপ্রাপ্ত। /models/health দেখুন।")
        return (
            "I couldn't reach the language model just now. The API key IS "
            "configured, so this is usually a retired model rather than a setup "
            "problem — check /models/health to see which models are still live."
        )

    def to_dict(self) -> dict:
        """Serialize agent info for API responses."""
        return {
            "agent_key": self.agent_key,
            "name": self.name,
            "role": self.role,
            "status": self.status,
        }
