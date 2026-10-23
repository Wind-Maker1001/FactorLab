"""Offline download-integrity regressions, not evidence about real markets.

Orchestration fixtures use fabricated bars and membership.  The public local
pipeline below uses real files and production cache, receipt and admission
code, but its two synthetic securities are not a CSI500 history.
"""

import json

import pandas as pd
import pytest
import requests

from factorlab import cli
from factorlab.config import load_config
from factorlab.data import ParquetBarCache
from factorlab.data.download_status import list_unresolved, record_fatal
from factorlab.data.providers import AKShareProvider, LocalParquetProvider
from factorlab.errors import (
    CacheError,
    DataQualityError,
    FactorLabError,
    ProviderError,
    ProviderNetworkError,
    SchemaError,
)
from factorlab.reproducibility import RunManifest
from tests.research.test_cli_reproducibility import (
    _benchmark_bars_with_warmup,
    _complete_security_data,
    _config,
    _membership,
)
from tests.research.test_akshare_provider import _vendor_frame
from tests.research.test_local_cli_pipeline import (
    _commands,
    _run_payloads,
    local_files,
)


SYMBOLS = ("000001.SZ", "600000.SH")
GIT_COMMIT = "a" * 40


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def download_context(tmp_path, monkeypatch):
    """Keep real per-symbol cache and receipts around mocked PIT discovery."""

    config = _config(tmp_path)
    object.__setattr__(config.universe, "provider", "local")
    object.__setattr__(config.universe, "status_provider", "local")
    benchmark = _benchmark_bars_with_warmup()
    membership = _membership()
    config.paths.universe_dir.mkdir(parents=True)
    (config.paths.universe_dir / "manifest.json").write_text(
        json.dumps({"source": "local-import", "sha256": "b" * 64}),
        encoding="utf-8",
    )
    status, listings = _complete_security_data(benchmark["date"])
    security = {
        "security_data_version": "c" * 64,
        "source": "local-import",
        "source_version": "synthetic-security-v1",
        "status_sha256": "d" * 64,
        "listings_sha256": "e" * 64,
        "input_sha256": {},
        "input_files": {},
    }

    class FabricatedProvider:
        """Only deterministic artificial data; no vendor request is possible."""

        def __init__(self):
            self.failures = {}
            self.calls = []

        def cache_source_version(self, symbol, is_index=False):
            return "synthetic-bars-v1"

        def get_daily_bars(self, symbol, start, end):
            self.calls.append((symbol, pd.Timestamp(start), pd.Timestamp(end)))
            if symbol in self.failures:
                raise self.failures[symbol]
            return benchmark.loc[
                benchmark["date"].between(pd.Timestamp(start), pd.Timestamp(end))
            ].assign(symbol=symbol).reset_index(drop=True)

    provider = FabricatedProvider()
    monkeypatch.setattr(cli, "load_config", lambda unused: config)
    monkeypatch.setattr(cli, "_provider", lambda unused: provider)
    monkeypatch.setattr(
        cli,
        "_download_benchmark_with_warmup",
        lambda current, upstream, **unused: benchmark,
    )
    monkeypatch.setattr(
        cli, "_load_verified_membership", lambda current, calendar: membership
    )
    monkeypatch.setattr(
        cli,
        "load_verified_security_data",
        lambda *args, **kwargs: (status, listings, security),
    )
    monkeypatch.setattr(
        "factorlab.reproducibility.git_commit", lambda root: GIT_COMMIT
    )
    monkeypatch.setattr(
        "factorlab.reproducibility.package_versions", lambda: {}
    )
    return {
        "config": config,
        "provider": provider,
        "benchmark": benchmark,
        "membership": membership,
        "cache": cli._stock_cache(config),
        "receipt_root": config.paths.raw_dir / "download_status",
        "failure_path": config.paths.raw_dir / "akshare" / "failed_symbols.json",
    }


def _run_download(context):
    config = context["config"]
    status = cli.main(["download-data", "--config", "unused.yaml"])
    manifests = sorted(config.paths.runs_dir.glob("*/manifest.json"))
    assert manifests
    return status, _read_json(manifests[-1])


