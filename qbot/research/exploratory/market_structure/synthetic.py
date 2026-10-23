"""Deterministic invented observations; no exchange calendar or data services."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .contracts import FLOW_DEFINITION, Record
from .store import Session


@dataclass(frozen=True)
class SyntheticSource:
    source_records: tuple
    sessions: tuple
    context_sessions: tuple
    universe: tuple
    seed: int

    def records(self):
        return self.source_records


def make_record(
    fact_id,
    kind,
    payload,
    known,
    *,
    revision=1,
    valid_from=None,
    valid_to=None,
    observation_end=None,
    **kwargs
):
    return Record(
        fact_id,
        revision,
        kind,
        payload,
        valid_from or known,
        known,
        known,
        known,
        ("SYNTHETIC_TEST_ONLY:" + fact_id,),
        valid_to=valid_to,
        observation_end=observation_end,
        **kwargs
    )


def make_synthetic_data(seed=20261018, stock_count=24, session_count=6):
    if stock_count < 4 or session_count < 1:
        raise ValueError("at least four securities and one session required")
    rng = np.random.RandomState(seed)
    universe = tuple("SYNTH_%03d" % i for i in range(stock_count))
    days = pd.bdate_range("2021-01-04", periods=session_count)
    history = pd.bdate_range(end=days[0] - pd.Timedelta(days=1), periods=80)

    def session(day):
        local = day.tz_localize("Asia/Shanghai")
        return Session(
            str(day.date()),
            (
                (
                    local + pd.Timedelta(hours=9, minutes=30),
                    local + pd.Timedelta(hours=11, minutes=30),
                ),
                (local + pd.Timedelta(hours=13), local + pd.Timedelta(hours=15)),
            ),
        )

    sessions = tuple(session(d) for d in days)
    context_sessions = tuple(session(d) for d in history) + sessions
    origin = context_sessions[0].segments[0][0] - pd.Timedelta(days=1)
    records = [
        make_record(
            "flow-definition",
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
    for family, namespace, groups in (
        ("industry", "SYN_IND_V1_L1", ["I%d" % i for i in range(4)]),
        ("concept", "SYN_THEME_V1", ["C%d" % i for i in range(6)]),
        ("supply_chain", "synthetic_supply", []),
    ):
        records.append(
            make_record(
                "scope-" + family,
                "scope",
                dict(
                    family=family,
                    namespace=namespace,
                    complete=True,
                    group_ids=groups,
                    taxonomy_version="v1",
                ),
                origin,
            )
        )
    change = sessions[min(2, len(sessions) - 1)].segments[0][0]
    for i, security in enumerate(universe):
        industries = [("I%d" % (i % 4), 1.0)] if i != 0 else [("I0", 0.5), ("I1", 0.5)]
        for j, (group, weight) in enumerate(industries):
            payload = dict(
                family="industry",
                security_id=security,
                namespace="SYN_IND_V1_L1",
                group_id=group,
                weight=weight,
                taxonomy_version="v1",
                change_date=str(origin.date()),
            )
            fact = "industry-%s-%d" % (security, j)
            records.append(make_record(fact, "membership", payload, origin))
            if i == 1 and session_count > 2:
                records.append(
                    make_record(
                        fact,
                        "membership",
                        payload,
                        change,
                        revision=2,
                        valid_from=origin,
                        valid_to=change,
                    )
                )
                records.append(
                    make_record(
                        fact + "-switch",
                        "membership",
                        dict(payload, group_id="I2", change_date=str(change.date())),
                        change,
                    )
                )
        for j, group in enumerate(("C%d" % (i % 6), "C%d" % ((i + 1) % 6))):
            payload = dict(
                family="concept",
                security_id=security,
                namespace="SYN_THEME_V1",
                group_id=group,
                weight=0.5,
                taxonomy_version="v1",
                change_date=str(origin.date()),
            )
            fact = "theme-%s-%d" % (security, j)
            records.append(make_record(fact, "membership", payload, origin))
            if i == 2 and session_count > 2:
                if j == 0:
                    records.append(
                        make_record(
                            fact,
                            "membership",
                            dict(payload, weight=1.0, change_date=str(change.date())),
                            change,
                            revision=2,
                            valid_from=change,
                        )
                    )
                else:
                    records.append(
                        make_record(
                            fact,
                            "membership",
                            payload,
                            change,
                            revision=2,
                            valid_from=origin,
                            valid_to=change,
                        )
                    )
        edge = dict(
            upstream=security,
            downstream=universe[(i + 1) % stock_count],
            relation_type="SUPPLIES_TO",
            confidence=0.85,
            evidence_status="CONFIRMED_SYNTHETIC",
        )
        records.append(make_record("edge-%d" % i, "edge", edge, origin))
        if i % 3 == 0:
            records.append(
                make_record(
                    "extra-edge-%d" % i,
                    "edge",
                    dict(edge, downstream=universe[(i + 3) % stock_count]),
                    origin,
                )
            )
        if i == 0 and session_count > 2:
            records.append(
                make_record(
                    "edge-0",
                    "edge",
                    edge,
                    change,
                    revision=2,
                    valid_from=origin,
                    withdrawn=True,
                )
            )
            records.append(
                make_record(
                    "edge-switch", "edge", dict(edge, downstream=universe[2]), change
                )
            )
    records.append(
        make_record(
            "inferred-edge",
            "edge",
            dict(
                upstream=universe[0],
                downstream=universe[-1],
                relation_type="SUPPLIES_TO",
                confidence=0.99,
                evidence_status="INFERRED",
            ),
            origin,
        )
    )
    for day_number, s in enumerate(context_sessions):
        end = s.segments[-1][1]
        records.append(
            make_record(
                "benchmark-" + s.session_id,
                "benchmark",
                dict(
                    session_id=s.session_id,
                    complete=True,
                    adj_close=float(
                        5000
                        * np.exp(day_number * 0.001 + 0.02 * np.sin(day_number / 7))
                    ),
                ),
                end + pd.Timedelta(minutes=1),
                valid_from=s.segments[0][0],
                observation_end=end,
            )
        )
    for day_number, s in enumerate(sessions):
        minutes = [
            t
            for a, b in s.segments
            for t in pd.date_range(a, b, freq="min", inclusive="left")
        ]
        for i, security in enumerate(universe):
            opening = (10 + i) * (1 + day_number * 0.002)
            for n, start in enumerate(minutes):
                ret = float(0.0004 * np.sin(n / 7 + i) + rng.normal(0, 0.0007))
                closing = opening * (1 + ret)
                volume = float(rng.randint(100, 2000) * (1 + i % 5))
                amount = float(volume * (opening + closing) / 2)
                fraction = float(0.5 + 0.35 * np.sin(n / 9 + i / 3))
                unknown = (
                    amount * 0.2 if i == 2 and day_number == 1 and 45 <= n < 50 else 0.0
                )
                payload = dict(
                    security_id=security,
                    open=float(opening),
                    close=float(closing),
                    high=float(max(opening, closing) * 1.0001),
                    low=float(min(opening, closing) * 0.9999),
                    volume=volume,
                    amount=amount,
                    unit="CNY",
                    price_basis="SYNTHETIC_RAW",
                    buy=(amount - unknown) * fraction,
                    sell=(amount - unknown) * (1 - fraction),
                    unclassified=unknown,
                    flow_definition=FLOW_DEFINITION,
                    flow_unit="CNY",
                    limit_definition="SYNTHETIC_LIMIT_STATE_V1",
                    limit_state=(
                        "UP"
                        if (n + i * 3) % 71 < 2
                        else "DOWN" if (n + i) % 89 < 2 else "NONE"
                    ),
                )
                end = start + pd.Timedelta(minutes=1)
                known = end + pd.Timedelta(seconds=1)
                if i == 0 and day_number == 1 and n == 15:
                    known += pd.Timedelta(minutes=10)
                records.append(
                    make_record(
                        "minute-%s-%s-%d" % (s.session_id, security, n),
                        "minute",
                        payload,
                        known,
                        valid_from=start,
                        observation_end=end,
                    )
                )
                opening = closing
    return SyntheticSource(tuple(records), sessions, context_sessions, universe, seed)
