# FactorLab P10-04：外部策略独立复现、金融有效性验证及联合风险研究报告

> **安全边界与治理声明**：`NO LIVE MONEY` · 纯离线本地工作站研究（未操作 Azure 生产 VM，未修改当前生产策略，未自动合并，不将历史回测收益包装为真实模拟或实盘盈利）。金融准入维持 **`FORMAL_ADMISSION_BLOCKED`**，Champion Selector 最终结论为 **`INCONCLUSIVE`（`promoted = false`，原样保留 incumbent `v69`）**。金融深度剖析另见 [FINANCIAL.md](FINANCIAL.md)。

---

## 0. 五大核心问题明确回答

| 编号 | 核心研究问题 | 基于实证数据的直接回答 |
|---|---|---|
| **1** | **FL 是否正确复现外部策略？** | **执行核算层 24/24 精确复现（误差 `< 2.8e-9 USD`），V69 原生策略端到端完整通过；V179/V182 原生策略移植待 P10-03 冻结提交接入。** (a) 在隔离 `Qlib 0.9.7` 环境中独立复跑全部 23 个 V179/V182 原始账户（23/23 通过，最大净值差 `4.66e-10 USD`）及 V69 原始账户（借助新版 `fixtures/target`，最大净值差 `5.82e-11 USD`）；(b) FactorLab 自有独立回测引擎（**零 Qlib 撮合**）在参考兼容模式（`qlib_float32`）下对 **原生 `V69` + 全部 23 个 `V179/V182` 账户实现 24/24 精确复现**（目标权重最大差 `9.71e-17`，成交价最大差 `5.68e-14 USD`，日净值最大差 `2.79e-9 USD`，标普指标最大差 `1.91e-14`）；(c) 严守工程纪律不在本分支重复移植 V179/V182，V179/V182 当前通过只读外部冻结信号适配器驱动 FL 独立回放，其原生信号等价状态显式标为 `PENDING_P10_03_NATIVE_PORT`。 |
| **2** | **策略收益是否依赖特定的数据和撮合假设？** | **是，高度依赖 `SNDK` 单一标的、人工事后选股池及较低的开盘滑点假设；但对 Qlib `float32` 价格舍入或 1 日开盘延迟不敏感。** (a) Qlib `.day.bin` 的 `float32` 存储相对 `float64` parquet 仅造成最大 `$0.368`（20 年 `0.000014%`）净值差，不构成收益依赖；(b) 主窗口净损益 `68.9%`（V179）/ `65.3%`（V182）/ `56.1%`（V69）及二十年总美元净利 `51.6%`（V179）来自 `SNDK`，剔除 `SNDK` 后 V179 主窗口与二十年均跑输标普 500 总回报；(c) 摩擦成本敏感性显著：二十年窗口内，5x 费用（50 bps）将 V179 CAGR 从 `11.30%` 压至 `5.88%`，日内不利成交（`open_high_mid`）将其压至 `3.73%`（MaxDD `-33.47%`），50%–70% 部分成交将二十年 MaxDD 扩大至 `-32.5%` 以上。 |
| **3** | **策略在不同市场体制下是否具有稳定优势？** | **否，优势集中在危机防御与强趋势低波牛市，在横盘震荡与低波慢牛中无稳定优势。** 2001–2002、2008–2009、2022 三次大熊市中三策略均大幅控制回撤（如 2008–2009 标普 `-35.0%`，V179 `+4.3%` / V69 `+0.5%`）；但在 20 年中占 `44%` 交易日（2,208 日）的横盘体制下三策略年化均值全部为负（`-0.7%` 至 `-2.9%`），且在 `2013–2019` 慢牛中大幅跑输指数。多重检验（`N=182`）下剔除 `SNDK` 后主窗口 DSR 仅为 `0.33–0.38`，挑战窗仅为 `0.15`。 |
| **4** | **策略的尾部风险是否得到可靠估计？** | **条件抽样误差已通过万级路径压低至约 `0.5 pp`，但市场模型误差与校准窗误差（`5–11 pp`）以及执行模型误差（`~3 pp`）远大于抽样误差，且 99% 极端尾部的跨种子波动对 V179/V182 仍偏高。** Tier-2 完整动态策略 10,000 路径同步区块 Bootstrap（`history_20y`）给出 V179 一年期 `CVaR95 = 17.79% [17.29%, 18.28%]`、`CVaR99 = 23.61% [22.74%, 24.38%]`、`DD>20%` 概率 `6.50%`；但在下跳压力（`down_jump_stress`）下 `CVaR95` 跳升至 `29.09%`、`CVaR99` 达 `35.17%`，而固定权重 Tier-1（GPU 50,000 路径）无法替代动态策略回放（MBB 高估 `CVaR99` 约 `4.0 pp`，GBM 低估 `CVaR95` 约 `2.1 pp`）。 |
| **5** | **多策略组合能否降低风险并改善资本利用率？** | **Alpaca 内部组合 V69/V179/V182 几乎无真实分散化（仅做仓位缩放），无法显著改善风险调整收益或资本效率；跨交易所隔离叠加 Binance NVDAUSDT 网格当前仅稀释敞口并拖累净收益。** (a) 同源继承链导致三策略日收益相关性高达 `0.980–0.999`、下尾相关性 $\lambda_L = 0.84–0.96$、标的持仓重叠日高达 `92.9%`（主窗口）/ `86.0%`（二十年），共享账户等权/风险预算组合 Sharpe（`1.77–1.80`）并未显著超过单策略，且资金闲置率高达 `44%–68%`；(b) Alpaca（USD）与 Binance（USDT）严格账户隔离不共用保证金，重叠 132 日内与 Binance NVDAUSDT 网格相关性虽低（`0.069`），但因网格自身负收益（`-4.38%`），跨所汇总仅以净收益恶化（`-2.11% → -2.45%`）换取回撤被动稀释。 |

