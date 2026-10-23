"""Curated contracts, never an extension of the production factor registry."""

from .contracts import ContractError, FeatureSpec


class MarketStructureRegistry:
    def __init__(self, specs):
        self.specs = tuple(specs)
        self._by_id = {s.feature_id: s for s in self.specs}
        if len(self._by_id) != len(self.specs):
            raise ContractError("duplicate feature ID")

    def get(self, feature_id):
        try:
            return self._by_id[feature_id]
        except KeyError as exc:
            raise ContractError("unknown market structure feature") from exc

    def catalog(self):
        return [s.as_dict() for s in self.specs]


def build_default_registry():
    # G is the historical membership-weighted mean; return=window close/open-1.
    rows = [
        ("IND_RET", "industry", "industry", "G(return)", True),
        (
            "IND_REL_RET",
            "industry",
            "security",
            "return-weighted_mean(affiliated industry returns; includes self)",
            True,
        ),
        (
            "IND_AMOUNT_SHARE",
            "industry",
            "industry",
            "sum(weight*amount)/market_amount",
            True,
        ),
        (
            "IND_RET_DISPERSION",
            "industry",
            "industry",
            "sqrt(G((return-G(return))**2))",
            False,
        ),
        (
            "IND_MEMBERSHIP_CHURN",
            "industry",
            "industry",
            "1-Jaccard(current,previous-window members)",
            False,
        ),
        ("THEME_ADVANCE_RATIO", "concept", "concept", "G(return>0)", True),
        (
            "THEME_MEMBERSHIP_COUNT",
            "concept",
            "security",
            "count(distinct active themes)",
            True,
        ),
        ("THEME_RET", "concept", "concept", "G(return)", False),
        (
            "THEME_OVERLAP",
            "concept",
            "security",
            "mean(pairwise Jaccard of affiliated theme member sets); k<2 missing",
            False,
        ),
        (
            "THEME_MEMBERSHIP_CHURN",
            "concept",
            "concept",
            "1-Jaccard(current,previous-window members)",
            False,
        ),
        (
            "SC_UPSTREAM_COUNT",
            "supply_chain",
            "security",
            "count(distinct confirmed suppliers)",
            True,
        ),
        (
            "SC_DOWNSTREAM_COUNT",
            "supply_chain",
            "security",
            "count(distinct confirmed customers)",
            True,
        ),
        (
            "SC_UPSTREAM_RET",
            "supply_chain",
            "security",
            "equal_mean(distinct confirmed suppliers' window returns)",
            True,
        ),
        (
            "SC_DOWNSTREAM_RET",
            "supply_chain",
            "security",
            "equal_mean(distinct confirmed customers' window returns)",
            False,
        ),
        (
            "SC_NEIGHBOR_RET_DISPERSION",
            "supply_chain",
            "security",
            "population_std(distinct suppliers/customers' returns)",
            False,
        ),
        (
            "SC_RELATION_CHURN",
            "supply_chain",
            "security",
            "1-Jaccard(current,previous-window directed neighbor sets)",
            False,
        ),
        (
            "BREADTH_ADVANCE_RATIO",
            "breadth",
            "market",
            "count(return>0)/count(valid return)",
            True,
        ),
        (
            "BREADTH_LIMIT_UP_RATIO",
            "breadth",
            "market",
            "count(window-end limit_state=UP)/count(known limit_state)",
            True,
        ),
        (
            "BREADTH_LIMIT_DOWN_RATIO",
            "breadth",
            "market",
            "count(window-end limit_state=DOWN)/count(known limit_state)",
            True,
        ),
        (
            "BREADTH_ABOVE_MA20",
            "breadth",
            "market",
            "share(close>mean(previous 20 completed session closes))",
            False,
        ),
        (
            "BREADTH_NEW_HIGH20",
            "breadth",
            "market",
            "share(close>max(previous 20 completed session closes))",
            False,
        ),
        (
            "FLOW_NET_ACTIVE_RATIO",
            "flow",
            "security",
            "(aggressor_buy-aggressor_sell)/amount only if unclassified=0",
            True,
        ),
        (
            "FLOW_INDUSTRY_NET_RATIO",
            "flow",
            "industry",
            "sum(weight*(buy-sell))/industry_amount",
            False,
        ),
        (
            "FLOW_MARKET_NET_RATIO",
            "flow",
            "market",
            "sum(buy-sell)/market_amount",
            False,
        ),
        (
            "FLOW_SIGN_PERSISTENCE5",
            "flow",
            "security",
            "mean(sign(net active amount),last 5 consecutive completed windows)",
            False,
        ),
        (
            "FLOW_DEFINED_LARGE_ORDER_RATIO",
            "flow",
            "security",
            "net active amount of single trades>=1000000 CNY/total amount",
            False,
        ),
        (
            "SENTIMENT_AMOUNT_HHI",
            "sentiment",
            "market",
            "sum((amount/market_amount)**2)",
            True,
        ),
        (
            "SENTIMENT_INDUSTRY_DIFFUSION",
            "sentiment",
            "market",
            "count(valid industry return>0)/count(valid industry)",
            True,
        ),
        (
            "SENTIMENT_TOP5_AMOUNT_SHARE",
            "sentiment",
            "market",
            "sum(largest min(5,N) security amounts)/market_amount",
            False,
        ),
        (
            "SENTIMENT_THEME_DIFFUSION",
            "sentiment",
            "market",
            "count(valid theme return>0)/count(valid theme)",
            False,
        ),
        (
            "SENTIMENT_RETURN_SIGN_ENTROPY",
            "sentiment",
            "market",
            "-sum(p*log(p),positive/flat/negative,0log0=0)/log(3)",
            False,
        ),
        (
            "SENTIMENT_ADVANCING_AMOUNT_SHARE",
            "sentiment",
            "market",
            "sum(amount where return>0)/sum(amount with valid return)",
            False,
        ),
    ]
    inputs = {
        "industry": (
            "minute_ohlcv",
            "historical industry membership",
            "taxonomy version",
            "complete source scope",
        ),
        "concept": (
            "minute_ohlcv",
            "historical concept membership",
            "taxonomy version",
            "complete source scope",
        ),
        "supply_chain": (
            "minute_ohlcv",
            "directed confirmed edges",
            "evidence/type/confidence",
            "complete graph scope",
        ),
        "breadth": (
            "minute_ohlcv",
            "explicit universe",
            "versioned limit-state definition",
            "completed session history for MA20/newhigh20",
        ),
        "flow": (
            "minute_amount",
            "buy/sell/unclassified amounts",
            "vendor definition/unit/currency/availability",
            "trade sizes for large-order contract",
        ),
        "sentiment": (
            "minute_ohlcv",
            "explicit universe",
            "historical classifications for diffusion",
        ),
    }
    # MVP contracts name actual dependencies rather than every possible input in
    # their family. Contract-only research candidates retain the broader inputs.
    implemented_inputs = {
        "IND_AMOUNT_SHARE": ("minute_amount",) + inputs["industry"][1:],
        "THEME_MEMBERSHIP_COUNT": inputs["concept"][1:],
        "SC_UPSTREAM_COUNT": inputs["supply_chain"][1:],
        "SC_DOWNSTREAM_COUNT": inputs["supply_chain"][1:],
        "BREADTH_ADVANCE_RATIO": inputs["breadth"][:2],
        "BREADTH_LIMIT_UP_RATIO": inputs["breadth"][:3],
        "BREADTH_LIMIT_DOWN_RATIO": inputs["breadth"][:3],
        "FLOW_NET_ACTIVE_RATIO": inputs["flow"][:3],
        "SENTIMENT_AMOUNT_HHI": ("minute_amount", "explicit universe"),
        "SENTIMENT_INDUSTRY_DIFFUSION": inputs["industry"] + ("explicit universe",),
    }
    specs = []
    for name, family, grain, formula, implemented in rows:
        lookback = (
            "20 previous completed sessions"
            if name.endswith(("MA20", "HIGH20"))
            else (
                "5 consecutive completed windows"
                if name.endswith("PERSISTENCE5")
                else (
                    "current and previous scheduled window with own as_of"
                    if "CHURN" in name
                    else "one observation window"
                )
            )
        )
        specs.append(
            FeatureSpec(
                name,
                family,
                grain,
                formula,
                implemented_inputs.get(name, inputs[family]),
                implemented,
                unit="COUNT" if name.endswith("COUNT") else "RATIO",
                lookback=lookback,
                definition_version=(
                    "v4-inputs"
                    if name == "IND_AMOUNT_SHARE"
                    else "v2-inputs" if name in implemented_inputs else "v1"
                ),
            )
        )
    return MarketStructureRegistry(specs)
