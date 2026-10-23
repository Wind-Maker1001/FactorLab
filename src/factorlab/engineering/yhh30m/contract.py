"""Lossless typed vendor observations, deliberately separate from daily bars."""

import json

import pyarrow as pa


RAW_COLUMNS = (
    "证券代码", "证券名称", "交易日期", "分钟时间", "开盘价（元）", "最高价（元）",
    "最低价（元）", "收盘价（元）", "成交量", "成交额", "复权因子",
)
NUMERIC_COLUMNS = ("open", "high", "low", "close", "volume_vendor", "amount_vendor", "adjustment_factor_vendor")
MAPPING = dict(zip(RAW_COLUMNS, (
    "symbol", "security_name", "trade_date", "timestamp_local", *NUMERIC_COLUMNS,
)))
FLAGS = {
    "missing_numeric": 1, "nonfinite_numeric": 2, "nonpositive_price": 4,
    "invalid_ohlc": 8, "negative_volume": 16, "negative_amount": 32,
    "unrecognized_time_label": 64, "outside_regular_session": 128,
}
EXPECTED_LABELS = ("09:30:00", "10:00:00", "10:30:00", "11:00:00", "11:30:00", "13:30:00", "14:00:00", "14:30:00", "15:00:00")
CONTRACT = {
    "schema": "FACTORLAB_YHH_30M_OBSERVATIONS_V1", "frequency": "VENDOR_30M_LABELS",
    "usage": "ENGINEERING_AND_EXPLORATORY_ONLY", "no_live_money": True,
    "formal_admission": False, "financial_research_eligible": False,
    "production_schema": "NOT_FACTORLAB_CANONICAL_DAILY_BARS",
    "price_unit": "VENDOR_DECLARED_YUAN_UNVERIFIED", "volume_unit": "VENDOR_NATIVE_UNKNOWN",
    "amount_unit": "VENDOR_NATIVE_UNKNOWN", "unit_scaling": "NONE",
    "timezone": "UNVERIFIED_VENDOR_WALL_CLOCK_PRESERVED_NAIVE",
    "expected_market_timezone": "Asia/Shanghai",
    "interval_start_end_and_label_side": "UNAVAILABLE_UNVERIFIED",
    "adjustment_method": "UNAVAILABLE", "company_actions": "UNAVAILABLE",
    "pit_membership": "UNAVAILABLE", "historical_security_identity": "UNVERIFIED",
    "status_and_turnover": "UNAVAILABLE", "daily_aggregation": "DISABLED",
    "primary_key": ["symbol", "timestamp_local"],
    "source_row": "ONE_BASED_DATA_ROW_WITHOUT_HEADER",
    "quality_flags": FLAGS, "expected_labels_diagnostic_only": list(EXPECTED_LABELS),
    "anomaly_policy": "PRESERVE_NUMERIC_AND_SESSION_ANOMALIES_WITH_FLAGS;REJECT_INVALID_KEYS_OR_DUPLICATES",
}


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")


SCHEMA = pa.schema([
    pa.field("symbol", pa.string(), nullable=False),
    pa.field("vendor_symbol", pa.string(), nullable=False),
    pa.field("security_name", pa.string()),
    pa.field("trade_date", pa.date32(), nullable=False),
    pa.field("timestamp_local", pa.timestamp("us"), nullable=False),
    *(pa.field(c, pa.float64()) for c in NUMERIC_COLUMNS),
    pa.field("source_id", pa.string(), nullable=False),
    pa.field("source_row", pa.int32(), nullable=False),
    pa.field("quality_flags", pa.uint16(), nullable=False),
], metadata={b"factorlab_contract": json_bytes(CONTRACT)})


class IntradayDataError(ValueError):
    """Invalid observation, source binding, or immutable publication."""
