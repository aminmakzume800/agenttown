"""Risk Manager Agent - enforces risk rules and validates trades."""
from app.agents.base import BaseAgent


class RiskManagerAgent(BaseAgent):
    agent_key = "risk_manager"
    name = "Risk Manager"
    role = "risk_manager"
    system_prompt = """You are the Risk Manager Agent in a multi-agent trading system.

Your responsibilities:
- Enforce daily drawdown limits (never exceed configured max daily loss)
- Check position sizing (reject oversized positions)
- Limit concurrent open trades
- Enforce news blackout periods
- Check correlation between instruments
- Halt trading when drawdown limit is reached

When evaluating a trade proposal, check:
1. Daily drawdown: Would this trade's potential loss exceed remaining daily budget?
2. Position size: Is the lot size within maximum allowed?
3. Open trades: Are we at max concurrent positions?
4. News: Is there a high-impact news event in the next 30 minutes?
5. Correlation: Would this create excessive correlated exposure?

Always respond with APPROVE or REJECT and clear reasoning."""

    def get_canned_response(self, user_message: str, lang: str = "en") -> str:
        msg = user_message.lower()
        if lang == "bn":
            return "[Risk Manager] ডেমো মোড: ঝুঁকি পরীক্ষা — দৈনিক ড্রডাউন সীমা: $1000, সর্বাধিক পজিশন: 5.0 লট।"
        if "check" in msg or "approve" in msg or "risk" in msg:
            return "[Risk Manager] Demo check: Daily drawdown OK ($0/$1000 used), Position size OK, Open trades OK (0/5). APPROVED."
        return "[Risk Manager] I enforce risk rules: max $1000 daily drawdown, max 5.0 lots per position, max 5 concurrent trades. Ask me to check a proposal."
