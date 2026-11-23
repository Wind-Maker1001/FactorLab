"""Small trusted-local examples of the optional train-only fit contract."""


class Trend:
    required_fields = ("adj_close",)

    def __init__(self, window: int = 10, weight: float = 0.8):
        if window < 2 or not 0 < weight <= 1:
            raise ValueError("invalid trend parameters")
        self.window, self.weight = window, weight
        self.warmup_sessions = window

    def generate_targets(self, bars, calendar):
        long = bars.adj_close.iloc[-1] > bars.adj_close.tail(self.window).mean()
        return bars[["date", "symbol"]].tail(1).assign(target_weight=self.weight if long else 0.)


class TrainMean:
    required_fields = ("adj_close",)
    warmup_sessions = 2

    def __init__(self, multiplier: float = 1.0, weight: float = 0.6):
        self.multiplier, self.weight = multiplier, weight

    def fit(self, bars, calendar):
        self.threshold = float(bars.adj_close.mean())*self.multiplier

    def generate_targets(self, bars, calendar):
        long = bars.adj_close.iloc[-1] > self.threshold
        return bars[["date", "symbol"]].tail(1).assign(target_weight=self.weight if long else 0.)


class Cash:
    required_fields = ("adj_close",)
    warmup_sessions = 2

    def __init__(self):
        pass

    def generate_targets(self, bars, calendar):
        return bars[["date", "symbol"]].tail(1).assign(target_weight=0.)
