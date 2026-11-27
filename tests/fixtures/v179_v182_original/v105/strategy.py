from exposure_research.policies import ExposurePolicy


class Strategy(ExposurePolicy):
    scale = 4
    core = 'none'
    overlay = 'none'
