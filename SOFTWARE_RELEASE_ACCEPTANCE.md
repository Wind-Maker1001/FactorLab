# FactorLab 0.1.0.dev0 软件发布验收

**SOFTWARE_RELEASE_READY=PASS**

本轮结论仅覆盖研究软件测试版、合成数据流程和下列实际执行的软件检查。
**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## 实际测试结果

| 检查 | 结果 |
|---|---|
| Windows x64 / Python 3.13 完整独立软件回归及环境修正重测 | PASS：3965 个不同用例最终通过；原始全量 3964 passed + 1 环境失败，相关模块重测 44 passed；未解决失败 0；全量耗时 3119.3 秒 |
| Windows x64 / Python 3.14 完整独立软件回归及环境修正重测 | PASS：3965 个不同用例最终通过；原始全量 3964 passed + 1 环境失败，相关模块重测 44 passed；未解决失败 0；全量耗时 2097.4 秒 |
| 四模型、14 因子、RankIC、统计推断、Fast/Execution 回测、CLI、时间/来源/安全拒绝 | PASS：包含于两套完整软件回归，原测试逐字节未改 |
| wheel/sdist × Python 3.13/3.14 全新 core-only 安装 | PASS：4 个独立 venv，在源码目录外执行 |
| 安装后离线导入与版本 | PASS：每个环境 176 个模块；__version__ / metadata / identity 均为 0.1.0.dev0；无 Legacy/开发/供应商依赖 |
| pip check、实际控制台入口及 CLI 帮助 | PASS：4 个安装环境及两个回归环境依赖检查通过 |
| 合成 Demo | PASS：4 次；每次四模型各 440 条预测、两级回测及报告；233 个输出哈希复核 |
| Python 3.13/3.14 和 wheel/sdist 数值比较 | PASS：两组比较各 30 项，均完全一致，包含因子、Dataset、预测及回测交易/净值 |
| 恶意归档测试 | PASS：19 类均被拒绝，包括凭据、路径穿越、重复成员、链接与未绑定 Golden |
| 最终包内容/元数据/RECORD/SHA256 | PASS：当前源码逐成员相同，MIT LICENSE 和 NOTICE 与已审计制品原字节一致 |
| 发布扫描 | PASS：两个包 630 个文件成员，0 findings；基于路径白名单、明确密钥模式、AST 和固定 Golden 哈希 |
| 已有许可结论与保全 | PASS：沿用 LICENSE_RELEASE_READY=PASS；299 个受保护文件/旧制品哈希不变；未重复全量许可调查 |

源码覆盖率（各自完整软件作业）为 Python 3.13 85.94% / Python 3.14 85.66%；它不代表金融认证或所有路径已验证。

两个完整作业各 8 deselected，沿用原有 --suite software 的离线/历史域选择；测试正文与选择器未修改。

## 失败判断与处理

未提交版本修改的原工作区中，源码身份单测如实拒绝 __init__.py 与 HEAD 不一致。
该结果为预期安全行为；两个初始长作业为改用正确提交上下文而中止，不能算 PASS。
随后将实际候选文件原字节复制到隔离本地 Git 仓库并生成真实快照提交；同一单测原样通过，
再完整运行上述两套软件回归，各原始作业保留 3964 passed + 1 failed，不冒充单次全绿。
该唯一失败由本轮验收临时目录放在另一个有效 FactorLab 仓库下引起：测试切换 cwd 后合法解析到外层仓库，
与测试 fixture 所记录的内层快照仓库不同。将 basetemp 放回实际快照根目录下，原样重测整个相关模块，
每个 Python 均 44 passed；最终每个环境 3965 个不同用例都有通过证据，未解决失败 0。
首次模块重测因未提前创建 basetemp 父目录产生 44 个 setup errors，未执行测试正文；修正后日志/XML 另存且通过。
没有改变测试选择、删除测试、修改 Golden、重签历史收据或绕过安全检查。
历史对象未被虚构，新快照提交仅代表当前软件文件，不授予任何金融资格。

