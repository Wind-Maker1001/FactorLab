# FactorLab 0.1.0.dev0 软件发布许可证决定

日期：2026-10-25。**LICENSE_RELEASE_READY=PASS**。

在实际代码、Git 来源、依赖实际版本、许可证全文和重新构建的发布包范围内，未发现未经授权内置的第三方源码或已证实的许可证冲突。可以解除本次软件包公开分发的许可证阻塞，按根 MIT LICENSE 发布下列已审制品及其对应软件源码。

| 直接问题 | 最终回答 |
|---|---|
| 实际第三方内置源码文件数 | 0 |
| 已证实许可证冲突数 | 0 |
| 来源未知的分发源码文件数 | 0 |
| 必须删除/替换的源码文件 | 无 |
| 需要补充的文件 | 已新增并打包 NOTICE；原根 MIT LICENSE 原样保留 |
| Qbot MIT 与 CC/GPL 文档差异是否阻塞当前软件包 | 否；UPSTREAM_LICENSE_AMBIGUITY 保留，未发现受影响的具体分发代码文件 |
| 阻止当前软件包发布的具体文件/条款 | 无 |

## 生效制品与来源

基线/当前 Git HEAD：`a11aadb0e098d515671ca4f0035aea810c0be269`。本次为未提交的最小文档/打包修复，不能只以 HEAD 代表最终字节；构建输入清单和原文证据在 `local-license-final-20261025/`。

| 制品 | 实际文件成员 | Python 文件 | SHA-256 |
|---|---:|---:|---|
| factorlab-0.1.0.dev0-py3-none-any.whl | 184 | 176 | `36fe46e5b0075c97d0bdb69d8f0a9d8c9e313ec17ea12816536a0b7e339fb6f9` |
| factorlab-0.1.0.dev0.tar.gz | 445 | 388 | `e91f3a25a573445eb9ff7386f5eed4c5a57ff1af1982b2ffa99b3898ef39b3fd` |

制品位于 `local-license-final-20261025/dist/`。wheel 的 176 个模块、sdist 的 388 个 Python 文件均已逐项与当前源码核验；两包许可证元数据为 MIT，携带 LICENSE 和 NOTICE。

## 结论依据及保留事项

研究软件由 Wind-Maker1001 于 2026 年加入 quant-tv，并迁移到 factorlab；没有把旧 qbot 目录名当作来自 Qbot 早期代码的证明。未发现包内代码受独立的 CC BY-NC-SA/GPL 文件级授权覆盖。
上游 MIT、文档 GPL/CC 的不一致如实记录；原文档和私人 verifier 未被纳入软件包。本次没有替 Qbot 制造授权或改写其文档许可证。

TDX 两个示例保留，未内置 pytdx reader 或展开 key table；针对性比较未发现复制受保护源码表达。外部 pytdx 授权仍为 UNVERIFIED，不能用本结论代替其授权。
BaoStock 0.8.9 元数据声明 BSD，但全文/子类型保持 UNVERIFIED；它没有被分发进 FactorLab。NumPy/SciPy 的 GCC GPL 运行库例外、setuptools/autocommand 的 LGPL 以及 MPL 外部包已按实际使用方式核验，没有构成当前包的冲突。

本次通过构建、包内完整性/许可文件检查、两种制品安装与 CLI 帮助检查、依赖检查及 33 项相关回归。原始量化TV、提取基线、旧审计及旧制品的保全见 `preservation-final.json`。
详证见 `LICENSE_FINAL_AUDIT.md`、`THIRD_PARTY_CODE_INVENTORY.csv`、`DEPENDENCY_LICENSE_REPORT.md`、`LICENSE_ISSUES.json`。

PASS 覆盖上述软件制品，不认证整份本机历史仓库、私人研究数据或未来另行内置的依赖合集。本次未推送 GitHub、未上传 PyPI；未触发 PIT/HFQ 工作或金融准入变更。

**NO LIVE MONEY / PIT_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**。
