"""Qualified research adapters over existing raw-study and execution APIs."""
from factorlab.data.hub.price_consumption import execution_bars, require_fields
from factorlab.research.real_model_study import PRICES, build_panel


def build_raw_panel(result, members, calendar, config, *, identity):
    """Gate every raw label/factor input before the existing study builds labels.

    The study remains retrospective and its existing status/missing-path filters
    still apply. A qualified price alone cannot establish PIT or tradability.
    """
    requirements = {field: ("RAW_LABEL", "CNY/share") for field in PRICES}
    require_fields(result, requirements, identity=identity)
    require_fields(result, {field: ("RAW_FACTOR", "CNY/share") for field in PRICES}, identity=identity)
    frame = require_fields(result, {"volume": ("UNIT_FACTOR", "shares")}, identity=identity)
    data, profile, snapshot, refs = build_panel(frame, members, calendar, config)
    data.attrs["price_consumption"] = {"source_id": identity[0], "data_version": identity[1],
                                       "purpose": "RAW_LABEL_AND_FACTOR", "pit": "UNVERIFIED"}
    return data, profile, snapshot, refs


def run_execution(engine, scores, result, benchmark, *, identity, **kwargs):
    """Gate stock marks before invoking the unchanged execution engine.

    Benchmark inputs retain the engine's separate benchmark contract. Independent
    Valuation objects remain book-accounting inputs, not market price columns.
    """
    bars = execution_bars(result, identity=identity, config=engine.config, **kwargs)
    output = engine.run(scores, bars, benchmark)
    output.nav.attrs["price_consumption"] = bars.attrs["price_consumption"]
    return output
