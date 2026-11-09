# P3-D06：中证500多证券研究数据与最小窗口

2026-11-09；主线基线 `51e6975`。P3-11、P3-12、P3-13 分别已由主线 PR #35、#34、#33 合入。

**P3D06_PARTIAL — NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

现有数据支持有明确缺失条件的多证券数值诊断，已经实际执行原14因子和 `p311-v1` 的64候选定义。最小12/3/3月与扩展24/3/3月窗口均有足够的共同数值样本；历史身份、成员知识时间、供应商修订链和价格语义仍阻止金融准入。这里的“结构候选可行”不等于可直接交给受保护研究入口训练或评估收益。

## 样本规模与新发现

|对象|实际结果|
|---|---:|
|研究日期|2021-09-13—2026-09-11，1,211个供应商CN会话|
|历史成员|605,500个唯一证券日；932个代码；每天500成员|
|原价匹配键|605,208；缺292|
|匹配行OHLC各列/amount空值|564个成员日；不是四列相加|
|原价完整OHLC|604,644 / 605,500，99.8586%|
|调整价候选OHLC完整|604,644 / 605,500；与原价分表读取|
|状态完整键|605,208；缺292；564行有停牌标志|
|实际数值计算定义|14 + 64 = 78；各定义均有有限输出|
|全部78因子共同有限|598,112 / 605,500，98.7799%|
|再要求t+1/t+6股票及基准开盘端点存在|597,723|
|再要求默认结构筛选代理|597,623 / 605,500，98.6991%|
|取得金融准入的因子/成员日|0 / 0|

现行输入上 **605,500 / 292 / 564 旧计数仍精确适用**。另有1条仅 `pre_close` 空值，六字段任一空值共565个匹配成员日。当前版本通过实际消费分片的SHA256绑定，而不是只复述旧汇总。

全计算输入范围为2021-01-13—2026-10-11，1,388个会话，预热与尾部也保持证券自身历史。新增范围内发现 **10行**原价及调整价候选OHLC关系异常，研究日期内0行，涉及 `600363.SH`、`601198.SH`。既有引擎拒绝该输入；本轮不改价、不补零、不放宽引擎，而将这两代码的 **1,517成员日**整体标为计算阻塞。该隔离是保守的、全诊断范围事后质量判断：上表因子覆盖是**可执行子集下界**，不是这1,517日每个公式都不可能计算，也不是历史时点股票池规则；不能直接拿隔离结果做Train-only选择。只给出“预热/尾部”合并范围，不推断10行全部发生于预热或全部发生于尾部。

## 证据与交付

- [资产盘点](assets.md)、[字段覆盖](assets-fields.csv)：18个实际行情/状态字段的缺键、已存在键上的空值、无穷和总缺失率。
- [其他字段合同矩阵](coverage-contracts.csv)、[全资产schema与收据](asset-metadata.json)：财务、行业、公司行为、上市、换手单位及价格语义的分层证据；收据行数不冒充实际日覆盖。
- [历史成员、身份、状态](membership-identity-status.md)：实施日期、退市/代码变更、幸存者偏差与PIT缺口。
- [78因子矩阵](factors.csv)：不可变定义/依赖/回看、真实有限值覆盖、NaN/inf、常数与低变化。
- [窗口说明](windows.md)、[逐折样本](walk-forward-windows.csv)：26折×3阶段，分别统计14/64/78共同有效样本。
- [扩展资产](extension-assets.json)：十年供应商日历、3个有界历史成员样本、2,025条中证500变更观察与CSMAR既有catalog聚合。
- [最小补数](minimum-data-plan.md)、[当前汇总](diagnostics.json)、[输入版本哈希](input-manifest.json)、[测试记录](tests.json)。

原始价从 `stock.zip/stock/cn_stock_real_bar1d` 读取，调整价候选从 `cn_stock_bar1d` 读取，分别命名 `raw_*` / `adj_*`。不把原始价乘当前因子生成HFQ，不将价格比值吻合或表名视为复权认证。`turn` 仅以字段别名映射到公式输入 `turnover`，单位与历史合同仍UNKNOWN；比例和百分数对TURN水平因子的影响不能被忽略。

## 复现

复用现有Python 3.13.9、pandas 2.3.3、numpy 2.3.4、pyarrow 22.0.0、psutil；无需新环境。所有路径参数由操作者绑定现有资产，输出必须为新目录。以下命令在仓库根目录运行，`<BQ>`、`<FD>`为供应商导出和已有本地数据根目录，不复制原件：

```text
python -I -B -X utf8 tools/data/audit_p3_d06.py --bigquant-root <BQ> --membership-zip <FD>/validation/joinquant_csi500_full_20261024/csi500_full_history.zip --inventory-root <FD>/inventory_v2 --csmar-catalog <FD>/candidates/csmar-601857-2018-20261105/inventory/catalog.json --output <NEW_AGGREGATE_DIR>
python -I -B -X utf8 tools/data/audit_p3_d06_extension.py --bigquant-root <BQ> --csmar-catalog <FD>/candidates/csmar-601857-2018-20261105/inventory/catalog.json --output <NEW_AGGREGATE_DIR>
python -I -B -X utf8 -m pytest tests/evidence/test_p3_d06_diagnostic.py -q -p no:cacheprovider
```

运行时将 `OMP_NUM_THREADS`、`OPENBLAS_NUM_THREADS`、`MKL_NUM_THREADS` 均设为2。注册快照哈希为 `df737cd443a259311f91e8615bfddd6f4408dd3ec05e933612e9c7d674b76a15`。输入清单逐个记录实际消费ZIP成员的哈希、所读列、物理行数、收据哈希和获取版本。主诊断225个Parquet成员哈希与收据一致，0差异；扩展脚本的哈希记录在自身JSON中。它们绑定2026年导出载荷，不证明历史vintage或供应商首次可得时间。

复用 Registry V2 的 `snapshot.compute`，以32证券×16因子分批、完整会话网格、无填充执行；复用 `ExpandingWalkForwardSplitter` 和 `build_filter_flags`。没有实现新的公式、标签收益或门禁。DataHub当前没有这些原始ZIP表的已验证适配合同，所以只沿用D01/D02的只读分片方法，不为ZIP伪造Provider/DatasetBuilder准入身份。完整因子矩阵不落盘，公共交付只有聚合证据和软件定义，无法重构供应商证券日载荷。

## 实际资源与测试

主诊断 **125.95秒墙钟 / 124.92秒进程CPU / 1.221 GiB进程峰值工作集**；225个Parquet分片，投影读取21,883,716条物理行，消费1,209,637,452字节解压成员内容。扩展检查 **0.49秒 / 0.300 GiB峰值工作集**，25分片、2,083,968物理行、8,491,428字节。字节是ZIP成员解压大小，不是列投影后的内存、磁盘写量或物理SSD读量；两个进程峰值不相加。大型中间归档和因子缓存写入均为0。

最终定向测试 **6 passed / 2.45秒**。早期夹具非规范证券代码导致1失败，修正夹具后5项通过；首轮真实扫描在完成512代码后被OHLC校验拒绝，因原先没有保留可恢复内存，需要一次故障后的真实重算。加入明确的原值保留/整证券计算阻塞诊断及测试后，最终完整运行成功。性能数字只描述成功运行，不把失败尝试计入吞吐分母；失败尝试未测量独立总资源。没有运行模型训练或收益执行器。
