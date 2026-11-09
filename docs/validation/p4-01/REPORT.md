# P4-01：Research Input Preflight

基线：`e27d2f893a9f432ea67f80a69f4e9300a9796a13`。

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

`pipeline_preflight` 消费现有 DataHub 与 `ResearchPipeline.HubQuery`，只检查输入合同，不运行 DatasetBuilder、因子、训练或投资组合。工程 PASS 与正式金融资格无关；请求正式研究时始终返回 `formal_request=REJECTED`，不能把输出作为资格包传入 ResearchPipeline。

## 输入合同

每个六类输入组由若干 `InputBinding` 组成，查询固定路由来源、返回版本、数据类型、含端点日期及证券；另固定返回元数据的 `source_id` 和**该次范围响应**的内容哈希。版本比较严格使用 `data_version`，与 ResearchPipeline 一致，不能用不同的治理归档版本替代。日历只接受一个权威响应；基准只接受一个显式证券。禁止重复键、跨源重叠及自动优先级融合。

原始行情要求 raw/adj OHLC、volume、amount、turnover；基准要求 open；状态要求 trade_status（0/1）和 is_st；上市表要求 listing_date、可空 delist_date。成员表支持正成员行，或显式 is_member 布尔列；缺行不生成历史非成员。数值字段需明确单位，原价/调整价各字段需原生字段或归档成员的来源声明，不能通过复权价重建原价。声明保留为来源声明，不是独立经济单位或HFQ认证。

内容身份沿用 `table_sha256`；日历 DatetimeIndex 以单列 date 表标识，公司行为沿用 `json_bytes`/`sha256` 事件包身份。元数据中的范围 `research_table_sha256` 同时核对；完整归档的 table_sha256 不冒充范围哈希。元数据、获取/供应商可得时间、上游分片收据完整保留；`known_at` 仍为 UNKNOWN，导出时间不转换为历史首次可得时间。

公司行为通过独立 `ActionBinding` 调用现有 DataHub `corporate_actions`，绑定稳定 security_id、版本、完整输入窗口及事件包身份；上市表必须提供不冲突的证券映射。空事件包也要求明确 `COMPLETE_QUERY_EVENT_COVERAGE` 来源覆盖声明，不能将未提供的事件当作“无公司行为”。该声明只描述工程查询覆盖，不认证公司行为全集或历史修订。事件版本、原件哈希、公告及可得时间原样引用。

API 示例：

```python
report = pipeline_preflight(
    hub, bindings, symbols=("600000.SH", "000001.SZ"),
    start="2021-01-04", end="2021-01-19",
    research_start="2021-01-07", research_end="2021-01-11",
    warmup_sessions=3, holding_days=5, execution_tail_sessions=1,
    actions=action_bindings,
)
```

六组绑定与公司行为绑定由调用方预先固定，不从读取结果自动批准版本。`require_actions=False` 只适用于明确不消费公司行为的诊断，不能解除正式阻塞。命令行的 D07 适配入口只路由已经过既有归档读取器验证的六类表；完整事件包的独立消费使用上述 Python API。

## 覆盖与历史边界

输入、研究窗口均含端点。预热、研究段和尾部以所选日历分层。尾部至少为 `max(holding_days + 1, execution_tail_sessions)` 个会话，沿用既有 t+1/t+(holding_days+1) 端点诊断，不生成收益标签。每证券另检查实际预热行情行数，不能以日历长度冒充证券预热。研究起止必须是所选日历会话。

矩阵逐证券/会话保留行情、成员、状态和基准观测。上市前/退市后的无行标为 `NOT_EXISTING_OBSERVATION`，不是供应商漏数；声明存续期内缺行情/状态为输入覆盖缺口。缺少历史资格时均保留 `historical_eligibility=UNKNOWN`。成员缺日/某证券整个研究段没有成员观察会拒绝工程合同，不从未来成员、价格或状态恢复过去。未来价格扰动不改写成员矩阵及知识时间。

## 实际输入与复用证据

`input-definition.json` 固定 D07 原双证券 BigQuant 查询（601857.SH、600000.SH）；其版本及范围内容身份来自原收据。沿用2017—2018原始范围，仅复用历史运行信息，不重新读取两年行情。预热244会话，研究段2018-01-02—2018-12-20，5日标签及1日执行要求显式声明。旧聚合不能提供其未绑定的五类源、调整价、公司行为、单位合同或历史可得时间。

`actual-input-gaps.json` 的原始输入消费为 **NOT_RUN**：未向本次执行提供受控原始归档路径。固定 `real-multisource.json` 身份为 `64163b6c2ce3f797da1d5b49a5c2bb1b110f550bebbfa352df0860cd1917f0b5`；保留其四个来源/证券查询、元数据和检查，标记 `REUSED_UPSTREAM_RUN_NOT_RECOMPUTED`。这不是本轮真实端到端成功。缺失的五组定义明确为空，不发明 source/version/hash。

TODO：补齐五类查询及原价/调整价单位与来源声明、稳定证券映射和公司行为覆盖收据，再消费同一明确范围的受控原始数据；历史PIT、HFQ及正式市场/执行资格仍需现有证据流程。

`synthetic-preflight.json` 是两个证券、12个会话的小型工程正向夹具，包含3日预热、3日研究、6日退出观察，分别绑定证券来源。它只验证软件合同。

## 复现

在工作树根目录使用现有依赖环境，输出指定新文件。合同不满足时 CLI 写出 JSON 和中文摘要后退出1；输入定义或固定收据身份损坏直接报错。

```sh
python -I -B -X utf8 tools/data/audit_p4_01_pipeline_inputs.py --manifest docs/validation/p4-01/input-definition.json --output <new-gaps.json>
```

消费已有受控 D07 数据时，另传 `--bigquant-root <BQ> --membership-zip <members.zip>`，并提供六组**事先固定的范围查询**清单。D07源的完整表内容身份与单证券切片身份分别保留。不会根据聚合收据生成原始行，不下载数据。

定向测试命令（从仓库根目录）：

```sh
python -I -B -X utf8 -c "import sys; sys.path.insert(0, 'src'); import pytest; sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', 'tests/research/test_p4_01_pipeline_inputs.py', 'tests/research/test_real_data_readiness.py', 'tests/research/test_d06_readiness_integration.py', 'tests/research/test_qualification_admission_boundaries.py']))"
```

测试涵盖完整六类工程合同、双证券来源分离、显式分层；缺源/缺组/缺字段/空值/重复/证券冲突/日期越界、版本与两种哈希错误、预热与尾部不足、上市前无行归因、未来价格扰动、原价来源/单位/事件缺失、固定收据篡改和CLI输出。正式门禁测试直接调用未修改的 ResearchPipeline REAL_CANDIDATE 路径，由现有准入函数拒绝无资格包请求。

现有 Python 3.13 环境：首轮四模块 **85 passed / 46.63秒**，见 `targeted-tests.xml`。补充公司行为覆盖声明、日历表示/顺序及 D07 范围视图后，最终新增模块 **36 passed / 4.21秒**，见 `preflight-final-tests.xml`；两个结果有重叠，不相加。日历必须是 ResearchPipeline 可直接消费的递增 DatetimeIndex，不能仅因 DataFrame 有 date 列就判定接口满足。

实际缺口CLI按预期退出1，输出工程 FAIL、9项未满足合同、原始消费 NOT_RUN；小型合成预检输出工程 PASS、0项未满足合同。两者均保持正式金融准入 BLOCKED。未运行全库矩阵、五年重算或模型训练。
