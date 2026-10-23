# FactorLab

开源、可审计、可复现的 A 股多因子研究与策略验证平台的独立工程候选。
面向学生、教师和研究者，保留原14因子、四模型、统计推断、两级回测、自动因子发现、
市场结构和 Forward 软件合同。独立导入包为 `factorlab`，源码位于 `src/factorlab`。

**NO LIVE MONEY / PIT_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

当前为 `0.1.0.dev0` 本地候选。软件安装与合成研究不授予正式金融资格，也不保证投资收益。
原 Qbot/quant-tv 固定基线、版权声明、旧 Golden 和历史收据均保留可追溯映射。

## 安装与快速开始

使用标准64位 Python 3.14；Python 3.13作为兼容环境：

```bash
python -m venv .venv
python -m pip install .
python -m pip check
factorlab --help
factorlab demo --output results/synthetic-first-run
```

先激活所创建的虚拟环境，或使用环境内 python/factorlab 的完整路径。
Windows 的实际复现版本见 `requirements/py314-win64.lock.txt`；供应商依赖单独放在
`.[suppliers]`，开发依赖见 `requirements/dev.txt`。不需要旧 Qbot 的 PYTHONPATH 或完整依赖。

Demo 完全本地生成行情、历史股票池和状态；经14因子和 DatasetBuilder，运行 FactorScore、
Ridge、LightGBM、XGBoost，输出 Fast/Execution 回测、统计、图表、报告与哈希清单。
所有输出标记 `SYNTHETIC / SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE`。

## 文档与证据

* [安装、Windows/Linux、四模型教程、数据 Schema、自定义因子与结果解释](docs/user/GUIDE.md)
* [贡献指南](CONTRIBUTING.md) 与 [安全与准入边界](SECURITY.md)
* [迁移审计](docs/migration/BASELINE_AUDIT.md)、[依赖边界](docs/migration/EXTRACTION_DEPENDENCY_GRAPH.md)
* [发布与许可审计](docs/migration/LICENSE_AND_PUBLICATION_AUDIT.md)
* 迁移完整验收状态与失败原件见 `docs/migration/`；原始本机日志不进入 wheel/sdist。

## 研究边界

真实数据必须独立证明 RAW、ADJUSTMENT、STATUS、PIT、SOURCE_BINDING、FORMAL。
当前迁移的独立信任根尚未审查，正式消费者有额外拒绝门禁。不得用当前成分回填历史，
不得从文件存在、自报 PASS、合成测试或新 SHA 推导金融认证。

市场结构32项合同中仅14项有合成实现，其余18项保持 CONTRACT_ONLY。日线执行模型
包含 T+1、整手、停牌、涨跌停、现金和费用近似，不能重现真实盘口。旧模型 pickle
绑定旧模块名；新版本须重新训练并生成新身份清单，不能冒用旧收据。

源代码保留上游根 MIT 版权：Copyright (c) 2022 UFund-Me，见 [LICENSE](LICENSE)。
上游文档另有 GPLv3/CC BY-NC-SA 声明，范围未解决；旧商业来源派生报告未批量复制，
本候选不构成公开发布授权。未创建公开仓库、远端 Release 或 PyPI 发布。
