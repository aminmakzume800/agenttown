"""Super Trader Agent - analyzes markets and proposes trades."""
from app.agents.base import BaseAgent


class SuperTraderAgent(BaseAgent):
    agent_key = "super_trader"
    name = "Super Trader"
    role = "super_trader"
    market_symbols = ["EUR/USD", "XAU/USD", "GBP/USD", "NAS100"]
    system_prompt = """You are the Super Trader Agent in a multi-agent trading system.

Your responsibilities:
- Analyze multiple timeframes (1m, 5m, 15m, 1h, 4h, daily)
- Study macro news, market structure, and liquidity zones
- Propose trades with clear entry, stop loss, take profit, and position size
- Evaluate trade ideas from Trader Bots before promoting them

When proposing a trade, always include:
- Symbol (e.g., EURUSD, XAUUSD)
- Direction (BUY/SELL)
- Entry price
- Stop loss
- Take profit
- Position size (lots)
- Reasoning (technical + fundamental)

Be specific with numbers. Never recommend trades without stop losses."""

    def get_canned_response(self, user_message: str, lang: str = "en") -> str:
        msg = user_message.lower()
        if lang == "bn":
            return "[Super Trader] ডেমো ট্রেড প্রস্তাব: EURUSD BUY @ 1.0850, SL: 1.0820, TP: 1.0910, সাইজ: 0.5 লট।"
        if "eurusd" in msg or "eur" in msg:
            return "[Super Trader] Demo proposal: BUY EURUSD @ 1.0850, SL: 1.0820 (30 pips), TP: 1.0910 (60 pips), Size: 0.5 lots. R:R = 1:2."
        if "gold" in msg or "xau" in msg:
            return "[Super Trader] Demo proposal: SELL XAUUSD @ 2350.00, SL: 2365.00, TP: 2320.00, Size: 0.1 lots. R:R = 1:2."
        return "[Super Trader] I analyze markets across multiple timeframes. Ask me about a specific pair (EURUSD, XAUUSD, GBPUSD, NAS100) for a trade proposal."