---

## 1. 第一阶段：源数据审计、V69 兼容性与隔离 Qlib 23+1 账户复跑

### 1.1 交付包完整性与策略继承链审计

- **交付包校验**（见 [verification.json](verification.json)）：
  - `Qlib_v182与v179_策略源码配置与回测复现.zip`（SHA-256 `21abc3eebaf816264b325525ce0997910e63d6abd72b47b96c21b5fffe91a79a`）：`SHA256SUMS.txt` 全部 **362 / 362** 文件通过校验，0 缺失、0 不匹配。
  - `F02_v69_策略入口_依赖_配置_结果.zip`（SHA-256 `d3e7346c8100e4c5ea6d9a98f3a60a151405288198bb5c84a1f4e32bb9dfe97e`）：`source-manifest.json` 全部 **28 / 28** 文件通过校验，0 不匹配；与新版包共享的 20 个文件中，全部 **18 个策略/引擎/审计源码文件逐字节一致**（仅 `README.md` 与 `requirements.txt` 不同）。
- **继承链**：
  - `V69`：`v69 → v61(×1.10) → v51(弱市均线退出) → BandParent(rule='band') → v50 → v48 → Qlib BaseStrategy`
  - `V125（进取腿）`：`v125(recovery防卫+个股相对强弱tilt) → v105(×4, 上限90%) → v100(giveback, ridge_confirm月度岭回归, dual_strength, 上限37.5%) → v69`
  - `V165（防御腿）`：`v165(fixed增配≤10%) → v161(tail_budget 63日下行波动32%单标的风险上限) → v150(首次入场减半) → v125`
  - `V179` / `V182`：共享同一 `Blend` 混合框架，分别按 `SP500TR > MA200 & breadth ≥ 50%`（75%/25% vs 25%/75%）与 `breadth_diff_10 > 10% & SP500TR > MA63`（75%/25% vs 40%/60%）线性混合 `V125` 与 `V165`。
- **行情与选择偏差审计**：
  - `fixtures/target`（`2011-01-03..2026-11-23`，3,997 日）与 `fixtures/history`（`1999-05-03..2026-11-20`，6,933 日）在全部 3,996 个重叠交易日及全部 8 只标的上 `open/close/volume` 最大相对误差为 **`0.000e+00`**，0 缺失值、0 零成交量日、0 OHLC 违规。
  - 上市与数据覆盖：`SNDK` 仅自 `2025-02-24` 起有行情（441/440 日），`TQQQ` 自 `2010-02-11` 起有行情（2001–2006 挑战窗与 2008 金融危机期间不存在）。复权方式为后复权价与拆股复权量（`factor = 1.0`），现金零利率，基准核算明确分离内部 `QQQ` 与面向用户的 `S&P 500 Total Return (^SP500TR)`。

### 1.2 V69 行情版本兼容性核查与隔离 Qlib 全量复跑矩阵

- **V69 数据兼容性确证**：
  - V69 原始精简包缺少 `fixture/market` 和 Provider。核查新版交付包 `Qlib_v182_v179/fixtures/target` 后确认：其 `manifest.json` 列出的 67 个文件中，**全部 60 个 parquet 与 Qlib Provider 二进制文件 100% 匹配 SHA-256**（仅 7 个原始 Yahoo HTTP `raw/*.json` 抓取快照在打包时被剥离，导致直接运行旧 `replay_v69.py` 会在哈希检查处报 `FileNotFoundError`）。
  - 在隔离 `Qlib 0.9.7`（`pandas==2.3.3`、`numpy==2.4.6`）环境中，以 `Qlib_v182_v179/fixtures/target` 复跑原始 V69 策略并逐日/逐笔比对 `F02_v69/fixture/v69_target_normal`：**完全通过校验（`verified = true`），日净值最大绝对差 `5.82e-11 USD`，371 笔成交、目标权重、归因与标普指标全部一致**。这正式关闭了 V69-F02 报告中遗留的 `FULL_REFERENCE_REPLAY: PENDING` 缺口。
