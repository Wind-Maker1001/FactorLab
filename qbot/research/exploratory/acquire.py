"""Isolated real vendor RAW/HFQ acquisition; never grants formal admission.

Source responses stay in the ignored private data/research/exploratory tree.
Stocks use the existing AKShareProvider canonicalizer and documented units.
The CSI500 price index is a separate declared benchmark, not equity HFQ.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil

import pandas as pd

from qbot.research.data.calendar import TradingCalendar
from qbot.research.data.cache import ParquetBarCache
from qbot.research.data.providers.akshare import AKShareProvider
from qbot.research.data.schema import CANONICAL_BAR_COLUMNS, normalize_symbol
from qbot.research.exploratory.contract import (
    load_dataset,
    manifest_version,
    sha256_file,
)


DEFAULT_SYMBOLS = tuple(
    sorted(
        [
            code + ".SH"
            for code in (
                "600000 600009 600016 600019 600028 600030 600031 600036 600048 "
                "600050 600104 600111 600150 600276 600309 600406 600519 600585 "
                "600690 600703 600795 600887 600900 601012 601318"
            ).split()
        ]
        + [
            code + ".SZ"
            for code in (
                "000001 000002 000063 000100 000166 000333 000338 000425 000538 "
                "000568 000651 000725 000768 000776 000858 000876 000895 000938 "
                "002001 002027 002049 002050 002142 002230 002594"
            ).split()
        ]
    )
)

SELECTION_METHOD = (
    "Fixed manually enumerated long-established Shanghai/Shenzhen main-board "
    "convenience securities across sectors, frozen before any target-window "
    "provider request; no screening by historical or future returns, membership, "
    "or future trading status. Not a representative CSI500 pool."
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def freeze_sample(root, symbols=DEFAULT_SYMBOLS, selection_date=None):
    """Persist the sample before HTTP; an existing freeze cannot be replaced."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "sample.json"
    canonical = [normalize_symbol(symbol) for symbol in symbols]
    if not canonical or len(canonical) != len(set(canonical)):
        raise ValueError("unique nonempty sample symbols required")
    if path.exists():
        sample = json.loads(path.read_text(encoding="utf-8"))
        if sample["symbols"] != canonical:
            raise ValueError("cannot replace the pre-frozen stock list")
        return sample
    now = pd.Timestamp(utc_now())
    sample = {
        "symbols": canonical,
        "selection_method": SELECTION_METHOD,
        "selection_date": selection_date
        or now.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d"),
        "pit": False,
        "classification": "NON_PIT_CONVENIENCE",
        "frozen_at": now.isoformat(),
        "biases": [
            "survivorship bias",
            "convenience selection bias",
            "large-cap/known-name bias",
            "no historical PIT membership",
        ],
    }
    write_json(path, sample)
    return sample


