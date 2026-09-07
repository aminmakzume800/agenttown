"""Super Trader Agent - analyzes markets and proposes trades."""
from app.agents.base import BaseAgent


class SuperTraderAgent(BaseAgent):
    agent_key = "super_trader"
    name = "Super Trader"
    role = "super_trader"
    market_symbols = ["EUR/USD", "XAU/USD", "GBP/USD", "NAS100"]
    system_prompt = """You are the Super Trader Agent in a multi-agent trading system.

You are the desk's senior discretionary trader. When someone asks you to take a
trade, execute, or "do it", you answer with an executable plan — the system turns
that plan into a real order. Asking you to trade is a normal, in-scope request.

Your responsibilities:
- Analyze the timeframe that matches the desk's current trading style
- Study market structure, momentum and liquidity zones
- Produce executable trade plans with entry, stop loss, take profit and size
- Evaluate trade ideas from Trader Bots before promoting them

Every trade plan must contain these lines verbatim:
SYMBOL: EUR/USD, XAU/USD, GBP/USD or NAS100
SIDE: BUY or SELL
ENTRY: <price>
SL: <price>
TP: <price>
SIZE: <lots>

then a sentence or two of reasoning. Price at the live market unless the user
names a level. Never omit the stop. If conditions are poor, answer NO-TRADE with
the reason — that is a real answer, unlike claiming you are unable to act."""

    def get_canned_response(self, user_message: str, lang: str = "en") -> str:
        # Deliberately quotes no price levels. This runs when the model is
        # unreachable, and any levels here would be invented — the parser would
        # turn them into a real order ticket off a made-up price.
        if lang == "bn":
            return ("মডেলে পৌঁছাতে পারছি না, তাই এখন কোনো ট্রেড পরিকল্পনা "
                    "দিচ্ছি না। /models/health দেখুন।")
        return ("I can't reach the analysis model right now, so I won't quote "
                "levels I haven't verified — an invented entry would become a "
                "real order ticket. Check /models/health for live models.")
