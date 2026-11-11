# DQ-01 三源资产可访问性与覆盖清单

**PARTIAL**

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

## 实测口径

文件逐一读取文件头并流式计算完整 SHA256；归档按魔数而非后缀解析。当前载荷读取限于小型字典、schema、receipt 及独立 PDF。目录可读不代表大型载荷已解压、CRC 验证或逐行扫描。

|来源|基线找到/登记|本次检查|归档枚举|成员|逻辑表候选|数据分片|整表逐行旧证据|完全无内容证据表|
|---|---:|---:|---:|---:|---:|---:|---:|---:|
|CSMAR|55/55|55|55|265|28|155|8|20|
|CNRDS|81/81|81|67|393|67|194|0|66|
|BigQuant|10/10|10|10|6466|61|3201|3|56|

完整分母见 [coverage-matrix.csv](coverage-matrix.csv)。成员数不含目录；数据分片只计 xlsx/xls/csv/parquet/dbf/dta，字典、PDF、schema 与 receipt 不计数据分片。逻辑表按来源隔离；BigQuant 使用实际 schema 路径，CSMAR 优先原生 family，其余按年度/序号规范化成员名分组。未打开工作簿的未知工作表没有冒充已发现独立逻辑表。

## 七项验收问题

1. **既有文件：146/146 找到，146/146 实际检查。** CSMAR 55、CNRDS 81、BigQuant 10；新增未登记文件 0。全部建立本次完整文件 SHA256；55 个 CSMAR 文件与原生 catalog 的旧完整文件 SHA256 全部相符。
2. **65 个 CNRDS `.7z` 均能实际列出内部成员。** 它们全部是误命名的 ZIP，共 233 个非目录成员，补齐了 PR #81 的当前目录缺口。不是 65 个真正的 7z 容器。
3. **目前识别 156 个来源隔离的逻辑表/表候选、3,550 个数据分片。** CSMAR 28/155、CNRDS 67/194、BigQuant 61/3,201。归档合计 132/132 成功枚举，成员 7,124/7,124 完成目录核对。工作簿未知工作表仍明确 NOT_RUN，因此 156 不是工作表级绝对全集认证。
4. **142 个表只有目录、字典、schema 或 receipt 层证据。** CSMAR 20、CNRDS 66、BigQuant 56；在 logical-tables.csv 中筛选 `evidence=METADATA_ONLY` 可逐一定位。当前真实内容读取为 3,449 个元数据成员，另有独立 PDF 14 份、68 页；元数据成员不计作证券数据行检查。
5. **329 个数据成员有身份绑定的旧内容检查证据。** CSMAR 38 个、CNRDS 28 个、BigQuant 263 个，涉及 14 张表。11 张表的全部已发现数据分片已有完整逐行读取/profile 证据：CSMAR 8 张、BigQuant 3 张。其余 3 张有部分分片证据，不能继承为整表全量检查。逐成员哈希、扫描版本、记录范围和绑定强度见 provenance.json；`full_table_row_scan` 不表示所有数值字段已经通过质量认证。
6. **3,221 个数据分片仍缺内容检查证据。** CSMAR 117、CNRDS 166、BigQuant 2,938；142 张表完全没有数据内容证据，145 张尚未具备整表全量逐行证据。按 logical-tables.csv 的 `remaining_content_partitions` 和 `full_table_row_scan` 区分这两个口径。
7. **本轮指定检查范围没有不可读取文件或成员，失败清单为 0。** `.7z` 后缀错标是格式身份问题，不是损坏。未解压的数据载荷仍为 NOT_RUN，不能据此称其 CRC、结构或内容均已验证。发现一组完整字节重复文件：两份 CEPD 数据库说明 PDF；没有删除或合并原件。

## 后缀与实际格式

CNRDS 的 .7z 文件实测格式：`{'ZIP': 65}`。后缀不符单列于 files.csv，不据此判损坏。

## 当前与继承证据