- **V179/V182 原始 23 个账户隔离 Qlib 复跑**：
  - 按 `replay.py --list` 列出的全部 **23 个组合（`v179` 14 个 + `v182` 9 个）** 在独立临时目录完成复跑，耗时 `55.49s`：**23 / 23 全部通过**，逐日净值相对原件最大绝对差 **`4.657e-10 USD`**，相对交付方 `verification.json` 全部绩效指标最大差 **`2.398e-14`**。逐账户完整复现矩阵见 [qlib_23_account_replay_matrix.csv](qlib_23_account_replay_matrix.csv)。

---

## 2. 第二阶段：FactorLab 独立历史回放与 7 层逐级核对

FactorLab 独立回测引擎（[replay.py](../../../src/factorlab/research/external_validation/replay.py)）**完全不调用 Qlib 撮合引擎**，独立实现周首日调度、前收盘信号截止、0.5% 死区与零目标强制退出、0.1% 前日成交量上限、先卖后买现金约束撮合、手续费、日终盯市、逐标的 PnL 归因及双基准（`^SP500TR` / `QQQ`）绩效核算。

### 2.1 双精度模式 7 层差分汇总（24 个参考账户：原生 V69 × 1 + V179 × 14 + V182 × 9）

完整逐账户 7 层差分矩阵见 [fl_independent_replay_diff_matrix.csv](fl_independent_replay_diff_matrix.csv)。

| 回放模式 | 精确复现数 | L2 目标权重最大差 | L3/L4 成交笔数匹配 | L4 成交价最大差 (USD) | L5 日净值最大差 (USD) | L6 标的归因最大差 (USD) | L7 标普指标最大差 |
|---|---:|---:|---|---:|---:|---:|---:|
| **`qlib_float32`（参考兼容模式）** | **24 / 24** | **`9.71e-17`** | **24 / 24 完全一致** | **`5.68e-14`** | **`2.79e-09`** | **`1.86e-10`** | **`1.91e-14`** |
| `float64`（独立高精度 parquet 模式） | 0 / 24* | `9.71e-17` | 24 / 24 完全一致 | `3.05e-05` | `3.68e-01` | `1.88e-01` | `1.11e-06` |

> **关键差分归因（精确定位最早偏差源）**：
> - 在 `float64` 模式下，最早出现 `> 1e-6 USD` 净值差的交易日为各窗口**第 1 个成交日**（如 `v179/challenge/normal` 的 `2001-01-02`，差 `$0.00132`），20 年累计最大净值差 `$0.368`（相对 \$2.54M 期末净值仅 `0.000014%`）。
> - **唯一根因**：Qlib Provider 的 `.day.bin` 二进制文件采用 **`float32` 单精度**存储开盘价与收盘价，而 parquet 源行情为 `float64`。当 FL 独立引擎仅在 `qlib_float32` 参考兼容模式下对齐 `float32` 开盘/收盘量化时，**全部 24 个账户（含原生 V69 977 日 / 371 笔成交及 V179 二十年 5,032 日 / 3,821 笔成交）误差即刻归零至 `< 2.79e-9 USD`**，彻底证明 FL 独立引擎与原策略/账户核算逻辑零语义偏差，且未修改任何生产交易执行语义。
> - **P10-03 协作边界**：本分支遵循“不得重复移植 V179/V182”的要求。`v69` 直接使用 FactorLab 原生 `NativeV69`（`signal_source = NATIVE_FL`）；`v179` / `v182` 通过只读外部冻结信号适配器接入（`signal_source = EXTERNAL_PACKAGE_FROZEN_SIGNAL_PENDING_P10_03`），同时已在 `replay.py` 中预留 `factorlab.strategies.{v179,v182}_native:build_signal_provider` 自动挂载钩子，待 P10-03 冻结提交后即可一键切换为原生信号校验。

---

## 3. 第三阶段：真实金融有效性研究概要

