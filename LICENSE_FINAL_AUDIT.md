# FactorLab 0.1.0.dev0 第三方代码与许可证最终复核

日期：2026-10-25（Asia/Shanghai）。最终状态：**LICENSE_RELEASE_READY=PASS**。

实际第三方内置源码文件 **0**；已证实许可证冲突 **0**；有待删除或替换的具体源码文件 **0**。
结论适用于下述重新构建、逐成员核验的 wheel/sdist 及其对应软件源码。没有发现当前发布内容的实质未解决授权问题。

## 1. 目录、版本和审计边界

| 对象 | 本次开始状态 |
|---|---|
| 独立工程 `D:\Code\FactorLab-independent-candidate` | HEAD `a11aadb0e098d515671ca4f0035aea810c0be269`；初始工作区干净 |
| 原始工程 `D:\Code\量化TV` | HEAD `aab509be694811ce27e22452fb161a5c38ae9a46`；已有未跟踪研究审计文件，原样保留 |
| 固定提取来源 `D:\Code\FactorLab-extraction` | HEAD `0ec7af5870c54a0ee202fbceeb97b7d87c293411`；只读读取原 Git 对象和历史 |
| 交接目录 `D:\Code\FactorLab-release-review-handoff-20261024` | 不是 Git 仓库；本次记录全部 13 个文件哈希 |
| 软件版本 | pyproject 和两包 METADATA/PKG-INFO 均为 `0.1.0.dev0`；模块 `__version__` 仍为 `0.1.0`，作为非许可证问题记录 LIC-005 |

原始工程与提取基线的 `qbot/research/__init__.py` 也声明 `0.1.0`；这两个旧树没有根 pyproject.toml，交接目录不承载独立软件版本。

没有推送、上传 PyPI、改动原始工程、覆盖旧审计、启动 PIT/HFQ 补证或修改金融准入门禁。
本次修改未提交，HEAD 仍为上述值；候选身份由 `build-input.json` 的实际文件哈希及以下制品哈希共同限定。
保全结果见 `local-license-final-20261025/preservation-final.json`，最终工作区和差异见同目录 `working-tree-final.json`、`license-changes.diff`。

## 2. 逐文件来源结论

`THIRD_PARTY_CODE_INVENTORY.csv` 对全部 **431** 个当前跟踪 Python 文件逐项记录路径、来源项目/原文件/固定版本、哈希、声明、兼容性、证据及包内位置；其中 **388** 个实际进入 sdist，wheel 的 **176** 个模块均为其中子集。
另将 70 个实际外部分发包、1 个构建工具内置组件和 1 个用户提供的外部参考分别列为 `DEPENDENCY_ONLY`，不计为内置源码。

| 当前发布 Python 文件分类 | 数量 | 依据 |
|---|---:|---|
| ORIGINAL_FACTORLAB | 388 | Wind-Maker1001 在 2026 年加入 quant-tv 的研究/适配器/测试/示例，以及独立工程新增代码；Git 对象、引入历史和源文件声明已检查 |
| QBOT_DERIVED | 0 | 未发现包内代码沿用 Qbot 早期实现；旧目录名不是代码来源证明 |
| THIRD_PARTY_EMBEDDED | 0 | 未发现复制、修改后内置的外部受保护源码；针对性核验 pytdx，另进行实际已安装依赖的非平凡函数 AST 比对 |
| UNKNOWN_PROVENANCE | 0 | 当前包内代码均有项目来源链；没有具体未定位的可疑源码文件 |

源码来源判断结合 Git 引入记录、原始 blob、当前实现、源文件署名与针对性比较；它不是仅凭提交作者名字作出的全互联网原创性保证。
原始提取清单中 449 个仍在磁盘的映射条目已重新读取并核对原 Git blob SHA-256；38 个不在提取清单内的 Python 文件另查独立工程历史。
Qbot 早期来源只出现在根 MIT LICENSE、docs/LICENSE 及 Git 配置，不在本次发布的 Python 实现中。
本机保存的上游固定提交 `f0425ae4ae8bd02b79656b8f7039f4cd6874095e` 的 Git tree 中不存在 `qbot/research` 或 `qbot/engineering/yhh30m`；本次核验的是实际对象，不是目录名推断。
主要证据：`source-provenance.json`、`source-addition-history.txt`、`new-python-files.json`、`upstream-local-tree-proof.json`、`installed-source-comparison.json`。
本候选根目录没有 `scripts/`；现有脚本位于 tools 和 .github，均在跟踪 Python 文件清单内；实际分发的是 sdist 中 5 个 tools/security 文件。
辅助比对覆盖 388 个分发 Python 文件和实际依赖的 6927 个 Python 源文件：按至少 80 个 AST 节点的非平凡函数检查，完全相同的规范化函数命中 0。该方法保留标识符，不能发现所有改名/改写，因此只作为 Git 来源及人工针对性核验的辅助证据。

