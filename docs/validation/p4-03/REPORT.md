# P4-03：受控真实输入组装与预检

2026-11-09；主线基线 `4b1baad1f661be5728cfb79ee9a4db5bc1b9f32f`。

**PARTIAL — NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

本轮实际读取已有商业归档，通过 D06 读取器及 P4-01 范围视图，将 BigQuant 和 JoinQuant 两个来源的六类输入送入原有 `pipeline_preflight`。六组查询均已提供，10次来源查询均完成字段、内容身份和基础结构读取；但工程结果仍为 **FAIL，26项未满足合同**。查询收据的 PASS 不代表该组单位、证券覆盖或公司行为合同整体通过。正式请求明确 REJECTED。

P4-01 原报告是2017—2018年双证券范围、9项问题、原件消费 NOT_RUN；这里是另一份预先固定的小窗口，不能将26与9相减作为改善率。实质进展是消除了未注册来源及五类未提供查询，实际暴露了单位、历史身份和预热缺口。

## 来源选择与冻结

先检查 D06/D07 已有观察、归档 schema、分区收据及成员归档清单，再固定 [selection.json](selection.json)：

- 证券：`600363.SH`、`601868.SH`，复用 D07 2021年秋季实际观察对象；不预设两者都是中证500成员。
- 输入：2021-09-13—2021-11-22，含端点。
- 研究：2021-09-28—2021-11-12，29个供应商CN会话。
- 预热：2021-09-13—2021-09-27，9会话；尾部：2021-11-15—2021-11-22，6会话。
- 标签持有5日，执行尾部要求1会话；原预检按 `max(5+1, 1)` 检查尾部。仅审计原价端点，不计算收益或训练模型。

BigQuant 原价、调整价、状态的2021年9—11月分区，与 JoinQuant 从2021-09-13开始的历史成员材料相交。基准和日历使用其2021年度分区，上市资料使用现有未分区快照。没有把 D07 2017—2018年601857/600000行情与2021年以后的成员材料拼接。

盘点读取生成 [input-definition.json](input-definition.json)，冻结每次查询的 source/kind/version/security/日期/范围内容哈希及原始成员身份。随后按该清单独立重放；最终 CLI 复现命令元数据修正后再产生最终收据，窗口和身份不变。盘点不是自动准入；后续重放不会重新生成或接受漂移后的 pins。

## 六类来源、能力与覆盖

实际能力矩阵和字段/类型/单位/空值/唯一键/覆盖边界在 [INPUT_MATRIX.csv](INPUT_MATRIX.csv)；逐查询原生元数据、版本、哈希和逐证券日缺失分类在 [receipt.json](receipt.json)。所有查询均无重复主键；六类最低字段均存在，但以下单位和补充身份缺口仍阻断。

|输入|真实来源及原生表|DataHub capability|本窗口实际观察|合同缺口|
|---|---|---|---|---|
|bars|BigQuant `cn_stock_real_bar1d` + `cn_stock_bar1d`|`daily_bars`|600363为44行；601868为35行，从9月28日开始|每证券11项数值单位未声明，共22项；601868预热0/9|
|benchmark|BigQuant `cn_stock_index_bar1d`，000905.SH|`index_bars`|44行，9月13日—11月22日|open经济单位未声明，1项|
|membership|JoinQuant `members_daily.csv`，000905.XSHG历史正成员快照|`membership`|600363为0行；601868为35行，从9月28日开始|600363研究段成员身份 UNKNOWN，1项|
|status|BigQuant `cn_stock_status`|`security_status`|600363为44行；601868为35行|观察级 suspended/ST 映射可消费；不是已认证历史状态|
|listings|BigQuant `cn_stock_basic_info` 快照|`listings`|每证券1行；601868上市前9会话没有行情/状态|缺稳定 security_id，公司行为映射未绑定，1项|
|calendar|BigQuant `trading_days` 的CN记录|`calendar`|44个递增唯一会话，返回 DatetimeIndex|供应商观察日历，不是独立认证的交易所历史日历|

