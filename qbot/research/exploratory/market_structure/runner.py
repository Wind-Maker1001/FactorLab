"""Reproducible offline demo and inspectable aggregate evidence."""

from dataclasses import asdict, replace
import hashlib
import importlib.metadata
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd

from .contracts import BASELINE, MARKERS, SCHEMA, ContractError, digest, json_bytes
from .diagnostics import RedundancyPolicy, diagnose_features, structural_diagnostics
from .features import compute_features
from .registry import MarketStructureRegistry, build_default_registry
from .store import AsOfStore, window_requests
from .synthetic import make_synthetic_data


def frame_hash(frame):
    return hashlib.sha256(
        json_bytes(list(frame.columns))
        + pd.util.hash_pandas_object(frame, index=False).to_numpy(dtype="<u8").tobytes()
    ).hexdigest()


def write_json(path, data):
    Path(path).write_bytes(json_bytes(data))


def verify_receipt(directory):
    """Verify local integrity, not trusted authorship or financial qualification."""
    import json

    directory = Path(directory)
    receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
    if receipt.get("schema") != SCHEMA or any(
        receipt.get(k) != v for k, v in MARKERS.items()
    ):
        raise ContractError("receipt research markers mismatch")
    actual = {p.name for p in directory.iterdir() if p.is_file()} - {"receipt.json"}
    if actual != set(receipt["artifacts"]):
        raise ContractError("receipt artifact inventory mismatch")
    for name, sha in receipt["artifacts"].items():
        if (
            Path(name).name != name
            or hashlib.sha256((directory / name).read_bytes()).hexdigest() != sha
        ):
            raise ContractError("receipt artifact hash mismatch")
    root = Path(__file__).resolve().parents[4]
    sources = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(__file__).parent.glob("*.py"))
    }
    if sources != receipt["source_sha256"]:
        raise ContractError("receipt source hash mismatch")
    return receipt