完整数据表、15 项执行压力测试、Leave-One-Out 剔除矩阵、25 年体制分解及 DSR 多重检验详情见独立专题报告 [FINANCIAL.md](FINANCIAL.md) 与 [financial_comparison_matrix.csv](financial_comparison_matrix.csv)。核心要点：
- **SNDK 单标的集中度极高**：主窗口 `V179` 净利 `68.9%`（\$386k / \$561k）、二十年净利 `51.6%`（\$1.16M / \$2.24M）来自仅上市 441 日的 `SNDK`；剔除 `SNDK` 后 `V179` 主窗口（`+68.62%`）与二十年（`+397.09%`）均跑输 `^SP500TR`（`+98.78%` / `+620.36%`）。
- **成本与滑点敏感**：二十年窗口 `V179` 在 3x 费用下 CAGR 由 `11.30%` 降至 `8.56%`，5x 费用下 `5.88%`，日内不利成交下 `3.73%`；部分成交使二十年最大回撤加深至 `-32.58%`。
- **体制分化**：三大熊市防御优异，但横盘震荡（2,208 日）年化均为负，低波慢牛显著跑输指数。

---

## 4. 第四与第五阶段：蒙特卡洛专项风险研究与统计收敛性

### 4.1 双层架构与计算资源实测

- **第一层（Tier-1 快速固定权重风险模拟）**：直接复用现有 `MonteCarloRunner` 与 RTX 2070 GPU（`cupy-cuda` 14.2.0），基于 20 年校准窗口在 `1,000 → 10,000 → 50,000` 路径 × 5 个独立种子（`1729, 2718, 31415, 65537, 104729`）× 2 模型（`gbm`, `moving_block_bootstrap`）× 3 策略完成 90 组运行，总墙钟 **`87.68s`**（GPU 内核耗时 `78.58s`，单次 50,000 路径仅 `0.27s–0.54s`）。完整数据见 [mc_tier1_fixed_portfolio.csv](mc_tier1_fixed_portfolio.csv)。
- **第二层（Tier-2 完整动态策略生命周期模拟）**：使用 16 核 CPU 并行（28 逻辑核本机工作站），联合生成因果一致的 9 维路径（8 标的 OHLC/开盘跳空/成交量比 + 同步 `^SP500TR`，252 日真实预热 + 252 日前瞻评估），对每条路径**完整重算全部指标、市场宽度、月度岭回归预测、V125/V165/V179/V182 动态目标仓位、订单与先卖后买撮合账本**，并在**完全相同的路径上配对比较 `V69`/`V179`/`V182`**。总墙钟 **`2,154.8s`**（含 5 种子逐级收敛至 4,096 路径 + 10,000 路径扩容复核 `199.2s` + 6 模型对比 + 3 校准窗敏感性 + 执行压力模拟），单进程主控 CPU `14.8s`，紧凑导出产物总计 `256.5 KB`。

### 4.2 逐级统计收敛曲线（Tier-2 完整动态策略，20 年校准窗，5 种子均值与种子间 SD）

完整表见 [mc_tier2_convergence_summary.csv](mc_tier2_convergence_summary.csv)。预先设定指标精度目标：`CVaR95` 95% 置信区间半宽 `≤ 0.50 pp`、`CVaR99` 半宽 `≤ 1.00 pp`、跨种子 SD `≤ 0.25 pp`。

| 策略 | 模型 | 路径数 `n` | 名义尾部样本 (95% / 99%) | VaR95 (种子 SD) | CVaR95 (种子 SD) | CVaR99 (种子 SD) | 亏损概率 | DD95 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `v69` | `sync_mbb` | 256 | 12.8 / 2.6 | 3.97% (0.52) | 5.40% (0.70) | 7.10% (1.29) | 29.5% | 6.87% |
| `v69` | `sync_mbb` | 1,024 | 51.2 / 10.2 | 4.11% (0.22) | 5.62% (0.36) | 7.70% (0.74) | 30.1% | 6.73% |
| `v69` | `sync_mbb` | 4,096 | 204.8 / 41.0 | 3.98% (0.07) | 5.45% (0.18) | 7.47% (0.45) | 29.7% | 6.67% |
| **`v69`** | **`sync_mbb` (10k 扩容)** | **10,000** | **500 / 100** | — | **5.38% [5.20, 5.55]** | **7.42% [7.10, 7.77]** | **29.65%** | `DD>20%=0.00%` |
| `v179` | `sync_mbb` | 256 | 12.8 / 2.6 | 13.78% (1.08) | 17.72% (1.89) | 22.94% (3.35) | 31.5% | 21.95% |
| `v179` | `sync_mbb` | 512 | 25.6 / 5.1 | 14.11% (0.95) | 18.24% (1.27) | 23.65% (2.74) | 30.5% | 21.89% |
| `v179` | `sync_mbb` | 1,024 | 51.2 / 10.2 | 14.09% (0.65) | 18.71% (0.85) | 25.08% (1.68) | 32.2% | 21.58% |
| `v179` | `sync_mbb` | 2,048 | 102.4 / 20.5 | 13.96% (0.34) | 18.35% (0.59) | 24.67% (1.47) | 31.4% | 21.26% |
| `v179` | `sync_mbb` | 4,096 | 204.8 / 41.0 | 13.84% (0.14) | 18.14% (**0.54**) | 24.39% (**1.51**) | 31.4% | 21.25% |
| **`v179`** | **`sync_mbb` (10k 扩容)** | **10,000** | **500 / 100** | — | **17.79% [17.29, 18.28]** | **23.61% [22.74, 24.38]** | **31.25%** | `DD>20%=6.50%` |
| `v179` | `corr_gbm` | 4,096 | 204.8 / 41.0 | 14.64% (0.09) | 18.03% (0.16) | 22.53% (0.33) | 36.1% | 20.35% |
| `v182` | `sync_mbb` | 4,096 | 204.8 / 41.0 | 13.83% (0.18) | 18.26% (**0.56**) | 24.65% (**1.53**) | 31.1% | 21.40% |
| **`v182`** | **`sync_mbb` (10k 扩容)** | **10,000** | **500 / 100** | — | **17.92% [17.41, 18.45]** | **23.89% [22.97, 24.77]** | **31.11%** | `DD>20%=6.69%` |

