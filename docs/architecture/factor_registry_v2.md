# P2-05 Factor Registry V2

基线：`967f9201ce349676b9ad3f2719c162176ffab9fd`。可选入口为
`factorlab.factors.registry_v2`，不迁移 DatasetBuilder、研究入口、四个模型或模型工厂。

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## API 与身份

| API | 行为 |
|---|---|
| `FactorDefinition.builtin(name, version="1", factor_id=None)` | 现有 14 因子的不可变描述；名称精确匹配 |
| `FactorDefinition.from_expression(id, version, compiled)` | 重新验证 CompiledExpression，保存不可变 canonical JSON |
| `FactorDefinition.alias(id, version, target)` | 引用一个已固定 ID、版本、哈希的定义 |
| `FactorRegistryV2.with_builtins()` | 创建含原 14 因子的独立目录 |
| `register` / `register_expression` / `register_many` | 校验、重复检测、原子注册；批量注册支持前向依赖 |
| `get(id, version=None)` | 精确查询；多版本必须指定版本，不隐式选 latest |
| `resolve(refs)` | 按请求和依赖顺序确定性拓扑解析，检测缺失、哈希冲突、循环 |
| `freeze()` | 独立不可变快照；后续注册不改变旧快照 |
| `snapshot.compute(bars, refs, calendar=..., context=...)` | 显式计算列表，返回 date、symbol 和所选因子列 |

`FactorRef` 固定 ID、版本、definition_hash。相同 ID/version 重复注册失败，
不同内容占用同一 ID/version 失败；不同版本可以共存。相同数学定义可以使用
不同目录名称/版本，definition_hash 保持相同，FactorRef 则不同。
内容重复由 definition_hash 识别，不把命名别名误当作独立经济信号。

定义哈希涵盖执行类型与实现协议版本、输入字段、回看窗口、频率、价格基准、
依赖引用、表达式、可得时间约定和窗口规则。哈希不包括目录 ID/version，
也不包括实际数据、数据来源和金融认证。快照哈希另行包含完整目录及版本。
依赖引用包含目标定义哈希，不能静默绑定另一个版本或定义。

`lookback` 统一表示当前交易日之前所需的交易日数量，不含当天：
MOM20=20、VOL20=20、TURN20=19、AMIHU20=20。原对象的 `lookback` 属性保持原值。
`price_basis=("HFQ",)` 是公式所用价格类别，**不是 HFQ 质量认证**。
全部频率为日频、信号为收盘已知；不能把当日收盘信号当作当日开盘已知信号。

## 可选接入示例

```python
from factorlab.factors.registry_v2 import FactorDataContext, FactorRegistryV2

registry = FactorRegistryV2.with_builtins()
snapshot = registry.freeze()
refs = [snapshot.get(name, "1").ref for name in ("MOM20", "VOL20", "TURN5")]
factors = snapshot.compute(
    bars, refs, calendar=trading_dates,
    context=FactorDataContext("caller-declared-source", "UNVERIFIED"),
)
evidence = factors.attrs["factor_registry_v2"]
```

输出只包含原输入存在的证券日期主键，按 date/symbol 排序；列顺序遵循显式列表，
不输出未请求的依赖或输入标签。输入索引不参与身份，因此重复索引也不会错配主键。
因子列表不允许输出 ID 重复，不能同时把同名的两个版本放入同一宽表。
快照提供一致的计算视图；注册构建器本身不保证并发写入安全。

## 原 14 因子兼容

`FACTOR_REGISTRY`、`get_factor`、`compute_factors` 的签名、名称、大小写处理、
返回模式和公开对象属性保持原样。原公式提取到 `_compute_ordered` 内部方法；
原公开 `compute` 继续执行原 validate、排序、索引对齐。V2 使用相同方法计算，
不复制公式，不注册调用方 callable，不改变原可变注册字典。

原有公式、缺失值、零分母及未来数据变更测试均通过。
V2 对 3 只合成证券、85 个完整交易日的 14 因子输出，与原公开入口逐值精确一致，
包括输入乱序及原输入重复索引的情况。详细结果见
[验证记录](../validation/p2-05/validation.json)。

旧入口仍按调用方传入的观测行计算。V2 强制显式完整交易日历，在共享面板中
保留缺失交易日为 NaN 后计算，不填充、不压缩缺失日；仅返回原输入的键。
因此，稀疏输入直接传给旧入口与传给 V2 的结果可能不同；V2 对应旧入口先补齐
同一交易日网格的计算方式，与 DatasetBuilder 既有做法一致。
停牌零成交行保留原公式规则：TURN 可读取 0，AMIHU 的非正 amount 产生 NaN。
V2 不推断停牌状态，也不提供证券交易准入判断。

## 自动发现适配

```python
from factorlab.exploratory.factor_discovery.contracts import Node
from factorlab.exploratory.factor_discovery.engine import compile_expression

compiled = compile_expression(Node("Lag", (Node("Field", value="RET1"),), param=1))
ref = registry.register_expression("lag_return", "1", compiled)
result = registry.freeze().compute(bars, [ref], calendar=trading_dates)
```