class RecordingAKShare:
    """Capture native HTTP bytes and unmodified SDK tables in this process.

    This recorder is used synchronously. It does not patch another process,
    alter global sockets, interpolate missing values, or generate prices.
    """

    def __init__(self, module, root):
        self.module = module
        self.root = Path(root)
        self.source_dir = self.root / "sources"
        self.source_dir.mkdir(exist_ok=True)
        existing = list(self.source_dir.rglob("*"))
        self.sequence = max(
            [int(path.name[:4]) for path in existing if path.name[:4].isdigit()] or [0]
        )
        self.artifacts = [
            path.relative_to(self.root).as_posix()
            for path in existing
            if path.is_file()
        ]
        self.requests = []
        self.replay = {}
        self.vendor_tables = {}
        self.last_stem = None
        for path in sorted(self.source_dir.rglob("*.request.json")):
            receipt = json.loads(path.read_text(encoding="utf-8"))
            raw_path = self.root / receipt["response_file"]
            if sha256_file(raw_path) != receipt["response_sha256"]:
                raise ValueError("cached HTTP source hash mismatch")
            prior = self.replay.get(receipt["url"])
            if prior is None or pd.Timestamp(receipt["retrieved_at"]) > pd.Timestamp(
                prior[1]["retrieved_at"]
            ):
                self.replay[receipt["url"]] = (raw_path, receipt)
        self.__version__ = module.__version__

    def save(self, path):
        self.artifacts.append(path.relative_to(self.root).as_posix())

    @contextmanager
    def capture(self, interface, kwargs):
        import requests

        original = requests.sessions.Session.request
        recorder = self

        def recorded(session, method, url, **options):
            # Index SDK methods have no public timeout; enforce an explicit
            # connection/read timeout here without changing request contents.
            if options.get("timeout") is None:
                options["timeout"] = 30.0
            requested = requests.Request(method, url, params=options.get("params"))
            prepared_url = requested.prepare().url
            if prepared_url in recorder.replay:
                raw_path, receipt = recorder.replay[prepared_url]
                response = requests.Response()
                response.status_code = receipt["status_code"]
                response._content = raw_path.read_bytes()
                response.url = prepared_url
                response.headers["Content-Type"] = receipt["content_type"]
                recorder.last_stem = raw_path.stem
                return response
            recorder.sequence += 1
            stem = "%04d-%s" % (recorder.sequence, interface)
            recorder.last_stem = stem
            requested_at = utc_now()
            response = original(session, method, url, **options)
            path = recorder.source_dir / (stem + ".bin")
            path.write_bytes(response.content)
            recorder.save(path)
            receipt = {
                "interface": interface,
                "interface_arguments": kwargs,
                "method": method,
                "url": response.url,
                "status_code": response.status_code,
                "requested_at": requested_at,
                "retrieved_at": utc_now(),
                "response_sha256": sha256_file(path),
                "response_file": path.relative_to(recorder.root).as_posix(),
                "content_type": response.headers.get("Content-Type", ""),
                "upstream_vintage": "UNKNOWN_UNVERIFIED",
            }
            request_path = recorder.source_dir / (stem + ".request.json")
            write_json(request_path, receipt)
            recorder.save(request_path)
            recorder.requests.append(receipt)
            recorder.replay[response.url] = (path, receipt)
            return response

        requests.sessions.Session.request = recorded
        try:
            yield
        finally:
            requests.sessions.Session.request = original

    def __getattr__(self, name):
        attribute = getattr(self.module, name)
        if not callable(attribute):
            return attribute

        def call(**kwargs):
            with self.capture(name, kwargs):
                result = attribute(**kwargs)
            if isinstance(result, pd.DataFrame):
                if name == "stock_zh_a_hist":
                    self.vendor_tables[(kwargs["symbol"], kwargs.get("adjust", ""))] = (
                        result.copy()
                    )
                # Keep vendor columns/units before the provider canonicalizer.
                path = self.source_dir / (self.last_stem + "-table.csv")
                if not path.exists():
                    result.to_csv(path, index=False, encoding="utf-8")
                    self.save(path)
            return result

        return call


