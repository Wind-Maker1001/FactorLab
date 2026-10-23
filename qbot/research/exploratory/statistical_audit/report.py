"""Chinese auditable narrative and marked static figures."""

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..report import _table
from .delivery import MARKERS, PARAMETERS, write_json
from .statistics import PAIRS


def save(figure, path):
    footer = " | ".join(MARKERS[:3]) + "\n" + " | ".join(MARKERS[3:])
    figure.text(0.5, 0.014, footer, ha="center", fontsize=7, color="#8c3826")
    figure.tight_layout(rect=(0, 0.065, 1, 1))
    figure.savefig(path, dpi=160, metadata={"Description": " | ".join(MARKERS)})
    plt.close(figure)


def interval_plot(axis, table, names, methods):
    colors = ("#306b94", "#c36b32", "#537949")
    for number, method in enumerate(methods):
        t = table.loc[table.method.eq(method)].set_index("factor").reindex(names)
        pos = np.arange(len(names)) + (number - (len(methods) - 1) / 2) * 0.22
        # Percentile intervals need not contain the original point estimate.
        axis.hlines(pos, t.ci_low, t.ci_high, color=colors[number], linewidth=1.5)
        axis.scatter(t.estimate, pos, s=12, color=colors[number], label=method)
    axis.axvline(0, color="#333", linewidth=0.8)
    axis.set_yticks(np.arange(len(names)))
    axis.set_yticklabels(names, fontsize=8)
    axis.invert_yaxis()
    axis.grid(axis="x", alpha=0.18)


def plots(destination, tables, panel, regimes):
    names = list(panel.columns)
    figure, axis = plt.subplots(figsize=(12, 8))
    interval_plot(
        axis, tables["rankic_inference.csv"], names, ["HAC5", "HAC20", "MBB20"]
    )
    axis.set_title("Original daily RankIC mean: pointwise 95% intervals")
    axis.set_xlabel("Spearman RankIC; post-hoc family of 14 tests")
    axis.legend()
    save(figure, destination / "rankic_intervals.png")
    figure, axes = plt.subplots(1, 3, figsize=(18, 8), sharey=True)
    comparisons = tables["regime_comparisons.csv"]
    for axis, pair in zip(axes, PAIRS):
        selected = comparisons.loc[comparisons.comparison.eq("-".join(pair))]
        interval_plot(axis, selected, names, ["HAC20", "MBB20"])
        axis.set_title(
            "-".join(pair)
            + (" (SIDEWAYS n=45: limited)" if "SIDEWAYS" in pair else ""),
            fontsize=10,
        )
        axis.set_xlabel("Difference in daily mean RankIC")
    axes[0].legend()
    figure.suptitle("Frozen-state comparisons: pointwise 95% intervals; 42-test family")
    save(figure, destination / "regime_intervals.png")
    spread = tables["quintile_spread.csv"].pivot(
        index="date", columns="factor", values="B_minus_A_spread"
    )
    figure, axes = plt.subplots(2, 1, figsize=(14, 8))
    spread.plot(ax=axes[0], linewidth=0.9)
    axes[0].set_title("Quintile selection diagnostic: B minus A in Q5-Q1")
    axes[0].set_ylabel("B-A excess return spread\n(descriptive; not portfolio P&L)")
    axes[0].legend(ncol=7, fontsize=7)
    mean = spread.mean().reindex(names)
    maximum = spread.abs().max().reindex(names)
    pd.DataFrame({"paired-day mean B-A": mean, "max |B-A|": maximum}).plot.bar(
        ax=axes[1]
    )
    axes[1].set_ylabel("Return difference\n(paired dates only)")
    save(figure, destination / "quintile_sensitivity.png")
    figure, axes = plt.subplots(3, 1, figsize=(14, 9), sharex=True)
    panel.plot(ax=axes[0], linewidth=0.7, legend=False)
    axes[0].set_title(
        "Raw daily RankIC on all 242 sessions (six tail sessions remain missing)"
    )
    axes[0].set_ylabel("RankIC (unsmoothed)")
    codes = regimes.map({"BULL": 1, "SIDEWAYS": 0, "BEAR": -1})
    axes[1].step(panel.index, codes, where="mid", color="#306b94")
    axes[1].set_yticks([-1, 0, 1], ["BEAR", "SIDEWAYS", "BULL"])
    axes[1].set_title("Frozen regimes; contiguous episodes retained")
    q = (
        tables["quintile_daily.csv"]
        .groupby("date")
        .agg(
            missing_rate=("label_missing_rate", "mean"),
            changed_buckets=(
                "observable_members_changed",
                lambda x: int(x.gt(0).sum()),
            ),
        )
    )
    axes[2].plot(
        q.index,
        q.missing_rate,
        color="#c36b32",
        label="mean missing label rate among factor-eligible",
    )
    axes[2].set_ylim(0, 1.05)
    axes[2].set_ylabel("Missing label rate")
    right = axes[2].twinx()
    right.plot(
        q.index, q.changed_buckets, color="#537949", label="changed observable buckets"
    )
    right.set_ylabel("Changed buckets / 70")
    axes[2].legend(loc="upper left", fontsize=8)
    right.legend(loc="upper right", fontsize=8)
    save(figure, destination / "temporal_coverage.png")