def _seed_fatal(context, symbol=SYMBOLS[0], start=None, end=None):
    dates = context["benchmark"]["date"]
    return record_fatal(
        context["receipt_root"],
        symbol=symbol,
        provider_class=type(context["provider"]).__name__,
        source_version=context["provider"].cache_source_version(symbol, False),
        start=start if start is not None else dates.min(),
        end=end if end is not None else dates.max(),
        run_id="synthetic-prior-download",
        git_commit=GIT_COMMIT,
        error_type="SchemaError",
        error_message="synthetic prior invalid adjustment factor",
    )


def _seed_complete_cache(context, symbol=SYMBOLS[0], end=None):
    provider = context["provider"]
    dates = context["benchmark"]["date"]
    last = dates.max() if end is None else pd.Timestamp(end)
    bars = provider.get_daily_bars(symbol, dates.min(), last)
    context["cache"].update(
        symbol,
        bars,
        covered_start=dates.min(),
        covered_end=last,
        source=type(provider).__name__,
        source_version=provider.cache_source_version(symbol, False),
    )
    provider.calls.clear()


def test_transient_network_failure_continues_and_persists_symbol_list(download_context):
    context = download_context
    context["provider"].failures[SYMBOLS[0]] = ProviderNetworkError(
        "synthetic upstream timeout"
    )

    status, run = _run_download(context)

    assert status == 0
    assert [call[0] for call in context["provider"].calls] == list(SYMBOLS)
    assert run["status"] == "success"
    assert run["failures"]["market_data"] == {
        SYMBOLS[0]: "synthetic upstream timeout"
    }
    assert _read_json(context["failure_path"]) == run["failures"]["market_data"]
    assert context["cache"].load(SYMBOLS[0]).empty
    assert not context["cache"].load(SYMBOLS[1]).empty
    assert list_unresolved(context["receipt_root"]) == []
    assert run["unresolved_download_receipts"] == []


@pytest.mark.parametrize(
    "exception",
    [
        ProviderError("synthetic unsupported provider contract"),
        SchemaError("synthetic invalid price schema"),
        CacheError("synthetic publication failure"),
        RuntimeError("synthetic unexpected bug"),
    ],
    ids=["provider-contract", "schema", "cache", "unexpected"],
)
def test_integrity_failures_stop_before_next_symbol_and_leave_fatal_evidence(
    download_context, exception
):
    context = download_context
    context["provider"].failures[SYMBOLS[0]] = exception

    status, run = _run_download(context)

    assert status == 1
    assert [call[0] for call in context["provider"].calls] == [SYMBOLS[0]]
    assert run["status"] == "failed"
    assert str(exception) in run["error"]
    assert str(exception) in json.dumps(run["failures"])
    assert run["outputs"]["download_contract_ledger"] == str(context["receipt_root"])
    assert SYMBOLS[0] in _read_json(context["failure_path"])
    assert context["cache"].load(SYMBOLS[1]).empty
    unresolved = list_unresolved(context["receipt_root"])
    assert len(unresolved) == 1
    receipt = unresolved[0]
    assert receipt["context"]["symbol"] == SYMBOLS[0]
    assert receipt["context"]["run_id"] == run["run_id"]
    assert receipt["context"]["git_commit"] == GIT_COMMIT
    assert receipt["fatal"]["error_type"] == type(exception).__name__
    assert receipt["fatal"]["error_message"] == str(exception)
    assert receipt["context"]["start"] == context["benchmark"]["date"].min().date().isoformat()
    assert receipt["context"]["end"] == context["benchmark"]["date"].max().date().isoformat()


def test_later_fatal_preserves_prior_stock_generation_and_failure_list(
    download_context,
):
    context = download_context
    context["provider"].failures[SYMBOLS[1]] = CacheError(
        "synthetic second-symbol cache failure"
    )

    status, run = _run_download(context)

    assert status == 1
    assert [call[0] for call in context["provider"].calls] == list(SYMBOLS)
    first = context["cache"].load_snapshot(SYMBOLS[0])
    assert len(first.frame) == len(context["benchmark"])
    assert first.metadata["content_sha256"]
    assert run["status"] == "failed"
    assert list(_read_json(context["failure_path"])) == [SYMBOLS[1]]
    assert [item["context"]["symbol"] for item in list_unresolved(context["receipt_root"])] == [SYMBOLS[1]]