- **统计稳定性诚实判定**：`v69` 在 `4,096` 路径下跨种子 SD（`0.18 pp`）已满足 `0.25 pp` 目标；但 `v179` 与 `v182` 在重尾 `sync_mbb` 下即使到达 `4,096` 路径，其 `CVaR95` 跨种子 SD 仍为 `0.54–0.56 pp`、`CVaR99` 跨种子 SD 仍达 **`1.51–1.53 pp`**（显著高于 `0.25 pp` 预设目标），因此对 `v179`/`v182` 如实标记为 **`STATISTICALLY_UNSTABLE_HIGH_TAIL_VARIANCE`**，不给出虚假收敛证明。经 10,000 路径扩容（`seed=1729`）后，`V179` 单子 `CVaR95` 95% 区间半宽收敛至 `0.50 pp`、`CVaR99` 半宽收敛至 `0.82 pp`。
- **同路径配对比较（10,000 路径 `sync_mbb`）**：`V179 − V69` 年化收益均值差 `+6.72 pp ± 0.12 pp`（胜率 `68.0%`），但伴随 3.3 倍更高的 `CVaR95`（`17.79%` vs `5.38%`）；`V182 − V179` 均值差仅 `+0.08 pp`（`CVaR99` 反而恶化 `+0.28 pp`）。

### 4.3 六大市场模型对比与三重误差分解（`n = 2,048`，3 种子均值）

| 市场模型（Tier-2 完整动态回放） | `V69` 均值 / CVaR95 / CVaR99 / 亏损率 | `V179` 均值 / CVaR95 / CVaR99 / 亏损率 | `V182` 均值 / CVaR95 / CVaR99 / 亏损率 |
|---|---:|---:|---:|
| `corr_gbm`（多资产联合 GBM） | +2.6% / 5.61% / 7.23% / 34.8% | +8.8% / 18.15% / 22.76% / 36.4% | +8.9% / 18.36% / 23.13% / 36.1% |
| `sync_mbb`（同步移动区块 Bootstrap） | +2.8% / 5.48% / 7.64% / 29.5% | +9.4% / 18.25% / 24.65% / 31.5% | +9.5% / 18.40% / 24.92% / 31.1% |
| `regime_cond_bootstrap`（状态条件 Bootstrap） | +2.7% / 5.54% / 7.44% / 30.4% | +9.3% / 18.53% / 24.47% / 31.9% | +9.4% / 18.58% / 24.62% / 31.7% |
| `student_t_garch`（厚尾 Student-t + GARCH） | +2.6% / 5.49% / 7.24% / 33.3% | +8.8% / 17.81% / 22.76% / 35.5% | +8.9% / 17.99% / 23.05% / 35.1% |
| `corr_shock_stress`（相关性向 0.88 坍塌冲击） | +1.5% / 7.18% / 9.11% / 45.4% | +4.8% / **22.09% / 27.29%** / 47.1% | +4.9% / **22.43% / 27.76%** / 46.8% |
| **`down_jump_stress`（共同市场下跳压力）** | **-1.5% / 9.70% / 12.00% / 67.7%** | **-4.9% / 29.09% / 35.17% / 69.3%** | **-5.0% / 29.58% / 35.90% / 69.5%** |