本轮唯一运行包源码修复是 __version__ 从 0.1.0 改为 0.1.0.dev0。发布扫描工具修复了
报告中的旧许可阻塞常量，改为“本扫描未评估许可，见最终许可决定”；其扫描/拒绝逻辑未变。
README、Release Notes、SECURITY 明确研究软件测试版和 HFQ_UNVERIFIED；两个包保留 LICENSE/NOTICE。

## 制品冻结

| 文件 | 字节数 | SHA256 |
|---|---:|---|
| factorlab-0.1.0.dev0-py3-none-any.whl | 575175 | `d9681acfc61670b472ba04c42be2d3fd20cc8648690dcf3ccb5186b39a4f3177` |
| factorlab-0.1.0.dev0.tar.gz | 1020220 | `0dfd816ea75a9f23b82dbefccda434b2e5e81e9b52aca4afcd084320c1f32ca6` |

制品与 SHA256SUMS.txt 位于 `local-release-20261025/dist/`。wheel 184 个文件成员、
176 个 Python 模块；sdist 446 个文件成员、388 个 Python 文件。wheel 无数据文件；
sdist 21 个 Golden Parquet 与 3 个小 CSV 与已许可审计版本完全一致；未发现商业受限数据、
密钥模式、二进制依赖、旧 notebook、原始日志或无关本地文件。扫描是有边界的内容检查，
不把模式检查表述为对任意秘密字符串的数学证明。

## Git 与证据绑定

起始候选 HEAD：`a11aadb0e098d515671ca4f0035aea810c0be269`。
实际验收快照提交：`ae4e8c317405dc21d036ff45c87756541162bb8c`。
软件回归前后提交一致、全部验收输入哈希未变；快照与本地候选受检文件原字节相同。
最终 Git blob 与工作区按既有源码身份协议仅规范化 CRLF→LF 后一致；resources/__init__.py
存在既有纯换行原始哈希差异，原始 Git/受检工作区哈希在最终 provenance 中分别保留。
最终本地提交在本报告生成和检查通过后创建，其完整 SHA 及制品/测试证据绑定记录在
`local-release-20261025/RELEASE_PROVENANCE.json`，避免在提交内容内循环引用自身 SHA。

| 作业 | JUnit SHA256 | receipt SHA256 |
|---|---|---|
| software-clean-py313 | `1e347cff256db6ab789095f087ae935792578394e75bf6979eded3df5a9d5369` | `0030324ff228bff6472ddf72e825502c0e7240465b5d8162b9c06e8b1098a013` |
| software-clean-py314 | `c0b4acc6d0d87b09314f6ab2e8968e841e8f80eb0638f0e1c3d3bd5093345b7a` | `05a314b560b8e1bffd6d8b35f51021a6ded5a1143f6b3464755f447901bf41ea` |

原始日志、JUnit、覆盖率、输入清单、安装及扫描证据均保存在 `local-release-20261025/`。
该目录被既有 .gitignore 排除，原审计记录未覆盖。

## NOT_RUN 与真实限制

* Linux、macOS、远端 GitHub Actions：NOT_RUN；本轮实际支持证据为 Windows x64 两个 Python 版本。
* 旧混合布局 `--suite full` 和原历史仓库重放：本轮 NOT_RUN；不是当前独立软件发布合同。
  既有历史失败和验收域映射原样保留在 docs/security 与 docs/migration；未冒充已通过。
* PIT/HFQ 正式认证、独立金融信任根批准：NOT_RUN，FORMAL_ADMISSION_BLOCKED 保持。
* GitHub 建仓/推送/Release、PyPI 上传：NOT_RUN；维护者步骤见 RELEASE_GITHUB_STEPS.md。
* 18 项市场结构特征仍为 CONTRACT_ONLY；真实市场有效性、实盘和兼容旧 pickle 不在通过范围。
* 公开仓库使用受检 sdist 的干净软件源码快照；该快照不含本机历史/私有 verifier/原仓库完整 CI。

没有经过验证的软件发布阻塞。上述金融认证或外部独立审查缺项不阻止普通合成数据研究软件测试版。
