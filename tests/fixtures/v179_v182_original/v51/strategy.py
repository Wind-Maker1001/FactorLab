from risk_research.parent import BandParent


class Strategy(BandParent):
    """In weak breadth or Nasdaq trends, exit at the mean instead of the lower band."""
    def targets(self, previous, prices, valid_prices, held):
        w = super().targets(previous, prices, valid_prices, held)
        weak = self.breadth.at[previous] < .5 or prices.QQQ <= self.qqq_ma63.at[previous]
        if weak:
            w = w.mask(held.gt(0) & prices.le(self.ma28.loc[previous]), 0.0)
        return w
