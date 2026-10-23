"""Chinese descriptive reports, containing no security-level rows or membership."""

import json

import numpy as np


def _number(value):
    if value is None or not np.isfinite(value):
        return "—"
    return "%.6g" % value


def write_report(path, payload, candidates, evaluation, redundancy):
    stats = payload["counts"]
    source = payload["input"]["classification"]
    lines = [
        "# 受约束自动因子发现 MVP：描述性探索报告",
        "",
        "**NO LIVE MONEY。软件执行成功不代表发现可靠或可交易 Alpha。**",
        "",
        "数据：%s。开发区间：%s 至 %s，之前历史只用于预热。"
        % (
            source,
            payload["config"]["development_start"],
            payload["config"]["development_end"],
        ),
        "",
        "实际候选记录 %(total_records)d；结构唯一 %(structurally_unique)d；成功计算 %(successful)d；计算失败 %(failed)d；语义拒绝 %(rejected)d；结构重复 %(exact_duplicates)d。"
        % stats,
        "",
        "软件状态：%s；真实数据状态：%s（%s）。"
        % (payload["status"], payload["real_data_status"], payload["real_data_reason"]),
        "",
        "总耗时 %.3f 秒；因子计算耗时 %.3f 秒；密集网格 %d 行；缓存峰值 %d 字节；子表达式缓存命中 %d 次。"
        % (
            payload["total_elapsed_seconds"],
            payload["compute_elapsed_seconds"],
            payload["panel_rows"],
            payload["peak_cache_bytes"],
            payload["cache_hits"],
        ),
        "",
        "## 评价合同",
        "",
        "生成器只接收冻结配置，先有限经济模板枚举，再规范化和结构去重，无遗传优化、实时 LLM 或标签输入。Add/Mul 仅交换两个操作数；不使用浮点结合律改写。最终候选为无物理量纲，保留 RAW/HFQ 来源和收益/换手语义。",
        "",
        "时序按证券与显式交易日历计算。Lag/Delta 仅正滞后；RollingStd 使用 ddof=1 且完整窗口；缺日保持缺失。零分母、非有限值保持缺失，原因计数见 candidates.csv。",
        "",
        "复用 FactorLab 逐日 MAD/Z-score 及 evaluate_factors 的 IC、RankIC 和五分位数学。未来标签仅在进入与退出交易日都落入开发窗口且成熟时参与描述性评价；信号分组在因子可用时形成。quantile_returns.csv 为实际信号分组；label_sensitivity.csv 仅披露完整标签子样本重选分组的选择敏感性。末尾未成熟标签不会重新决定信号分组。",
        "",
        "冗余使用逐日横截面 Pearson，分别报告平均有符号与平均绝对相关，禁止跨日期拼接后得出单一结论。至少 %d 证券、%d 有效日期、平均配对覆盖 %.0f%%；高冗余阈值 %.2f。数值相同还要求每个开发日已知值及缺失掩码完全一致。证据不足为 UNDETERMINED。"
        % (
            payload["config"]["minimum_cross_section"],
            payload["config"]["minimum_evaluation_dates"],
            payload["config"]["minimum_pair_coverage"] * 100,
            payload["config"]["redundancy_threshold"],
        ),
        "",
        "结构重复跳过计算；数值冗余候选保留全部审计记录，不自动筛除或翻转方向。旧 14 因子只读调用；AMIHU20 不可表示为首版 AST，其特殊量纲只参与数值比较。",
        "",
        "## 每个候选的开发期质量与指标",
        "",
        "|候选|语义类别|计算状态|有效率|缺失率|异常值率|处理后覆盖|IC均值|RankIC均值|旧14结构匹配|",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    quality = (
        evaluation.quality.set_index("candidate_id").to_dict("index")
        if evaluation is not None and not evaluation.quality.empty
        else {}
    )
    summary = (
        evaluation.summary.set_index("factor").to_dict("index")
        if evaluation is not None and not evaluation.summary.empty
        else {}
    )
    for row in candidates.to_dict("records"):
        name = row["candidate_id"]
        q, s = quality.get(name, {}), summary.get(name, {})
        metrics = [
            _number(q.get(column))
            for column in (
                "valid_ratio",
                "missing_ratio",
                "outlier_ratio",
                "processed_valid_ratio",
            )
        ]
        metrics += [_number(s.get(column)) for column in ("IC Mean", "RankIC Mean")]
        lines.append(
            "|%s|%s|%s|%s|%s|"
            % (
                name,
                row["family"],
                row["status"],
                "|".join(metrics),
                row.get("original_structure_matches", ""),
            )
        )
    lines.extend(["", "## 原 14 因子及候选之间的冗余", ""])
    if not redundancy.empty:
        for comparison, group in redundancy.groupby("comparison"):
            lines.append(
                "%s：%s。完整配对结论、有效日期与覆盖见 redundancy_summary.csv。"
                % (
                    comparison,
                    json.dumps(
                        group["status"].value_counts().to_dict(), ensure_ascii=False
                    ),
                )
            )
            lines.append("")
        for name in candidates.loc[candidates["status"].eq("SUCCESS"), "candidate_id"]:
            pairs = redundancy.loc[
                redundancy["left"].eq(name) & redundancy["comparison"].eq("ORIGINAL14")
            ]
            if not pairs.empty:
                best = pairs.loc[pairs["mean_abs_pearson"].fillna(-1).idxmax()]
                lines.append(
                    "- %s 与旧14最接近：%s，平均绝对相关 %s，%s，有效 %d 日，配对覆盖 %s。"
                    % (
                        name,
                        best["right"],
                        _number(best["mean_abs_pearson"]),
                        best["status"],
                        best["valid_dates"],
                        _number(best["mean_pair_coverage"]),
                    )
                )
    lines.extend(["", "## 表达式、量纲、规范形式与复杂度", ""])
    for row in candidates.to_dict("records"):
        lines.extend(
            [
                "### %s" % row["candidate_id"],
                "",
                "%s：%s" % (row["family"], row["meaning"]),
                "",
                "表达式：`%s`" % row["original_expression"],
                "",
                "量纲：`%s`；复杂度：`%s`；有效回看 %s 交易日。"
                % (row["dimension"], row["complexity"], row["lookback"]),
                "",
                "SHA-256：`%s`。状态 %s；拒绝/失败原因 `%s`。"
                % (row["expression_sha256"], row["status"], row["rejection_reason"]),
                "",
                "```json",
                row["normalized_ast"],
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## 数据状态、统计局限与后续前提",
            "",
            "SYNTHETIC_TEST_ONLY 使用确定性模拟行情和业务日日历，含多证券、预热、缺日、零成交与极端值；不能解释为金融实证。私有真实数据只有本地原授权 manifest 字节 SHA、冻结版本、410 源哈希、旧收据及冻结历史标签全部通过才可读入；无下载、补价、换股或改写旧产物。",
            "",
            "历史50股一年便利样本已参加以前研究，有幸存偏差、非 PIT 成分、HFQ 来源未正式核验、样本短与多候选事后筛选风险。它不是独立未观察测试集。这里不输出选优 p 值，不因最大 RankIC 宣称 Alpha。",
            "",
            "接入更长历史数据须新增独立授权和冻结来源合同、明确证券身份与实际日历、无前视的 PIT 成分及状态、独立可重放 HFQ 资格与成熟标签；另设未参与生成/去重/阈值调整的验证与测试窗口。当前软件不更改任何正式金融门禁。",
            "",
            "首版不需要遗传编程或实时 LLM 搜索：有限模板已验证软件闭环，而当前历史样本无法支持更大搜索空间的金融结论。下一步先增加独立合格数据与跨环境复现证据，再决定是否需要更大候选空间及多重筛选控制。",
            "",
            "工程限制：P2——预算为预估操作/缓存上限与每个有界向量算子之间的单调时钟检查，不能抢占底层单个 NumPy/Pandas 调用；有界面板与 AST 防止无界执行，未依赖 Unix 信号。P2——浮点完全相同保证限于同一冻结 Python/依赖与源码字节环境；不同平台可发生舍入差异。P2——合成日历不是实际交易所日历。金融正式资格仍未取得，属于数据前提，不能由软件通过替代。",
            "",
            "收据记录输入 SHA、开发区间、模板版本/种子/白名单/所有预算、表达式集合、源码逐文件哈希/Git身份、Python/依赖版本与完成原因；证券级因子、未来收益及供应商响应不会写入公开产物。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