状态映射沿用 D06：`suspended == 0 → trade_status=1`，非零为0；`st_status != 0 → is_st=True`；缺值仍为缺值。0/1是软件观察映射的无量纲单位声明，不认证供应商编码的历史金融资格。原始ST/停牌字段仍在既有读取器投影及来源身份中，不将未知状态默认成正常。

其他已有来源的能力界限：CSMAR 原生 `CsmarAdapter` 支持 `daily_bars`、`security_status`、`observed_sessions`，不能从证券行情推导基准、成员、上市表或官方日历；D07 的 prepared-v4 又限于601857及2017—2018年既有准备范围，因此未注册到本窗口。现有 `CorporateActionAdapter` 支持 `corporate_actions`，但须独立稳定身份、来源快照及可复核覆盖材料；这里没有为其伪造注册。JoinQuant 只注册实际提供的 membership，不作为完整行情来源。

## 原价、调整价、公司行为与时间角色

原价和调整价按 `(date, symbol)` 使用原读取器的一对一合并，八个价格字段的来源分别固定到两个原生表。本范围两证券这些必需数值字段均无空值；不从调整价反推原价，不将供应商调整价称为认证HFQ。价格、volume、amount、turn/turnover单位保留 `UNKNOWN_VENDOR_CONTRACT`；schema只给出物理类型，不能充当经济单位合同。

上市快照没有稳定 `security_id`。代码仅用于显式证券查询，不被转换成公司行为身份；本轮不发出公司行为事件查询，`action_receipts=[]` 表示未绑定，不表示历史无事件。现有事件适配器的来源覆盖记录也不能被路由层擅自改成 `COMPLETE_QUERY_EVENT_COVERAGE`。

- 市场观测有效日期是 `date`；上市边界是 `listing_date/delist_date`，均只是原源观察。
- 来源 `announced_at`、历史 `known_at`、首次分发和修订版本仍 UNKNOWN；没有从2026年获取时间构造2017/2021年的可得时间。
- BigQuant 每个消费分片保留2026-11-07的 `exported_at_utc`；它是导出角色。原 DataHub `acquired_at` 未提供，仍保留 null。
- JoinQuant manifest 的 `run_completed_utc=2026-10-23T21:01:58Z` 单独记为获取完成声明；`historical_available_at=null`。它不被写入 known_at。
- `source_version` 保留原读取器的消费分片身份版本；不是历史vintage证明。

## 覆盖与未满足合同

原预检保留88个证券/会话格子。600363有44个行情/状态观察，成员全部 UNKNOWN。601868有35个行情/状态/成员观察；上市前9会话的缺行情、状态、成员分类为 `NOT_EXISTING_OBSERVATION`，历史资格仍 UNKNOWN，不索取不存在的上市前价格，也不跨600068拼接历史。

研究段600363无正成员记录，不能凭零行证成非成员；601868有29个研究成员观察，但不因此取得PIT资格。601868研究信号的原价入口/退出端点可观察，末个信号2021-11-12的退出观察到2021-11-22；600363没有成员信号，不生成其成员收益标签。

26项由 **23项单位缺口 + 1项公司行为稳定映射缺口 + 1项证券预热不足 + 1项成员未知** 构成。预热不足是本窗口证券自身上市边界，不是通过适配或补零就能修复的缺数。当前六类来源在同一时期可读取，但不能宣称所有证券的完整研究合同共同通过。

## 原始身份与可复现性

冻结13个身份条目：12个BigQuant Parquet成员和1个JoinQuant成员文件；另记录各消费表schema、对应收据原始字节哈希及JoinQuant manifest哈希。D06实际校验每个消费Parquet载荷与其收据，额外核验JoinQuant载荷与manifest声明；重放再比较冻结的全部原始身份。改内容同时重写供应商收据也不能绕过旧pins。

一次有界读取投影822,365条物理Parquet行，12成员合计50,089,021字节解压载荷；这不是44会话×两证券的最终行数，也不是整库扫描或SSD物理读量。成员CSV复用已有611,500行文件，仅过滤所选日期/证券，不重新计算五年成员全集。公共交付不含商业证券日原价、调整价或完整商业载荷。

