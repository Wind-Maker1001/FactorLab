# P10-G01：历史价格基准安全消费约束

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

基线为启动时重新获取的 `origin/main`：`0cf5404510b497cf2ad0d974d21a04119219a0df`。交付采用限定研究适配器，不改变正式金融准入，也不为全部供应商历史行情认证复权或 PIT。

## 证据与接口边界

核对已合并的 #80、#85、#89、#88、#92，以及 DataHub、因子注册和执行接口。#80 的来源哈希及导出版本不是历史首次分发时间；#85 的精确数值对账不自动证明经济同义；#89 的 135,074 个四价共现差异日及公司行动关联不能外推为全市场复权认证。两个窗口的 172 项精确变换关系、300682 的六项残差分别保留。BigQuant 经济单位、精度合同继续 UNKNOWN。

DataHub 已有 `QueryResult` / `SourceMetadata`，包括来源版本、时间信息和 `field_semantics`；没有字段级用途许可。`FactorDefinition` 已有 `input_fields`、`price_basis`、依赖与定义哈希；RAW/HFQ 描述因子需要的口径，不证明输入来源满足它。执行器已有独立的开/收盘字段配置及成交量、状态、涨跌停检查。#88 的 `Valuation` 已将 `raw_close=None` 与模型估值分离，#92 保留此边界。

因此不新增第二个 DataHub 或金融认证引擎，只在既有 `field_semantics[field].consumption` 中增加用途合同；因子新增从已有声明推导的 `consumption_requirements` 属性，不改变定义字段或哈希。完整合同见 `price-contract.json`，用途边界见 `consumption-matrix.csv`。

## 合同的含义

每个价格字段分别记录来源、版本、字段身份、单位、基准、调整政策、观察证券日期范围、信息可得时间、允许用途、资格、拒绝原因及证据定位。版本应绑定上游快照/原件；消费方另外传入其固定的 `(source_id, data_version)`，并与对象元数据、字段合同三方比较。更换来源、版本、字段、证券或日期不能沿用原资格；重叠范围不是任选其一，而是歧义拒绝。

基准区分 `OBSERVED_RAW`、`ADJUSTED_CANDIDATE`、`SUPPLIER_FILLED`、`MODEL_VALUATION_ASSUMPTION`、`UNKNOWN`、`CONFLICT`；非价格的量/额使用 `NOT_PRICE` 并另核经济单位。只有合同为 `RESEARCH_QUALIFIED`、证据非空、用途许可及全部语义要求同时满足，限定入口才接受。`RESEARCH_QUALIFIED` 是指定研究用途的操作条件，不是既有金融认证结果；合同中的声明和证据判读仍由上游证据生产者负责，消费检查不从数值或供应商名字制造证据。

`available_at=null` 明确为 UNKNOWN。未给 cutoff 的使用是回顾研究；提供 cutoff 时，未知或晚于 cutoff 的可得时间以及晚发生的调整事件均拒绝。观察日期、采集时间、文件版本不代替信息可得时间。原价必须使用 `adjustment_policy=NONE` 且没有调整事件；未来除息值不能凭资格字符串伪装成原价。

## 三个限定入口

- `factorlab.research.price_inputs.build_raw_panel`：在现有真实研究标签构造前检查 OHLC 的 RAW_LABEL / RAW_FACTOR 用途及成交量 shares 合同，再调用原 `build_panel`。原有停牌、状态、缺值路径和标签成熟期规则不变。
- `factorlab.data.hub.price_consumption.raw_forward_returns`：独立的原价收益入口，按显式交易日历计算端点收益，路径缺失不填充、不按剩余行移位。数值文本使用 Decimal 无损解析；收益最后输出浮点。没有跨源接近度比较、自动择源、舍入归一或价格比率认证。
- `compute_factors`：解析所有依赖的既有 FactorDefinition，只校验实际使用字段。原价因子、调整研究因子、amount/volume/turnover 单位要求分开处理。调整候选必须单独具备调整研究用途资格；分类为候选本身不赋权。`snapshot.compute` 的普通 DataFrame 调用保持原行为。
- `execution_bars` / `factorlab.research.price_inputs.run_execution`：检查执行器配置的价格字段、所有实际存在的 raw 字段及 execution_price 后调用原执行器。改名不能使模型基准合法化。独立估值列不传入执行行情；缺失 raw_close 不补写，停牌、状态、成交量与涨跌停仍由原执行合同决定。基准行情保留执行器原有独立 benchmark 合同。

输出标签、因子、研究面板及执行 NAV 附带消费来源/版本收据。独立账面估值仍使用现有 Valuation 和持仓/停牌证据政策；BOOK_VALUATION 用途许可不授予交易许可。同一日期可具备账面估值资格而不具备成交价资格。

