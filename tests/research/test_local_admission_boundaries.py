"""Legacy bundles remain readable diagnostics, never source-qualified research."""

from pathlib import Path

import pandas as pd
import pytest
import yaml

from factorlab import cli
from factorlab.config import load_config
from factorlab.data.providers.local import LocalParquetProvider, publish_local_market_data
from factorlab.errors import ProviderError
from tests.research.market_data_fixture import qualification_inputs


def legacy_bundle(tmp_path, source, periods=8):
    inputs, keys, dates, stocks, benchmark = qualification_inputs(tmp_path, periods)
    stock_path, benchmark_path = tmp_path / "stocks.parquet", tmp_path / "benchmark.parquet"
    stocks.to_parquet(stock_path, index=False)
    benchmark.to_parquet(benchmark_path, index=False)
    root = tmp_path / "legacy-bundle"
    manifest = publish_local_market_data(stock_path, benchmark_path, root, source, "synthetic-v1")
    return inputs, dates, stocks, benchmark, root, manifest


@pytest.mark.parametrize("source", ["TongDaXin", "vipdoc", "local-cache", "SYNTHETIC_SOFTWARE_ONLY"])
def test_v1_source_name_cannot_grant_research_admission(tmp_path, source):
    inputs, dates, stocks, _, root, manifest = legacy_bundle(tmp_path, source)
    provider = LocalParquetProvider(root)
    assert manifest["schema_version"] == 1 and provider.qualification_report is None
    # Legacy diagnostic access remains useful without asserting formal evidence.
    pd.testing.assert_frame_equal(provider.get_daily_bars("600000.SH", dates[0], dates[-1]), stocks)
    with pytest.raises(ProviderError, match="v2 qualification"):
        provider.assert_research_admission(inputs.membership, dates, stocks)


def test_cached_alias_v1_is_rejected_by_the_research_cli_loader(tmp_path):
    inputs, dates, stocks, benchmark, root, _ = legacy_bundle(tmp_path, "vipdoc", periods=85)
    repository = Path(__file__).resolve().parents[2]
    document = yaml.safe_load((repository / "configs/research/csi500_local_smoke.yaml").read_text(encoding="utf-8"))
    document["project"]["name"] = "SYNTHETIC_LEGACY_ALIAS_REJECTION"
    document["paths"] = {"data_root": str(tmp_path / "cache"), "results_root": str(tmp_path / "results"),
                         "market_input_root": str(root)}
    document["data"].update(start=str(dates[-20].date()), end=str(dates[-1].date()), warmup_trading_days=65)
    document["universe"].update(expected_member_count=1, min_listed_days=60)
    path = tmp_path / "synthetic.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    config = load_config(path)
    provider = cli._provider(config)
    for symbol, frame, cache, is_index in [("000905.SH", benchmark, cli._benchmark_cache(config), True),
                                          ("600000.SH", stocks, cli._stock_cache(config), False)]:
        cache.update(symbol, frame, dates[0], dates[-1], source=provider.__class__.__name__,
                     source_version=provider.cache_source_version(symbol, is_index))
    with pytest.raises(ProviderError, match="v2 qualification"):
        cli._load_bar_inputs(config, inputs.membership.loc[inputs.membership.date.isin(dates[-20:])])