def test_network_retry_cannot_erase_prior_fatal_even_with_complete_cached_prices(
    download_context,
):
    context = download_context
    _seed_complete_cache(context)
    prior = context["cache"].load_snapshot(SYMBOLS[0])
    receipt = _seed_fatal(context)
    context["provider"].failures[SYMBOLS[0]] = ProviderNetworkError(
        "synthetic retry timeout"
    )

    status, run = _run_download(context)

    assert status == 0
    assert [call[0] for call in context["provider"].calls] == list(SYMBOLS)
    assert list_unresolved(context["receipt_root"]) == [receipt]
    after = context["cache"].load_snapshot(SYMBOLS[0])
    assert after.metadata_sha256 == prior.metadata_sha256
    pd.testing.assert_frame_equal(after.frame, prior.frame)
    assert SYMBOLS[0] in run["failures"]["market_data"]
    assert run["unresolved_download_receipts"] == [receipt["receipt_id"]]


def test_successful_retry_refetches_covered_cache_and_resolves_original_fatal(
    download_context,
):
    context = download_context
    _seed_complete_cache(context)
    before = context["cache"].load_snapshot(SYMBOLS[0])
    fatal = _seed_fatal(context)
    historical_receipts = set(context["receipt_root"].glob("receipt.*.json"))

    status, run = _run_download(context)

    assert status == 0
    assert [call[0] for call in context["provider"].calls] == list(SYMBOLS)
    first_call = context["provider"].calls[0]
    assert first_call[1:] == (
        context["benchmark"]["date"].min(),
        context["benchmark"]["date"].max(),
    )
    after = context["cache"].load_snapshot(SYMBOLS[0])
    assert after.metadata_sha256 != before.metadata_sha256
    assert list_unresolved(context["receipt_root"]) == []
    assert historical_receipts.issubset(set(context["receipt_root"].glob("receipt.*.json")))
    assert run["status"] == "success"
    assert _read_json(context["failure_path"]) == {}
    assert run["resolved_download_receipts"] == [fatal["receipt_id"]]
    assert run["outputs"]["download_contract_ledger"] == str(context["receipt_root"])


def test_recovery_refetch_keeps_earlier_fatal_window_outside_current_research_range(
    download_context,
):
    context = download_context
    _seed_complete_cache(context)
    old_start = context["benchmark"]["date"].min() - pd.Timedelta(days=9)
    _seed_fatal(context, start=old_start)

    status, _ = _run_download(context)

    assert status == 0
    assert context["provider"].calls[0] == (
        SYMBOLS[0], old_start, context["benchmark"]["date"].max()
    )
    assert list_unresolved(context["receipt_root"]) == []


def test_empty_forced_fetch_cannot_count_old_cache_rows_as_verified_recovery(
    download_context, monkeypatch,
):
    context = download_context
    _seed_complete_cache(context)
    _seed_fatal(context)
    provider = context["provider"]

    def empty_fetch(symbol, start, end):
        provider.calls.append((symbol, pd.Timestamp(start), pd.Timestamp(end)))
        return context["benchmark"].iloc[:0].assign(symbol=symbol)

    monkeypatch.setattr(provider, "get_daily_bars", empty_fetch)

    status, run = _run_download(context)

    assert status == 1
    assert run["status"] == "failed"
    assert [call[0] for call in provider.calls] == [SYMBOLS[0]]
    assert list_unresolved(context["receipt_root"])
    assert not context["cache"].load(SYMBOLS[0]).empty


def test_unresolved_fatal_blocks_shared_bar_input_loader_with_old_cache_present(
    download_context,
):
    context = download_context
    _seed_complete_cache(context)
    _seed_fatal(context)

    with pytest.raises(DataQualityError, match="(?i)fatal|integrity|unresolved"):
        cli._load_bar_inputs(context["config"], context["membership"])

    assert context["provider"].calls == []
    assert not context["cache"].load(SYMBOLS[0]).empty


