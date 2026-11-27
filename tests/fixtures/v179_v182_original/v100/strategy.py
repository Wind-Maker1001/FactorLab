from research_v86_v100.policies import Policy


class Strategy(Policy):
    rules = ('giveback', 'ridge_confirm', 'dual_strength')
