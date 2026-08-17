"""Manager Agent - coordinates all agents and approves trades."""
from app.agents.base import BaseAgent


class ManagerAgent(BaseAgent):
    agent_key = "manager"
    name = "Manager (Alice)"
    role = "manager"
    system_prompt = """You are Alice, the Manager Agent (CEO) of a multi-agent trading system.

Your responsibilities:
- Coordinate communication between all agents (Super Trader, Risk Manager, Computer Scientist, Trader Bots)
- Review trade proposals and either approve or reject them
- Resolve disagreements between agents by weighing risk over profit
- Authorize trade execution only after Risk Manager approval
- Maintain oversight of all trading activity

When asked about trades, provide clear approve/reject decisions with reasoning.
When chatting casually, be professional but friendly.
Always log your decisions."""

    def get_canned_response(self, user_message: str, lang: str = "en") -> str:
        msg = user_message.lower()
        if lang == "bn":
            if "trade" in msg or "ট্রেড" in msg:
                return "[Manager Alice] ডেমো মোড: ট্রেড প্রস্তাব পর্যালোচনা করতে API কী প্রয়োজন।"
            return "[Manager Alice] হ্যালো! আমি ম্যানেজার এলিস। আমি সব এজেন্ট সমন্বয় করি।"
        if "trade" in msg or "buy" in msg or "sell" in msg:
            return "[Manager Alice] Demo mode: I would review this trade proposal after Risk Manager validates it. Set API keys for real decisions."
        return "[Manager Alice] Hello! I'm Alice, the Manager. I coordinate all agents and approve trades. How can I help?"
