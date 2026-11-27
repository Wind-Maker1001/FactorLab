from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from factorlab.trading.contracts import AccountSnapshot, Market
from factorlab.trading.portfolio_execution import PortfolioExecutor, PortfolioStore
from factorlab.trading.portfolio_risk import PortfolioRiskLimits
from factorlab.trading.storage import dumps


ACCOUNT = 'paper-nav'
START = datetime(2026, 11, 24, tzinfo=timezone.utc)


@contextmanager
def risk_executor(path):
    with closing(PortfolioStore(path)) as store:
        store.capital_policy.configure(account_id=ACCOUNT, currency='USD',
                                       budget=D(1000), fee_rate=D(0))
        executor = PortfolioExecutor(None, store, clock=lambda: START + timedelta(days=2))
        executor.configure_portfolio_risk(ACCOUNT, PortfolioRiskLimits(
            max_drawdown=D('.1'), max_daily_loss=D(50), max_rolling_loss=D(50),
            rolling_loss_hours=1))
        yield executor


def risk_values(executor, hours, equity, flow, *, reconciled=True):
    account = AccountSnapshot(
        Market.ALPACA_US_EQUITY, ACCOUNT, (), (), (), START + timedelta(hours=hours),
        reconciled=reconciled, official_equity=D(equity),
        cumulative_external_cash_flow=None if flow is None else D(flow))
    risk = executor.enforce_portfolio_risk(ACCOUNT, account=account)
    return risk.drawdown, risk.daily_loss, risk.rolling_loss


def seed_legacy_history(executor, nav, *, adjusted):
    nav = D(nav)
    hour = int(START.timestamp()) // 3600
    point = dict(at=START, peak=nav, start=nav, day=hour // 24, flow_adjusted=adjusted)
    with executor.store.transaction():
        executor.store.db.execute('INSERT INTO portfolio_risk_nav VALUES (?,?)',
                                  (ACCOUNT, dumps(point)))
        executor.store.db.execute('INSERT INTO portfolio_risk_hour VALUES (?,?,?,?)',
                                  (ACCOUNT, hour, START.isoformat(), str(nav)))


def test_adjusted_nav_preserves_funded_equity_across_restart(tmp_path):
    path = tmp_path / 'risk.sqlite'
    with risk_executor(path) as executor:
        # Initial funding, an addition, a withdrawal, then a trading loss.
        assert risk_values(executor, 0, 1000, 1000) == (D(0), D(0), None)
        assert risk_values(executor, 1, 1200, 1200) == (D(0), D(0), D(0))
        assert risk_values(executor, 2, 900, 900) == (D(0), D(0), D(0))
        assert risk_values(executor, 3, 875, 900) == (D('.025'), D(25), D(25))

    with risk_executor(path) as executor:
        assert risk_values(executor, 3.5, 875, None) == (None, None, None)
        # Profit lifts the original high water mark. A withdrawal leaves it
        # intact, then a trading loss causes drawdown and a rolling loss.
        assert risk_values(executor, 4, 950, 900) == (D(0), D(0), D(0))
        assert risk_values(executor, 5, 850, 800) == (D(0), D(0), D(0))
        assert risk_values(executor, 6, 829, 800) == (D('.02'), D(0), D(21))
        assert risk_values(executor, 24, 829, 800) == (D('.02'), D(0), None)
        assert risk_values(executor, 25, 808, 800) == (D('.04'), D(21), D(21))


def test_legacy_raw_history_runs_until_the_first_healthy_known_flow(tmp_path):
    with risk_executor(tmp_path / 'risk.sqlite') as executor:
        seed_legacy_history(executor, 1000, adjusted=False)
        assert risk_values(executor, 1, 900, None) == (D('.1'), D(100), D(100))
        assert risk_values(executor, 1.5, 950, 1000, reconciled=False) == (None, None, None)
        assert risk_values(executor, 1.75, 900, None) == (D('.1'), D(100), D(100))

        assert risk_values(executor, 2, 900, 1000) == (D(0), D(0), None)
        assert risk_values(executor, 3, 1200, 1300) == (D(0), D(0), D(0))
        assert risk_values(executor, 4, 1191, 1300) == (D('.01'), D(9), D(9))


def test_legacy_adjusted_history_restarts_without_inventing_a_flow_reference(tmp_path):
    path = tmp_path / 'risk.sqlite'
    with risk_executor(path) as executor:
        # The former equity-minus-all-deposits calculation stored only 20.
        seed_legacy_history(executor, 20, adjusted=True)

    with risk_executor(path) as executor:
        assert risk_values(executor, 1, 1020, None) == (None, None, None)
        assert risk_values(executor, 2, 1020, 1000) == (D(0), D(0), None)

    with risk_executor(path) as executor:
        assert risk_values(executor, 3, 918, 1000) == (D('.1'), D(102), D(102))