**误差结构核心发现**：
1. **抽样误差**（10k 路径 `CVaR95` 半宽）≈ **`±0.50 pp`**；
2. **市场模型与校准窗误差**（`target_4y` `13.99%` → `history_10y` `19.42%` → `corr_shock` `22.09%` → `down_jump` `29.09%`）高达 **`5.4–15.1 pp`**；
3. **执行模型误差**（`normal 10 bps` → `30 bps + 10 bps 滑点 + 0.02% 成交量上限`，V179 `sync_mbb` 均值收益从 `+9.6%` 降至 `+6.0%`，`CVaR95` 上升 `+2.84 pp` 至 `20.89%`）≈ **`2.8–3.6 pp`**。
4. **Tier-1 vs Tier-2 不可替代性**：RTX 2070 GPU 上 50,000 路径固定权重 `MBB` 给出 V179 `CVaR99 = 27.63%`，比完整动态策略回放（`23.61%`）高估 `4.02 pp`（忽略动态趋势减仓）；而固定权重 `GBM` 给出 `CVaR95 = 15.89%`，比动态回放（`18.03%`）低估 `2.14 pp`（忽略周频换手费用与震荡损耗）。
5. **Student-t GARCH 价值评估**：在本组合日频参数下，其 20 年 `CVaR99`（`22.76%`）与联合高斯 GBM 接近，低于非参数 `sync_mbb`（`24.65%`）与 `regime_cond_bootstrap`（`24.47%`），在极端崩盘聚类捕捉上不如同步区块与压力模型，因此仅保留为对照研究模型。

---

## 5. 第六阶段：多策略共享账户联合风险（基于 #260 公共资金政策）

### 5.1 #260 公共资金政策合约校验与尾部相关性

- 已在真实 `PortfolioStore`（SQLite）上验证 `#260` `CapitalPolicy` 的 `SHARED_SAME_DIRECTION` 模式配置（账户预算 `\$300,000 USD`，三策略预算各 `\$100,000 USD`，单标的上限 `\$105,000 USD`，`contract_verified = true`）。
- **高相关性与下尾聚集警示**：
  - 主窗口日收益相关性：`V69 vs V179 = 0.9804`、`V69 vs V182 = 0.9821`、`V179 vs V182 = 0.9980`；二十年窗口分别为 `0.9789`、`0.9807`、`0.9988`。
  - **经验 5% 下尾相关性 $\lambda_L$**：`V69 vs V179/V182 = 0.839`（主窗口）/ `0.855`（二十年），`V179 vs V182 = 0.942`（主窗口）/ `0.962`（二十年）。三策略同日同时亏损比例高达 **`41.2%`**（主窗口）/ **`39.7%`**（二十年），同标的持仓重叠日高达 **`92.9%`**（主窗口）/ **`86.0%`**（二十年）。

### 5.2 Alpaca USD 共享账户多分配方案对比（真实共享约束 vs 简单加权对照）

完整结果见 [shared_account_alpaca_schemes.csv](shared_account_alpaca_schemes.csv) 与 [shared_account_risk.json](shared_account_risk.json)。

| 窗口 | 共享账户分配方案 | 共享真实 CAGR（vs 简单加权差） | MaxDD | Sharpe | 日 CVaR95 | 资金闲置率 | 持仓重叠日占比 | 峰值单标敞口 | 现金竞争次数 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `target` | `equal_weight_3` (1/3, 1/3, 1/3) | 23.10% (-0.01 pp) | -11.60% | 1.78 | 1.63% | 50.3% | 92.9% | 21.1% | 0 |
| `target` | `risk_budget_inv_vol_3` (dev_b 因果反波) | 16.77% (+0.02 pp) | -8.39% | 1.80 | 1.19% | 63.6% | 92.9% | 14.4% | 0 |
| `target` | `fixed_20_40_40` | 26.10% (-0.01 pp) | -13.04% | 1.77 | 1.83% | 44.1% | 92.9% | 24.2% | 0 |
| `target` | `pair_v69_v179_50_50` | 19.73% (+0.02 pp) | -9.80% | 1.80 | 1.39% | 57.9% | 92.9% | 17.1% | 0 |
| `target` | `pair_v179_v182_50_50` | 30.67% (+0.01 pp) | -15.19% | 1.77 | 2.14% | 35.0% | 92.9% | 28.8% | 0 |
| `target` | `dynamic_regime_research_only`* | 25.51% (+2.40 pp) | -13.05% | 1.75 | 1.84% | 44.5% | 92.9% | 24.2% | 0 |
| `twenty` | `equal_weight_3` | 8.57% (-0.03 pp) | -20.42% | 0.81 | 1.64% | 56.0% | 86.0% | 22.2% | 0 |
| `twenty` | `risk_budget_inv_vol_3` | 6.32% (-0.01 pp) | -15.27% | 0.81 | 1.20% | 67.8% | 86.0% | 14.8% | 0 |
| `twenty` | `fixed_20_40_40` | 9.62% (-0.02 pp) | -22.68% | 0.81 | 1.84% | 50.5% | 86.0% | 25.6% | 0 |
| `twenty` | `pair_v69_v179_50_50` | 7.36% (-0.02 pp) | -17.35% | 0.82 | 1.39% | 62.8% | 86.0% | 18.0% | 0 |
| `twenty` | `pair_v179_v182_50_50` | 11.17% (+0.00 pp) | -25.89% | 0.82 | 2.15% | 42.3% | 86.0% | 30.5% | 0 |
| `twenty` | `dynamic_regime_research_only`* | 9.02% (+0.41 pp) | -22.58% | 0.80 | 1.76% | 51.7% | 86.0% | 25.6% | 0 |