def write_report(destination, tables, evidence, panel, regimes):
    write_json(destination / "input_audit.json", evidence)
    for filename, frame in tables.items():
        frame.to_csv(destination / filename, index=False)
    plots(destination, tables, panel, regimes)
    desc = tables["rankic_descriptive.csv"]
    inference = tables["rankic_inference.csv"]
    differences = tables["regime_comparisons.csv"]
    all_methods = inference.groupby("factor").conditional_support.all()
    supported = all_methods.index[all_methods].tolist()
    robust_states = differences.groupby(
        ["factor", "comparison"]
    ).conditional_support.all()
    state_support = ["%s %s" % key for key in robust_states.index[robust_states]]
    raw = (
        inference.groupby("method")
        .p_two_sided.apply(lambda x: int(x.lt(0.05).sum()))
        .to_dict()
    )
    adjusted = inference.groupby("method").conditional_support.sum().to_dict()
    state_raw = (
        differences.groupby("method")
        .p_two_sided.apply(lambda x: int(x.lt(0.05).sum()))
        .to_dict()
    )
    state_adjusted = differences.groupby("method").conditional_support.sum().to_dict()
    spread = tables["quintile_spread.csv"]
    paired = spread.A_Q5_minus_Q1.notna() & spread.B_Q5_minus_Q1.notna()
    summary = (
        spread.loc[paired]
        .groupby("factor")
        .agg(
            paired_dates=("B_minus_A_spread", "count"),
            mean_A=("A_Q5_minus_Q1", "mean"),
            mean_B=("B_Q5_minus_Q1", "mean"),
            mean_B_minus_A=("B_minus_A_spread", "mean"),
            max_abs_difference=("B_minus_A_spread", lambda x: x.abs().max()),
            changed_spread_dates=(
                "B_minus_A_spread",
                lambda x: int(x.abs().gt(1e-12).sum()),
            ),
        )
    )
    daily = tables["quintile_daily.csv"]
    changed_dates = (
        daily.loc[daily.observable_members_changed.gt(0), "date"]
        .drop_duplicates()
        .sort_values()
    )
    intervals = tables["regime_intervals.csv"]
    state_counts = tables["regime_means.csv"].loc[
        lambda x: x.factor.eq(desc.factor.iloc[0]) & x.method.eq("HAC20")
    ]
    invalid = (
        tables["daily_input_audit.csv"]
        .loc[lambda x: ~x.valid_rank_ic, "date"]
        .drop_duplicates()
    )
    report = f"""# 首次真实 A 股14因子统计稳健性与方法论敏感性审计

**SUCCESS · {' · '.join(MARKERS)}**

本报告为事后探索性复核，全部14因子及42个状态差异均不是预注册主假设或独立验证。软件成功依据输入和统计合同、可复核产物及测试完成，不依据单因子显著、预测正确或策略盈利。

本轮14因子均值经三方法复核与Holm后的条件性支持为{len(supported)}项；42项状态差的三方法一致支持为{len(state_support)}项。SIDEWAYS45日无法可靠判断。A/B可观察成员变化仅出现在六个内部标签缺口日，不能解释为真实组合收益或总体偏差估计。

## 输入、版本与缺失审计

- 数据语义版本：`{evidence['data_version']}`。
- 基线：PR #3 合并后 main `{evidence['baseline_commit']}`；[完整研究CI]({evidence['baseline_ci']['research']})、[静态CI]({evidence['baseline_ci']['static']}) 均成功后建立独立审计分支。
- 原 manifest SHA256：`{evidence['manifest_sha256']}`；410份源产物逐项重新验哈希，不重下载、合成、补价、扩池或训练模型。
- v2私有运行收据SHA：`{evidence['private_run_receipt_sha256']}`；v2公开收据SHA：`{evidence['public_v2_receipt_sha256']}`。既有计算代码每个源文件字节哈希一致，证券级因子、标签、状态精确复算一致，聚合CSV容差1e-12。源代码原revision、实际字节SHA和环境见input_audit.json；本轮代码见新receipt.json。
- 50只NON_PIT_CONVENIENCE股票，2025-09-12至2026-09-11共242实际交易日，每因子236有效原始日度RankIC。缺失日：{', '.join(pd.to_datetime(invalid).dt.strftime('%Y-%m-%d'))}，均因t+6标签未成熟；保留在242日轴上，不把六日变成零收益。
- 12,100证券日期行缺315标签：末尾六日300行，内部缺价15行；内部标签缺失并未令整个横截面失效。每日有效因子/标签对数量、IC、RankIC和缺失原因见daily_input_audit.csv。冻结收益仍为t+1 HFQ开盘至t+6开盘减中证500同期开盘收益。
- 来源哈希证明封存内部一致，不能认证历史vintage、PIT可知性或HFQ算法。私有行情、供应商响应、证券级因子标签与实际分位成员表不发布；v1/v2产物及旧收据不改写。

## RankIC统计推断

单位为原始日度截面Spearman RankIC，标准差ddof=1，正值比例分母为有效日数。没有使用股票日期行独立性、20日平滑值或自动翻转负方向。

{_table(desc)}

HAC采用截距OLS、Bartlett核、n/(n-k)有限样本尺度修正、正态近似双侧检验与95%点置信区间。lag=5对应五交易日重叠标签的局部依赖敏感性；lag=20覆盖更长约一交易月依赖，不声称这是数据驱动最优带宽。完整242日轴上的缺失日估计方程score为零（并非RankIC补零），有效样本用于bread，不压缩内部缺失日期。

Moving Block Bootstrap：20个实际交易日的重叠、非循环块，2000重复，固定seed={PARAMETERS['random_seed']}。从242日联合抽取完整日期元组（14因子、冻结状态、缺失位置），拼接抽到的块，仅截末块到242日。使用有限有效日均值、百分位95%区间；双侧p=(1+count(|theta*-theta_hat|>=|theta_hat|))/(B_valid+1)。有效重复不足95%或少于100则不给推断。bootstrap_bias列披露非循环端点权重偏差；百分位CI和中心化检验并非严格对偶，报告保留两者，不能挑一种支持结论。

14个均值检验是一个事后探索性族，分别在HAC5、HAC20、MBB20下Holm校正；这是同一检验族的三种敏感性实现，不能选最小p。未提供跨方法或全部诊断的全局FWER保证。95%区间为单项区间，不能称为14项同时区间。Holm也不能修复原样本选择偏差或不可靠单项p。

原始p<0.05项数：{raw}；Holm且样本门槛满足项数：{adjusted}。
三方法均通过Holm的条件性支持：{'、'.join(supported) if supported else '无'}。
均值与标准差只能描述一年便利样本；没有拒绝零假设不证明因子无效，原方向不变。

完整14×3行估计、标准误、点区间、双侧原始p、Holm p见rankic_inference.csv。iid_se_descriptive_only仅为标准误膨胀比较基准，未用于独立性检验。

逐因子方法差异见rankic_method_sensitivity.csv（标准误、p、区间和标准误比）。lag或方法差异是敏感性证据，不据此选择参数。

![RankIC三方法点置信区间](rankic_intervals.png)

## 冻结市场状态差异

固定规则为BULL：close>MA60且MA20>MA60；BEAR：close<MA60且MA20<MA60；其余SIDEWAYS，复用原函数，不按结果调边界。回归用完整日轴三个状态指示变量且不加截距，系数即状态均值；状态差是两系数contrast，其HAC sandwich含两状态协方差，不把两份不独立样本当独立均值相减。

{_table(state_counts[['state', 'signal_dates', 'valid_dates', 'signal_runs', 'valid_runs', 'coverage', 'sample_insufficient']])}

冻结状态共有{intervals.interval_id.nunique()}连续区间；区间起止、长短、逐因子有效覆盖见regime_intervals.csv。BULL虽然128有效日，但仅6区间；BEAR63有效日，仅3区间，末尾6缺标签；SIDEWAYS45有效日，8短区间（最长12日），少于既有60日诊断门槛。门槛是解释限制，不能认证60日足够；区间不是独立实验，BEAR/BULL的结果也需要更多独立状态周期验证。

比较BULL-BEAR、BULL-SIDEWAYS、BEAR-SIDEWAYS各14因子，共42项事后检验族。每种方法对全部42项Holm校正，SIDEWAYS比较仍列近似CI/p以展示不确定性，但标sample_insufficient=True，不给支持结论。状态均值的单项区间本身没有多重比较保护。Bootstrap复用同一全轴区块，绝不先筛同状态日期再拼接；缺失某状态的重复不计，valid_repeats完整披露。

状态差原始p<0.05项数：{state_raw}；Holm且样本门槛满足项数：{state_adjusted}。
三方法均通过且两状态至少60有效日的条件性支持：{'；'.join(state_support) if state_support else '无'}。
涉及SIDEWAYS的差异均为描述性/样本不足，不能根据显著原始p升格；完整三方法结果见regime_comparisons.csv和regime_means.csv。

![冻结状态均值差与区间](regime_intervals.png)

## 五分位口径敏感性

A复用原探索口径：先取有限未来标签且有限因子，至少20个，再以rank(method=first)+qcut成五组。B在t日全部合格有限因子（至少20个）上分组，再仅于可观察标签上计算实现超额收益。保留原行序处理并列。A逐日数量、可观察数量、收益与冻结原ALL五分位CSV重放一致，正式evaluate_factors与Golden未修改。

quintile_daily.csv逐因子逐日逐Q1–Q5记录A/B总成员数、可观察成员数、可观察成员交集与对称差数、平均收益、B-A差及标签缺失率。A桶内缺失率机械为零，不代表无选择偏差；label_missing_rate的分母是B口径当日全部有限因子证券。尾部A无分组、B仍形成组但无实现收益，保留NA，不能补零。实际证券成员及可观察标记仅在私有private_quintile_membership.csv可复核，整份私有表由收据SHA绑定。公开表不提供逐桶成员集合哈希，避免对已知50股小集合的枚举与相等性链接。

{_table(summary.reset_index())}

有可观察成员变动的交易日共{len(changed_dates)}日：{', '.join(pd.to_datetime(changed_dates).dt.strftime('%Y-%m-%d')) if len(changed_dates) else '无'}。quintile_spread.csv记录每日Q5-Q1及B-A，quintile_monthly.csv记录每月变化、标签缺失率和有效配对数；平均仅使用两口径均可观察的日，NA与零分别保留。完整中间桶也可变，不能只看Q5-Q1。

这只测量同一冻结样本内未来标签可取得性的分组选择偏差。它不测量停牌、退市、生存者、非PIT样本选择等全部偏差；未估计缺失标签的真实收益，也不能从A/B差推断总体偏差大小。没有真实建仓、再平衡、成交、成本或执行约束，Q5-Q1不是实际交易组合收益。

![五分位选择敏感性](quintile_sensitivity.png)

![原始日度序列、状态和标签覆盖](temporal_coverage.png)

## 统计限制、结论等级和五年研究

- 条件性统计支持只指上述三方法一致、Holm后及样本门槛满足的观察。HAC和MBB仍依赖弱相关、有限矩及适当平稳/近似稳定条件，一年数据不能验证这些前提；市场切换、供应商事后修订和长期依赖可能令覆盖率失真。最坏情况下只应当作敏感性描述，不作显著性保证。
- 单一方法或未经Holm的显著性、状态均值不同、五分位A/B差异均仅描述性。涉及SIDEWAYS无法可靠判断。标签五日重叠、因子高度相关以及共同市场序列使有效信息远少于证券日期行数，分位结果也不做独立样本收益检验。
- 值得开展**隔离的五年探索研究**以增加独立历史周期并检验本轮结论稳定性；本轮不能据此批准正式中证500五年四模型研究或实盘。扩展研究应先冻结样本/参数/状态/检验族与时间分割，保留独立历史验证期，不根据本轮p挑因子、反转方向或调参数。
- 必须用独立历史样本解决：状态均值差是否跨多个牛熊周期持续、SIDEWAYS是否有足够独立区间、依赖长度和结构变化是否超20日、14因子方向及RankIC均值是否稳定、标签可取得性偏差是否在停牌退市期放大、行业规模和生存者偏差是否影响推广。历史PIT成员/逐日证券状态、同源长期HFQ及vintage资格是另外的输入问题，单纯增加年数不能修复。

## 复核与方法来源

receipt.json绑定输入语义版本、旧收据SHA、基线、实际计算源字节SHA、运行revision、环境、固定参数和本目录全部产物。公开收据另绑定私有审计收据SHA；不含原始行情/账户/供应商响应。检验结果完成不意味着金融资格改变。定向与完整CI、审查及交付状态见独立交付说明。

HAC方法依据[Newey–West原论文](https://doi.org/10.2307/1913610)；连续区块与平稳弱依赖前提依据[Künsch原论文](https://doi.org/10.1214/aos/1176347265)；多重比较依据[Holm原论文](https://www.ime.usp.br/~abe/lista/pdf4R8xPVzCnX.pdf)。本实现参数是明确固定的审计选择，不声称论文证明本样本前提成立。

**{' · '.join(MARKERS)}**
"""
    (destination / "report.md").write_text(report, encoding="utf-8")