### pytdx 两个示例的具体核验

`examples/research/audit_tdx_quality.py` 的 `candidate_gbbq` 引用固定 pytdx reader 的散列和格式布局；源码使用独立 NumPy 数组运算、不同的流程组织与验证。与固定 `gbbq_reader.py` 对比未发现复制的源码表达；算法运算、数据布局与兼容性常量本身不足以认定内置第三方源码。
关键字节表不是本项目常量：函数只从用户显式提供的外部源文件 AST 读取该表。`review_tdx_quality.py` 显式调用用户提供的外部 reader 作对照；外部文件、类实现和 key table 均未进入两包，也没有被本次审计执行。
因此两个示例保留，不删除依赖它们的测试；实际 33 项相关回归通过。
pytdx 固定版本的 setup/README/tree 未找到清楚的再分发许可，README 有个人研究用途和机构使用提示；此项作为**外部参考授权 UNVERIFIED**保留，未声称 pytdx 是 MIT，也未替作者授权。
这不产生一个不存在于包内的“侵权 pytdx 文件”。针对性记录与代码片段见 `pytdx-targeted-review.json`。

## 3. Qbot 的 MIT、CC BY-NC-SA 和 GPL 文档差异

* [Qbot 根 LICENSE](https://github.com/UFund-Me/Qbot/blob/f0425ae4ae8bd02b79656b8f7039f4cd6874095e/LICENSE) 是 MIT，版权 `Copyright (c) 2022 UFund-Me`。
* 历史 `481fe1cc...`（2022-11-23）已是 MIT；`aa049ed8...`（2024-11-10）只将版权主体拼写 UFund 改为 UFund-Me，未删除 MIT 的使用、修改、分发、销售授权。2026 年 quant-tv 提取基线仍保留同一 MIT 条款。
* [Qbot README LICENSE 段](https://github.com/UFund-Me/Qbot/blob/f0425ae4ae8bd02b79656b8f7039f4cd6874095e/README.md) 第 840–844 行有 CC BY-NC-SA 图和声明；docs/README 第 744–748 行也有对应声明。其非商业条款确实存在。
* [安装文档](https://github.com/UFund-Me/Qbot/blob/f0425ae4ae8bd02b79656b8f7039f4cd6874095e/docs/Install_guide.md) 第 6 行说支持非商业用途二次开发；README 专业付费版表格也提到该开放协议。没有把这些文字解释成对所有根 MIT 源码的自动撤销，也没有忽略其可能适用于特定材料。
* `docs/LICENSE` 自 `4b92cc98...`（2022-12-01）存在 GPLv3 全文；`b5bc8eb4...`（2024-06-06）等历史提交记录 CC/非商业文档文字的变化。根 MIT 与这些文档声明的范围没有被上游清楚地统一。

该差异记录为 **UPSTREAM_LICENSE_AMBIGUITY / DOCUMENTATION_INCONSISTENCY**。本次没有宣布双许可证、替上游选许可或认定上游全部内容可商用。
对实际软件的关键证据是：当前发布代码没有早期 Qbot 实现或文件级 NC/GPL 声明，原 Qbot README、安装文档、docs/LICENSE 以及私人 verifier 均未进入制品；`docs/user/GUIDE.md` 是独立工程新增说明。
据此，没有证据证明上述歧义影响 FactorLab 本次纳入的软件文件，**不构成软件包发布阻塞**。继承并再分发已按 MIT 授权的软件时，保留适用版权与许可全文即可；本次原始根 LICENSE 原字节保留并进入两包。
MIT 义务对照 [OSI 原文](https://opensource.org/license/mit)，CC 适用范围对照 [CC BY-NC-SA 4.0 原文](https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode.en) 第 1、2、3 节。

原文及历史证据在 `local-license-final-20261025/upstream/` 和 `qbot-noncommercial-history.txt`。Firecrawl 额度不足、GNU 官网连接失败及一次 GitHub API 限额均有检索记录；MIT/CC/MPL 官方页、GitHub 固定文件与本地原 Git 对象已提供所需证据，没有把请求失败当作许可缺失。

## 4. 依赖与实际再分发方式

详见 `DEPENDENCY_LICENSE_REPORT.md` 的全部实际版本表和具体组件分析。确认主体 MIT/BSD/Apache 2.0 等许可，识别 MPL、GCC runtime 的 GPL+例外以及 setuptools 内的 LGPL 组件；没有把传递依赖算成 FactorLab 自有文件或凭许可名字判冲突。
当前制品没有外部库源码/二进制；BaoStock 全文缺口与 pytdx 外部参考保持 UNVERIFIED 记录，均未被改写成“已获权利人授权”。没有已知影响当前被分发文件的未解决授权问题。

## 5. 实际 wheel/sdist 检查与最小修复

先读取已有 packaging-r1 至 r6 的 **12** 个制品，保存全部成员/哈希和与当前源码的差异；它们不是本次最终制品，全部原样保留。
随后仅从受审跟踪文件及新 NOTICE 复制到隔离 `build-source`，使用 Python 3.14、setuptools 80.9.0、wheel 0.45.1、build 1.3.0 构建；wheel 由本轮 sdist 构建。实际构建命令、退出码和日志均已保存。

| 制品 | 实际文件成员 | Python 文件 | SHA-256 |
|---|---:|---:|---|
| factorlab-0.1.0.dev0-py3-none-any.whl | 184 | 176 | `36fe46e5b0075c97d0bdb69d8f0a9d8c9e313ec17ea12816536a0b7e339fb6f9` |
| factorlab-0.1.0.dev0.tar.gz | 445 | 388 | `e91f3a25a573445eb9ff7386f5eed4c5a57ff1af1982b2ffa99b3898ef39b3fd` |

每个包的当前源文件与工作区逐字节相同；176 个运行模块和 YAML 完整，sdist 保留 171 个测试文件、36 个示例、5 个安全工具及必要配置/文档/要求文件。
两包 METADATA/PKG-INFO 均声明 MIT 和 LICENSE/NOTICE；未发现误带 Qbot Legacy、付费组件、外部 pytdx 源码/密钥表、依赖 DLL、私人归档、供应商原始数据、notebook、密钥模式或本地安装 URL。
21 个 Parquet 成员与原有合成 Golden 清单哈希完全相同；3 个小 CSV 保留原贡献提交与测试用途证据，未发现商业数据来源或受限声明，未因缺少单独 synthetic 标签假定侵权，也未冒充权威金融数据。
凭据检查是明确模式和路径检查，不是对任意秘密字符串的绝对保证。

本次只作下列修复：新增事实性来源 NOTICE；让 pyproject/MANIFEST 将其放进两包；更新 README 的软件许可说明。根 LICENSE 无需更换；没有删除、替换或修改任何运行源码、测试或 Golden，没有修改文档 GPL/CC 原件。
验证：构建通过；两包逐成员/源码完整性/许可证检查通过；wheel 和 sdist 分别安装、版本及许可元数据、实际 CLI 入口帮助通过；现存运行环境 `pip check` 通过；33 项相关测试通过。
保留过一次不支持的 `python -m factorlab` 调用失败（包没有 __main__）；按实际入口 `python -m factorlab.cli --help` 验证通过，未为此改动代码。详细记录见 `verification-final.json`。
本次没有重跑全量金融/安全准入测试，也没有把历史测试成功沿用为当前整体验收。

## 6. 对六个发布问题的直接回答

1. **第三方内置源码：0 个文件。** 388 个分发 Python 文件均归为 ORIGINAL_FACTORLAB；外部包单独列 DEPENDENCY_ONLY。
2. **实际许可证冲突：0 项。** `CONFIRMED_CONFLICT` 清单为空。
3. **源码删除/替换：无。** 原 MIT 版权全文已保留；新增 NOTICE 改善来源与贡献范围说明，两包实际携带它。
4. **Qbot 文档差异：存在歧义，但没有具体证据影响当前 FactorLab 软件文件。** 记录 UPSTREAM_LICENSE_AMBIGUITY，不制造发布阻塞。
5. **可以公开发布本次已审 wheel/sdist 及对应软件源码，许可证门禁可解除。** PASS 是软件许可证结论，不要求额外外部审查方批准普通 MIT 软件分发，也不是本次已执行远端发布。
6. **没有某个具体文件或条款阻止本次软件包发布。** 外部参考与未被分发材料的许可没有被本报告扩展或代授。

本结论取代旧审计对本次软件包范围的笼统许可阻塞判断，旧记录保留不改。范围不包含整份本机历史仓库、私有研究归档或另行打包外部依赖。
保持 **NO LIVE MONEY / PIT_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**。