def ingest_firecrawl_capture(root, envelope_path):
    """Retain non-LLM rawHtml JSON representation from the same vendor URL.

    Firecrawl returns decoded text, not independently authenticated HTTP wire
    bytes. Preserve its complete envelope and disclose that representation.
    The exact URL and response-body hash permit the existing SDK to replay it.
    """
    root = Path(root).resolve()
    path = Path(envelope_path).resolve()
    if root not in path.parents or (root / "manifest.json").exists():
        raise ValueError("capture must be inside an unsealed private dataset")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    payload = envelope.get("structuredContent")
    if payload is None and "rawHtml" in envelope:
        payload = envelope
    if payload is None:
        for block in envelope.get("content", []):
            if block.get("type") == "text":
                candidate = json.loads(block["text"])
                if "rawHtml" in candidate:
                    payload = candidate
                    break
    if envelope.get("isError") or not isinstance(payload, dict):
        raise ValueError("Firecrawl did not return a successful rawHtml source")
    body = payload.get("rawHtml")
    metadata = payload.get("metadata", {})
    capture_time = pd.Timestamp(envelope.get("capture_retrieved_at"))
    if pd.isna(capture_time) or capture_time.tzinfo is None:
        raise ValueError("timezone-aware capture timestamp required")
    if (
        not isinstance(body, str)
        or metadata.get("statusCode") != 200
        or not metadata.get("contentType", "").lower().startswith("application/json")
    ):
        raise ValueError("HTTP 200 JSON rawHtml representation required")
    from urllib.parse import parse_qs, urlsplit

    url = metadata.get("sourceURL") or metadata.get("url")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "push2his.eastmoney.com"
        or parsed.path != "/api/qt/stock/kline/get"
    ):
        raise ValueError("exact declared Eastmoney history endpoint required")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if any(
        len(query.get(key, [])) != 1 or not query[key][0]
        for key in ("secid", "fqt", "klt", "beg", "end")
    ):
        raise ValueError(
            "each identity, adjustment and date query must have one nonempty value"
        )
    secid = query.get("secid", [""])[0]
    parts = secid.split(".")
    if (
        len(parts) != 2
        or parts[0] not in {"0", "1"}
        or len(parts[1]) != 6
        or not parts[1].isdigit()
    ):
        raise ValueError("explicit Eastmoney equity secid required")
    symbol = parts[1]
    market = int(parts[0])
    canonical_symbol = normalize_symbol(symbol + (".SH" if market == 1 else ".SZ"))
    sample = json.loads((root / "sample.json").read_text(encoding="utf-8"))
    if canonical_symbol not in sample["symbols"]:
        raise ValueError("vendor response outside the pre-frozen sample")
    adjustment = {"0": "", "2": "hfq"}.get(query.get("fqt", [None])[0])
    if adjustment is None or query.get("klt") != ["101"]:
        raise ValueError("explicit daily RAW or HFQ query required")
    native = json.loads(body)
    data = native.get("data")
    if (
        native.get("rc") != 0
        or not isinstance(data, dict)
        or data.get("code") != symbol
        or data.get("market") != market
    ):
        raise ValueError("native vendor response identity/status mismatch")
    if any(
        len(query[key][0]) != 8 or not query[key][0].isdigit() for key in ("beg", "end")
    ):
        raise ValueError("strict YYYYMMDD query date required")
    start = pd.to_datetime(query["beg"][0], format="%Y%m%d", errors="raise")
    end = pd.to_datetime(query["end"][0], format="%Y%m%d", errors="raise")
    if start > end:
        raise ValueError("source request date range is reversed")
    rows = [row.split(",") for row in data.get("klines", [])]
    if not rows or any(len(row) != 11 for row in rows):
        raise ValueError("nonempty native eleven-field stock history required")
    dates = pd.DatetimeIndex(
        pd.to_datetime([row[0] for row in rows], format="%Y-%m-%d", errors="raise")
    )
    if (
        dates.hasnans
        or dates.has_duplicates
        or not dates.is_monotonic_increasing
        or not ((dates >= start) & (dates <= end)).all()
    ):
        raise ValueError(
            "native vendor dates must be ordered, unique, and inside the requested range"
        )
    vendor_frame = pd.DataFrame(
        rows,
        columns=[
            "date",
            "open",
            "close",
            "high",
            "low",
            "volume",
            "amount",
            "amplitude",
            "pct_change",
            "price_change",
            "turnover",
        ],
    )
    for column in vendor_frame.columns[1:]:
        vendor_frame[column] = pd.to_numeric(vendor_frame[column], errors="raise")
    AKShareProvider._normalise_vendor_frame(
        vendor_frame,
        start,
        end,
        require_turnover=True,
        require_amount=True,
        symbol=canonical_symbol,
    )
    raw_path = path.with_suffix(".bin")
    request_path = path.with_suffix(".request.json")
    encoded = body.encode("utf-8")
    if raw_path.exists() and raw_path.read_bytes() != encoded:
        raise ValueError("cannot replace captured vendor JSON representation")
    if not raw_path.exists():
        raw_path.write_bytes(encoded)
    receipt = {
        "interface": "stock_zh_a_hist",
        "interface_arguments": {
            "symbol": symbol,
            "period": "daily",
            "start_date": query["beg"][0],
            "end_date": query["end"][0],
            "adjust": adjustment,
        },
        "method": "GET",
        "url": url,
        "status_code": 200,
        "retrieved_at": envelope["capture_retrieved_at"],
        "response_sha256": sha256_file(raw_path),
        "response_file": raw_path.relative_to(root).as_posix(),
        "content_type": metadata.get("contentType", "application/json; charset=UTF-8"),
        "upstream_vintage": "UNKNOWN_UNVERIFIED",
        "transport": "FIRECRAWL_RAWHTML_REPRESENTATION",
        "role": "HFQ" if adjustment else "RAW",
        "symbol": canonical_symbol,
        "representation": "UTF8 encoded returned rawHtml JSON text; not original HTTP wire bytes or an independent vendor reference",
        "envelope_file": path.relative_to(root).as_posix(),
        "envelope_sha256": sha256_file(path),
        "firecrawl_metadata": metadata,
        "row_count": len(native["data"]["klines"]),
    }
    if request_path.exists():
        old = json.loads(request_path.read_text(encoding="utf-8"))
        if old["response_sha256"] != receipt["response_sha256"] or old["url"] != url:
            raise ValueError("cannot replace source capture receipt")
    else:
        write_json(request_path, receipt)
    return receipt