@pytest.mark.parametrize("missing", ["end", "warmup"])
def test_fresh_benchmark_repair_cannot_hide_truncated_response_in_complete_old_cache(
    download_context, monkeypatch, missing,
):
    context = download_context
    config = context["config"]
    benchmark = context["benchmark"]
    symbol = config.data.benchmark
    provider = context["provider"]
    cache = cli._benchmark_cache(config)
    cache.update(
        symbol, benchmark,
        covered_start=benchmark["date"].min(),
        covered_end=benchmark["date"].max(),
        source=type(provider).__name__,
        source_version=provider.cache_source_version(symbol, True),
    )
    before = cache.load_snapshot(symbol)
    fatal = _seed_fatal(context, symbol=symbol)
    incoming = benchmark.iloc[:-1] if missing == "end" else benchmark.iloc[-65:]
    requests = []

    def truncated_index(requested_symbol, start, end):
        requests.append((requested_symbol, pd.Timestamp(start), pd.Timestamp(end)))
        return incoming.copy()

    monkeypatch.setattr(provider, "get_index_bars", truncated_index, raising=False)
    run = RunManifest(config, "download-data", {"test": "truncated-benchmark-repair"})
    expected = r"configured data\.end" if missing == "end" else "required warm-up"

    with pytest.raises(FactorLabError, match=expected) as caught:
        cli._update_guarded_bars(
            config, run, provider, cache, symbol,
            cli._benchmark_request_start(config), config.data.end,
            is_index=True,
        )
    run.finish("failed", str(caught.value))

    assert len(requests) == 1
    assert requests[0][0] == symbol
    assert requests[0][1] <= pd.Timestamp(fatal["context"]["start"])
    assert requests[0][2] >= pd.Timestamp(fatal["context"]["end"])
    after = cache.load_snapshot(symbol)
    assert after.metadata_sha256 == before.metadata_sha256
    pd.testing.assert_frame_equal(after.frame, before.frame)
    pending = list_unresolved(context["receipt_root"])
    assert fatal["receipt_id"] in {item["receipt_id"] for item in pending}
    assert not run.payload.get("resolved_download_receipts")
    assert _read_json(run.path)["status"] == "failed"


@pytest.mark.parametrize(
    "command,handler,extra_arguments",
    [
        ("build-dataset", "command_build_dataset", []),
        ("evaluate-factors", "command_evaluate", []),
        ("walk-forward", "command_walk_forward", ["--model", "factor_score"]),
        ("compare-models", "command_compare_models", []),
        ("activate-forward", "command_activate_forward", ["--date", "2024-01-03"]),
        ("freeze-forward-model", "command_freeze_forward_model", []),
        ("forward-signal", "command_forward_signal", ["--date", "2024-01-03"]),
    ],
)
def test_public_research_commands_stop_before_dispatch_when_prior_fatal_exists(
    download_context, monkeypatch, command, handler, extra_arguments,
):
    context = download_context
    fatal = _seed_fatal(context)
    dispatched = []

    def forbidden_dispatch(*args, **kwargs):
        dispatched.append((args, kwargs))
        pytest.fail("Research command ran with an unresolved download contract")

    monkeypatch.setattr(cli, handler, forbidden_dispatch)

    assert cli.main([command, "--config", "unused.yaml"] + extra_arguments) == 1

    assert dispatched == []
    manifests = sorted(context["config"].paths.runs_dir.glob("*/manifest.json"))
    assert len(manifests) == 1
    failed = _read_json(manifests[0])
    assert failed["command"] == command
    assert failed["status"] == "failed"
    assert "UNRESOLVED DOWNLOAD CONTRACT" in failed["error"]
    assert list_unresolved(context["receipt_root"]) == [fatal]
    assert context["provider"].calls == []