*\*`dynamic_regime_research_only` 仅作为研究对照方案，不改变生产 `#260` 静态公共资金政策。*
- **同时调仓与成交顺序敏感性**：由于三策略各自仓位上限均 `≤ 90%` 且共享账户总敞口上限 `90%`，先卖后买后始终留有 `≥ 10%` 现金缓冲，在常规流动性下 `cash_contention_events = 0`，`strategy_order`、`reverse_order` 与 `proportional_cash` 三种顺序结果完全一致（`23.1009%` CAGR / `-11.6006%` MaxDD / `2,126` 笔成交）。

### 5.3 Binance USD-M 独立账户与跨交易所隔离汇总风险

- 基于本地已审计 193 天真实 `NVDAUSDT` 1h 数据（`4,632` 根 K 线、`593` 条资金费记录，均值 `0.7256 bps/8h`），在**严格不共享现金或保证金**的前提下与 Alpaca 共享账户对齐重叠 `132` 个交易日（`2026-05-15` 至 `2026-11-20`）：
  - Binance `NVDAUSDT` 1x 网格独立重叠表现：净收益 `-4.38%`、MaxDD `-6.30%`、日 `CVaR95 = 2.19%`（与 P8-R11/P8-S02-F01 结论一致）。
  - 跨所相关性：Alpaca 共享组合 vs Binance 网格日相关 `0.0692`（vs NVDA 现货日相关 `0.2530`）。
  - 隔离跨所汇总（Alpaca USD / Binance USDT 权重 `100/0 → 95/5 → 90/10 → 80/20`）：重叠期组合净收益由 `-2.11%` 恶化至 `-2.19% / -2.27% / -2.45%`，最大回撤由 `-10.79%` 降至 `-10.27% / -9.75% / -8.92%`，日 `CVaR95` 由 `2.13%` 降至 `2.03% / 1.96% / 1.85%`——本质为低相关负收益资产的敞口稀释，非正期望分散化增益。

---

## 6. 第九阶段：多维状态合同与 Champion Selector 最终判定

完全复用现有 `ChampionSelector`（[champion.py](../../../src/factorlab/research/champion.py)）与 `ExperimentTracker`（[tracker.py](../../../src/factorlab/experiments/tracker.py)），不建立第二套晋升状态机。详见 [champion_evaluation.json](champion_evaluation.json)。

### 6.1 七维验证状态矩阵

| 验证维度 | `V69` | `V179` | `V182` |
|---|---|---|---|
| `SOURCE_REPLAY` | `PASSED`（Qlib + `fixtures/target` `5.82e-11 USD`） | `PASSED_23_ACCOUNTS`（`4.66e-10 USD`） | `PASSED_23_ACCOUNTS`（`4.66e-10 USD`） |
| `SIGNAL_EQUIVALENCE` | `PASSED_NATIVE_FL`（`6.77e-17`） | `PENDING_P10_03_NATIVE_PORT` | `PENDING_P10_03_NATIVE_PORT` |
| `FULL_REFERENCE_REPLAY` | `PASSED_NATIVE_FL`（`6.98e-10 USD`） | `FL_EXECUTION_EXACT_PENDING_P10_03_NATIVE_SIGNAL`（`2.79e-9 USD`） | `FL_EXECUTION_EXACT_PENDING_P10_03_NATIVE_SIGNAL` |
| `EXECUTION_ROBUSTNESS` | `CONDITIONAL_COST_AND_CONCENTRATION_SENSITIVE` | `CONDITIONAL_COST_AND_CONCENTRATION_SENSITIVE` | `CONDITIONAL_COST_AND_CONCENTRATION_SENSITIVE` |
| `STATISTICAL_RELIABILITY` | `CONVERGED_CVAR95_TAIL99_DIAGNOSTIC_MONITORED` | `STATISTICALLY_UNSTABLE_HIGH_TAIL_VARIANCE` | `STATISTICALLY_UNSTABLE_HIGH_TAIL_VARIANCE` |
| `FINANCIAL_VALIDITY` | `CONDITIONAL_HISTORICAL_EDGE_HIGH_SNDK_CONCENTRATION` | `CONDITIONAL_HISTORICAL_EDGE_HIGH_SNDK_CONCENTRATION` | `CONDITIONAL_HISTORICAL_EDGE_HIGH_SNDK_CONCENTRATION` |
| `OOS_VALIDITY` | `INSUFFICIENT_FRESH_OOS_HISTORICAL_REUSE_ONLY` | `INSUFFICIENT_FRESH_OOS_HISTORICAL_REUSE_ONLY` | `INSUFFICIENT_FRESH_OOS_HISTORICAL_REUSE_ONLY` |
| **Champion Verdict** | **`INCONCLUSIVE`（保留 incumbent）** | **`INCONCLUSIVE`（未晋升）** | **`INCONCLUSIVE`（未晋升）** |

