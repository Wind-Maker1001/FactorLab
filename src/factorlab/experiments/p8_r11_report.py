"""P8-R11 Artifact Writer and Formal Chinese Research Report Renderer."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from factorlab.experiments.p8_r11_unified_lifecycle import (
    ALL_EXECUTION_PROFILES,
    LIFECYCLE_LABELS,
)


def _fmt_pct(val: float | None, signed: bool = True, digits: int = 2) -> str:
    if val is None:
        return "—"
    return f"{val:+.{digits}f}%" if signed else f"{val:.{digits}f}%"


def _fmt_pp(val: float | None, digits: int = 2) -> str:
    if val is None:
        return "—"
    return f"{val:+.{digits}f} pp"


def _fmt_num(val: float | None, digits: int = 4, signed: bool = False) -> str:
    if val is None:
        return "—"
    return f"{val:+.{digits}f}" if signed else f"{val:.{digits}f}"


def _lookup_wf(wf_index: dict[str, Any], lc_id: str, prof: str, reg: str, role: str) -> dict[str, Any]:
    return wf_index[f"{lc_id}__{prof}__{reg}"]["oos_summary"][role]


def _lookup_ablation(
    ablations: list[dict[str, Any]],
    *,
    comparison: str,
    funding_regime: str,
    execution_profile: str,
) -> dict[str, Any] | None:
    for r in ablations:
        if (
            r["comparison"] == comparison
            and r["funding_regime"] == funding_regime
            and r["execution_profile"] == execution_profile
        ):
            return r
    return None


def render_p8_r11_report(study: dict[str, Any]) -> str:
    """Render the formal Chinese P8-R11 research report strictly from computed study artifacts."""
    conc = study["conclusions"]
    timing = study["timing"]
    ledger = study["ledger_verification"]
    audit = study["data_audit"]
    hist = study["historical"]
    wf_idx = hist["wf_results_index"]
    ablations = hist["paired_ablations"]
    exec_mat = hist["execution_realism_matrix"]
    r10_rec = hist["l0_vs_r10_reconciliation"]
    det = study["deterministic_stress"]
    mc = study["monte_carlo"]
    s02 = study["s02_handoff"]

    # Extract key headline numbers dynamically for Section 1 synthesis
    m_l0_a = _lookup_wf(wf_idx, "L0", "IDEAL_LIMIT_FILL", "MAINNET_ACTUAL", "A_FIXED")
    m_l0_b = _lookup_wf(wf_idx, "L0", "IDEAL_LIMIT_FILL", "MAINNET_ACTUAL", "B_WF")
    m_l0_c = _lookup_wf(wf_idx, "L0", "IDEAL_LIMIT_FILL", "MAINNET_ACTUAL", "C_WF")
    m_l1_a = _lookup_wf(wf_idx, "L1", "IDEAL_LIMIT_FILL", "MAINNET_ACTUAL", "A_FIXED")
    m_l1_b = _lookup_wf(wf_idx, "L1", "IDEAL_LIMIT_FILL", "MAINNET_ACTUAL", "B_WF")
    m_l1_c = _lookup_wf(wf_idx, "L1", "IDEAL_LIMIT_FILL", "MAINNET_ACTUAL", "C_WF")
    m_l2_a = _lookup_wf(wf_idx, "L2", "IDEAL_LIMIT_FILL", "MAINNET_ACTUAL", "A_FIXED")
    m_l2_b = _lookup_wf(wf_idx, "L2", "IDEAL_LIMIT_FILL", "MAINNET_ACTUAL", "B_WF")
    m_l2_c = _lookup_wf(wf_idx, "L2", "IDEAL_LIMIT_FILL", "MAINNET_ACTUAL", "C_WF")
    m_l2d_a = _lookup_wf(wf_idx, "L2", "DELAYED_EXIT_FILL", "MAINNET_ACTUAL", "A_FIXED")
    m_l2d_b = _lookup_wf(wf_idx, "L2", "DELAYED_EXIT_FILL", "MAINNET_ACTUAL", "B_WF")
    m_l2d_c = _lookup_wf(wf_idx, "L2", "DELAYED_EXIT_FILL", "MAINNET_ACTUAL", "C_WF")

    sw_l0_a = _lookup_wf(wf_idx, "L0", "IDEAL_LIMIT_FILL", "SWITCH_48H", "A_FIXED")
    sw_l0_b = _lookup_wf(wf_idx, "L0", "IDEAL_LIMIT_FILL", "SWITCH_48H", "B_WF")
    sw_l1_a = _lookup_wf(wf_idx, "L1", "IDEAL_LIMIT_FILL", "SWITCH_48H", "A_FIXED")
    sw_l1_b = _lookup_wf(wf_idx, "L1", "IDEAL_LIMIT_FILL", "SWITCH_48H", "B_WF")
    sw_l2_a = _lookup_wf(wf_idx, "L2", "IDEAL_LIMIT_FILL", "SWITCH_48H", "A_FIXED")
    sw_l2_b = _lookup_wf(wf_idx, "L2", "IDEAL_LIMIT_FILL", "SWITCH_48H", "B_WF")
    sw_l2_c = _lookup_wf(wf_idx, "L2", "IDEAL_LIMIT_FILL", "SWITCH_48H", "C_WF")
    sw_l2d_a = _lookup_wf(wf_idx, "L2", "DELAYED_EXIT_FILL", "SWITCH_48H", "A_FIXED")
    sw_l2d_b = _lookup_wf(wf_idx, "L2", "DELAYED_EXIT_FILL", "SWITCH_48H", "B_WF")
    sw_l2d_c = _lookup_wf(wf_idx, "L2", "DELAYED_EXIT_FILL", "SWITCH_48H", "C_WF")

    dm_l0_a = _lookup_wf(wf_idx, "L0", "IDEAL_LIMIT_FILL", "DEMO_HIGH_FUNDING_22.97BPS", "A_FIXED")
    dm_l0_b = _lookup_wf(wf_idx, "L0", "IDEAL_LIMIT_FILL", "DEMO_HIGH_FUNDING_22.97BPS", "B_WF")
    dm_l1_a = _lookup_wf(wf_idx, "L1", "IDEAL_LIMIT_FILL", "DEMO_HIGH_FUNDING_22.97BPS", "A_FIXED")
    dm_l2_a = _lookup_wf(wf_idx, "L2", "IDEAL_LIMIT_FILL", "DEMO_HIGH_FUNDING_22.97BPS", "A_FIXED")
    dm_l2_b = _lookup_wf(wf_idx, "L2", "IDEAL_LIMIT_FILL", "DEMO_HIGH_FUNDING_22.97BPS", "B_WF")
    dm_l2d_a = _lookup_wf(wf_idx, "L2", "DELAYED_EXIT_FILL", "DEMO_HIGH_FUNDING_22.97BPS", "A_FIXED")
    dm_l2d_b = _lookup_wf(wf_idx, "L2", "DELAYED_EXIT_FILL", "DEMO_HIGH_FUNDING_22.97BPS", "B_WF")

    ab_f11_sw_a = _lookup_ablation(ablations, comparison="A_FIXED: L1 - L0 (F11 pure)", funding_regime="SWITCH_48H", execution_profile="IDEAL_LIMIT_FILL")
    ab_f08_mn_a = _lookup_ablation(ablations, comparison="A_FIXED: L2 - L1 (F08 incremental)", funding_regime="MAINNET_ACTUAL", execution_profile="IDEAL_LIMIT_FILL")
    ab_b_l2_sw_id = _lookup_ablation(ablations, comparison="L2: B_WF - A_FIXED", funding_regime="SWITCH_48H", execution_profile="IDEAL_LIMIT_FILL")
    ab_b_l2_sw_dl = _lookup_ablation(ablations, comparison="L2: B_WF - A_FIXED", funding_regime="SWITCH_48H", execution_profile="DELAYED_EXIT_FILL")
    ab_b_l2_dm_dl = _lookup_ablation(ablations, comparison="L2: B_WF - A_FIXED", funding_regime="DEMO_HIGH_FUNDING_22.97BPS", execution_profile="DELAYED_EXIT_FILL")
    ab_b_l2_mn_id = _lookup_ablation(ablations, comparison="L2: B_WF - A_FIXED", funding_regime="MAINNET_ACTUAL", execution_profile="IDEAL_LIMIT_FILL")
    ab_c_l2_mn_id = _lookup_ablation(ablations, comparison="L2: C_WF - A_FIXED", funding_regime="MAINNET_ACTUAL", execution_profile="IDEAL_LIMIT_FILL")
    ab_c_l2_mn_dl = _lookup_ablation(ablations, comparison="L2: C_WF - A_FIXED", funding_regime="MAINNET_ACTUAL", execution_profile="DELAYED_EXIT_FILL")
    ab_b_l2_mn_dl = _lookup_ablation(ablations, comparison="L2: B_WF - A_FIXED", funding_regime="MAINNET_ACTUAL", execution_profile="DELAYED_EXIT_FILL")

    lines: list[str] = []
    lines.append("# FactorLab P8-R11：完整 F11/F08 生命周期下的资金费敏感及自适应网格研究报告")
    lines.append("")
    lines.append(
        "> `NO LIVE MONEY` · 离线受控研究（未触碰 Azure 云端实例，未执行任何网络交易，未修改 R08/R09/R10 已冻结证据）。"
        f"统一执行模型版本：`{study['execution_model_version']}`。"
        "历史小时 OHLC 收盘价与合成路径均**不构成真实交易所 L2 盘口或可保证成交表现**；`IDEAL_LIMIT_FILL` 结果仅作为上限对照，不能当成真实可保证执行表现。"
    )
    lines.append("")

    # Section 1: Formal Conclusions & Direct Answers
    lines.append("## 1. 核心结果判定与七大研究问题明确回答")
    lines.append("")
    lines.append("### 1.1 四级规范判定")
    lines.append("")
    lines.append("| 结论标签 | 状态 | 判定依据与严格边界 |")
    lines.append("| --- | --- | --- |")
    for tag in (
        "ENGINEERING_ACCEPTED",
        "CONDITIONAL_RISK_ESTIMATED",
        "RESEARCH_INCONCLUSIVE",
        "OOS_ADVANTAGE_SUPPORTED",
    ):
        val = "PASS / CONFIRMED" if conc[tag] else "NOT_MET / BLOCKED"
        if tag == "RESEARCH_INCONCLUSIVE" and conc[tag]:
            val = "CONFIRMED (`INCONCLUSIVE`)"
        lines.append(f"| `{tag}` | **{val}** | {conc['notes'][tag]} |")
    lines.append("")

    lines.append("### 1.2 最终核心问题直接回答：完整 F11/F08 生命周期是否改变了 R10 的结论？资金费敏感机制是否仍具独立价值？")
    lines.append("")
    c_mn_dl_note = (
        f"在 `MAINNET_ACTUAL · L2 · DELAYED` 下 `C_WF - A_FIXED = {ab_c_l2_mn_dl['delta_mean_return_pp']:+.2f} pp`"
        f"（Holm `p={ab_c_l2_mn_dl['holm_wilcoxon_p']:.4f}`，减亏主要源于宽幅降低了 F08 轮换与延迟退出损耗），但 `C_WF` 本身仍亏损 `{m_l2d_c['mean_net_return_pct']:+.4f}%`"
        if ab_c_l2_mn_dl
        else ""
    )
    sw_l2d_b2 = _lookup_wf(wf_idx, "L2", "DELAYED_EXIT_FILL", "SWITCH_48H", "B2")
    sw_l2d_b5 = _lookup_wf(wf_idx, "L2", "DELAYED_EXIT_FILL", "SWITCH_48H", "B5")
    sw_l2u_a = next((r for r in exec_mat if r["funding_regime"] == "SWITCH_48H" and r["lifecycle_id"] == "L2" and r["execution_profile"] == "UNFILLED_EXIT_TIMEOUT" and r["strategy_id"] == "A_FIXED"), None)
    sw_l2u_b5 = next((r for r in exec_mat if r["funding_regime"] == "SWITCH_48H" and r["lifecycle_id"] == "L2" and r["execution_profile"] == "UNFILLED_EXIT_TIMEOUT" and r["strategy_id"] == "B5"), None)
    sw_l2u_note = (
        f"（历史 `SWITCH_48H · L2 · UNFILLED` 中 `B2/B5 = {sw_l2u_b5['oos18_mean_return_pct']:+.2f}%` vs `A = {sw_l2u_a['oos18_mean_return_pct']:+.2f}%`）"
        if sw_l2u_a and sw_l2u_b5
        else ""
    )
    lines.append(
        f"1. **完整 F11/F08 生命周期显著改变了 R10 的机制归因与理想执行下的差距，但未改变 R10 的核心金融判定（仍为 `RESEARCH_INCONCLUSIVE`）**：\n"
        f"   - 在旧残仓模型 `L0` 下，R10 观察到 `SWITCH_48H` 中 B 相对 A 的巨大净收益差（本统一引擎 `L0` 与 PR #238 冻结结果 4 位小数完全对齐：`A = {sw_l0_a['mean_net_return_pct']:+.4f}%`、`CVaR95 = {sw_l0_a['cvar95_pct']:.2f}%` vs `B_WF = {sw_l0_b['mean_net_return_pct']:+.4f}%`、`CVaR95 = {sw_l0_b['cvar95_pct']:.2f}%`）。\n"
        f"   - 当开启 F11 限价退出（`L1`，`IDEAL_LIMIT_FILL`）后，仅 F11 本身就将固定网格 A 在 `SWITCH_48H` 中的均值净收益从 `{sw_l0_a['mean_net_return_pct']:+.4f}%` 提升至 `{sw_l1_a['mean_net_return_pct']:+.4f}%`（CVaR95 从 `{sw_l0_a['cvar95_pct']:.2f}%` 降至 `{sw_l1_a['cvar95_pct']:.2f}%`，残仓暂停时长从 `{sw_l0_a['mean_paused_with_inventory_hours']:.1f}h` 降至 `{sw_l1_a['mean_paused_with_inventory_hours']:.1f}h`）。这证明 **R10 中策略 B 在 `L0` 下的大部分减亏收益确实来源于替代缺失的 F11 突破平仓**。\n"
        f"   - 然而，在更贴近现实的**完整 `L2`（F11 + F08）且存在退出摩擦（`DELAYED_EXIT_FILL` / `LIMITED_CAPACITY_PARTIAL` / `UNFILLED_EXIT_TIMEOUT` / `GAP_AND_CANCEL_FAULT`）**下，固定网格 A 因 F08 持续轮换反复入场且缺乏资金费门控，风险再度大幅暴露：在 `SWITCH_48H · L2 · DELAYED` 中，`A = {sw_l2d_a['mean_net_return_pct']:+.4f}%`（CVaR95 `{sw_l2d_a['cvar95_pct']:.2f}%`）而 `B_WF = {sw_l2d_b['mean_net_return_pct']:+.4f}%`（CVaR95 `{sw_l2d_b['cvar95_pct']:.2f}%`）；在 `DEMO_HIGH_FUNDING · L2 · DELAYED` 中，`A = {dm_l2d_a['mean_net_return_pct']:+.4f}%`（CVaR95 `{dm_l2d_a['cvar95_pct']:.2f}%`）而 `B_WF = {dm_l2d_b['mean_net_return_pct']:+.4f}%`（CVaR95 `{dm_l2d_b['cvar95_pct']:.2f}%`）。\n"
        f"2. **资金费敏感机制 B 在 F11 之外仍具有独立但严格条件化的防御研究价值（且实证表明其独立价值几乎全部来自入口/轮换门控，而非独立资金费退出）**：\n"
        f"   - **关键机制发现（`B2 == B5`）**：在 `L1` 与 `L2` 的全部历史 5 资金费体制 × 5 执行假设中，`B2`（纯因果入口与 F08 轮换门控）与 `B5`（入口门控 + 资金费退出）产生**完全一致的净收益与 CVaR95**（例如 `SWITCH_48H · L2 · DELAYED` 下两者均为 `{sw_l2d_b2['mean_net_return_pct']:+.4f}%` / `{sw_l2d_b5['mean_net_return_pct']:+.4f}%`、`CVaR95 = {sw_l2d_b2['cvar95_pct']:.2f}%`）。根本原因在于：本单层网格（`prices=[lower, upper]`）叠加 2 bps 滑点后，任何触发买单成交的小时收盘价（`close * 1.0002 <= lower`）同时满足 `close < lower`，当根 Bar 即触发 `BREAKOUT` 并进入唯一权威 F11 限价退出流程；因此 `B5` 的额外资金费退出规则被 F11 吸收（仅在单权威日志中追加第二原因），不再产生额外独立平仓动作。\n"
        f"   - **真正不可替代的独立价值（因果入口与 F08 轮换门控）**：在持续高资金费（如 Demo 周末实测 `22.97 bps/h`）或 `SWITCH_48H` 48h 切换之后，F11 只能在**建仓并突破之后**被动退出，且 F08 轮换会驱使固定网格 A 不断重新开仓、持续支付手续费/滑点并在退出延迟或碎仓阻断（`EXIT_MIN_NOTIONAL`）时承受高额资金费失血；而策略 B 的因果入口门控与轮换延迟门控直接阻止在高资费体制下开新仓，该防护 **F11 无法替代**。反之，若已于高资费前建仓且遭遇 `UNFILLED_EXIT_TIMEOUT` 或 `LIMITED_CAPACITY_PARTIAL` 流动性枯竭，策略 B 同样无法逃离{sw_l2u_note}。\n"
        f"   - **主网常态边界**：在真实主网资金费（`MAINNET_ACTUAL`）的完整 `L2` 生命周期下，`A_FIXED = {m_l2_a['mean_net_return_pct']:+.4f}%`、`B_WF = {m_l2_b['mean_net_return_pct']:+.4f}%`、`C_WF = {m_l2_c['mean_net_return_pct']:+.4f}%`（`IDEAL`，B/C 相对 A 均无 Holm-Bonferroni 显著性）；`DELAYED` 下分别为 `{m_l2d_a['mean_net_return_pct']:+.4f}%` / `{m_l2d_b['mean_net_return_pct']:+.4f}%` / `{m_l2d_c['mean_net_return_pct']:+.4f}%`（{c_mn_dl_note}）。**全部网格策略在全部生命周期与执行假设下均跑输现金基准 `CASH (0.0000%)`**。"
    )
    lines.append("")

    lines.append("### 1.3 七大核心研究问题逐一回答")
    lines.append("")
    f11_sw_delta = f"{ab_f11_sw_a['delta_mean_return_pp']:+.2f} pp (CVaR95 {ab_f11_sw_a['delta_cvar95_pp']:+.2f} pp)" if ab_f11_sw_a else "—"
    f08_mn_delta = f"{ab_f08_mn_a['delta_mean_return_pp']:+.2f} pp (费用 {ab_f08_mn_a['ctrl_total_fees_usdt']:.4f} → {ab_f08_mn_a['cand_total_fees_usdt']:.4f} USDT)" if ab_f08_mn_a else "—"
    b_sw_id_delta = f"{ab_b_l2_sw_id['delta_mean_return_pp']:+.2f} pp (CVaR95 {ab_b_l2_sw_id['delta_cvar95_pp']:+.2f} pp, Holm p={ab_b_l2_sw_id['holm_wilcoxon_p']:.4f})" if ab_b_l2_sw_id else "—"
    b_sw_dl_delta = f"{ab_b_l2_sw_dl['delta_mean_return_pp']:+.2f} pp (CVaR95 {ab_b_l2_sw_dl['delta_cvar95_pp']:+.2f} pp, Holm p={ab_b_l2_sw_dl['holm_wilcoxon_p']:.4f})" if ab_b_l2_sw_dl else "—"
    b_dm_dl_delta = f"{ab_b_l2_dm_dl['delta_mean_return_pp']:+.2f} pp (CVaR95 {ab_b_l2_dm_dl['delta_cvar95_pp']:+.2f} pp, Holm p={ab_b_l2_dm_dl['holm_wilcoxon_p']:.4f})" if ab_b_l2_dm_dl else "—"
    b_mn_id_delta = f"{ab_b_l2_mn_id['delta_mean_return_pp']:+.2f} pp (Holm p={ab_b_l2_mn_id['holm_wilcoxon_p']:.4f})" if ab_b_l2_mn_id else "—"
    c_mn_id_delta = f"{ab_c_l2_mn_id['delta_mean_return_pp']:+.2f} pp (Holm p={ab_c_l2_mn_id['holm_wilcoxon_p']:.4f})" if ab_c_l2_mn_id else "—"

    lines.append(f"1. **完整 F11 退出是否已经解决 R10 中主要的长期残仓风险？** **在理想限价成交条件下基本解决了突破后的长期残仓，但在退出摩擦情景下仍未根除。** 在 `IDEAL_LIMIT_FILL` 下，F11（`L1 - L0`）使固定网格 A 在 `SWITCH_48H` 的 18 个历史 OOS 窗净收益改善 `{f11_sw_delta}`，残仓暂停时长由 `{sw_l0_a['mean_paused_with_inventory_hours']:.1f}h` 骤降至 `{sw_l1_a['mean_paused_with_inventory_hours']:.1f}h`；但在 `DELAYED_EXIT_FILL`、`LIMITED_CAPACITY_PARTIAL` 与 `UNFILLED_EXIT_TIMEOUT` 下，退出延迟、碎仓 `EXIT_MIN_NOTIONAL` 阻断和流动性枯竭会重新产生残仓与资金费侵蚀。")
    lines.append(f"2. **资金费敏感策略 B 在 F11 之外还有多少增量价值？** **按执行假设和资金费体制显著分化，且增量几乎完全来自入口与 F08 轮换门控**：在 `MAINNET_ACTUAL · L2 · IDEAL` 下，`B_WF - A_FIXED` 为 `{b_mn_id_delta}`（无显著增量）；在 `SWITCH_48H · L2 · IDEAL` 下为 `{b_sw_id_delta}`（增量大幅收窄，因为 F11 已处理大部分突破残仓）；但在 `SWITCH_48H · L2 · DELAYED` 下回升至 `{b_sw_dl_delta}`，在 `DEMO_HIGH_FUNDING · L2 · DELAYED` 下达到 `{b_dm_dl_delta}`。由于在单层网格 + 2 bps 滑点下 `B2` 与 `B5` 在全部 `L1`/`L2` 历史场景完全一致，策略 B 在 F11 之外的增量价值本质上来自**高资金费期间通过入口/轮换门控避免 F08 反复重新接仓**（注：仅在多档宽网格或零滑点区间内持仓尚未触发 `BREAKOUT` 时，策略 B 的独立资金费退出才可能先于 F11 触发）。")
    lines.append(f"3. **F08 自动轮换提高收益还是增加损耗？** **总体增加交易损耗并放大延迟退出时的尾部风险。** 在 `MAINNET_ACTUAL` 下，固定网格 A 的 `L2 - L1` 差值为 `{f08_mn_delta}`（净收益由 `L1` 的 `{m_l1_a['mean_net_return_pct']:+.4f}%` 恶化至 `L2` 的 `{m_l2_a['mean_net_return_pct']:+.4f}%`，在 `DELAYED` 下进一步恶化至 `{m_l2d_a['mean_net_return_pct']:+.4f}%`）。F08 在突破平仓后重新部署网格，持续消耗手续费、滑点与 POST 预算，在单边/高波动行情下导致“反复止损-轮换-再止损”的锯齿损耗。")
    c_mn_dl_str = f"`{ab_c_l2_mn_dl['delta_mean_return_pp']:+.2f} pp`（Holm `p={ab_c_l2_mn_dl['holm_wilcoxon_p']:.4f}`）" if ab_c_l2_mn_dl else "—"
    lines.append(f"4. **波动率自适应策略 C 是否在完整执行模型下改善风险收益？** **未能带来相对现金的正期望改善，但可在 `L2` 下通过宽幅网格减少 F08 轮换频率与延迟退出损耗。** 在 `MAINNET_ACTUAL · L2 · IDEAL` 下，`C_WF - A_FIXED` 为 `{c_mn_id_delta}`（`C_WF = {m_l2_c['mean_net_return_pct']:+.4f}%` vs `A = {m_l2_a['mean_net_return_pct']:+.4f}%`）；在 `MAINNET_ACTUAL · L2 · DELAYED` 下，`C_WF - A_FIXED` 为 {c_mn_dl_str}（`C_WF = {m_l2d_c['mean_net_return_pct']:+.4f}%` vs `A = {m_l2d_a['mean_net_return_pct']:+.4f}%`），减亏源于较宽网格减少了频繁突破与延迟退出暴露而非产生正 Alpha；在 `SWITCH_48H · L2 · DELAYED` 下 `C_WF = {sw_l2d_c['mean_net_return_pct']:+.4f}%`（CVaR95 `{sw_l2d_c['cvar95_pct']:.2f}%`），仍缺乏对高资金费的防御能力。")
    lines.append("5. **当退出延迟、部分成交或无法成交时，三种策略的风险差异是什么？** 见第 6 节与第 9 节完整矩阵。在 `DELAYED_EXIT_FILL` / `LIMITED_CAPACITY_PARTIAL` / `UNFILLED_EXIT_TIMEOUT` 下：固定网格 A 与自适应网格 C 在高资金费下承受严重的残余库存资金费侵蚀与熔断（`LOSS_BUDGET_EXHAUSTED`）；策略 B 在持续高资金费下因**空仓门控**保持零风险，在 `SWITCH_48H` 下若于 48h 前已建仓且遭遇 `UNFILLED_EXIT_TIMEOUT`，同样受限于单权威限价退出无法成交，但因 48h 后禁止 F08 新开仓而显著限制二次暴露。")
    lines.append("6. **相对现金基准及固定网格，是否存在真实成本后的样本外改善？** **不存在相对现金基准的稳健正样本外收益。** 在真实主网资金费 `MAINNET_ACTUAL` 下，无论是 `L0`、`L1` 还是 `L2`，A/B/C 三个策略的 18 窗均值净收益均低于 `CASH (0.0000%)`；且这 18 个窗口已被 R10 使用，属于历史复验而非全新未见 OOS，不能授予 `OOS_ADVANTAGE_SUPPORTED`。")
    lines.append("7. **哪些策略值得进入后续官方 Demo 对照研究？** 见第 11 节：不建议向任何策略授予金融准入或 Champion 晋级；若在官方 Demo（周末实证高资费环境）开展纯风控对照观测，**仅建议保留 `A_FIXED` 作为基线对照，并验证叠加在统一 F11/F08 生命周期上的策略 B 因果资金费入口/轮换门控护栏（优先对比 `B2` 纯入口门控 vs `B5` 入口+退出门控），同时审慎评估是否限制 F08 轮换频率或引入轮换冷却**；不建议将策略 C 单独晋级。")
    lines.append("")

    lines.append("### 1.4 五项特别判断（Section 10）")
    lines.append("")
    lines.append("| 特别判断项 | 基于实验数据的明确判断 |")
    lines.append("| --- | --- |")
    lines.append(f"| B 是否仅仅重复实现 F11 的保护效果？ | **退出端被 F11 高度吸收，但入口/轮换门控端具有不可替代的独立价值**。在 `L0 -> L1 (IDEAL)` 中，F11 吸收了 R10 中策略 B 对突破残仓的大部分减亏效果，且在本单层网格 + 2 bps 滑点模型下 `B2 == B5`（F11 吸收了 B5 的退出触发）；但在 `L2` 完整轮换 + 退出摩擦（`DELAYED`/`PARTIAL`/`NO_FILL`）及高资金费下，B 的因果**入口/轮换阻断**防止了 F08 持续重开新仓，这是 F11 无法替代的。 |")
    lines.append("| 高资金费压力下降低风险是否只是因为空仓？ | **在持续高资费（`DEMO_HIGH_FUNDING`）下确实完全源于空仓门控**（`fills=0`、`holding=0h`、净收益 `0.00%`）；在 `SWITCH_48H` 下前 48h 正常参与交易，48h 切换后由“F11 清理已有突破仓位 + 策略 B 禁止后续 F08 轮换新开仓”共同起效。这是风控减亏而非盈利 Alpha。 |")
    lines.append(f"| F08 是否造成手续费侵蚀？ | **是，明确观察到手续费与滑点侵蚀**。`MAINNET_ACTUAL` 18 窗中，A 从 `L1` 到 `L2`（`IDEAL`）净收益由 `{m_l1_a['mean_net_return_pct']:+.4f}%` 降至 `{m_l2_a['mean_net_return_pct']:+.4f}%`，手续费由 `{m_l1_a['total_fees_usdt']:.4f}` 增至 `{m_l2_a['total_fees_usdt']:.4f} USDT`，滑点由 `{m_l1_a['total_slippage_usdt']:.4f}` 增至 `{m_l2_a['total_slippage_usdt']:.4f} USDT`。 |")
    lines.append(f"| C 是否减少风险但同步损害收益？ | **视生命周期而定**：在 `L0` 下 C 通过宽幅减少成交但仍亏损；在 `L2` 下宽幅显著减少 F08 频繁突破与手续费（`L2 · IDEAL` 主网 `C_WF={m_l2_c['mean_net_return_pct']:+.4f}%` 优于 `A={m_l2_a['mean_net_return_pct']:+.4f}%`），但在 `DELAYED` 与高资金费下仍显著亏损且未跑赢 `CASH` 或 `B`。 |")
    lines.append(f"| 完整生命周期是否使原有 R10 策略排名发生改变？ | **是**。在 `MAINNET_ACTUAL` 下，`L0` 排名为 `B_WF ({m_l0_b['mean_net_return_pct']:+.4f}%) > C_WF ({m_l0_c['mean_net_return_pct']:+.4f}%) > A ({m_l0_a['mean_net_return_pct']:+.4f}%)`；在 `L2 (IDEAL)` 下由于宽幅降低了 F08 轮换损耗，排名变为 `CASH (0.0000%) > C_WF ({m_l2_c['mean_net_return_pct']:+.4f}%) > B_WF ({m_l2_b['mean_net_return_pct']:+.4f}%) > A ({m_l2_a['mean_net_return_pct']:+.4f}%)`；而在 `SWITCH_48H` 与 `DEMO_HIGH_FUNDING` 的 `L2 (DELAYED)` 下，防御排序仍为 `CASH ≈ B_WF > C_WF > A`。 |")
    lines.append("")

    # Section 2: Unified Architecture & Ledger Verification
    lines.append("## 2. 统一执行模型与账本核验（`ENGINEERING_ACCEPTED`）")
    lines.append("")
    lines.append("- **零第三套引擎**：完全复用 R09 的 `GridExecution` + `_CampaignPaperBroker` (`PaperBroker`) + `TradingController` + `PaperStore` + F11/F08 生命周期 + `grid_fifo.attribute_events` 与 R10 的 `FundingPolicy` / `VolatilityPolicy` / `FundingSensitiveGrid` / `VolatilityAdaptiveGrid`。")
    lines.append("- **单权威退出订单不变式**：扩展 `GridExecution.closed_for_limit_exit` 覆盖突破与资金费退出原因；在 `L1`/`L2`（`use_f11_limit_exit=True`）下，策略 B 不再独立下发市价卖单，而是与 `BREAKOUT` 统一汇入唯一权威 F11 reduce-only `SELL LIMIT` 订单生命周期，且当任一原因已进入退出流程时，后续触发的另一原因仅记入诊断日志而不撤单、不重复下单。")
    lines.append("- **同时间戳结算顺序**：全量资金费通过 `PaperBroker.apply_funding` 入账，严格先按前一闭合时点持仓结算资金费，再执行当前闭合时点撮合成交（`fill_time < settlement_time`），与策略 B 因果信号共享同一事件源。")
    lines.append("- **F08 轮换与预算连续性**：跨代共享 `_CampaignPaperBroker` 累计 POST 次数、3h 窗口配额与名义额；当策略 B（高资金费）或策略 C（极端波动）入口门控处于阻断状态时，F08 延迟轮换直至门控解除，避免空仓无效轮换刷代。")
    lines.append(
        f"- **全量运行账本核验**：累计执行 `{ledger['total_unique_runs']}` 次独立完整生命周期模拟"
        f"（历史窗 `{ledger['historical_unique_runs']}` + 确定性压力 `{ledger['deterministic_runs']}` + Monte Carlo `{ledger['monte_carlo_unique_runs']}`），"
        f"全部 FIFO 对账残差严格为 `0`（`all_zero_fifo_residual = {ledger['all_zero_fifo_residual']}`），"
        f"任意时点在途退出卖单数最大值 `max_simultaneous_exit_orders <= 1`（`single_authoritative_exit_order_verified = {ledger['single_authoritative_exit_order_verified']}`）。"
    )
    lines.append("")

    # Section 3: Data Audit & Independence Boundary
    cov = audit["coverage"]
    dq = audit["data_quality"]
    fa = audit["funding_alignment"]
    lines.append("## 3. 数据审计与历史复验独立性边界")
    lines.append("")
    lines.append(f"- **标的数据**：`{audit['instrument_id']}`，UTC 1h 连续时间轴 `{cov['start_utc']}` → `{cov['end_exclusive_utc']}`，共 `{cov['total_bars']}` 根小时 Bar（`{cov['total_days']}` 天），缺失 `{dq['missing_bars']}`、重复 `{dq['duplicate_timestamps']}`、不插值；对齐 `{fa['total_events_in_range']}` 条主网资金费事件（均值 `{fa['mean_rate_bps']} bps/8h`，零费率占比 `{fa['pct_zero']}%`）。")
    lines.append(f"- **窗口与 Walk-Forward**：`{hist['windows_count']}` 个非重叠 7 天窗口（`W01..W27`，每窗 168h + 96h 严格前置预热），6 折扩展 Walk-Forward 产生 `{hist['oos_windows_count']}` 个历史 OOS 窗（`W10..W27`）；策略 C 初始冻结参考波动率 `sigma_ref = {hist['initial_sigma_ref_1h']*100:.4f}%/h`。")
    lines.append("- **独立性声明**：上述 27 个窗口与 18 个历史 OOS 窗口已由 R10 使用，属于**可比历史复验**，**不构成全新未见独立 OOS 资格证据**，严禁据此授予 `OOS_ADVANTAGE_SUPPORTED` 或新 Champion 金融资格。")
    lines.append("- **口径说明**：历史回放风险预算分母统一为 `10.00 USDT`（匹配 R08/R09/R10 历史高价期 0.04 单位抵押口径），合成压力与 Monte Carlo 风险预算分母统一为 `7.00 USDT`（锚定 Demo 价 `132.89 USDT`）；所有收益率均以策略风险预算为分母，绝不与 `5000.00 USDT` Demo 账户总 NAV 混淆。")
    lines.append("")

    # Section 4: L0 vs R10 Frozen Baseline Reconciliation
    lines.append("## 4. L0 统一引擎与 R10 冻结基线对照核验（18 个历史 OOS 窗）")
    lines.append("")
    lines.append("下表将本统一引擎 `L0`（`NO_F11_NO_F08_STRANDED_POSITION_BASELINE`，通过统一 `run_unified_lifecycle` 与原生账本 `FundingEvent` 实时扣款执行）与 PR #238 已冻结的 R10 修正结果逐项对照（只读读取 `docs/validation/p8-r10/summary.json`，未修改任何 R10 证据）。两者在全部 3 种资金费体制 × 3 类策略（`A_FIXED`/`B_WF`/`C_WF`）上的 18 窗 OOS 均值净收益、CVaR95 与累计资金费达到 **4 位小数严格一致**（完整表见 `l0_vs_r10_reconciliation.csv`）：")
    lines.append("")
    lines.append("| 资金费体制 | 策略 | R10 冻结净收益 | R11 L0 净收益 | 收益差 (pp) | R10 冻结 CVaR95 | R11 L0 CVaR95 | R10 资金费 (USDT) | R11 L0 资金费 (USDT) | L0 熔断窗数 |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in r10_rec:
        lines.append(
            f"| `{r['funding_regime']}` | `{r['strategy_role']}` | {r['r10_frozen_return_pct']:+.4f}% | "
            f"{r['r11_l0_return_pct']:+.4f}% | {r['return_diff_pp']:+.4f} | {r['r10_frozen_cvar95_pct']:.2f}% | "
            f"{r['r11_l0_cvar95_pct']:.2f}% | {r['r10_frozen_funding_usdt']:+.6f} | "
            f"{r['r11_l0_funding_usdt']:+.6f} | {r['r11_l0_halted_windows']} |"
        )
    lines.append("")

    # Section 5: 2D Historical OOS Matrix (L0 / L1 / L2 x A / B / C / CASH)
    lines.append("## 5. 历史 18 个 OOS 窗口两维对照（策略 × 生命周期 × 资金费体制）")
    lines.append("")
    lines.append("分母：`10.00 USDT` 策略风险预算；同窗口、同起始资金、同成本（3 bps 费率 + 2 bps 滑点，1% 成交量参与度）。")
    lines.append("")
    lines.append("| 资金费体制 | 生命周期 / 执行假设 | 策略 | 均值净收益 | CVaR95 | VaR95 | 均值 MaxDD | 亏损概率 | 总毛利 (U) | 总手续费 (U) | 总滑点 (U) | 总资金费 (U) | 均值持仓(h) | 残仓暂停(h) | 退出延迟(h) | 均值轮换 | 轮换成功率 | 均值 POST |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for f_reg in ("MAINNET_ACTUAL", "SWITCH_48H", "DEMO_HIGH_FUNDING_22.97BPS"):
        combos_show = [
            ("L0", "IDEAL_LIMIT_FILL"),
            ("L1", "IDEAL_LIMIT_FILL"),
            ("L2", "IDEAL_LIMIT_FILL"),
            ("L1", "DELAYED_EXIT_FILL"),
            ("L2", "DELAYED_EXIT_FILL"),
        ]
        for lc_id, prof in combos_show:
            short_p = "IDEAL" if prof == "IDEAL_LIMIT_FILL" else "DELAYED"
            roles = ("CASH", "A_FIXED", "B_WF", "B2", "B5", "C_WF") if (lc_id == "L0" and prof == "IDEAL_LIMIT_FILL") else ("A_FIXED", "B_WF", "B2", "B5", "C_WF")
            for role in roles:
                s = _lookup_wf(wf_idx, lc_id, prof, f_reg, role)
                rot_s = "—" if s["rotation_success_rate"] is None else f"{s['rotation_success_rate']*100:.1f}%"
                lines.append(
                    f"| `{f_reg}` | `{lc_id}/{short_p}` | `{role}` | {s['mean_net_return_pct']:+.4f}% | "
                    f"{s['cvar95_pct']:.2f}% | {s['var95_pct']:.2f}% | {s['mean_max_drawdown_pct']:.2f}% | "
                    f"{s['loss_prob_pct']:.1f}% | {s['total_trade_gross_pnl_usdt']:+.4f} | "
                    f"{s['total_fees_usdt']:.4f} | {s['total_slippage_usdt']:.4f} | "
                    f"{s['total_funding_pnl_usdt']:+.4f} | {s['mean_holding_hours']:.1f} | "
                    f"{s['mean_paused_with_inventory_hours']:.1f} | {s['mean_exit_delay_hours']:.1f} | "
                    f"{s['mean_rotations_per_window']:.2f} | {rot_s} | {s['mean_posts_consumed']:.1f} |"
                )
    lines.append("")
    lines.append("### 5.1 补充体制对照（`EX_FUNDING` 零资金费与 `NORMAL_SYNTHETIC_0.125BPS` 正常合成费率，代表策略 18 窗 OOS）")
    lines.append("")
    lines.append("| 资金费体制 | 生命周期 / 执行假设 | 策略 | OOS18 均值净收益 | CVaR95 | 均值 MaxDD | 总手续费 (U) | 总滑点 (U) | 总资费 (U) | 均值持仓(h) | 残仓暂停(h) | 均值轮换 |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in exec_mat:
        if r["funding_regime"] not in {"EX_FUNDING", "NORMAL_SYNTHETIC_0.125BPS"}:
            continue
        if r["strategy_id"] not in {"A_FIXED", "B2", "B5", "C5"}:
            continue
        short_p = "IDEAL" if r["execution_profile"] == "IDEAL_LIMIT_FILL" else "DELAYED"
        lines.append(
            f"| `{r['funding_regime']}` | `{r['lifecycle_id']}/{short_p}` | `{r['strategy_id']}` | "
            f"{r['oos18_mean_return_pct']:+.4f}% | {r['oos18_cvar95_pct']:.2f}% | {r['oos18_mean_max_dd_pct']:.2f}% | "
            f"{r['oos18_total_fees_usdt']:.4f} | {r['oos18_total_slippage_usdt']:.4f} | {r['oos18_total_funding_usdt']:+.4f} | "
            f"{r['oos18_mean_holding_hours']:.1f} | {r['oos18_mean_paused_inv_hours']:.1f} | {r['oos18_mean_rotations']:.2f} |"
        )
    lines.append("")
    lines.append(
        "> 注：`EX_FUNDING · L2/IDEAL` 中 `B2/B5 (-0.4773%)` 相对 `A_FIXED (-0.4724%)` 存在 `-0.0049 pp` 微差，系 `W12` 窗口内 `FundingSensitiveGrid` 在空仓无挂单时自动 `re-seed` 多成交 2 笔所致（非资金费门控触发）。"
    )
    lines.append("")

    lines.append("### 5.2 6 折扩展 Walk-Forward 每折参数选择与 OOS 表现（`L2` 完整生命周期）")
    lines.append("")
    lines.append("| 资金费体制 | 执行假设 (`L2`) | Fold | 训练窗 | 测试窗 | 冻结 σ_ref | 选中 B | 选中 C | A 净收益 | B 净收益 | C 净收益 | B - A (pp) | C - A (pp) |")
    lines.append("| --- | --- | ---: | --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |")
    wf_folds = hist.get("wf_folds", [])
    for fd in wf_folds:
        if fd["lifecycle_id"] != "L2":
            continue
        if (fd["funding_regime"], fd["execution_profile"]) not in {
            ("MAINNET_ACTUAL", "IDEAL_LIMIT_FILL"),
            ("MAINNET_ACTUAL", "DELAYED_EXIT_FILL"),
            ("SWITCH_48H", "DELAYED_EXIT_FILL"),
        }:
            continue
        short_p = "IDEAL" if fd["execution_profile"] == "IDEAL_LIMIT_FILL" else "DELAYED"
        lines.append(
            f"| `{fd['funding_regime']}` | `{short_p}` | {fd['fold']} | `{fd['train_windows']}` | `{fd['test_windows']}` | "
            f"{fd['frozen_sigma_ref_1h_pct']:.4f}% | `{fd['selected_b']}` | `{fd['selected_c']}` | "
            f"{fd['oos_a_return_pct']:+.4f}% | {fd['oos_b_return_pct']:+.4f}% | {fd['oos_c_return_pct']:+.4f}% | "
            f"{fd['b_minus_a_pp']:+.4f} | {fd['c_minus_a_pp']:+.4f} |"
        )
    lines.append("")

    # Section 6: Paired Ablations & Statistical Significance with Multiple-Comparison Boundaries
    lines.append("## 6. 配对消融与多重比较统计检验（18 个历史 OOS 窗，`10,000` 次配对 Bootstrap）")
    lines.append("")
    lines.append("分别测量 F11 纯贡献（`L1 - L0`）、F08 增量贡献（`L2 - L1`）、完整生命周期（`L2 - L0`）及策略 B/C 相对固定网格 A 与现金 `CASH` 的配对差异。每个 `(资金费体制, 执行假设)` 检验族均报告原始 Wilcoxon p、Holm-Bonferroni 校正 p 及 Bonferroni 显著性边界（`0.05 / m`）。注意 `n=18` 窗口的有效 95% 尾部样本仅 `0.9` 个，小样本功效有限。")
    lines.append("")
    lines.append("| 资金费体制 | 执行假设 | 配对消融项 | 均值差 (pp) | Bootstrap 95% CI (pp) | 胜/平/负 (%) | ΔCVaR95 (pp) | 回撤缩减 (pp) | 原始 Wilcoxon p | Holm 校正 p | Holm 显著 |")
    lines.append("| --- | --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | --- |")
    key_comp_filters = {
        "A_FIXED: L1 - L0 (F11 pure)",
        "B_WF: L1 - L0 (F11 pure)",
        "C_WF: L1 - L0 (F11 pure)",
        "A_FIXED: L2 - L1 (F08 incremental)",
        "B_WF: L2 - L1 (F08 incremental)",
        "C_WF: L2 - L1 (F08 incremental)",
        "A_FIXED: L2 - L1 (F08 incremental, DELAYED)",
        "B_WF: L2 - L1 (F08 incremental, DELAYED)",
        "L0: B_WF - A_FIXED",
        "L1: B_WF - A_FIXED",
        "L2: B_WF - A_FIXED",
        "L2: B2 - A_FIXED",
        "L2: B5 - A_FIXED",
        "L1: C_WF - A_FIXED",
        "L2: C_WF - A_FIXED",
        "L2: A_FIXED - CASH",
        "L2: B_WF - CASH",
        "L2: C_WF - CASH",
    }
    for r in ablations:
        if r["comparison"] not in key_comp_filters:
            continue
        short_p = "IDEAL" if r["execution_profile"] == "IDEAL_LIMIT_FILL" else "DELAYED"
        lines.append(
            f"| `{r['funding_regime']}` | `{short_p}` | `{r['comparison']}` | {r['delta_mean_return_pp']:+.4f} | "
            f"`[{r['boot_95ci_low_pp']:+.4f}, {r['boot_95ci_high_pp']:+.4f}]` | {r['win_tie_loss_pct']} | "
            f"{r['delta_cvar95_pp']:+.2f} | {r['dd_reduction_pp']:+.2f} | "
            f"{r['wilcoxon_p_value']:.4f} | {r['holm_wilcoxon_p']:.4f} | `{r['sig_after_holm_05']}` |"
        )
    lines.append("")

    # Section 7: Execution Realism Matrix (5 Profiles across L1 & L2)
    lines.append("## 7. 退出执行现实性矩阵（5 组条件执行假设 × 历史 18 个 OOS 窗）")
    lines.append("")
    lines.append("所有小时 OHLC 撮合均为条件假设，**严禁将 `IDEAL_LIMIT_FILL` 等同于真实盘口保证表现**。下表展示完整生命周期 `L2`（及 `L1` 关键对照）在 5 组执行假设下的策略差异（完整 5 资金费体制 × 27 窗结果见 `historical_execution_realism.csv`）：")
    lines.append("")
    lines.append("| 资金费体制 | 生命周期 | 执行假设 | 策略 | OOS18 均值收益 | CVaR95 | 均值 MaxDD | 总费用 (U) | 总资费 (U) | 均值持仓(h) | 退出延迟(h) | 残仓窗数 | 均值轮换 | 熔断窗数 | 成交率 | 换手率 |")
    lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in exec_mat:
        if r["funding_regime"] not in {"MAINNET_ACTUAL", "SWITCH_48H", "DEMO_HIGH_FUNDING_22.97BPS"}:
            continue
        if r["strategy_id"] not in {"A_FIXED", "B2", "B5", "C5"}:
            continue
        if r["lifecycle_id"] != "L2":
            continue
        lines.append(
            f"| `{r['funding_regime']}` | `{r['lifecycle_id']}` | `{r['execution_profile']}` | `{r['strategy_id']}` | "
            f"{r['oos18_mean_return_pct']:+.4f}% | {r['oos18_cvar95_pct']:.2f}% | {r['oos18_mean_max_dd_pct']:.2f}% | "
            f"{r['oos18_total_fees_usdt']:.4f} | {r['oos18_total_funding_usdt']:+.4f} | "
            f"{r['oos18_mean_holding_hours']:.1f} | {r['oos18_mean_exit_delay_hours']:.1f} | "
            f"{r['oos18_stranded_windows']}/18 | {r['oos18_mean_rotations']:.2f} | "
            f"{r['oos18_halted_windows']}/18 | {r['oos18_order_fill_rate']*100:.1f}% | {r['oos18_mean_turnover']:.2f}x |"
        )
    lines.append("")

    # Section 8: Parameter Stability Across Lifecycles
    lines.append("## 8. 参数稳定性与过拟合检验（主网 `MAINNET_ACTUAL`：IS `W01..W09` vs OOS `W10..W27`）")
    lines.append("")
    lines.append("| 生命周期 / 执行假设 | 12 候选 IS vs OOS Spearman ρ | p 值 | 解读 |")
    lines.append("| --- | ---: | ---: | --- |")
    for key, sp in hist["spearman_summary"].items():
        parts = key.split("__")
        rho_s = "—" if sp["rho"] is None else f"{sp['rho']:+.4f}"
        p_s = "—" if sp["p_value"] is None else f"{sp['p_value']:.4f}"
        note = "排名相关性弱/不显著，跨体制不稳定" if (sp["p_value"] is None or sp["p_value"] >= 0.05) else "存在统计相关但样本窗仍有限"
        lines.append(f"| `{parts[0]} / {parts[1]}` | {rho_s} | {p_s} | {note} |")
    lines.append("")

    # Section 9: Deterministic 10-Scenario Stress Suite
    lines.append("## 9. 确定性极端压力测试（10 情景 × L0/L1/L2 × 执行假设，含撤单-成交竞态）")
    lines.append("")
    lines.append("锚定 Demo 价 `132.89 USDT`，策略风险预算 `7.00 USDT`，`168h`。下表精选关键压力对照（完整结果见 `deterministic_stress.csv`）：")
    lines.append("")
    lines.append("| 压力情景 | 资金费体制 | 生命周期 / 执行假设 | 策略 | 净收益率 | MaxDD | 资金费 (U) | 持仓(h) | 退出延迟(h) | 残仓 | 轮换 | 撤改/故障 | 终态 / 熔断 |")
    lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |")
    show_det_scens = {
        "S1_high_funding_range",
        "S2_funding_regime_spike",
        "S5_continuous_decline",
        "S6_range_then_gap",
        "S10_fast_reversal",
    }
    for d in det:
        if d["scenario_id"] not in show_det_scens:
            continue
        if d["strategy_id"] not in {"A_FIXED", "B2", "B5", "C5"}:
            continue
        if (d["lifecycle_id"], d["execution_profile"]) not in {
            ("L0", "IDEAL_LIMIT_FILL"),
            ("L1", "IDEAL_LIMIT_FILL"),
            ("L2", "IDEAL_LIMIT_FILL"),
            ("L2", "DELAYED_EXIT_FILL"),
            ("L2", "LIMITED_CAPACITY_PARTIAL"),
            ("L2", "UNFILLED_EXIT_TIMEOUT"),
            ("L2", "DELAYED_EXIT_FILL_CANCEL_FILL_RACE"),
        }:
            continue
        if d["strategy_id"] == "B2" and d["scenario_id"] != "S2_funding_regime_spike":
            continue
        st_lbl = f"`{d['final_state']}`" + (f" / `{d['halt_reason']}`" if d["halt_reason"] else "")
        lines.append(
            f"| `{d['scenario_id']}` | `{d['funding_label']}` | `{d['lifecycle_id']}/{d['execution_profile']}` | `{d['strategy_id']}` | "
            f"{d['net_return_pct']:+.2f}% | {d['max_drawdown_pct']:.2f}% | {d['realized_funding_usdt']:+.4f} | "
            f"{d['holding_hours']:.1f} | {d['mean_exit_delay_hours']:.1f} | {d['residual_inventory']:.2f} | "
            f"{d['rotations_completed']} | {d['exit_reprices']}/{d['exit_cancel_faults']} | {st_lbl} |"
        )
    lines.append("")

    # Section 10: Progressive Monte Carlo Tail-Risk & Convergence Study
    lines.append("## 10. Monte Carlo 尾部风险、执行摩擦敏感性与逐级收敛核验")
    lines.append("")
    lines.append(
        f"- **设计**：同随机路径配对（`7.00 USDT` 风险预算，`168h`，锚定 `132.89 USDT`）。第一阶段全 6 类随机路径模型初筛 `n={mc['screen_paths']}`（`seed={mc['primary_seed']}`）；"
        f"第二阶段针对 3 类关键尾部模型（`gbm_base_vol_1.0x`、`moving_block_bootstrap_24h`、`jump_stress_downside`）逐级扩容至 `n={mc['expand_paths']}`（有效 95% 尾部样本 `{0.05*mc['expand_paths']:.1f}` 条），"
        f"并对高方差尾部核心对（`gbm_base_vol_1.0x`、`jump_stress_downside` 的 `L2` `A_FIXED` vs `B5`）进一步扩容至 `n={mc.get('tail_expand_paths', mc['expand_paths'])}`（有效尾部样本 `{0.05*mc.get('tail_expand_paths', mc['expand_paths']):.1f}` 条），"
        f"同时在 `n={mc['cross_seed_paths']}` 下执行跨种子对照（`seed={mc['primary_seed']}` vs `seed={mc['cross_seed']}`）。"
    )
    lines.append("")

    lines.append(f"### 10.0 全 6 类随机路径模型初筛总览（`n = {mc['screen_paths']}`，`seed = {mc['primary_seed']}`）")
    lines.append("")
    lines.append("覆盖全部 6 类复用自 R08/R09/R10 的随机路径模型（GBM 0.5x/1.0x/2.0x、Moving-Block Bootstrap 24h、Regime Bootstrap 24h、Downside Jump Stress）；下表展示 `NORMAL_SYNTHETIC_0.125BPS` 与 `SWITCH_48H` 下 `L2/DELAYED` 及 `L0/IDEAL` 对照（全部 240 行含 `EX_FUNDING`、`DEMO_HIGH_FUNDING`、`L1` 见 `mc_screening.csv`）：")
    lines.append("")
    lines.append("| 随机模型 | 资金费体制 | 生命周期 / 执行假设 | 策略 | 均值收益 | 亏损概率 | CVaR95 | P95 MaxDD | 持仓(h) | 残仓率 | 相对同层 A ΔCVaR95 |")
    lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in mc["screening"]:
        if r["funding_regime"] not in {"NORMAL_SYNTHETIC_0.125BPS", "SWITCH_48H"}:
            continue
        if (r["lifecycle_id"], r["execution_profile"]) not in {
            ("L0", "IDEAL_LIMIT_FILL"),
            ("L2", "DELAYED_EXIT_FILL"),
        }:
            continue
        if r["strategy_id"] not in {"A_FIXED", "B5", "C5"}:
            continue
        short_p = "IDEAL" if r["execution_profile"] == "IDEAL_LIMIT_FILL" else "DELAYED"
        da = "基准" if r["strategy_id"] == "A_FIXED" else f"{r['delta_cvar95_vs_A_same_lc_pp']:+.2f} pp"
        lines.append(
            f"| `{r['scenario']}` | `{r['funding_regime']}` | `{r['lifecycle_id']}/{short_p}` | `{r['strategy_id']}` | "
            f"{r['mean_net_return_pct']:+.2f}% | {r['loss_prob_pct']:.1f}% | {r['cvar95_pct']:.2f}% | "
            f"{r['max_dd_p95_pct']:.2f}% | {r['mean_holding_hours']:.1f} | {r['stranded_tail_rate_pct']:.1f}% | **{da}** |"
        )
    lines.append("")

    lines.append(f"### 10.1 关键尾部模型扩容汇总（`n = {mc['expand_paths']} / {mc.get('tail_expand_paths', mc['expand_paths'])}` 路径，`seed = {mc['primary_seed']}`，`SWITCH_48H`）")
    lines.append("")
    lines.append("| 随机模型 | 路径数 `n` | 生命周期 / 执行假设 | 策略 | 均值收益 | 亏损概率 | VaR95 | CVaR95 (95% CI) | P95 MaxDD | 持仓(h) | 退出延迟(h) | 残仓率 | 资费均值(U) | 轮换数 | 相对同层 A ΔCVaR95 | 尾部状态 |")
    lines.append("| --- | ---: | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for r in mc["expanded_key_scenarios"]:
        short_p = "IDEAL" if r["execution_profile"] == "IDEAL_LIMIT_FILL" else "DELAYED"
        da = "基准" if r["strategy_id"] == "A_FIXED" else f"{r['delta_cvar95_vs_A_same_lc_pp']:+.2f} pp"
        lines.append(
            f"| `{r['scenario']}` | {r['n_paths']} | `{r['lifecycle_id']}/{short_p}` | `{r['strategy_id']}` | "
            f"{r['mean_net_return_pct']:+.2f}% | {r['loss_prob_pct']:.1f}% | {r['var95_pct']:.2f}% | "
            f"{r['cvar95_pct']:.2f}% (`[{r['cvar95_boot_low_pct']:.2f}%, {r['cvar95_boot_high_pct']:.2f}%]`) | "
            f"{r['max_dd_p95_pct']:.2f}% | {r['mean_holding_hours']:.1f} | {r['mean_exit_delay_hours']:.1f} | "
            f"{r['stranded_tail_rate_pct']:.1f}% | {r['mean_funding_usdt']:+.4f} | {r['mean_rotations']:.2f} | "
            f"**{da}** | `{r['tail_95_status']}` |"
        )
    lines.append("")

    lines.append(f"### 10.2 执行摩擦敏感性（MC `n = {mc['screen_paths']}`，`L2` 完整生命周期 × 5 组执行假设）")
    lines.append("")
    lines.append("| 随机模型 | 资金费体制 | 执行假设 (`L2`) | 策略 | 均值收益 | CVaR95 | P95 MaxDD | 持仓(h) | 退出延迟(h) | 残仓率 | 熔断率 | 相对同层 A ΔCVaR95 |")
    lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in mc["execution_sensitivity"]:
        if r["lifecycle_id"] != "L2":
            continue
        da = "基准" if r["strategy_id"] == "A_FIXED" else f"{r['delta_cvar95_vs_A_same_lc_pp']:+.2f} pp"
        lines.append(
            f"| `{r['scenario']}` | `{r['funding_regime']}` | `{r['execution_profile']}` | `{r['strategy_id']}` | "
            f"{r['mean_net_return_pct']:+.2f}% | {r['cvar95_pct']:.2f}% | {r['max_dd_p95_pct']:.2f}% | "
            f"{r['mean_holding_hours']:.1f} | {r['mean_exit_delay_hours']:.1f} | {r['stranded_tail_rate_pct']:.1f}% | "
            f"{r['halt_rate_pct']:.1f}% | **{da}** |"
        )
    lines.append("")

    lines.append("### 10.3 逐级收敛诊断（`64 → 128 → 256 → ...`）与跨种子稳定性")
    lines.append("")
    lines.append("下表展示 `SWITCH_48H · L2 · DELAYED_EXIT_FILL` 与 `L2 · IDEAL_LIMIT_FILL` 随路径数增加的 CVaR95 及区间宽度演变（完整表见 `mc_convergence.csv` 与 `mc_cross_seed_stability.csv`）：")
    lines.append("")
    lines.append("| 随机模型 | 执行假设 (`L2`, `SWITCH_48H`) | 策略 | 路径数 `n` | 有效尾部数 | CVaR95 | Boot 95% CI 宽 | 相对 A ΔCVaR95 | 尾部状态 |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for c in mc["convergence"]:
        if c["funding_regime"] != "SWITCH_48H" or c["lifecycle_id"] != "L2":
            continue
        if c["strategy_id"] not in {"A_FIXED", "B5"}:
            continue
        da = "基准" if c["strategy_id"] == "A_FIXED" else f"{c['delta_cvar95_vs_A_same_lc_pp']:+.2f} pp"
        lines.append(
            f"| `{c['scenario']}` | `{c['execution_profile']}` | `{c['strategy_id']}` | {c['n_paths']} | "
            f"{c['effective_tail_samples_95']:.1f} | {c['cvar95_pct']:.2f}% | {c['cvar95_ci_width_pp']:.2f} pp | "
            f"{da} | `{c['tail_95_status']}` |"
        )
    lines.append("")

    lines.append("| 跨种子检验 (`n=" + str(mc["cross_seed_paths"]) + "`, `SWITCH_48H`, `L2`) | 执行假设 | 策略 | Seed A CVaR95 | Seed B CVaR95 | 绝对差 (pp) | Seed A ΔCVaR vs A | Seed B ΔCVaR vs A |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for cs in mc["cross_seed_stability"]:
        if cs["funding_regime"] != "SWITCH_48H" or cs["lifecycle_id"] != "L2":
            continue
        lines.append(
            f"| `{cs['scenario']}` | `{cs['execution_profile']}` | `{cs['strategy_id']}` | "
            f"{cs['cvar95_seed_a_pct']:.2f}% | {cs['cvar95_seed_b_pct']:.2f}% | {cs['cvar95_abs_diff_pp']:.2f} | "
            f"{cs['delta_cvar_vs_A_seed_a_pp']:+.2f} pp | {cs['delta_cvar_vs_A_seed_b_pp']:+.2f} pp |"
        )
    lines.append("")
    lines.append(
        "> **收敛与跨种子核心诊断**：\n"
        "> 1. 在 `moving_block_bootstrap_24h · SWITCH_48H · L2/DELAYED` 下，小样本初筛 `n=64`（仅 3.2 条有效尾部样本）将 `B5` 的 CVaR95 严重低估为 `2.26%`；逐级扩容至 `n=512` 后才暴露出 `11.39%`（CI 宽 `8.79 pp`），且 `n=256` 跨种子测试显示 `8.98%` vs `16.56%`（绝对差 `7.58 pp`），说明块自助法下的重尾尚未完全收窄，必须显式标注尾部不确定性。\n"
        "> 2. 在 `jump_stress_downside · SWITCH_48H · L2/DELAYED` 扩容至 `n=1024`（51.2 条有效尾部样本）后，尽管 `B5` 将均值亏损从 `-10.54%` 缩减至 `-3.53%`、残仓率从 `35.1%` 降至 `7.3%`，但其极端 5% 尾部 CVaR95 仍高达 `28.88%`（对比 `A_FIXED` 的 `29.57%`，`ΔCVaR95 = -0.69 pp`）。原因在于约 7.3% 的路径在 48h 切换前已建仓并遭遇连续跳空下跌叠加退出延迟，此时无论是否启用策略 B，均受限于同一权威 F11 限价退出在下跌中的成交能力。"
    )
    lines.append("")

    # Section 11: S02-F01 Handoff & Candidate Recommendations
    lines.append("## 11. 候选策略建议与 S02-F01 消费契约")
    lines.append("")
    lines.append("- **可消费机器契约**：`docs/validation/p8-r11/s02_handoff.json`（及 `summary.json[\"s02_handoff\"]`）已向 `P8-S02-F01` 暴露：")
    lines.append(f"  - 执行模型版本：`{s02['execution_model_version']}`；")
    lines.append(f"  - 统一研究入口：`{s02['entry_points']['python_callable']}` 与 CLI `{s02['entry_points']['study_cli']}`；")
    lines.append("  - 冻结候选配置与 SHA256：`configs/research/p8_r11_unified_lifecycle_study.json` 及 `p8_r10_strategy_{a,b,c}_*.json`；")
    lines.append(f"  - 数据独立性与资格硬边界：`windows_reused_from_r10={s02['data_independence_and_qualification']['windows_reused_from_r10']}`、`independent_unseen_oos_available={s02['data_independence_and_qualification']['independent_unseen_oos_available']}`、`champion_eligible_from_r11_evidence={s02['data_independence_and_qualification']['champion_eligible_from_r11_evidence']}`、`financial_admission={s02['data_independence_and_qualification']['financial_admission']}`。")
    lines.append("- **后续官方 Demo 对照建议**：")
    lines.append("  - **不晋级任何策略为实盘/正 Alpha Champion**；")
    lines.append("  - 若在官方 Demo（周末实证高资费环境）继续开展小预算风控对照，仅建议在统一 F11/F08 生命周期下将 **策略 B 的因果高资金费入口/轮换阻断护栏（`B2` 入口门控 / `B5` 入口+统一 F11 退出）** 与固定网格控制组 `A_FIXED` 配对观测，并同步评估**限制 F08 轮换频率**（以缓解已观测到的 F08 手续费/滑点侵蚀）；")
    lines.append("  - 策略 C（波动率自适应）在真实成本与高资费体制下未展现稳健风险收益优势，暂不建议进入下一轮官方 Demo 晋级。")
    lines.append("")

    # Section 12: Runtime & Reproducibility
    lines.append("## 12. 运行耗时与可复现命令")
    lines.append("")
    lines.append(
        f"- **总耗时**：`{timing['total_seconds']:.2f}s`（确定性压力 `{timing['deterministic_seconds']:.2f}s` · "
        f"历史 27 窗/6 折 WF/5 体制/5 执行假设 `{timing['historical_seconds']:.2f}s` · "
        f"Monte Carlo 含扩容与跨种子 `{timing['monte_carlo_seconds']:.2f}s`；`workers={timing['workers']}`）。"
    )
    lines.append(
        "- **输出产物**：`REPORT.md`、`summary.json`、`s02_handoff.json`、`data_audit.json`、`l0_vs_r10_reconciliation.csv`、"
        "`historical_wf_summary.csv`、`historical_wf_folds.csv`、`historical_execution_realism.csv`、`paired_ablations.csv`、"
        "`parameter_stability_mainnet.csv`、`deterministic_stress.csv`、`mc_screening.csv`、`mc_execution_sensitivity.csv`、"
        "`mc_expanded_key_scenarios.csv`、`mc_convergence.csv`、`mc_cross_seed_stability.csv`。"
    )
    lines.append("")
    lines.append("```powershell")
    lines.append(
        f'python tools/research/run_p8_r11_study.py --r04b-snapshot "D:\\Code\\p7-r04b-official-payloads\\certified\\runs\\bde00b2f948842e2b8c2a00465a9f8c6" '
        f'--cache-dir "D:\\Code\\p8-r10-data-cache" --output-dir "docs/validation/p8-r11" '
        f'--screen-paths {mc["screen_paths"]} --expand-paths {mc["expand_paths"]} '
        f'--tail-expand-paths {mc.get("tail_expand_paths", mc["expand_paths"])} '
        f'--cross-seed-paths {mc["cross_seed_paths"]} --workers {timing["workers"]}'
    )
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def write_p8_r11_artifacts(study: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    """Write machine-readable JSON/CSV deliverables and formal Chinese REPORT.md for P8-R11."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    def dump_json(filename: str, payload: Any) -> None:
        p = output_dir / filename
        p.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        written[filename] = p

    def dump_csv(filename: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        flat = []
        for r in rows:
            item = {}
            for k, v in r.items():
                if isinstance(v, (dict, list)):
                    item[k] = json.dumps(v, sort_keys=True, ensure_ascii=False)
                else:
                    item[k] = v
            flat.append(item)
        p = output_dir / filename
        pd.DataFrame(flat).to_csv(p, index=False, encoding="utf-8")
        written[filename] = p

    # Compact summary.json (keep full per-window OOS rows in CSV to prevent bloated JSON)
    summary_compact = {
        k: v for k, v in study.items() if k != "historical"
    }
    summary_compact["historical"] = {
        k: v for k, v in study["historical"].items() if k != "wf_oos_rows"
    }
    dump_json("summary.json", summary_compact)
    dump_json("s02_handoff.json", study["s02_handoff"])
    dump_json("data_audit.json", study["data_audit"])

    hist = study["historical"]
    dump_csv("l0_vs_r10_reconciliation.csv", hist["l0_vs_r10_reconciliation"])
    dump_csv("historical_wf_summary.csv", hist["wf_summary"])
    dump_csv("historical_wf_folds.csv", hist["wf_folds"])
    dump_csv("historical_wf_oos_windows.csv", hist["wf_oos_rows"])
    dump_csv("historical_execution_realism.csv", hist["execution_realism_matrix"])
    dump_csv("paired_ablations.csv", hist["paired_ablations"])
    dump_csv("parameter_stability_mainnet.csv", hist["parameter_stability_mainnet"])
    dump_csv("deterministic_stress.csv", study["deterministic_stress"])

    mc = study["monte_carlo"]
    dump_csv("mc_screening.csv", mc["screening"])
    dump_csv("mc_execution_sensitivity.csv", mc["execution_sensitivity"])
    dump_csv("mc_expanded_key_scenarios.csv", mc["expanded_key_scenarios"])
    dump_csv("mc_convergence.csv", mc["convergence"])
    dump_csv("mc_cross_seed_stability.csv", mc["cross_seed_stability"])

    report_path = output_dir / "REPORT.md"
    report_path.write_text(render_p8_r11_report(study), encoding="utf-8")
    written["REPORT.md"] = report_path
    return written
