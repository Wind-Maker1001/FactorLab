"""Only synthetic hand-calculable inputs; no data provider imports."""

from dataclasses import dataclass

import pandas as pd
import pytest

from qbot.research.exploratory.market_structure.contracts import (
    ComputeRequest,
    FLOW_DEFINITION,
)
from qbot.research.exploratory.market_structure.store import AsOfStore, Session
from qbot.research.exploratory.market_structure.synthetic import make_record


@dataclass
class Manual:
    records: list
    sessions: tuple
    universe: tuple
    request: ComputeRequest

    def store(self):
        return AsOfStore(self.records)


@pytest.fixture
def manual():
    start = pd.Timestamp("2021-01-04T09:30:00+08:00")
    end = start + pd.Timedelta(minutes=5)
    origin = start - pd.Timedelta(days=1)
    securities = ("A", "B", "C", "D")
    sessions = (
        Session(
            "2021-01-04",
            (
                (start, start + pd.Timedelta(minutes=120)),
                (start + pd.Timedelta(minutes=210), start + pd.Timedelta(minutes=330)),
            ),
        ),
    )
    records = [
        make_record(
            "def",
            "source_definition",
            dict(
                definition_id=FLOW_DEFINITION,
                vendor="SYNTHETIC_TEST_ONLY",
                unit="CNY",
                currency="CNY",
            ),
            origin,
        )
    ]
    for family, groups in (
        ("industry", ["I0", "I1", "I2"]),
        ("concept", ["C0", "C1"]),
        ("supply_chain", []),
    ):
        records.append(
            make_record(
                "scope-" + family,
                "scope",
                dict(
                    family=family,
                    namespace=family,
                    complete=True,
                    taxonomy_version="v1",
                    group_ids=groups,
                ),
                origin,
            )
        )
    mapping = {
        "A": [("I0", 0.5), ("I1", 0.5)],
        "B": [("I0", 0.5), ("I2", 0.5)],
        "C": [("I1", 0.5), ("I2", 0.5)],
        "D": [("I2", 1.0)],
    }
    returns = (0.1, -0.1, 0.2, 0.0)
    for i, security in enumerate(securities):
        for family, groups in (
            ("industry", mapping[security]),
            ("concept", [("C0", 0.5), ("C1", 0.5)]),
        ):
            for group, weight in groups:
                records.append(
                    make_record(
                        "%s-%s-%s" % (family, security, group),
                        "membership",
                        dict(
                            family=family,
                            namespace=family,
                            security_id=security,
                            group_id=group,
                            weight=weight,
                            taxonomy_version="v1",
                            change_date="2021-01-03",
                        ),
                        origin,
                    )
                )
        for n in range(5):
            a = start + pd.Timedelta(minutes=n)
            b = a + pd.Timedelta(minutes=1)
            close = 10 * (1 + returns[i]) if n == 4 else 10.0
            amount = float((i + 1) * 100)
            p = dict(
                security_id=security,
                open=10.0,
                high=max(10.0, close),
                low=min(10.0, close),
                close=close,
                amount=amount,
                volume=10.0,
                unit="CNY",
                price_basis="SYNTHETIC_RAW",
                buy=amount * 0.6,
                sell=amount * 0.4,
                unclassified=0.0,
                flow_definition=FLOW_DEFINITION,
                flow_unit="CNY",
                limit_definition="SYNTHETIC_LIMIT_STATE_V1",
                limit_state=("UP", "DOWN", "NONE", "NONE")[i],
            )
            records.append(
                make_record(
                    "minute-%s-%d" % (security, n),
                    "minute",
                    p,
                    b + pd.Timedelta(seconds=1),
                    valid_from=a,
                    observation_end=b,
                )
            )
    for i, (a, b, status) in enumerate(
        (
            ("A", "B", "CONFIRMED_SYNTHETIC"),
            ("C", "B", "CONFIRMED_SYNTHETIC"),
            ("B", "D", "CONFIRMED_SYNTHETIC"),
            ("D", "A", "INFERRED"),
            ("A", "B", "CONFIRMED_SYNTHETIC"),
        )
    ):
        records.append(
            make_record(
                "edge-%d" % i,
                "edge",
                dict(
                    upstream=a,
                    downstream=b,
                    relation_type="SUPPLIES_TO",
                    evidence_status=status,
                    confidence=0.9,
                ),
                origin,
            )
        )
    request = ComputeRequest(
        start,
        end,
        end + pd.Timedelta(seconds=2),
        end + pd.Timedelta(seconds=2),
        5,
        securities,
        sessions[0].session_id,
    )
    return Manual(records, sessions, securities, request)
