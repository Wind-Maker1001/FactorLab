"""Fourteen interpretable synthetic features at their native entity grains."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .adapters import market_context
from .aggregation import aggregate_intraday
from .contracts import ContractError, digest, real_number
from .registry import build_default_registry
from .store import memberships, snapshot_digest


@dataclass
class FeatureResult:
    values: pd.DataFrame
    conservation: pd.DataFrame
    context: dict
    selected_input_sha256: str
    snapshot_id: str
    source_trace: dict


def compute_features(
    store, sessions, request, feature_ids=None, *, include_market_context=True
):
    if type(include_market_context) is not bool:
        raise ContractError("explicit market-context boolean required")
    registry = build_default_registry()
    wanted = (
        tuple(feature_ids)
        if feature_ids is not None
        else tuple(s.feature_id for s in registry.specs if s.implemented)
    )
    if not wanted or len(set(wanted)) != len(wanted):
        raise ContractError("explicit nonempty unique requested features required")
    for name in wanted:
        if not registry.get(name).implemented:
            raise ContractError("CONTRACT_ONLY: " + name)
    declared = {name: registry.get(name).required_inputs for name in wanted}
    needed = {item for inputs in declared.values() for item in inputs}
    known_inputs = {
        "minute_ohlcv",
        "minute_amount",
        "explicit universe",
        "historical industry membership",
        "historical concept membership",
        "taxonomy version",
        "complete source scope",
        "directed confirmed edges",
        "evidence/type/confidence",
        "complete graph scope",
        "versioned limit-state definition",
        "buy/sell/unclassified amounts",
        "vendor definition/unit/currency/availability",
    }
    if needed - known_inputs:
        raise ContractError("unresolved declared feature dependencies")
    needs_minutes = bool(needed & {"minute_ohlcv", "minute_amount"})
    needs_flow = "buy/sell/unclassified amounts" in needed
    needs_graph = "directed confirmed edges" in needed
    aggregated = aggregate_intraday(
        store,
        sessions,
        request,
        include_minutes=needs_minutes,
        include_flow=needs_flow,
        include_prices="minute_ohlcv" in needed,
        include_amounts="minute_amount" in needed,
    )
    bars = aggregated.frame.set_index("security_id")
    valid = bars.reason.eq("OK")
    market_coverage = float(valid.mean())
    amount_valid = bars.amount_reason.eq("OK")
    amount_coverage = float(amount_valid.mean())
    market_amount = float(bars.loc[amount_valid, "amount"].sum())
    used = list(aggregated.records)
    rows, conservations, industry_returns = [], [], {}
    industry_namespaces = set()

    def emit(name, entity, namespace, value, coverage, reason="OK", **extra):
        if name not in wanted:
            return
        if request.observation_end > request.as_of:
            value = np.nan
            reason = "WINDOW_NOT_CLOSED"
            coverage = 0.0
        if not np.isfinite(value):
            value = np.nan
            if reason == "OK":
                reason = "MISSING_INPUT"
        rows.append(
            dict(
                feature_id=name,
                grain=registry.get(name).grain,
                entity_id=entity,
                namespace=namespace,
                value=value,
                reason=reason,
                input_coverage=float(coverage),
                frequency=request.frequency,
                session_id=request.session_id,
                scenario=request.scenario,
                observation_start=request.observation_start.isoformat(),
                observation_end=request.observation_end.isoformat(),
                as_of=request.as_of.isoformat(),
                decision_time=request.decision_time.isoformat(),
                **extra,
            )
        )

    industry_projected = {s: [] for s in request.universe}
    for family in ("industry", "concept"):
        if "historical %s membership" % family not in needed:
            continue
        members, complete, sources = memberships(store, family, request)
        used.extend(sources)
        scope_records = [r for r in sources if r.kind == "scope"]
        for scope in scope_records:
            namespace = scope.payload["namespace"]
            if family == "industry":
                industry_namespaces.add(namespace)
            local = members[members.namespace == namespace]
            expected_groups = tuple(scope.payload.get("group_ids", ()))
            if not expected_groups or set(local.group_id) - set(expected_groups):
                raise ContractError("explicit taxonomy group scope required")
            if not local.empty and len(set(local.taxonomy_version)) != 1:
                raise ContractError("mixed taxonomy versions")
            mapped = set(local.security_id)
            allocated = 0.0
            for group_id in expected_groups:
                group = local[local.group_id == group_id]
                good = group[group.security_id.isin(bars.index[valid])]
                weights = good.weight.astype(float)
                total_weight = float(group.weight.astype(float).sum())
                coverage = float(weights.sum() / total_weight) if total_weight else 0.0
                amount_good = group[group.security_id.isin(bars.index[amount_valid])]
                amount_weights = amount_good.weight.astype(float)
                group_amount_coverage = (
                    float(amount_weights.sum() / total_weight) if total_weight else 0.0
                )
                amount = float(
                    sum(
                        float(w) * bars.at[s, "amount"]
                        for s, w in zip(amount_good.security_id, amount_weights)
                    )
                )
                allocated += amount
                eligible = (
                    len(good) >= 2
                    and coverage >= 0.8
                    and complete.get(namespace, False)
                )
                reason = (
                    "OK"
                    if eligible
                    else (
                        "INCOMPLETE_SOURCE_SCOPE"
                        if not complete.get(namespace, False)
                        else "INSUFFICIENT_GROUP_COVERAGE"
                    )
                )
                ret = (
                    float(
                        sum(
                            float(w) * bars.at[s, "return_value"]
                            for s, w in zip(good.security_id, weights)
                        )
                        / weights.sum()
                    )
                    if eligible
                    else np.nan
                )
                if family == "industry":
                    key = (namespace, group_id)
                    industry_returns[key] = ret
                    emit("IND_RET", group_id, namespace, ret, coverage, reason)
                    share = (
                        amount / market_amount
                        if len(amount_good) >= 2
                        and group_amount_coverage >= 0.8
                        and complete.get(namespace, False)
                        and market_amount > 0
                        and amount_coverage >= 0.8
                        else np.nan
                    )
                    emit(
                        "IND_AMOUNT_SHARE",
                        group_id,
                        namespace,
                        share,
                        min(group_amount_coverage, amount_coverage),
                        (
                            "OK"
                            if np.isfinite(share)
                            else (
                                "INCOMPLETE_SOURCE_SCOPE"
                                if not complete.get(namespace, False)
                                else (
                                    "INSUFFICIENT_GROUP_COVERAGE"
                                    if len(amount_good) < 2
                                    or group_amount_coverage < 0.8
                                    else "ZERO_DENOMINATOR_OR_COVERAGE"
                                )
                            )
                        ),
                    )
                    for s, w in zip(group.security_id, group.weight):
                        industry_projected[s].append((namespace, float(w), ret))
                else:
                    rise = (
                        float(
                            sum(
                                float(w) * (bars.at[s, "return_value"] > 0)
                                for s, w in zip(good.security_id, weights)
                            )
                            / weights.sum()
                        )
                        if eligible
                        else np.nan
                    )
                    emit(
                        "THEME_ADVANCE_RATIO",
                        group_id,
                        namespace,
                        rise,
                        coverage,
                        reason,
                    )
            mapped_amount = float(
                bars.loc[bars.index.isin(mapped) & amount_valid, "amount"].sum()
            )
            unmapped = market_amount - mapped_amount
            if not np.isclose(allocated, mapped_amount, atol=1e-8, rtol=1e-12):
                raise ContractError("industry/concept amount conservation failed")
            if "minute_amount" in needed:
                conservations.append(
                    dict(
                        family=family,
                        namespace=namespace,
                        allocated_amount=allocated,
                        mapped_amount=mapped_amount,
                        unmapped_amount=unmapped,
                        market_amount=market_amount,
                        error=allocated - mapped_amount,
                        frequency=request.frequency,
                        observation_end=request.observation_end.isoformat(),
                        status=(
                            "VERIFIED"
                            if amount_coverage == 1.0
                            else "INCOMPLETE_AMOUNT_COVERAGE"
                        ),
                    )
                )
            if family == "concept":
                for security in request.universe:
                    count = local[local.security_id == security].group_id.nunique()
                    emit(
                        "THEME_MEMBERSHIP_COUNT",
                        security,
                        namespace,
                        float(count) if complete.get(namespace) else np.nan,
                        1.0 if complete.get(namespace) else 0.0,
                        "OK" if complete.get(namespace) else "INCOMPLETE_SOURCE_SCOPE",
                    )
    for namespace in sorted(industry_namespaces):
        for security, seq in industry_projected.items():
            seq = [(w, r) for n, w, r in seq if n == namespace]
            available = (
                bool(seq) and all(np.isfinite(r) for _, r in seq) and valid.at[security]
            )
            relative = (
                bars.at[security, "return_value"] - sum(w * r for w, r in seq)
                if available
                else np.nan
            )
            emit(
                "IND_REL_RET",
                security,
                namespace,
                relative,
                1.0 if available else 0.0,
                "OK" if available else "MISSING_INDUSTRY_INPUT",
            )

    scopes = (
        [
            r
            for r in store.snapshot("scope", request.observation_end, request.as_of)
            if r.payload.get("family") == "supply_chain"
        ]
        if needs_graph
        else []
    )
    graph_identities = set()
    for scope in scopes:
        identity = (
            scope.payload.get("namespace"),
            scope.payload.get("taxonomy_version"),
        )
        if any(not isinstance(value, str) or not value.strip() for value in identity):
            raise ContractError("explicit supply-chain namespace/version required")
        graph_identities.add(identity)
    if len(graph_identities) > 1:
        raise ContractError("ambiguous supply-chain scope")
    graph_namespace, graph_version = (
        next(iter(graph_identities))
        if graph_identities
        else ("UNAVAILABLE_SUPPLY_SCOPE", None)
    )
    needs_upstream = bool(set(wanted) & {"SC_UPSTREAM_COUNT", "SC_UPSTREAM_RET"})
    needs_downstream = "SC_DOWNSTREAM_COUNT" in wanted
    edges = (
        tuple(
            r
            for r in store.snapshot("edge", request.observation_end, request.as_of)
            if (needs_upstream and r.payload["downstream"] in request.universe)
            or (needs_downstream and r.payload["upstream"] in request.universe)
        )
        if needs_graph
        else ()
    )
    used.extend(scopes + list(edges))
    graph_complete = bool(scopes) and all(
        r.payload.get("complete") is True for r in scopes
    )
    qualified = set()
    for edge in edges:
        p = edge.payload
        if any(
            not isinstance(p.get(key), str) or not p[key].strip()
            for key in ("upstream", "downstream")
        ):
            raise ContractError(
                "directed relationship endpoints require nonempty string IDs"
            )
        if "namespace" in p or "taxonomy_version" in p:
            if not p.get("namespace") or not p.get("taxonomy_version"):
                raise ContractError("explicit edge namespace/version required together")
            if (p["namespace"], p["taxonomy_version"]) != (
                graph_namespace,
                graph_version,
            ):
                raise ContractError("edge does not match selected supply-chain scope")
        confidence = real_number(p["confidence"], "relationship confidence", True)
        if confidence > 1 or p["upstream"] == p["downstream"]:
            raise ContractError("invalid directed relationship/confidence")
        if (
            p.get("relation_type") == "SUPPLIES_TO"
            and p.get("evidence_status") == "CONFIRMED_SYNTHETIC"
        ):
            qualified.add((p["upstream"], p["downstream"]))
    for security in request.universe:
        upstream = {a for a, b in qualified if b == security}
        downstream = {b for a, b in qualified if a == security}
        for name, neighbors in (
            ("SC_UPSTREAM_COUNT", upstream),
            ("SC_DOWNSTREAM_COUNT", downstream),
        ):
            emit(
                name,
                security,
                graph_namespace,
                float(len(neighbors)) if graph_complete else np.nan,
                1.0 if graph_complete else 0.0,
                "OK" if graph_complete else "INCOMPLETE_GRAPH_SCOPE",
            )
        good = [s for s in upstream if s in bars.index and valid.at[s]]
        cov = len(good) / len(upstream) if upstream else 0.0
        ret = (
            float(bars.loc[good, "return_value"].mean())
            if graph_complete and good and cov >= 0.8
            else np.nan
        )
        emit(
            "SC_UPSTREAM_RET",
            security,
            graph_namespace,
            ret,
            cov,
            (
                "OK"
                if np.isfinite(ret)
                else (
                    "NO_KNOWN_NEIGHBORS"
                    if not upstream and graph_complete
                    else "INSUFFICIENT_NEIGHBOR_COVERAGE"
                )
            ),
        )
        if needs_flow:
            cash = bars.loc[security]
            flow = (
                (cash.buy - cash.sell) / cash.amount
                if cash.flow_reason == "OK"
                else np.nan
            )
            emit(
                "FLOW_NET_ACTIVE_RATIO",
                security,
                "synthetic_flow_v1",
                flow,
                cash.input_minute_coverage,
                cash.flow_reason,
                unclassified_amount=cash.unclassified,
                unclassified_amount_ratio=cash.unclassified_amount_ratio,
                classification_status=cash.flow_reason,
                observed_amount=cash.amount,
            )

    advance = (
        float((bars.loc[valid, "return_value"] > 0).mean())
        if market_coverage >= 0.8
        else np.nan
    )
    emit(
        "BREADTH_ADVANCE_RATIO",
        "MARKET",
        "synthetic_market",
        advance,
        market_coverage,
        "OK" if np.isfinite(advance) else "INSUFFICIENT_MARKET_COVERAGE",
    )
    known = valid & bars.limit_state.isin(["UP", "DOWN", "NONE"])
    for name, state in (
        ("BREADTH_LIMIT_UP_RATIO", "UP"),
        ("BREADTH_LIMIT_DOWN_RATIO", "DOWN"),
    ):
        cov = float(known.mean())
        ratio = (
            float(bars.loc[known, "limit_state"].eq(state).mean())
            if cov >= 0.8
            else np.nan
        )
        emit(
            name,
            "MARKET",
            "synthetic_market",
            ratio,
            cov,
            "OK" if np.isfinite(ratio) else "UNKNOWN_LIMIT_STATE_OR_COVERAGE",
        )
    hhi = (
        float(((bars.loc[amount_valid, "amount"] / market_amount) ** 2).sum())
        if market_amount > 0 and amount_coverage >= 0.8
        else np.nan
    )
    emit(
        "SENTIMENT_AMOUNT_HHI",
        "MARKET",
        "synthetic_market",
        hhi,
        amount_coverage,
        "OK" if np.isfinite(hhi) else "ZERO_DENOMINATOR_OR_COVERAGE",
    )
    for namespace in sorted(industry_namespaces):
        rs = [r for key, r in industry_returns.items() if key[0] == namespace]
        finite = [r for r in rs if np.isfinite(r)]
        cov = len(finite) / len(rs) if rs else 0.0
        diffusion = (
            float(np.mean(np.array(finite) > 0))
            if len(finite) >= 3 and cov >= 0.8
            else np.nan
        )
        emit(
            "SENTIMENT_INDUSTRY_DIFFUSION",
            "MARKET",
            namespace,
            diffusion,
            cov,
            "OK" if np.isfinite(diffusion) else "INSUFFICIENT_INDUSTRY_COVERAGE",
        )
    context, benchmark_records = (
        market_context(store, sessions, request)
        if include_market_context
        else (dict(status="MARKET_CONTEXT_NOT_REQUESTED"), ())
    )
    selected_hash = snapshot_digest(used)
    context_hash = (
        digest(
            dict(
                benchmark_input_sha256=snapshot_digest(benchmark_records),
                calendar_sha256=context["calendar_sha256"],
            )
        )
        if include_market_context
        else snapshot_digest(())
    )
    identity = digest(
        dict(
            request=request.as_dict(),
            selected_input_sha256=selected_hash,
            feature_ids=wanted,
            context_input_sha256=context_hash,
            include_market_context=include_market_context,
        )
    )
    values = pd.DataFrame(rows)
    if not values.empty:
        values["selected_input_sha256"] = selected_hash
        values["snapshot_id"] = identity
    return FeatureResult(
        values,
        pd.DataFrame(conservations),
        context,
        selected_hash,
        identity,
        dict(
            declared_inputs=declared,
            feature_records=[
                dict(
                    fact_id=r.fact_id, revision=r.revision, kind=r.kind, sha256=r.sha256
                )
                for r in sorted(
                    {r.sha256: r for r in used}.values(),
                    key=lambda r: (r.fact_id, r.revision),
                )
            ],
            context_records=[
                dict(
                    fact_id=r.fact_id, revision=r.revision, kind=r.kind, sha256=r.sha256
                )
                for r in benchmark_records
            ],
            context_input_sha256=context_hash,
            context_calendar_sha256=context.get("calendar_sha256"),
            include_market_context=include_market_context,
        ),
    )
