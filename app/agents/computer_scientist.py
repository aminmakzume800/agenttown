"""Computer Scientist Agent - code analysis and system improvements."""
from app.agents.base import BaseAgent


class ComputerScientistAgent(BaseAgent):
    agent_key = "computer_scientist"
    name = "Computer Scientist (Bob)"
    role = "computer_scientist"
    system_prompt = """You are Bob, the Computer Scientist Agent in a multi-agent trading system.

Your responsibilities:
- Analyze trading performance (win rate, profit factor, drawdown)
- Review code and suggest improvements
- Help with debugging and testing
- Backtest trading strategies
- Optimize system performance

When analyzing results, provide:
- Win rate percentage
- Average profit/loss per trade
- Maximum drawdown
- Suggestions for improvement

Be technical and precise. Use code examples when helpful."""

    def get_canned_response(self, user_message: str, lang: str = "en") -> str:
        msg = user_message.lower()
        if lang == "bn":
            return "[Bob] ডেমো মোড: কোড বিশ্লেষণ এবং পারফরম্যান্স রিভিউ করতে API কী দরকার।"
        if "code" in msg or "bug" in msg or "error" in msg:
            return "[Bob] I can analyze code, debug issues, and suggest fixes. Share the error message or code snippet and I'll help."
        if "performance" in msg or "result" in msg or "backtest" in msg:
            return "[Bob] Demo: No trading data yet. Once trades are executed, I'll analyze win rate, drawdown, and suggest improvements."
        return "[Bob] Hi! I'm Bob, the Computer Scientist. I analyze code, review trading performance, and suggest optimizations. How can I help?"
