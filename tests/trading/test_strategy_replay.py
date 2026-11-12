"""SYNTHETIC fixtures exercise wiring, not historical or predictive evidence."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from factorlab.trading import strategy_replay as replay
from factorlab.trading.strategy import ClosedBar
from factorlab.trading.strategy_config import select_strategy


@pytest.fixture
def synthetic_history():
    symbol = dict(symbol='NVDAUSDT', status='TRADING', quoteAsset='USDT', marginAsset='USDT',
                  orderTypes=['MARKET', 'LIMIT'],
                  filters=[dict(filterType='LOT_SIZE', minQty='.01', maxQty='3000', stepSize='.01'),
                           dict(filterType='MARKET_LOT_SIZE', minQty='.01', maxQty='3000', stepSize='.01'),
                           dict(filterType='MIN_NOTIONAL', notional='5'),
                           dict(filterType='PRICE_FILTER', minPrice='.01', maxPrice='20000', tickSize='.01'),
                           dict(filterType='PERCENT_PRICE', multiplierDown='.97', multiplierUp='1.03')])
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i in range(100):
        price = Decimal('200') + Decimal(i) * Decimal('.2')
        bars.append(ClosedBar('NVDAUSDT', start + timedelta(hours=i),
                              start + timedelta(hours=i + 1), price, price + 1, price - 1,
                              price + Decimal('.1'), Decimal('100'), 3600, replay.SOURCE))
    reader = SimpleNamespace(manifest=dict(interval='1h', data_version='a'*64,
        tables=dict(bars=dict(sha256='b'*64)), contract=dict(symbol='NVDAUSDT',
        instrument_id='BINANCE:USDM:NVDAUSDT', exchange_info=symbol)))
    return reader, bars, symbol


def test_synthetic_fixed_ridge_fit_freeze_and_risk_replay(tmp_path, synthetic_history):
    reader, bars, symbol = synthetic_history
    strategy, evidence, start = replay.fit_frozen_ridge(reader, bars, tmp_path, 'c'*40)
    loaded = select_strategy('frozen-model', config_path=tmp_path / 'deployment.json')
    assert loaded.describe() == strategy.describe()
    assert evidence['quality']['train']['rows'] > evidence['quality']['test']['rows'] > 0
    train_exit = pd.Timestamp(evidence['contract']['train_last_exit'])
    assert pd.Timestamp(evidence['contract']['validation_start']).date() > train_exit.date()
    assert pd.Timestamp(evidence['contract']['test_start']).date() > train_exit.date()
    result = replay.replay_history(tmp_path / 'paper.sqlite', loaded, bars, symbol, start_index=start)
    assert result['orders'] > 0, (result['signals'], result['last_rejection'], evidence['quality'])
    assert result['fills'] > 0
    assert Decimal(result['total_fees']) > 0
    assert result['order_records'] and result['fill_confirmations']
    assert result['account']['positions']
    assert result['liquidity'] == 'HYPOTHETICAL_PAPER_LIQUIDITY_NOT_OBSERVED_TRADES'


def test_synthetic_72_hours_rejects_empty_day_aligned_test(tmp_path, synthetic_history):
    reader, bars, _ = synthetic_history
    with pytest.raises(ValueError, match='day-aligned chronological'):
        replay.fit_frozen_ridge(reader, bars[:72], tmp_path, 'c'*40)


@pytest.mark.parametrize('mutation', ['symbol', 'instrument', 'source', 'close_time', 'interval'])
def test_synthetic_loader_rejects_identity_and_boundary(monkeypatch, synthetic_history, mutation):
    reader, bars, _ = synthetic_history
    frame = pd.DataFrame([dict(open_time=b.opened_at, close_time=b.closed_at-timedelta(milliseconds=1),
        open=str(b.open), high=str(b.high), low=str(b.low), close=str(b.close), volume=str(b.volume))
        for b in bars])
    source = replay.SOURCE
    if mutation == 'symbol':
        reader.manifest['contract']['symbol'] = 'BTCUSDT'
    elif mutation == 'instrument':
        reader.manifest['contract']['instrument_id'] = 'OTHER:NVDAUSDT'
    elif mutation == 'source':
        source = 'SYNTHETIC_OTHER_SOURCE'
    elif mutation == 'interval':
        reader.manifest['interval'] = '5m'
    else:
        frame.loc[0, 'close_time'] += timedelta(milliseconds=1)
    reader.read = lambda _: SimpleNamespace(data=frame, metadata=SimpleNamespace(source_id=source))
    monkeypatch.setattr(replay, 'Reader', lambda _: reader)
    with pytest.raises(ValueError):
        replay.load_snapshot_bars('SYNTHETIC_FIXTURE')
