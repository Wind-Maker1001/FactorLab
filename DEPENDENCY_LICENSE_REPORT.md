# FactorLab 0.1.0.dev0 依赖许可证最终复核

审计日期：2026-10-25。结论：本次实际发布方式未发现依赖许可证冲突；`LICENSE_RELEASE_READY=PASS`。

## 实际版本与证据

解析当前 `pyproject.toml`、三个版本快照锁文件，以及现存 Python 3.14.0 / 3.13.9 环境的实际 METADATA 和法律文件；没有只沿用昨日交接结论。
三个锁文件分别含 23、69、44 个条目；全部固定版本均在本次实际环境元数据中得到核验。
两个环境各读取 70 个外部分发包，其中 69 个属于 3.14 完整锁，另一个为环境安装器 pip 26.2.2；两环境这些包的名称/版本集合相同。
这不表示 3.13 完整锁含 70 个条目，也不把所有环境工具都当作运行必需依赖。
每个环境保存 161 个实际非 Python 代码的许可证/通知文件，并保存 METADATA；JSON 中逐项列出 SHA-256。
主项目有 10 个直接核心依赖，另外解析 suppliers、pdf、dev extras 和实际快照中的传递/开发包。
另外读取 `requirements/py314-core.txt`、`suppliers.txt`、`dev.txt` 的当前固定声明，版本与本次快照一致。
`dev/requirements-*.txt` 四份旧 Python 3.9 配置也已读取并保存；它们未进入 wheel/sdist，不是当前 Python >=3.13 发布的依赖锁，未另行安装或重审其旧版本制品。

证据：`local-license-final-20261025/dependencies-py314.json`、`dependencies-py313.json`、`dependency-texts/py314/`、`dependency-texts/py313/`、`lock-verification.json`、`dependency-declarations.json`。
锁文件属于实际安装版本快照，并非逐制品哈希锁；本次报告对所读实际版本负责，未给范围内所有未来版本预先作许可证保证。

## 使用和再分发边界

实际 wheel/sdist 没有第三方库源码、二进制、site-packages 或 vendored 目录。Requires-Dist 和 requirements 仅声明分别安装的依赖。
没有证据表明 FactorLab 把 NumPy、Pandas、LightGBM 或 XGBoost 源码复制进自己的模块；通过公开 API 使用这些库不等于内置其源码。
当前软件包没有触发再分发外部组件所需的额外 LICENSE/NOTICE 附带义务。未来若实际打包依赖二进制/源码，应按当时被再分发的具体组件保留其条款；本次没有创建这样的依赖合集。

## 已具体复核的 copyleft、例外和元数据缺口