def run_synthetic(output, frequencies=(5, 15, 30), seed=20261018):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    if (
        not frequencies
        or len(set(frequencies)) != len(frequencies)
        or set(frequencies) - {5, 15, 30}
    ):
        raise ValueError("unique 5/15/30 frequencies required")
    bundle = make_synthetic_data(seed=seed)
    store = AsOfStore(bundle.records())
    registry = build_default_registry()
    policy = RedundancyPolicy()
    values, conservation, contexts, snapshots = [], [], [], []
    for frequency in frequencies:
        for request in window_requests(bundle.sessions, bundle.universe, frequency):
            result = compute_features(store, bundle.context_sessions, request)
            values.append(result.values)
            conservation.append(result.conservation)
            contexts.append(
                dict(
                    frequency=frequency,
                    observation_end=request.observation_end.isoformat(),
                    as_of=request.as_of.isoformat(),
                    decision_time=request.decision_time.isoformat(),
                    **result.context,
                )
            )
            snapshots.append(
                dict(
                    frequency=frequency,
                    observation_end=request.observation_end.isoformat(),
                    as_of=request.as_of.isoformat(),
                    decision_time=request.decision_time.isoformat(),
                    snapshot_id=result.snapshot_id,
                    selected_input_sha256=result.selected_input_sha256,
                    context_input_sha256=result.source_trace["context_input_sha256"],
                    feature_input_record_count=len(
                        result.source_trace["feature_records"]
                    ),
                    context_input_record_count=len(
                        result.source_trace["context_records"]
                    ),
                )
            )
    values = pd.concat(values, ignore_index=True)
    diagnostics = diagnose_features(values, policy)
    diagnostics["conservation"] = pd.concat(conservation, ignore_index=True)
    diagnostics["market_context"] = pd.DataFrame(contexts)
    diagnostics["snapshots"] = pd.DataFrame(snapshots)
    diagnostics["structure"] = structural_diagnostics(registry)
    cash_reports = []
    for (frequency, end), cash in values[
        values.feature_id == "FLOW_NET_ACTIVE_RATIO"
    ].groupby(["frequency", "observation_end"]):
        partition_known = (
            cash.unclassified_amount.notna() & cash.observed_amount.notna()
        )
        amount = float(cash.loc[partition_known, "observed_amount"].sum())
        unclassified = (
            float(cash.loc[partition_known, "unclassified_amount"].sum())
            if partition_known.any()
            else np.nan
        )
        cash_reports.append(
            dict(
                frequency=frequency,
                observation_end=end,
                expected_security_count=len(cash),
                observed_partition_security_count=int(partition_known.sum()),
                known_partition_amount=amount,
                unclassified_amount=unclassified,
                unclassified_amount_ratio=(
                    unclassified / amount if amount > 0 else np.nan
                ),
                incomplete_classification_count=int(
                    cash.classification_status.eq("INCOMPLETE_CLASSIFICATION").sum()
                ),
                missing_or_blocked_count=int((~partition_known).sum()),
            )
        )
    diagnostics["flow_classification"] = pd.DataFrame(cash_reports)
    # Deliberate engineering controls, never additional research candidates.
    control = values[
        (values.feature_id == "SC_UPSTREAM_COUNT")
        & (values.frequency == frequencies[0])
    ].copy()
    pieces = [control]
    for name, multiplier in (
        ("CONTROL_COPY", 1),
        ("CONTROL_SCALED", 2),
        ("CONTROL_CONSTANT", 0),
    ):
        extra = control.copy()
        extra["feature_id"] = name
        extra["value"] *= multiplier
        pieces.append(extra)
    control_results = diagnose_features(pd.concat(pieces, ignore_index=True), policy)
    for key in ("quality", "redundancy", "rejections"):
        diagnostics["control_" + key] = control_results[key]
    original = registry.get("SC_UPSTREAM_COUNT")
    diagnostics["control_structure"] = structural_diagnostics(
        MarketStructureRegistry(
            [original, replace(original, feature_id="CONTROL_SAME_STRUCTURE")]
        )
    )
    states = []
    for spec in registry.specs:
        f = values[values.feature_id == spec.feature_id]
        q = diagnostics["quality"]
        rejected = bool(
            (
                q[q.feature_id == spec.feature_id].quality_status == "QUALITY_REJECTED"
            ).any()
        )
        states.append(
            dict(
                feature_id=spec.feature_id,
                implementation_status=(
                    "IMPLEMENTED_SYNTHETIC" if spec.implemented else "CONTRACT_ONLY"
                ),
                source_status=(
                    "SYNTHETIC_TEST_ONLY"
                    if spec.implemented
                    else "REQUIRED_INPUTS_NOT_CONNECTED"
                ),
                real_source_status="REAL_RESEARCH_BLOCKED",
                computation_status=(
                    "COMPUTED_SYNTHETIC" if len(f) else "NOT_IMPLEMENTED"
                ),
                quality_status=(
                    ("HAS_REJECTION_EVIDENCE" if rejected else "DESCRIBED")
                    if len(f)
                    else "NOT_RUN"
                ),
                output_cells=len(f),
                finite_cells=int(np.isfinite(f.value).sum()) if len(f) else 0,
            )
        )
    diagnostics["contract_status"] = pd.DataFrame(states)
    for name, frame in diagnostics.items():
        frame.to_csv(output / (name + ".csv"), index=False, lineterminator="\n")
    write_json(output / "catalog.json", registry.catalog())
    write_json(output / "policy.json", asdict(policy))
    report = [
        "# 市场结构合成特征诊断",
        "",
        "NO LIVE MONEY。来源全部为 SYNTHETIC_TEST_ONLY。",
        "",
        "本报告是合成软件运行记录，完整验收结论另见 acceptance.json。真实研究状态：REAL_RESEARCH_BLOCKED。",
        "",
        "32 个合同，14 个合成实现，18 个 CONTRACT_ONLY；频率："
        + str(list(frequencies))
        + " 分钟。",
        "",
        "收益定义为窗口 close/open−1。未接入收益标签，不计算 IC、策略收益或预测能力。",
        "",
        "诊断按原生实体、分类范围及频率分开。观察窗口数和真实合成交易日数分别列示；市场单值仅做时间序列描述。",
        "",
        "主动资金分类不完整时净比例缺失，另报告未分类金额和占比。行业分摊按体系守恒，未映射金额单列。",
        "",
        "control_* 为刻意构造的工程诊断控制，不属于 32 个候选。拒绝证据不删除合同。",
        "",
        "Python 3.9 验收仅限源码入口；find_packages 可能遗漏命名空间，安装包未验收。",
        "",
        "## 各频率有限值与拒绝证据",
        "",
    ]
    for frequency, f in values.groupby("frequency"):
        report.append(
            "- %d 分钟：%d 个原生实体单元，%d 个有限值。"
            % (frequency, len(f), np.isfinite(f.value).sum())
        )
    report += ["", "## 全部合同状态", "", "|合同|实现|计算|质量|", "|---|---|---|---|"]
    for state in states:
        report.append(
            "|{feature_id}|{implementation_status}|{computation_status}|{quality_status}|".format(
                **state
            )
        )
    (output / "summary.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    root = Path(__file__).resolve().parents[4]
    sources = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(__file__).parent.glob("*.py"))
    }
    receipt = dict(
        schema=SCHEMA,
        baseline=BASELINE,
        **MARKERS,
        software_status="SYNTHETIC_DEMO_COMPLETED",
        real_research_status="REAL_RESEARCH_BLOCKED",
        seed=seed,
        frequencies=list(frequencies),
        candidate_count=32,
        implemented_count=14,
        output_value_sha256=frame_hash(values),
        numeric_value_sha256=frame_hash(
            values.drop(columns=["selected_input_sha256", "snapshot_id"])
        ),
        evidence_revision="MARKET_STRUCTURE_CORRECTIONS_V4",
        input_sha256=digest(sorted(r.sha256 for r in bundle.records())),
        source_sha256=sources,
        catalog_sha256=digest(registry.catalog()),
        python=sys.version,
        tested_commit=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        dependencies={
            p: importlib.metadata.version(p)
            for p in ("numpy", "pandas", "pytest", "coverage")
        },
        artifacts={
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(output.iterdir())
            if p.is_file()
        },
    )
    write_json(output / "receipt.json", receipt)
    return receipt, values