入口仅接收精确的 CompiledExpression 类型，使用原 compile_expression 重新验证
白名单节点、字段、维度、正向历史窗口和复杂度，并比对全部编译元数据。
注册后只保留 canonical JSON 字符串，原对象内部 dict 后续变化不影响定义。
计算时有界解码 JSON 为内部 Node，再由原 ExpressionEngine 校验、执行。
没有 eval/exec、动态导入、Python 表达式源码或可插拔 callable 执行路径。
可注册已验证的有量纲中间表达式；100 定义演示全部为无量纲表达式。

RET1 由历史 adj_close 推导，不读取输入中伪造的 RET1 或标签。
每批共享一个解释器和子表达式缓存，保留原预算；预算耗尽会明确抛错，
不把失败的全 NaN 列当作成功。定义构造使用默认编译白名单；运行时可传
更严格的 `expression_config`，不借此放宽默认注册约束。

## 计算与数据证据边界

V2 每批统一检查字段、数值类型、每日主键和日历，按证券/日期整理一次。
内置因子共用面板，表达式共用原解释器自己的验证面板；输出一次组装，
没有逐因子的 DataFrame copy、merge 或逐行 DataFrame 循环。
滚动窗口严格分证券，缺失值不填充，负 lag 和未来字段不能注册。
测试同时覆盖未来数据变更、截断到历史前缀和逐证券独立计算的精确一致性。

FactorDataContext 单独记录调用方声明的数据来源和时间可得性证据描述，
并固定 `no_live_money=True`、PIT_UNVERIFIED、HFQ_UNVERIFIED、
FORMAL_ADMISSION_BLOCKED。注册状态仅为 SOFTWARE_VALIDATED_ONLY。
输出 attrs 中保留这些记录、所选引用及快照哈希；持久化到其他格式时应明确
保存此记录作为 sidecar，不能假定所有 DataFrame 导出方式都保存 attrs。
实际数据修订、供应商 PIT 和后复权质量仍须独立验证；注册成功不提供认证。

## 性能与验证

复现命令（已有 Python 3.13.9，无新环境）：

```powershell
$env:PYTHONPATH = 'src'
& 'D:/Environments/factorlab-py313/Scripts/python.exe' -B -m examples.research.factor_registry_v2_demo --output docs/validation/p2-05/performance.json
& 'D:/Environments/factorlab-py313/Scripts/python.exe' -B -m pytest tests/research/test_factor_registry_v2.py tests/research/test_factors_phase1.py tests/research/test_dataset_phase1.py -q -p no:cacheprovider --basetemp=local-validation/pytest-v2-final
```

首次在新 checkout 运行测试前先创建 `local-validation` 目录。
100 个不同 canonical AST 由有限网格生成，全部经过原白名单与维度校验并原子注册。
它们不代表 100 个独立经济因子。默认演示 20 只证券、160 个交易日、3,197 行，
随机打乱输入后完整输出逐值精确一致，每列均有有限值。
耗时及内存的实际测量、输入/输出哈希、规范化源码哈希见
[性能记录](../validation/p2-05/performance.json)。
tracemalloc 峰值覆盖定义创建、注册、冻结及批量计算；不含输入生成，耗时包含追踪开销。
OS 进程峰值 RSS 另行记录，包含导入和输入生成，与 tracemalloc 不是同一口径。
仅保留小型 JSON 记录，不保存可重建的大型输入/输出或新冻结归档。

额外自动发现回归有 3 项失败，分别缺历史 Git 对象、synthetic receipt、private receipt；
对应引用在当前 main 也缺失，且测试和原 discovery 源码未改。实际失败计入记录，
不纳入本任务通过数，不恢复历史归档，不重复 Legacy 矩阵。

## 尚未实现与 P2-06 最小建议

当前只支持原 14 内置定义、受控 AST 表达式和单依赖别名；通用多依赖自定义算子、
跨截面表达式、其他频率、并行/流式/增量计算、持久化目录加载与结果缓存均未实现。
解释器仍要求完整 raw/adj OHLC、volume、amount、turnover 白名单输入，
即使某个表达式只使用其中一列；保留其严格校验约束。
日历须由调用方提供，面板上限 250,000 行；不自动认证交易日历或数据可得性。
实现协议版本须随未来公式语义变化显式升级；定义哈希不是源码签名，
实际计算代码的源码哈希在性能记录中另行保留。

P2-06 建议仅为 DatasetBuilder 增加可选 snapshot/FactorRef 接入，默认保持旧入口；
在 dataset sidecar 绑定有序因子引用、快照哈希、数据版本与时间可得性证据。
增加同一完整网格下新旧数据集一致性及缺失 sidecar 拒绝测试即可，
不改四模型、不扩建引擎、不把此绑定当作金融门禁放行。
