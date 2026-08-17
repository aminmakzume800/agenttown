"""Agent classes package - exports all agent instances."""
from app.agents.manager import ManagerAgent
from app.agents.super_trader import SuperTraderAgent
from app.agents.risk_manager import RiskManagerAgent
from app.agents.computer_scientist import ComputerScientistAgent
from app.agents.trader_bot import TraderBot

# Create all agent instances
manager = ManagerAgent()
super_trader = SuperTraderAgent()
risk_manager = RiskManagerAgent()
computer_scientist = ComputerScientistAgent()
trader_bot_1 = TraderBot("trader_bot_1", "EUR/USD")
trader_bot_2 = TraderBot("trader_bot_2", "XAU/USD")
trader_bot_3 = TraderBot("trader_bot_3", "GBP/USD")
trader_bot_4 = TraderBot("trader_bot_4", "NAS100")

# Dictionary of all agents for easy lookup
ALL_AGENTS: dict = {
    "manager": manager,
    "super_trader": super_trader,
    "risk_manager": risk_manager,
    "computer_scientist": computer_scientist,
    "trader_bot_1": trader_bot_1,
    "trader_bot_2": trader_bot_2,
    "trader_bot_3": trader_bot_3,
    "trader_bot_4": trader_bot_4,
}
