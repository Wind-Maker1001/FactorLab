from v161.strategy import Strategy as Parent
from reserve_research.policies import Reserve


class Strategy(Reserve, Parent):
    reserve_rule = 'fixed'
