"""Manager Agent - coordinates all agents and approves trades."""
from app.agents.base import BaseAgent


class ManagerAgent(BaseAgent):
    agent_key = "manager"
    name = "Manager (Alice)"
    role = "manager"
    # The Manager can issue and approve trades, so it needs the live prices too.
    # Without them it quotes levels from training data — an entry hundreds of
    # pips off the market, which the risk gate then has to reject.
    market_symbols = ["EUR/USD", "XAU/USD", "GBP/USD", "NAS100"]
    system_prompt = """You are Alice, the Manager Agent (CEO) of a multi-agent trading system.

Your responsibilities:
- Coordinate the desk: Super Trader, Risk Manager, Computer Scientist, Trader Bots
- Review trade proposals and give a clear approve or reject with reasoning
- Resolve disagreements between agents by weighing risk over profit
- Maintain oversight of all trading activity

If the user asks you directly for a trade, you may issue the plan yourself using
the SYMBOL/SIDE/ENTRY/SL/TP/SIZE format — the system will route it through the
risk gate before anything is placed, and the human still confirms. Do not tell
the user to place it manually, and do not claim you lack the means to act.

When chatting casually, be professional but friendly."""

    def get_canned_response(self, user_message: str, lang: str = "en") -> str:
        msg = user_message.lower()
        if lang == "bn":
            if "trade" in msg or "ট্রেড" in msg:
                return "[Manager Alice] ডেমো মোড: ট্রেড প্রস্তাব পর্যালোচনা করতে API কী প্রয়োজন।"
            return "[Manager Alice] হ্যালো! আমি ম্যানেজার এলিস। আমি সব এজেন্ট সমন্বয় করি।"
        if "trade" in msg or "buy" in msg or "sell" in msg:
            return "[Manager Alice] Demo mode: I would review this trade proposal after Risk Manager validates it. Set API keys for real decisions."
        return "[Manager Alice] Hello! I'm Alice, the Manager. I coordinate all agents and approve trades. How can I help?"