* **NumPy 2.3.4 / SciPy 1.16.3**：主体 BSD；实际 Windows LICENSE 明列 OpenBLAS/LAPACK BSD，以及 GCC runtime 的 `GPL-3.0-or-later WITH GCC-exception-3.1`。运行库例外文本已随实际法律文件保存，不能把 GPL 字样当作 FactorLab 整体 GPL 的证据。FactorLab 两包未内置这些 DLL，也没有改动或再分发该运行库。
* **setuptools 80.9.0 → autocommand 2.2.2**：确有 LGPL 组件。安装的子包元数据标 LGPLv3，源码头明确“version 3 ... or any later version”，因此记录 `LGPL-3.0-or-later`；Copyright 2014-2016 Nathan West。它属于外部构建工具，不在 FactorLab 两包中。没有触发该组件的修改/再分发义务。详见 `component-autocommand.json` 及所保存 LGPL 全文。
* **certifi 2026.10.19、pathspec 1.1.1、tqdm 4.70.1**：分别出现 MPL-2.0、MPL-2.0、MPL-2.0 AND MIT；本次未复制/修改或再分发其文件。MPL 的文件级分发义务不自动扩展到独立的 FactorLab 文件。核对 [Mozilla 官方 MPL 2.0](https://www.mozilla.org/en-US/MPL/2.0/) 第 3.1–3.3 节。
* **Apache 2.0**：PyArrow、XGBoost、requests 等保留自己的许可。若实际再分发覆盖组件，Apache 2.0 第 4 节的许可证、修改说明及适用 NOTICE 义务应随组件履行。本次只有外部依赖声明；[XGBoost v2.1.4 官方 LICENSE](https://github.com/dmlc/xgboost/blob/v2.1.4/LICENSE) 已另行保存，解决该 Windows 安装包未附独立法律文件的证据缺口。
* **jsonpath 0.82.2**：安装元数据未列独立 LICENSE，但 `jsonpath.py` 源码头有完整 MIT 授权及 Stefan Goessner、Kay Rhodes、Philip Budne 版权，已实际读取并保存。不是“无许可证”。
* **BaoStock 0.8.9**：元数据与分类器声明 BSD；安装记录未找到许可证全文，具体 BSD 子类型/全文保持 `UNVERIFIED`。未发现相反许可，也未将 BaoStock 源码或二进制并入 FactorLab，所以不是已证实冲突或当前包的发布阻塞。
* **AGPL/GPL/LGPL 误报排除**：packaging 的 SPDX 标识符字典列出很多许可证名称，它不是那些许可证对该包的授权声明。未发现实际 AGPL 组件；没有以字典命中增加冲突数。实际 GCC runtime GPL+例外和 autocommand LGPL 已单独处理。

## 全部实际分发包

各行均为 `DEPENDENCY_ONLY`，没有一行表示 FactorLab 内置该组件。顶层声明不是对外部 wheel 内所有组件重新许可；表中复杂组件应连同其保存的通知阅读。

| 包 | 实际版本 | 许可证/实际通知范围 | 实际角色 | 证据状态 |
|---|---|---|---|---|
| akshare | 1.18.88 | MIT | suppliers extra | METADATA + 1 legal/notice files |
| baostock | 0.8.9 | BSD family; subtype/full text UNVERIFIED (metadata declaration) | suppliers extra | UNVERIFIED 全文/子类型；BSD 元数据 |
| beautifulsoup4 | 4.15.0 | MIT License | optional/transitive/development; actual lock snapshot | METADATA + 2 legal/notice files |
| black | 25.9.0 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 3 legal/notice files |
| build | 1.3.0 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| certifi | 2026.10.19 | MPL-2.0 | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| cffi | 2.1.1 | MIT-0 | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| charset-normalizer | 3.5.2 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| click | 8.5.0 | BSD-3-Clause | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| cloudpickle | 3.1.2 | BSD-3-Clause | core runtime snapshot | METADATA + 1 legal/notice files |
| colorama | 0.4.6 | BSD-3-Clause | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| contourpy | 1.4.0 | BSD-3-Clause | core runtime snapshot | METADATA + 1 legal/notice files |
| coverage | 7.11.0 | Apache-2.0 | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| curl_cffi | 0.16.3 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| cycler | 0.12.1 | BSD-3-Clause | core runtime snapshot | METADATA + 1 legal/notice files |
| decorator | 5.3.1 | BSD-2-Clause | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| et_xmlfile | 2.0.0 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 2 legal/notice files |
| flake8 | 7.3.0 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| fonttools | 4.67.0 | MIT; additional Apache/BSD notices in LICENSE.external | core runtime snapshot | METADATA + 2 legal/notice files |
| html5lib | 1.1 | MIT License | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| idna | 3.20 | BSD-3-Clause | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| iniconfig | 2.3.1 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| joblib | 1.6.0 | BSD-3-Clause | core runtime snapshot | METADATA + 1 legal/notice files |
| jsonpath | 0.82.2 | MIT (complete grant in installed source header) | optional/transitive/development; actual lock snapshot | 源码头完整 MIT 授权已核验 |
| kiwisolver | 1.5.1 | BSD-3-Clause | core runtime snapshot | METADATA + 1 legal/notice files |
| lightgbm | 4.6.0 | MIT License | core runtime snapshot | METADATA + 1 legal/notice files |
| lxml | 6.1.3 | BSD-3-Clause; additional component terms in LICENSES.txt | optional/transitive/development; actual lock snapshot | METADATA + 2 legal/notice files |
| matplotlib | 3.10.7 | Matplotlib license (PSF-based); DejaVu/Bitstream Vera and STIX font notices | core runtime snapshot | METADATA + 3 legal/notice files |
| mccabe | 0.7.0 | MIT (Expat) | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| mini-racer | 0.14.1 | ISC | optional/transitive/development; actual lock snapshot | METADATA + 2 legal/notice files |
| mypy_extensions | 1.1.0 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| numpy | 2.3.4 | BSD-3-Clause; bundled OpenBLAS/LAPACK BSD; GCC runtime GPL-3.0-or-later WITH GCC-exception-3.1 | core runtime snapshot | METADATA + 4 legal/notice files |
| openpyxl | 3.1.5 | MIT | pdf/supplier extra | METADATA + 1 legal/notice files |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | core runtime snapshot | METADATA + 3 legal/notice files |
| pandas | 2.3.3 | BSD License | core runtime snapshot | METADATA + 1 legal/notice files |
| pathspec | 1.1.1 | MPL-2.0 | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| pillow | 13.0.0 | MIT-CMU | core runtime snapshot | METADATA + 1 legal/notice files |
| pip | 26.2.2 | MIT | environment installer; not locked or redistributed | METADATA + 42 legal/notice files |
| platformdirs | 4.13.5 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| pluggy | 1.7.0 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| pyarrow | 22.0.0 | Apache Software License | core runtime snapshot | METADATA + 2 legal/notice files |
| pycodestyle | 2.14.0 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| pycparser | 3.12 | BSD-3-Clause | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| pyflakes | 3.4.0 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| Pygments | 2.21.0 | BSD-2-Clause | optional/transitive/development; actual lock snapshot | METADATA + 2 legal/notice files |
| pyparsing | 3.3.3 | MIT | core runtime snapshot | METADATA + 1 legal/notice files |
| pypdf | 5.9.0 | BSD-3-Clause | pdf/supplier extra | METADATA + 1 legal/notice files |
| pyproject_hooks | 1.3.3 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| pytest | 8.4.2 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 2 legal/notice files |
| pytest-cov | 7.0.0 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 2 legal/notice files |
| python-dateutil | 3.0.0 | Apache-2.0 and BSD-3-Clause by contribution scope (dual-license notice) | core runtime snapshot | METADATA + 1 legal/notice files |
| pytokens | 0.4.1 | MIT License | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| pytz | 2026.5 | MIT | core runtime snapshot | METADATA + 1 legal/notice files |
| PyYAML | 6.0.3 | MIT | core runtime snapshot | METADATA + 1 legal/notice files |
| requests | 2.34.2 | Apache-2.0 | optional/transitive/development; actual lock snapshot | METADATA + 2 legal/notice files |
| scikit-learn | 1.7.2 | BSD-3-Clause | core runtime snapshot | METADATA + 4 legal/notice files |
| scipy | 1.16.3 | BSD-3-Clause and bundled notices; GCC runtime GPL-3.0-or-later WITH GCC-exception-3.1 | core runtime snapshot | METADATA + 5 legal/notice files |
| setuptools | 80.9.0 | MIT; vendored autocommand 2.2.2 LGPL-3.0-or-later; other component notices | optional/transitive/development; actual lock snapshot | METADATA + 24 legal/notice files |
| six | 1.17.0 | MIT | core runtime snapshot | METADATA + 1 legal/notice files |
| soupsieve | 3.0.4 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| tabulate | 0.10.0 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| threadpoolctl | 3.7.0 | BSD-3-Clause | core runtime snapshot | METADATA + 1 legal/notice files |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| typing_extensions | 4.16.0 | PSF-2.0 | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| tzdata | 2026.4 | Apache-2.0 | core runtime snapshot | METADATA + 2 legal/notice files |
| urllib3 | 2.8.0 | MIT | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| webencodings | 0.6.1 | BSD-3-Clause | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |
| wheel | 0.45.1 | MIT; vendored packaging Apache-2.0 OR BSD-2-Clause | optional/transitive/development; actual lock snapshot | METADATA + 4 legal/notice files |
| xgboost | 2.1.4 | Apache-2.0 (v2.1.4 upstream LICENSE verified) | core runtime snapshot | 元数据 + 官方 v2.1.4 LICENSE |
| xlrd | 2.0.2 | BSD-3-Clause | optional/transitive/development; actual lock snapshot | METADATA + 1 legal/notice files |

外部 pytdx 固定源码参考不属于包管理器安装依赖，也不在此 70 包计数内；其授权缺口及未内置事实见主审计 LIC-004。

没有发现需要删除、替换或把 FactorLab 整体改为 GPL/LGPL/AGPL 的具体依赖。保留 NO LIVE MONEY；本报告不调整金融研究准入。
