from v51.strategy import Strategy as Parent


class Strategy(Parent):
    def targets(self, previous, prices, valid_prices, held):
        return 1.10 * super().targets(previous,prices,valid_prices,held)