最终收据绑定基线提交、实现文件内容身份、输入定义哈希、Python 3.13.9/pandas版本及路径参数化命令。源码变更由实现哈希补充标识，不能将基线提交误认为未提交工具已经存在于该提交。原件不可提供时，同一CLI只读取固定上游D07收据，并明确 `actual_raw_consumption=NOT_RUN`；聚合不会生成原始行或通过六类合同。

## 最小下一步

1. **补来源合同，不猜单位**：索取本版本两行情表的价格/volume/amount/turn以及指数open的经济单位和价格语义。当前物理字段已齐，不需要重新下载全库。
2. **补身份与覆盖证据**：提供稳定证券映射、完整窗口的公司行为来源查询覆盖及可核查事件/修订材料。TODO：证据齐备后，复用现有 `ActionBinding`/`corporate_actions` 合同接入；不要在路由层制造全集声明。
3. **澄清成员观察**：核对600363在本窗口的独立成员材料；若不属于所需成员集合，应明确该实验对象不满足研究池合同，而不是补造成员行。零行不提供历史负成员认证。
4. **保留本窗口失败**：601868在上市前不可能有自身9会话预热。另一项未来实验可以事先选择具有真实预热历史的证券/窗口，但不得修改本次固定窗口以改变本次结论。
5. **历史资格补证**：历史首次分发、同版修订、身份、独立日历及HFQ算法/锚点/独立参考走既有资格流程。这里的路由修复只解决软件接线，不能替代这些材料。

## 复现与定向验证

在此工作树根目录运行，`<BQ>` 指向已有BigQuant归档根，`<MEMBERSHIP>` 指向既有 `csi500_full_history.zip`。使用已有依赖环境，`OMP_NUM_THREADS`、`OPENBLAS_NUM_THREADS`、`MKL_NUM_THREADS` 均为2。所有输出必须使用新文件名。

```sh
# 首次盘点，生成候选身份；不能用它覆盖已冻结清单以接受漂移。
python -I -B -X utf8 tools/data/audit_p4_03_controlled_inputs.py --inventory docs/validation/p4-03/selection.json --bigquant-root '<BQ>' --membership-zip '<MEMBERSHIP>' --output <NEW_DEFINITION.json>

# 重放本次已冻结清单；正常写出真实FAIL报告后退出1。
python -I -B -X utf8 tools/data/audit_p4_03_controlled_inputs.py --manifest docs/validation/p4-03/input-definition.json --bigquant-root '<BQ>' --membership-zip '<MEMBERSHIP>' --output <NEW_RECEIPT.json> --matrix <NEW_MATRIX.csv>

# 不提供商业原件：只能复核查询缺口及固定上游收据，NOT_RUN，退出1。
python -I -B -X utf8 tools/data/audit_p4_03_controlled_inputs.py --manifest docs/validation/p4-03/input-definition.json --output <NEW_NOT_RUN.json>

python -I -B -X utf8 -c "import sys; sys.path.insert(0, 'src'); import pytest; sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/research/test_p4_03_controlled_inputs.py', 'tests/research/test_p4_01_pipeline_inputs.py', 'tests/research/test_d06_readiness_integration.py', 'tests/research/test_real_data_readiness.py', 'tests/research/test_qualification_admission_boundaries.py', 'tests/research/test_datahub.py']))"
```

首轮五模块 **112 passed / 50.73秒**，见 [targeted-tests.xml](targeted-tests.xml)。最终工具的新增模块与DataHub组合 **39 passed / 14.06秒**，见 [cli-hub-tests.xml](cli-hub-tests.xml)；两个集合重叠，不相加。真实预检最终按预期退出1，输出26项问题而不是工程PASS。

正向夹具验证六类直接可消费、两来源明确隔离及日历/预热/尾部；负向验证来源/kind/窗口错路由、重复注册、版本与范围哈希漂移、原始成员/收据/schema/manifest身份漂移、缺turnover字段、无成员观察、上市前、停牌与缺状态、未来区间价格扰动不改历史身份/可得时间、PIT cutoff拒绝、无原件NOT_RUN。正式边界测试直接调用未修改的 ResearchPipeline REAL_CANDIDATE 路径，拒绝缺少独立可重放资格包的请求。