def test_manifest_output_bookkeeping_failure_cannot_suppress_new_fatal_receipt(
    download_context, monkeypatch,
):
    context = download_context
    _seed_complete_cache(context, end=context["benchmark"]["date"].iloc[-2])
    before = context["cache"].load_snapshot(SYMBOLS[0])
    context["provider"].failures[SYMBOLS[0]] = SchemaError(
        "synthetic first fatal despite output-bookkeeping failure"
    )
    original = RunManifest.add_output

    def failed_ledger_output(self, name, path):
        if name == "download_contract_ledger":
            raise OSError("synthetic manifest output registration failure")
        return original(self, name, path)

    monkeypatch.setattr(RunManifest, "add_output", failed_ledger_output)

    status, run = _run_download(context)

    assert status == 1
    assert run["status"] == "failed"
    pending = list_unresolved(context["receipt_root"])
    assert len(pending) == 1
    assert pending[0]["fatal"]["error_type"] == "SchemaError"
    assert pending[0]["context"]["run_id"] == run["run_id"]
    assert context["cache"].load_snapshot(SYMBOLS[0]).metadata_sha256 == before.metadata_sha256
    with pytest.raises(DataQualityError, match="UNRESOLVED DOWNLOAD CONTRACT"):
        cli._load_bar_inputs(context["config"], context["membership"])


def test_actual_akshare_transport_exception_is_skipped_through_cache_and_cli(
    download_context, monkeypatch,
):
    context = download_context

    def forbidden_network(*args, **kwargs):
        pytest.fail("The injected AKShare SDK test attempted a real network call")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_network)

    class SDK:
        __version__ = "synthetic-sdk-not-market-data"

        def __init__(self):
            self.stock_calls = []

        def stock_zh_a_hist(self, **kwargs):
            self.stock_calls.append(kwargs)
            if kwargs["symbol"] == "000001":
                raise requests.ConnectionError("synthetic SDK connection failure")
            return _vendor_frame(multiplier=2.0 if kwargs["adjust"] == "hfq" else 1.0)

    sdk = SDK()
    provider = AKShareProvider(
        akshare_module=sdk, retry_count=0, request_interval=0,
        sleep_fn=forbidden_network,
    )
    monkeypatch.setattr(cli, "_provider", lambda current: provider)

    status, run = _run_download(context)

    assert status == 0
    assert [(item["symbol"], item["adjust"]) for item in sdk.stock_calls] == [
        ("000001", ""), ("600000", ""), ("600000", "hfq"),
    ]
    assert run["status"] == "success"
    assert list(run["failures"]["market_data"]) == [SYMBOLS[0]]
    assert "failed after retries" in run["failures"]["market_data"][SYMBOLS[0]]
    assert run["unresolved_download_receipts"] == []
    assert list_unresolved(context["receipt_root"]) == []
    assert context["cache"].load(SYMBOLS[0]).empty
    snapshot = context["cache"].load_snapshot(SYMBOLS[1])
    assert len(snapshot.frame) == 2
    assert snapshot.metadata["source"] == "AKShareProvider"
    assert snapshot.metadata["source_version"] == provider.cache_source_version(SYMBOLS[1], False)