**接入范围：旧 DataFrame API 没有新增全局强制检查。** 缺失上游合同的既有真实研究数据不被自动补发资格，也不因新增门禁使全部普通因子失效。新研究要获得本约束，应从 QueryResult 调用上述限定入口；直接绕回旧 API 不属于受保护路径。TODO：待既有真实研究来源具备版本绑定的字段合同后，将相应调用迁入限定入口；禁止用来源名称、价差拟合或本报告的局部案例代替补证。

## 真实定向验证

复用 #89 / #85 私有原生月检查点及已定位的原生分配记录。读取带证券过滤的指定月份，不重开行情 ZIP，不重建检查点，不扫描完整差异索引。证券和日期仅出现在验证工具的选例中，不出现在生产门禁规则中。

|案例|重新读取的结果|消费结果|
|---|---|---|
|600565 / 2019-05|80/80 项符合下一次现金除息变换|调整候选、资格 UNKNOWN；原价标签拒绝，历史 cutoff 也报告未来事件及可得时间缺口|
|600578 / 2019-07|92/92 项符合变换|同上，不认证全历史调整政策|
|300682 / 2018-12|74/80 项符合，6 项残差保留|整个未闭合对象不能当原价消费，不用测试公式修补残差|
|688189.SH / 六日|2020-10-30、11-02 至 11-06；六对原生行均零量，CSMAR Filling=1，收盘相同，开高低 18 项不同|填充对象被执行价格入口拒绝，不推断实际成交|
|601857.SH / 2017—2018|487 日、2,922 项六字段精确相等；CSMAR Filling 全为 0|固定字段字典/原件版本的限定原价路径通过；不提升金融准入|

601857 的研究资格只用于这个固定对象，依据已有字段字典、原件身份、原生填充状态和回归证据共同约束，不是因两源接近而选取 CSMAR。CNRDS 控制对象仍 UNKNOWN。实际重新计算 481 个可评估原价标签，与旧浮点公式绝对差不超过 `1e-15`；Decimal 运算次序与旧 binary64 除法可有末位差别。原价研究因子值精确相同。原执行器用相同原生价格、回顾性状态映射、单证券固定信号和原默认费率运行：486 会话、1 笔成交，NAV/成交/持仓/拒绝/信号表与未加门禁路径精确相同；这是兼容性探针，不是新增策略绩效。

来源版本替换及缺合同 UNKNOWN 的测试为在该控制对象上注入元数据变化的拒绝探针，不冒充重新读取 BigQuant 的实测。停牌 carry 与原价字段隔离、原价/调整字段改名仍拒绝、单位缺失、用途未授权、未来事件、可得时间未知、证券/日期/字段错位及范围歧义由定向测试覆盖；现有独立估值回归同时运行。

## 验收回答

1. **能否区分原价、复权候选和模型估值？** 能；字段基准与研究用途分别表达，候选状态不等于许可，模型估值保留独立对象。
2. **未认证价格能否静默进入原价收益标签？** 限定入口不能：缺合同或不满足资格直接给出具体 gaps。保留兼容的旧 DataFrame API 仍没有这项新检查，因此不声称全仓库所有调用已封堵。
3. **carry 能否进入成交价格？** 限定执行入口拒绝模型基准，即使列名为 raw_open/raw_close 或配置为执行价；缺失原价仍缺失，BOOK_VALUATION 不改变可成交状态。
4. **正常因子和回测是否兼容？** 既有 API 保留，原价合格对象经适配器计算的因子和执行账本与旧路径精确相同；真实研究面板的合成回归也核对标签/因子/样本输出不变。
5. **UNKNOWN 是否保守？** 是；缺版本、单位、合同、资格、用途、覆盖或所请求 cutoff 的可得时间均拒绝。不向任何未闭合供应商发放全库价格资格。

## 复现与成本

```text
python -m pytest tests/research/test_price_consumption.py tests/research/test_datahub.py tests/research/test_factor_registry_v2.py tests/research/test_execution_backtest_phase3.py tests/research/test_evidence_consumption_study.py tests/research/test_real_model_study.py -q --junitxml=<private-tests.xml>
python -I -X utf8 tools/data/p10_g01_validate.py --native <existing-native-checkpoints> --work <p10-d02-private-work> --output docs/validation/p10-g01 --tests-xml <private-tests.xml>
```

使用 Python 3.13，BLAS/OpenMP 和 Arrow 限制单线程。定向/相关回归 133 项通过；早期验证暴露的测试证券格式及原生字段到既有执行器的 schema 映射问题已修正。最终机器收据记录实际运行耗时、原生验证进程峰值工作集及测试计数。最多并行两个任务进程，最终复现命令顺序运行；原生读取约 0.15 GiB 峰值量级。正常消费仅检查内存中的元数据及所消费证券日期范围，不读取 ZIP 或历史证据库。

交付包含本报告、合同、消费矩阵、真实案例、兼容结果及验证收据六份文件。商业原始绝对价格、模型预测及冻结账本不进入公开产物。
