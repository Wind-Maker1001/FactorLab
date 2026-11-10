# 自定义日线策略

`NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`

## 完整示例

在已安装项目依赖的源码环境中运行：

```bash
PYTHONPATH=src python examples/custom_strategy/run.py local-custom-ma
```

输出目录必须不存在。示例生成合成数据、执行、保存并重放，打印研究评价。
工作日日历不是交易所日历，合成结果不是金融收益证据。

独立策略见 `examples/custom_strategy/ma_strategy.py`，配置见同目录 `config.json`。
JSON 沿用现有技术策略配置风格：

```json
{
  "strategy": {
    "file": "ma_strategy.py",
    "class": "MyStrategy",
    "parameters": {"window": 20}
  },
  "research": {
    "symbol": "601857.SH",
    "start": "2018-01-01",
    "end": "2018-06-30",
    "initial_cash": 100000.0
  }
}
```

`file` 相对配置文件解析。构造参数仅支持显式注解的 int/float/bool/str，拒绝未知参数、错误类型和非有限数；省略参数的默认值会展开保存。策略构造器应拒绝自身不合法的参数范围。research 还可指定 max_weight、board_lot，不接受金融资格或执行模式开关。max_weight 是买入持有基准的资金权重；策略实际入场权重由自身输出决定。

```python
from factorlab.strategies.custom import run_custom_strategy, save_custom_run, replay_custom_run
from factorlab.experiments.provenance import capture_provenance

result = run_custom_strategy(bars, calendar, "my-config.json")
save_custom_run(result, "new-output", provenance=capture_provenance(checkout_root),
                command=["python", "my-research.py"])
replayed = replay_custom_run("new-output")
```

只有明确生成的合成数据才指定 source_kind="SYNTHETIC"；默认 REAL_CANDIDATE 只是未认证输入标签，不授予准入。

## 协议与因果边界

策略声明 `required_fields: tuple[str, ...]`、`warmup_sessions: int`（至少2个首个研究日前会话），实现 `generate_targets(bars, calendar)`。调用方逐研究日只提供截至当日收盘的历史前缀，并只暴露 date、symbol 与声明的 required_fields，调用者须保证这些字段本身为当时可得的行情而非未来标签；输出当前日单行或该前缀内严格递增、日期唯一的多行 DataFrame，必须包含 date、symbol、target_weight。每次仅采用当前日行。缺失当前日明确拒绝，不插值。

生成函数应为无跨调用可变状态的历史到意图映射，不自行获取外部或未来数据。前缀模式会重复计算历史，适用于最小日线研究，不是流式高性能框架。定向未来扰动测试检验过去输出不受后来行情改变影响。`BuiltinDualMA` 演示复用已有 moving_average_signals。

仅支持 `DAILY / SINGLE_SECURITY / LONG_ONLY / NEXT_OPEN`：

- NaN 保持仓位；0清仓；(0,1]仅在空仓时申请入场，不持续再平衡。
- 当日收盘意图最早下一交易日开盘成交，失败订单重试和费用沿用现有执行器。
- 预热期不承接入场意图；期末不强制清仓。
- 行情合同复用双均线接口；拒绝可变复权比例，不能解释为公司行为总收益。

动态网格订单、配对双腿、期货保证金/换月、期权、高频盘口、逐笔和真实券商路由不属于此协议，继续使用专用执行器。

## 信任与复现

仅加载用户显式选择的本地可信 .py 文件，不扫描第三方模块，不下载远程代码，不执行配置字符串。Python 导入会执行代码，**不是安全沙箱**；不要在可信 CI 控制面执行任意 PR 提供的策略。策略异常带类名及日期向上传播，不输出成功记录。

保存内容包括展开参数的 config.json、源文件 SHA-256、输入 bars/calendar parquet、费用、信号、双方 trades/rejections/nav CSV、ResearchEvaluation 结果和不可变 ExperimentTracker 记录。策略源文件须保留在原路径，重放前检查其身份；外部导入依赖由研究者维护与固定，环境及 FactorLab 代码身份记录在 tracker 中。结果保持金融门禁，`P5_S01_ENGINEERING_ACCEPTED` 仅指软件工程验收。
