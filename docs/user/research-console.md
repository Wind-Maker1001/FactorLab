# 本地研究控制台

在仓库根目录、已有 FactorLab Python 3.13/3.14 环境中运行：

```powershell
python -m pip install -e .
python -m factorlab.console --config configs/console/public-research.json
```

打开 <http://127.0.0.1:8765>。端口可通过 `--port 8766` 改变；监听地址固定为 `127.0.0.1`。关闭终端或按 Ctrl+C 停止。
界面和图表资源均本地提供，无 CDN、Node 构建、账户、数据库服务或额外 Python 运行依赖。
安装 wheel 的用户仍需单独配置本地公开报告目录；研究报告不嵌入软件 wheel。

## 数据配置

配置中的路径相对于**配置文件所在目录**解析，与启动时工作目录无关。默认配置只开放三组已有公开派生证据。
`projects[].root` 是允许的公开研究根目录，`files` 是明确允许读取的文件名清单，不支持目录通配符。
不要把包含凭证的报告或未经允许的供应商数据加入清单。新增缺失文件后重启服务以更新资源目录。

默认配置没有本地 Tracker 路径，所以实验数为 0；这不表示 P6 研究未执行。公开研究摘要与实验记录是不同的数据源。
要浏览已有实验，复制配置到私有位置，在 `trackers` 中明确加入允许的研究存储：

```json
{
  "id": "local-research",
  "root": "D:/Research/approved-experiments",
  "project": "p6-r01",
  "strategy": "UNKNOWN"
}
```

`project` / `strategy` 是本地存储的显式分组标签，不是从模型名推测的运行属性；混合策略存储应使用 `UNKNOWN`，不要补造身份。
存储沿用 ExperimentTracker 的 `experiments/*.json` 和 `runs/*.json`。控制台验证原记录及绑定，复用 query、compare、reproduction；
只在内存建立一次请求所需的查询索引，不访问持久化 SQLite。没有 run 的定义计入已发现定义数，但不会生成虚构运行。
日期筛选接受已保存运行时间的前缀。历史比较要求同一存储、相容数据/测试窗/标签/成本/样本/评估身份，并沿用 Tracker 对成功执行和证据资格的限制。

实验详情显示配置、身份、已记录的 Python/平台/常见库版本、指标和校验收据。未知值保持未知。
原命令不传到浏览器，完整进程环境不传到浏览器，常见凭证字段及绝对本地路径被遮蔽；产物路径不被跟随。
复现信息是保存的证据，不是新执行或产物完整性认证；`artifact_verification` 明确为 NOT_RUN。

## 浏览方式与研究含义

- 左侧导航切换六个视图；顶部研究项目筛选控制项目范围。与所选项目不匹配的专用视图会明确提示。
- 策略页切换总体、年度、行业、逐证券剔除或独立账户统计，再选择策略、指标及适用的年度/证券。
  图中一个圆点表示一个行级统计，不是一条组合净值；悬停或键盘聚焦可查精确值，同源表格保留原始精度。
  多页数据的图只展示当前页，不对筛选后的数据另算一套汇总。
- 54 是每策略的独立证券年度账户数，324 是六策略账户结果行数。年度账户会重置，不能跨年连成组合收益。
- 原始价格收益已经扣交易成本；现金分红权益不是实际到账现金。总收益不可用时保持 null，不转成 0。
- D01 的 24 证券、22,955 合格证券日、445 拒绝日、43 连续区间是研究输入质量，不是正式历史 PIT 或 HFQ 认证。
- 报告打开为本地纯文本，PNG 为本地图片；Markdown 中的 HTML/脚本不会执行。表格可横向滚动。
- 默认采用单指标、单色点图。圆点位置、标签、悬停及表格共同表达数值，不靠颜色区分策略；支持强制颜色和打印。

任务状态始终同时展示规范状态、原始状态和来源。P6-R01 的明确完成状态映射为 RESEARCH_COMPLETED；
P6-D01 的研究输入就绪映射为 PARTIAL，保留未认证边界；P6-E01 的证据阻塞映射为 BLOCKED。
已明确保存的 SOFTWARE_ACCEPTED、RESEARCH_COMPLETED、PARTIAL、BLOCKED、NOT_RUN、UNKNOWN 原样呈现；其他值为 UNKNOWN。
SUCCEEDED 只表示执行状态，不自动表示研究完成或软件验收。不得通过文件名、文件存在、修改时间或数值正负升级状态。

`milestones` 可保存显式 GitHub 链接、PR 标题、本地阶段、报告 ID、实验身份、快照时间和来源。
默认三条记录来自 2026-11-10 获取的本地 origin/main 提交信息，GitHub/CI 状态为 UNKNOWN；它们不是实时 API 查询结果。
只有 HTTPS GitHub 链接会成为外部可点击链接。没有认证、后台同步或自动轮询。

## 数据访问与安全边界

- 只有固定 API 路由和启动时登记的资源 ID 可被读取，不接受文件路径、命令或动态 Python 代码。
- 每次读取重新解析真实路径；文件和 Tracker 记录必须留在明确允许的根目录，符号链接逃逸被拒绝。
- 单文件读取上限 8 MiB；表格最多每页 100 行，默认只传 50 行；账户页只传 30 行。大账本不在默认清单中。
- JSON 证据按视图投影，不开放整个 manifest、供应商原始文件或研究目录下载。自定义 CSV 也须是允许公开的派生摘要。
- GET 是唯一数据方法。POST/PUT/PATCH/DELETE 等返回 405；无订单、账户、交易、训练、回测或编辑入口。
- Host 与 Origin 必须为本地服务地址；拒绝跨站请求，不提供 CORS；CSP 禁止内联脚本、外部资源、表单和嵌入。
  报告用纯文本及明确 MIME，禁用 MIME 嗅探；错误响应不返回堆栈或本地路径。
- 配置是本机操作者的信任边界。允许文件中的公开文字会被展示；不要配置含私密信息的报告。
  该服务不是同机恶意进程之间的隔离机制，不应通过代理暴露公网。

定向验证：

```powershell
python -m pytest tests/research/test_console.py tests/research/test_experiment_tracker.py -q
```

始终保持：`NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`。
