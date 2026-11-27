from v150.strategy import Strategy as Parent
from entry_research.risk import StockRisk


class Strategy(StockRisk, Parent):
    risk_rule='tail_budget'