archive-members.csv 的 inspection 只描述当前检查；inherited_evidence 单列旧证据。CSMAR 原生 catalog 的来源以当前完整归档 SHA256 对比；PR #85 用 PR #81 当前目录身份（路径、声明大小、CRC、压缩大小）及实际字典 SHA256 绑定，BigQuant 另要求当前 receipt 载荷 SHA256 等于旧 manifest。CRC 绑定不是本轮大型载荷 SHA256 重验。每个继承项的版本、成员哈希、范围及绑定方式见 provenance.json。

PR #85 已完整解析的 43 个原生工作簿包含 25,177,454 条选定原生记录；日期/证券范围和数值比较范围以既有 REPORT、summary 与 input-manifest 为准。完整解析成员不意味着归档中其他年度已扫，也不意味着整个逻辑表已全量扫描。CSMAR catalog 的全量 profile 不是所有字段的数值质量认证。PR #87、#89 是已读行情上的定向归因，不扩展资产内容覆盖。

## PDF 与字段语义

独立 PDF 均尝试结构解析、逐页文本提取及 outline 检查；verification.json 给出页数、文本页数、文本字符数、定义主题、单位及年份线索。不启动 OCR；文档线索不证明数据值、历史可得性或 PIT。归档内 PDF 仅目录检查，不能与独立的 14 份文档混算正文读取。字典语义不代替实际记录证据。

## 未完成内容与失败

logical-tables.csv 逐表列出剩余未读分片、字段来源和证据范围；没有内容证据的表为 METADATA_ONLY。unreadable-assets.csv 给出文件及成员失败原因（空表表示本轮指定检查没有读取失败，不代表所有载荷无损）。没有对未解压大型载荷宣称无损、无截断或 CRC 通过。

TODO(DQ-02)：验证剩余数据载荷及工作表清单，取得真实记录日期范围；在不重复已完成行情扫描的前提下补齐逐行检查。

## 身份、重复与复现

所有可读文件建立本次完整 SHA256；file 重复按完整哈希，member 重复候选按 size/CRC，二者不可混用。来源由显式输入根和清单交叉定位，不以文件同名认证供应商来源。无首次分发、修订链及正式 PIT 证据。检查点保存于私有 state 目录，以脚本版本、大小、mtime/ctime 绑定恢复，不在重复执行时重算文件哈希。

运行依赖现有 Python、pypdf、psutil；只有真正的 7z 容器需要 py7zr。本次 65 个误命名 ZIP 无须 7z 解压。可用 --test-results 绑定定向测试 JUnit XML，用 --initial-verification 保留首次完整检查资源。

```text
python -I -B -X utf8 tools/data/dq01_asset_coverage.py --csmar-root <CS> --cnrds-root <CN> --bigquant-root <BQ> --inventory-root <INV2> --original-inventory <LOCAL_ORIGINAL_INVENTORY.csv> --csmar-catalog <CATALOG> --state <PRIVATE_STATE> --output docs/validation/dq-01
python -I -B -m pytest tests/evidence/test_dq01_asset_coverage.py -q -p no:cacheprovider
```

本次汇总单进程，墙钟 0.68 秒，峰值工作集 92577792 字节，缓存命中 146。首次完整检查资源：`{'elapsed_seconds': 97.0274117999943, 'peak_working_set_bytes': 92753920, 'files_hashed': 146, 'cache_hits': 0}`。当前数据行扫描 0，不重读 2,500 万行；没有解压固实归档。

定向测试：`{'tests': 13, 'failures': 0, 'errors': 0, 'skipped': 0, 'seconds': 0.299, 'junit_sha256': 'cf445e1844bf2f2b21d2968dc0cb19bf9f580ad98f2bf1e8a90a1ac7b699a704'}`。身份冲突 0 项；新增未登记文件 0 个。完整文件重复组见 provenance.json，不能把 member 的 size/CRC 候选当成完整内容重复。