### 6.2 Champion Selector 配对统计与 `V182 vs V179` 裁定

| 评估窗口（均标记 `HISTORICAL_REUSE_PSEUDO_OOS`） | Champion 结论 | `V179 vs V69` 年化均差 / 块胜率 / 风险调整分数 CI | `V182 vs V179` 诊断年化均差 / 块胜率 | 核心否决原因 |
|---|---|---:|---:|---|
| `target/normal` | `INCONCLUSIVE` (保留 `v69`) | +19.91 pp / 57.7% / `[-0.082, +0.260]` | **-0.95 pp / 48.5%** | 非全新 OOS、分数 CI 跨零、块胜率 `< 60%` |
| `target/no_sndk` | `INCONCLUSIVE` (保留 `v69`) | +9.91 pp / 53.6% / `[-0.112, +0.144]` | +0.22 pp / 51.5% | 非全新 OOS、分数增量不足、HAC/Bootstrap 跨零 |
| `twenty/normal` | `INCONCLUSIVE` (保留 `v69`) | +8.35 pp / 52.7% / `[-0.075, +0.048]` | **-0.25 pp / 47.9%** | 非全新 OOS、风险调整分数未优于 V69 |
| `twenty/no_sndk` | `INCONCLUSIVE` (保留 `v69`) | +6.40 pp / 51.9% / `[-0.088, +0.027]` | **-0.02 pp / 49.7%** | 非全新 OOS、各项检验均跨零 |
| `challenge/normal` | `INCONCLUSIVE` (保留 `v69`) | +6.34 pp / 55.8% / `[-0.137, +0.063]` | +0.03 pp / 43.5% | 非全新 OOS、各项检验均跨零 |

> **明确结论**：`V182` 虽然编号最新，但在历史主窗口、二十年窗口与成对块胜率上均未优于 `V179`，且在风险调整后目标分数（`mean - 1.0 * vol`）上 `V179`/`V182` 均未在统计意义上显著优于低回撤 incumbent `V69`。因此严格输出 **`INCONCLUSIVE`**，保持现有生产策略不变。

---

## 7. 可复现入口、配置与软件回归验证

- **核心代码模块**：
  - [config.py](../../../src/factorlab/research/external_validation/config.py)、[audit.py](../../../src/factorlab/research/external_validation/audit.py)、[replay.py](../../../src/factorlab/research/external_validation/replay.py)、[financial.py](../../../src/factorlab/research/external_validation/financial.py)、[monte_carlo.py](../../../src/factorlab/research/external_validation/monte_carlo.py)、[shared_portfolio.py](../../../src/factorlab/research/external_validation/shared_portfolio.py)、[evaluation.py](../../../src/factorlab/research/external_validation/evaluation.py)、[pipeline.py](../../../src/factorlab/research/external_validation/pipeline.py)
- **冻结配置与 CLI**：
  - [p10_04_external_validation.json](../../../configs/research/p10_04_external_validation.json)
  - [run_external_strategy_validation.py](../../../tools/research/run_external_strategy_validation.py)
- **标准复现命令**：
```powershell
$env:PYTHONPATH = "src"
python tools/research/run_external_strategy_validation.py `
  --config configs/research/p10_04_external_validation.json `
  --output <new_output_dir>
```
- **本地软件回归验证（Python 3.13.9，最新改动后实测）**：
  - `pytest -q tests/research/test_p10_04_external_validation.py tests/research/test_v69_*.py tests/validation/test_v69_f01.py tests/research/test_mc_tail_reliability.py tests/research/test_monte_carlo.py tests/research/test_champion_selector.py tests/research/test_experiment_tracker.py tests/trading/test_shared_capital_*.py tests/research/test_config*.py tests/security` → **`701 passed in 592.10s`**（0 失败，0 跳过）。
- **后续与 P10-03 联调方式**：待 P10-03 在 `src/factorlab/strategies/{v179,v182}_native.py` 提供冻结提交并暴露 `build_signal_provider(...)` 后，本流水线将自动优先加载原生实现并更新 `SIGNAL_EQUIVALENCE` 与 `FULL_REFERENCE_REPLAY` 为 `PASSED_NATIVE_FL`。