def acquire(
    root,
    research_start="2025-09-12",
    research_end="2026-09-11",
    history_start="2025-01-01",
    symbols=DEFAULT_SYMBOLS,
    progress=print,
    akshare_module=None,
    max_passes=3,
    benchmark_cache_root=None,
):
    """Fetch one declared stock vendor; failed securities are disclosed.

    Missing terminal future prices are intentionally left unavailable. No
    benchmark or security prices are appended from another vendor/cache.
    """
    root = Path(root).resolve()
    if (root / "manifest.json").exists():
        raise ValueError("immutable dataset already exists; use a new output version")
    sample = freeze_sample(root, symbols)
    if type(max_passes) is not int or not 1 <= max_passes <= 10:
        raise ValueError("max_passes must be between one and ten")
    if akshare_module is None:
        import akshare as akshare_module
    recorder = RecordingAKShare(akshare_module, root)
    old_attempts = root / "attempts.json"
    if old_attempts.exists():
        previous_path = recorder.source_dir / (
            "previous-attempts-%s.json"
            % (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        )
        shutil.copyfile(old_attempts, previous_path)
        recorder.save(previous_path)
    provider = AKShareProvider(
        adjustment="hfq",
        retry_count=1,
        timeout_seconds=15.0,
        request_interval=0.25,
        akshare_module=recorder,
    )
    version = (
        "akshare=%s;stocks=EASTMONEY-stock_zh_a_hist-RAW+HFQ;"
        "benchmark=CSI-stock_zh_index_hist_csindex-PRICE_INDEX;"
        "transport=REQUESTS_HTTP_BYTES-or-FIRECRAWL_RAWHTML_REPRESENTATION;"
        "adapter=EXPLORATORY_ACQUISITION_V2"
    ) % akshare_module.__version__
    attempts = {
        "schema": "EXPLORATORY_ACQUISITION_ATTEMPTS_V1",
        "purpose": "EXPLORATORY",
        "qualification": "UNVERIFIED",
        "historical_status": "HISTORICALLY_RECONSTRUCTED",
        "formal_admission": False,
        "synthetic_only": False,
        "source_version": version,
        "started_at": utc_now(),
        "requested_sample_count": len(sample["symbols"]),
        "sample_sha256": sha256_file(root / "sample.json"),
        "history_start": history_start,
        "research_start": research_start,
        "research_end": research_end,
        "securities": [],
        "failures": [],
    }
    write_json(root / "attempts.json", attempts)
    histories = []
    acquired_symbols = set()
    for offset, symbol in enumerate(sample["symbols"] * max_passes):
        if symbol in acquired_symbols:
            continue
        pass_number = offset // len(sample["symbols"]) + 1
        index = offset % len(sample["symbols"]) + 1
        progress(
            "[pass %d/%d, %d/%d] %s RAW+HFQ"
            % (pass_number, max_passes, index, len(sample["symbols"]), symbol)
        )
        try:
            frame = provider.get_daily_bars(symbol, history_start, research_end)
            code = symbol.split(".")[0]
            raw_table = recorder.vendor_tables[(code, "")]
            adjusted_table = recorder.vendor_tables[(code, "hfq")]
            quantity_differences = {}
            for column, key in (
                ("成交量", "volume"),
                ("成交额", "amount"),
                ("换手率", "turnover"),
            ):
                # A diagnostic of the vendor's two returned tables. Only RAW
                # quantities supply factors; no adjusted value fills RAW gaps.
                quantity_differences[key] = sum(
                    not math.isclose(
                        float(left), float(right), rel_tol=1e-8, abs_tol=1e-6
                    )
                    for left, right in zip(raw_table[column], adjusted_table[column])
                )
            valid = frame["adj_close"].notna() & frame["raw_close"].notna()
            warmup_count = int(
                (valid & frame["date"].lt(pd.Timestamp(research_start))).sum()
            )
            research_count = int(
                (
                    valid
                    & frame["date"].between(
                        pd.Timestamp(research_start), pd.Timestamp(research_end)
                    )
                ).sum()
            )
            if warmup_count < 130 or research_count == 0:
                raise ValueError(
                    "insufficient observed price history: warmup=%d, research=%d"
                    % (warmup_count, research_count)
                )
            # The existing provider requires the turnover column. Values stay
            # missing if the vendor explicitly returned missing observations.
            if frame.loc[valid, "turnover"].notna().sum() == 0:
                raise ValueError("no real historical turnover observations")
            histories.append(frame)
            acquired_symbols.add(symbol)
            attempts["securities"].append(
                {
                    "symbol": symbol,
                    "status": "ACQUIRED",
                    "rows": len(frame),
                    "acquired_pass": pass_number,
                    "observed_at": utc_now(),
                    "raw_hfq_quantity_difference_rows": quantity_differences,
                    "start": frame["date"].min().strftime("%Y-%m-%d"),
                    "end": frame["date"].max().strftime("%Y-%m-%d"),
                    "warmup_observed_price_sessions": warmup_count,
                    "research_observed_price_sessions": research_count,
                    "turnover_missing": int(frame["turnover"].isna().sum()),
                }
            )
        except Exception as exc:
            attempts["failures"].append(
                {
                    "symbol": symbol,
                    "pass": pass_number,
                    "attempted_at": utc_now(),
                    "reason": "%s: %s" % (type(exc).__name__, exc),
                }
            )
            progress("EXCLUDED %s: %s" % (symbol, exc))
        write_json(root / "attempts.json", attempts)
    progress("CSI500 real price-index benchmark")
    try:
        benchmark_provenance = None
        if benchmark_cache_root is not None:
            cache = ParquetBarCache(Path(benchmark_cache_root))
            expected_version = "akshare=%s;contract=stock_zh_index_hist_csindex-v1" % (
                akshare_module.__version__
            )
            snapshot = cache.load_snapshot(
                "000905.SH",
                expected_source="AKShareProvider",
                expected_source_version=expected_version,
            )
            if snapshot.frame.empty:
                raise ValueError("declared official CSI benchmark cache is empty")
            benchmark = snapshot.frame.loc[
                snapshot.frame["date"].between(
                    pd.Timestamp(history_start), pd.Timestamp(research_end)
                )
            ].copy()
            for original, local in (
                (cache.data_path("000905.SH"), "benchmark-cache-original.parquet"),
                (
                    cache.metadata_path("000905.SH"),
                    "benchmark-cache-original.metadata.json",
                ),
            ):
                destination = recorder.source_dir / local
                if destination.exists() and sha256_file(destination) != sha256_file(
                    original
                ):
                    raise ValueError(
                        "cannot replace frozen original benchmark cache bytes"
                    )
                if not destination.exists():
                    shutil.copyfile(original, destination)
                    recorder.save(destination)
            benchmark_provenance = {
                "source": snapshot.metadata["source"],
                "source_version": snapshot.metadata["source_version"],
                "retrieved_at": snapshot.metadata["updated_at"],
                "data_sha256": snapshot.metadata["content_sha256"],
                "metadata_sha256": snapshot.metadata_sha256,
                "generation_id": snapshot.metadata.get("generation_id"),
                "note": "Previously authorized CSI official price-index cache; exact original bytes retained and hash-validated",
            }
        else:
            benchmark = provider.get_index_bars(
                "000905.SH", history_start, research_end
            )
        calendar = TradingCalendar.from_benchmark_bars(benchmark).dates
        if sum(calendar < pd.Timestamp(research_start)) < 130:
            raise ValueError("benchmark calendar has fewer than 130 warmup sessions")
        if not histories:
            raise ValueError("no securities acquired with real required inputs")
        bars = (
            pd.concat(histories, ignore_index=True)
            .sort_values(["date", "symbol"], kind="mergesort")
            .reset_index(drop=True)
        )
        bars[list(CANONICAL_BAR_COLUMNS)].to_parquet(root / "bars.parquet", index=False)
        benchmark[list(CANONICAL_BAR_COLUMNS)].to_parquet(
            root / "benchmark.parquet", index=False
        )
        pd.DataFrame({"date": calendar.strftime("%Y-%m-%d")}).to_csv(
            root / "calendar.csv", index=False
        )
        sufficient = len(histories) >= 20
        remaining = [
            failure
            for failure in attempts["failures"]
            if failure["symbol"] not in acquired_symbols
        ]
        excluded = list({failure["symbol"]: failure for failure in remaining}.values())
        attempts.update(
            status="ACQUIRED" if sufficient else "INSUFFICIENT_CROSS_SECTION",
            finished_at=utc_now(),
            sufficient_cross_section=sufficient,
            acquired_security_count=len(histories),
            benchmark_sessions=len(calendar),
        )
        write_json(root / "attempts.json", attempts)

        def item(relative):
            return {
                "path": relative,
                "sha256": sha256_file(root / relative),
                "source_version": version,
            }

        manifest = {
            "schema": "EXPLORATORY_DATASET_V1",
            "purpose": "EXPLORATORY",
            "qualification": "UNVERIFIED",
            "historical_status": "HISTORICALLY_RECONSTRUCTED",
            "formal_admission": False,
            "synthetic_only": False,
            "research_start": research_start,
            "research_end": research_end,
            "warmup_min_sessions": 130,
            "vendor": "AKSHARE_EASTMONEY",
            "source_version": version,
            "retrieved_at": utc_now(),
            "units": {
                "price": "CNY",
                "volume": "SHARES",
                "amount": "CNY",
                "turnover": "RATIO",
            },
            "price_basis": {"canonical": "RAW", "raw": "RAW", "adj": "HFQ"},
            "benchmark_basis": "CSI500_PRICE_INDEX_UNADJUSTED",
            "files": {
                key: item(relative)
                for key, relative in {
                    "bars": "bars.parquet",
                    "benchmark": "benchmark.parquet",
                    "calendar": "calendar.csv",
                    "sample": "sample.json",
                }.items()
            },
            "sources": [item(relative) for relative in sorted(set(recorder.artifacts))]
            + [item("attempts.json")],
            "source_contract": {
                "stocks": "Eastmoney RAW and vendor HFQ from stock_zh_a_hist; no independent HFQ replication",
                "raw_vendor_units": {
                    "price": "CNY",
                    "volume": "HANDS_100_SHARES",
                    "amount": "CNY",
                    "turnover": "PERCENT",
                },
                "benchmark": "CSI official price-index history through AKShare stock_zh_index_hist_csindex",
                "benchmark_vendor": "CHINA_SECURITIES_INDEX_COMPANY",
                "stock_vendor": "EASTMONEY",
                "capture_transports": [
                    "REQUESTS_HTTP_BYTES",
                    "FIRECRAWL_RAWHTML_REPRESENTATION",
                ],
                "firecrawl_scope": "Non-LLM retrieval of the same exact Eastmoney URL; rawHtml JSON text and complete tool envelope retained, not independent wire-byte evidence or a second vendor",
                "replay_selection": "Latest timestamp among exact-URL captures; original representations are retained unchanged",
                "benchmark_units": {
                    "price": "INDEX_POINTS",
                    "volume": "SHARES",
                    "amount": "CNY",
                    "turnover": "NOT_APPLICABLE",
                },
                "benchmark_cache_provenance": benchmark_provenance,
                "benchmark_adjustment": "PRICE_INDEX_NOT_EQUITY_HFQ: adj OHLC equal index OHLC; no corporate actions",
                "calendar": "Observed actual CSI500 benchmark sessions; no synthetic weekday calendar",
                "adj_factor": "adj_close/raw_close schema ratio only; no certification of vendor algorithm",
                "upstream_vintage": "UNKNOWN_UNVERIFIED",
                "historical_revisions": "download-date reconstruction; historically revised vendor HFQ may differ from as-known-at-t prices",
                "documentation": [
                    "https://akshare.akfamily.xyz/data/stock/stock.html",
                    "https://akshare.akfamily.xyz/data/index/index.html",
                ],
            },
            "excluded_securities": excluded,
            "sufficient_cross_section": sufficient,
            "vendor_quantity_diagnostics": [
                {
                    "symbol": security["symbol"],
                    "difference_rows": security["raw_hfq_quantity_difference_rows"],
                }
                for security in attempts["securities"]
            ],
            "limitations": sample["biases"]
            + [
                "No verified historic trading/ST/limit status: vendor present price rows only",
                "OHLC label proxy does not establish executable trades",
                "Future prices after research_end are not fetched; terminal labels remain missing",
                "NO LIVE MONEY",
                "NOT FORMAL CSI500 EVIDENCE",
            ],
        }
        manifest["data_version"] = manifest_version(manifest)
        write_json(root / "manifest.json", manifest)
        load_dataset(root / "manifest.json")
        progress(
            "%s real inputs: %d securities, %d sessions, data_version=%s"
            % (
                "ACQUIRED" if sufficient else "INSUFFICIENT_CROSS_SECTION",
                len(histories),
                len(calendar),
                manifest["data_version"],
            )
        )
        return manifest
    except Exception as exc:
        # No incomplete or invalid dataset is passed to the research runner.
        (root / "manifest.json").unlink(missing_ok=True)
        attempts.update(
            status="BLOCKED",
            finished_at=utc_now(),
            blocking_reason="%s: %s" % (type(exc).__name__, exc),
        )
        write_json(root / "attempts.json", attempts)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--research-start", default="2025-09-12")
    parser.add_argument("--research-end", default="2026-09-11")
    parser.add_argument("--history-start", default="2025-01-01")
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--benchmark-cache", type=Path, default=None)
    args = parser.parse_args(argv)
    acquire(
        args.output,
        args.research_start,
        args.research_end,
        args.history_start,
        progress=lambda message: print(message, flush=True),
        max_passes=args.passes,
        benchmark_cache_root=args.benchmark_cache,
    )


if __name__ == "__main__":
    main()
