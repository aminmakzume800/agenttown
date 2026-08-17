"""Trader Bot - market-specific trade idea generators."""
from app.agents.base import BaseAgent


class TraderBot(BaseAgent):
    """A trader bot specialized for a specific instrument."""

    def __init__(self, agent_key: str, symbol: str):
        self.agent_key = agent_key
        self.symbol = symbol
        self.name = f"Trader Bot ({symbol})"
        self.role = "trader_bot"
        self.market_symbols = [symbol]
        self.system_prompt = f"""You are a specialized Trader Bot for {symbol} in a multi-agent trading system.

Your responsibilities:
- Monitor {symbol} price action and generate trade ideas
- Analyze technical indicators (moving averages, RSI, MACD, support/resistance)
- Identify entry opportunities with clear stop loss and take profit levels
- Report your trade ideas to the Super Trader for evaluation

When generating a trade idea, include:
- Direction: BUY or SELL
- Entry price
- Stop loss level
- Take profit level
- Confidence level (low/medium/high)
- Brief technical reasoning

Focus ONLY on {symbol}. Be specific with price levels."""
        super().__init__()

    def get_canned_response(self, user_message: str, lang: str = "en") -> str:
        if lang == "bn":
            return f"[{self.name}] ডেমো: {self.symbol}-এ একটি ট্রেড আইডিয়া তৈরি করতে API কী দরকার।"
        return f"[{self.name}] Demo mode: I monitor {self.symbol} for trade opportunities. Set API keys for real-time analysis."
