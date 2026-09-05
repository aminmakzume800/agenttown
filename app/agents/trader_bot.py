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

When you have a setup, output these lines verbatim so the system can place it:
SYMBOL: {symbol}
SIDE: BUY or SELL
ENTRY: <price>
SL: <price>
TP: <price>
SIZE: <lots>

then one short line of technical reasoning and your confidence (low/medium/high).

Price the entry at the live market unless told otherwise. Focus ONLY on {symbol}.
If there is no clean setup, answer NO-TRADE and say why — never say you are
unable to execute, because this desk executes for you."""
        super().__init__()

    def get_canned_response(self, user_message: str, lang: str = "en") -> str:
        # No invented price levels here — see the note in super_trader.py.
        if lang == "bn":
            return (f"[{self.name}] মডেলে পৌঁছাতে পারছি না, তাই {self.symbol}-এ "
                    f"কোনো লেভেল দিচ্ছি না। API কী পরীক্ষা করুন।")
        return (f"[{self.name}] Can't reach the model, so I won't quote {self.symbol} "
                f"levels I haven't checked. Set the NVIDIA API key and ask again.")
