# FactorLab 0.1.0.dev0

首个开源研究软件测试版，供合成数据教学、离线研究流程验证与软件评估。
开发预发布版本；接口、结果格式和模型工件协议可能变化，不承诺兼容旧 pickle。

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 软件范围

14 因子、DatasetBuilder、RankIC、统计推断、FactorScore / Ridge / LightGBM /
XGBoost 四模型、Fast / Execution 两级回测、CLI、报告及完全本地合成 Demo。
支持标准 64 位 Python 3.13 / 3.14；依赖范围见 pyproject.toml。

## 本次修复

* 包内 `factorlab.__version__` 与分发元数据统一为 `0.1.0.dev0`。
* 保留原 MIT LICENSE、版权和已完成的许可复核，将 NOTICE 纳入两个发布包。
* 发布扫描仅报告其实际检查范围，不再把旧许可阻塞常量当作新的审核结论。

## 金融与软件边界

所有 Demo 数据和结果均为 SYNTHETIC / SOFTWARE_ONLY / NOT FINANCIAL EVIDENCE。
PIT/HFQ 正式认证和独立金融信任根审查未完成。既有 RAW / ADJUSTMENT / STATUS /
PIT / SOURCE_BINDING / FORMAL 检查及额外正式准入拒绝保持启用。
这不阻止普通研究软件测试版发布，也不构成真实市场效果、投资收益或实盘适用性证明。
18 项市场结构特征仍为 CONTRACT_ONLY，日线执行回测仅作近似模拟。

历史 Qbot 仓库重放与当前独立软件回归为不同验收合同；不能把缺少旧历史对象
解释成新软件已经通过旧金融认证，也不能用软件 PASS 签发正式准入。
本轮不推送 GitHub、不创建远端 Release、不上传 PyPI。