def test_public_local_file_failure_recovers_cache_but_still_rejects_research(
    local_files, monkeypatch,
):
    """Only an artificial provider fault is injected; networking is prohibited."""

    files = local_files
    commands = _commands(files)
    assert [cli.main(command) for command in commands[:3]] == [0, 0, 0]
    config = load_config(files["config_path"])
    assert _read_json(config.paths.market_input_root / "manifest.json")["schema_version"] == 1
    provider = LocalParquetProvider(
        config.paths.market_input_root, benchmark_symbol=config.data.benchmark
    )
    cache = ParquetBarCache(config.paths.raw_dir / "local_parquet" / "daily")
    dates = files["dates"]
    for symbol in SYMBOLS:
        # Both real on-disk generations precede the failed incremental request.
        cache.update(
            symbol,
            provider.get_daily_bars(symbol, dates.min(), dates[-2]),
            covered_start=dates.min(), covered_end=dates[-2],
            source=type(provider).__name__,
            source_version=provider.cache_source_version(symbol, False),
        )
    before = {
        symbol: cache.load_snapshot(symbol).metadata_sha256 for symbol in SYMBOLS
    }
    requests = []
    original = LocalParquetProvider.get_daily_bars

    def corrupted_stock(self, symbol, start, end):
        requests.append((symbol, pd.Timestamp(start), pd.Timestamp(end)))
        raise SchemaError("synthetic delayed adjustment-factor corruption")

    monkeypatch.setattr(LocalParquetProvider, "get_daily_bars", corrupted_stock)
    assert cli.main(commands[3]) == 1
    assert [item[0] for item in requests] == [SYMBOLS[0]]
    receipt_root = config.paths.raw_dir / "download_status"
    fatal = list_unresolved(receipt_root)
    assert len(fatal) == 1
    assert fatal[0]["fatal"]["error_type"] == "SchemaError"
    assert {
        symbol: cache.load_snapshot(symbol).metadata_sha256 for symbol in SYMBOLS
    } == before
    for command in commands[4:]:
        assert cli.main(command) == 1
        failed = max(_run_payloads(files), key=lambda item: item["run_id"])
        assert failed["status"] == "failed"
        assert any(term in failed["error"].lower() for term in ("fatal", "unresolved", "integrity"))
    assert not (config.paths.processed_dir / "dataset.parquet").exists()

    requests.clear()

    def repaired_stock(self, symbol, start, end):
        requests.append((symbol, pd.Timestamp(start), pd.Timestamp(end)))
        return original(self, symbol, start, end)

    monkeypatch.setattr(LocalParquetProvider, "get_daily_bars", repaired_stock)
    # Download recovery proves cache integrity, independently of qualification.
    assert cli.main(commands[3]) == 0
    assert requests[0] == (SYMBOLS[0], dates.min(), dates.max())
    assert list_unresolved(receipt_root) == []
    for symbol in SYMBOLS:
        snapshot = cache.load_snapshot(symbol)
        pd.testing.assert_frame_equal(
            snapshot.frame, original(provider, symbol, dates.min(), dates.max()),
            check_exact=True,
        )
        assert snapshot.metadata_sha256 != before[symbol]
        assert snapshot.metadata["covered_ranges"] == [{
            "start": dates.min().date().isoformat(),
            "end": dates.max().date().isoformat(),
        }]
    repaired = next(
        run for run in _run_payloads(files)
        if run["command"] == "download-data" and run["status"] == "success"
    )
    assert repaired["resolved_download_receipts"] == [fatal[0]["receipt_id"]]
    assert repaired["unresolved_download_receipts"] == []

    # A repaired schema-v1 cache remains diagnostic; it is not formal evidence.
    recovered_metadata = {
        symbol: cache.load_snapshot(symbol).metadata_sha256 for symbol in SYMBOLS
    }
    for command in commands[4:]:
        assert cli.main(command) == 1
        rejected = max(_run_payloads(files), key=lambda item: item["run_id"])
        assert rejected["command"] == command[0]
        assert rejected["status"] == "failed"
        assert "v2 qualification" in rejected["error"]
        assert "schema-v1 bundles are diagnostic only" in rejected["error"]
        assert rejected["outputs"] == {}
    assert list_unresolved(receipt_root) == []
    assert {
        symbol: cache.load_snapshot(symbol).metadata_sha256 for symbol in SYMBOLS
    } == recovered_metadata
    assert not (config.paths.processed_dir / "dataset.parquet").exists()
    assert not (config.paths.processed_dir / "dataset_manifest.json").exists()
    assert not list(config.paths.features_dir.glob("*.parquet"))
    assert not list(config.paths.factor_results_dir.glob("*"))
    runs = _run_payloads(files)
    assert len(runs) == 9
    assert sum(run["status"] == "failed" for run in runs) == 5
    assert sum(run["status"] == "success" for run in runs) == 4
    failed_download = next(
        run for run in runs if run["command"] == "download-data" and run["status"] == "failed"
    )
    assert "synthetic delayed adjustment-factor corruption" in failed_download["error"]
    assert fatal[0]["context"]["run_id"] == failed_download["run_id"]
