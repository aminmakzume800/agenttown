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

    if where["mode"] == "broker":
        lines.append(
            "The MT5 bridge IS connected. Never tell the user you have no broker "
            "connection or no API — you do, through this system."
        )
    if where.get("warning"):
        lines.append(f"Caveat right now: {where['warning']}")

    # Restating the live prices here, right next to the instruction to use them,
    # is deliberate. Models otherwise reach for a familiar-looking level from
    # training data (EUR/USD near 1.08, gold near 2350) which is thousands of
    # pips stale and gets the whole plan rejected.
    from app.market_data import get_quote

    marks = []
    for sym in ("EUR/USD", "XAU/USD", "GBP/USD", "NAS100"):
        quote = get_quote(sym)
        if quote and quote.get("last"):
            marks.append(
                f"  {sym}: bid {quote['bid']} / ask {quote['ask']} "
                f"({quote['source']}, {quote['age_sec']:.0f}s old)"
            )
    if marks:
        lines += [
            "",
            "LIVE PRICES RIGHT NOW — use these, never a remembered level:",
            *marks,
            "If a price you were about to quote differs from these by more than a",
            "fraction of a percent, you are using stale knowledge. Use the number above.",
        ]

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

            # Inject live market data if agent has market_symbols
            if self.market_symbols:
                for sym in self.market_symbols:
                    market_ctx = format_market_context(sym)
                    if "[Market data unavailable" not in market_ctx:
                        prompt += f"\n\n{market_ctx}"

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
        """Fallback response when no API key or API fails."""
        if lang == "bn":
            return f"[{self.name}] আমি এখন ডেমো মোডে আছি। API কী সেট করুন।"
        return f"[{self.name}] I'm in demo mode. Set API keys in .env for real responses."

    def to_dict(self) -> dict:
        """Serialize agent info for API responses."""
        return {
            "agent_key": self.agent_key,
            "name": self.name,
            "role": self.role,
            "status": self.status,
        }
