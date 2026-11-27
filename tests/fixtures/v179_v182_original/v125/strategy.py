from v105.strategy import Strategy as Parent
from exposure_validation.policies import Refinement


class Strategy(Refinement, Parent):
    defence = 'recovery'
    allocation = 'relative'
