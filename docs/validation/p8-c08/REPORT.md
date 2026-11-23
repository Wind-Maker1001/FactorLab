# P8-C08 · Azure Binance Demo Execute 恢复部署

北京时间 2026-11-23 22:43，Azure `factorlab-demo-01` 从应急热补丁切换到了 `origin/main` 的 `22b24957e22703bd75dbd4fe2e71979c3e752c06`。新进程在原账本上恢复，通过官方 Binance USD-M Demo 签名接口重新对账。原挂单随后通过策略路径撤销并确认终态，新收盘 K 线触发了一笔符合准入条件的自然订单。

| 验收项 | 结论 | 依据 |
| --- | --- | --- |
| `DEPLOYMENT_ACCEPTED` | 达成 | 已安装包的来源、独立发布路径和 systemd 实际 `ExecStart` 均指向上述 SHA |
| `EXECUTE_RECOVERED` | 达成 | 原账户和授权恢复，原订单由签名 GET 对账，新 Grid 准入产生实际 Demo 回执 |
| `LONG_RUNNING_OBSERVATION_ACTIVE` | 达成 | Execute 已启用并在运行。C06 每分钟采样、日报与 C07 每日备份定时器保持启用 |
| `24H/72H/7D_VERIFIED` | 三个窗口均待完整观测 | 新版本观测起点为 2026-11-23 14:43:05 UTC。既有窗口包含旧进程和历史停机 |

## 05:48 退出与修复

Azure journal 在 2026-11-22 21:48:03 UTC 先记录 `grid_start_refused / STATE_OR_CONFIGURATION_ERROR`，随后记录 `status=78/CONFIG`。最后一个退出快照的进程计数为 GET 56,194、POST 0、DELETE 1。服务的 `RestartPreventExitStatus=78` 解释了为什么 systemd 保持停机。此前 C06 在北京时间 16:31 的只读观测记录了 `MainPID=0`。

事故运维记录将触发异常定位为 `RiskRejected(STALE_RULES)`。VM 留存的 journal 只有上层分类，无法从这份 journal 单独还原异常类型。旧版代码会在启动取得交易规则，并在准入时检查 300 秒有效期。到期时，新订单在 POST 前被拒绝，旧 supervisor 将异常归入配置退出。VM 保留的 `demo_session.py.pre-rule-refresh-20261123` 与旧运行包的差异显示，现场热补丁在每次新提交前通过 `exchangeInfo` 和签名 `leverageBracket` 更新网关规则。热补丁进程 PID 62866 于 09:20:03 UTC 启动，成为本次发布前的实际执行基线。其仓库 checkout 是 `7c549e8d4ae8d88d118b30104f723a39860b8561`，安装包另有热补丁。

[#232](https://github.com/Wind-Maker1001/FactorLab/pull/232) 将这两个 GET 改为规则到期时按需调用，先更新规则，再开始计算账户和报价的新鲜度。精确 Grid 意图继续用自己的价格、数量和 client order ID。若发生到期边界竞争，`STALE_RULES` 进入现有可恢复路径。PR 的离线夹具验证了跨越 300 秒、变更交易过滤器和两个 GET 的失败路径。[合并检查](https://github.com/Wind-Maker1001/FactorLab/actions/runs/45181439101)通过。

## 同账本发布

从最新 `origin/main` 建立独立 worktree。将该提交的 Git 归档安装至 `/opt/factorlab/releases/22b24957e22703bd75dbd4fe2e71979c3e752c06/venv`。使用 Azure 原有 Python 3.13.18 和依赖，重装该提交的 FactorLab 包。服务用户导入了新路径中的运行模块，环境枚举仍指向 [Binance 官方 USD-M Demo REST](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info#testnet-api-information)。

在现有 Execute unit 增加 `20-release.conf` 指向新入口，保留原 state-root drop-in、systemd 凭据、退出及重启配置。systemd 正常停止旧进程后立即启动新进程，停止完成与启动命令返回之间约 51 ms。新 PID 为 `107003`，持有 `/var/lib/factorlab-demo/execute-state/session.lock`。GET-only unit 保持 `inactive/disabled`。原热补丁仍位于 `/opt/factorlab/venv`，如需回退可切换 Execute 入口并继续使用原账本。

前后核对了 binding、runtime、Grid control、protocol 和 controller 文件的 inode。新进程签名 GET 恢复后，protocol 账户与原 binding 一致，持久控制和全局授权均为 `RUNNING`。发布前授权已消耗 31 / 100 POST、190.4184000 / 700 USDT 名义金额。启动对账后的读数相同，最大亏损额度仍为 1 USDT。

## 官方 Demo 观察

时间均为 UTC。22:43:05 北京时间对应下表的 14:43:05 UTC。

| 观测点 | 交易和账本事实 |
| --- | --- |
| 14:43:05，停止后、启动前 | 原账本共 34 个 POST、17 个 DELETE，所有 dispatch 有回执，重复 POST client ID 为 0。已有 14 笔 FILLED、17 笔 CANCELED、1 笔 SUBMITTED。 |
| 14:43:27，初次恢复 | 启动恢复用签名 GET 查询账户、仓位、挂单、订单、成交和资金费，原有 1 笔挂单、零仓位，UNKNOWN 为 0。22 秒内 GET 83、POST 0、DELETE 0。首次恢复完成时间为 14:43:11.760 UTC，距 systemd 启动约 6.3 s。该快照的最近一次账户恢复操作耗时 1.568 s。 |
| 14:44:18，既有订单终态 | 原挂单由正常策略路径撤销，账本由 17 增至 18 笔 CANCELED，32 笔带交易所恢复记录的既有订单均有终态 GET 确认。累计 POST 和已用授权保持原数，DELETE 增加 1。 |
| 14:45:12，自然信号与轮换 | 14:45 收盘 K 线使 Grid 进入有效 `RUNNING`。新 POST 获得 Demo 回执并由签名 GET 确认。Grid 部署尾号由 00013 轮换至 00014，Campaign 总数从 23 增至 24，全局用量从 31 增至 32 POST、190.4184000 增至 196.8284000 USDT。账本共 35 个 POST，重复 client ID 为 0，UNKNOWN 为 0。 |

14:45:12 经济对账使用旧账本的 [5,000 USDT 期初钱包](../p8-r08/REPORT.md) 和恢复后的官方回执。15 笔去重成交的已实现盈亏为 −0.15560001 USDT，成交手续费为 0.01103530 USDT，8 笔唯一资金费流水合计 −0.09763384 USDT。三者与期初钱包相加减得到 4,999.73573085 USDT，等于当前签名账户钱包。加入当时的未实现盈亏 0 USDT 后，NAV 同为 4,999.73573085 USDT，钱包和 NAV 残差均为 0.00000000 USDT。资金费覆盖水位已推进到 14:45:10.713 UTC，状态为 complete。

## 观测、告警与备份

C06 沿用 `/var/lib/factorlab-demo/monitoring/samples.jsonl`、`alert-events.jsonl`、`daily-reports/` 和 journald。每分钟健康采样已经记录新 PID。日报按 00:05 UTC 生成 24h、72h、7d 汇总。14:44 UTC 读取的滚动报告含 1,250 个历史和当前混合样本，24h 采样覆盖率 86.81%，样本可用率 48.64%，三个窗口均为 `window_complete=false`。新版本分析以 PID 107003 及切换时间为起点，区分服务存活、账户/行情就绪与 Grid 下单条件。

外部告警投递缺少 `/etc/factorlab-demo/alert-config.json` 和 health unit 下的 `20-alert-webhook.conf`。本机继续通过 C06 样本、告警事件文件和 journald 留存运行证据。

C07 备份定时器 03:15 UTC 触发。最近一次计划备份 `backup-20261123T031502Z` 为 `SUCCEEDED`、`verified=true`。14:43:29 UTC 用新版本完成隔离恢复演练，12 个 SQLite 文件及 9 项校验全部通过，覆盖绑定、订单、资金费、持久控制、累计预算和凭据排除。恢复副本保留 GET-only 隔离约束，演练结束后由原机制清理。

## 新版本观测截止点

截至 14:48:16 UTC，Execute PID `107003` 为 `active/running/enabled`，systemd 自动重启计数为 0。服务从 14:43:05 UTC 起运行约 5 分 11 秒。C06 同 PID 的 5 个分钟样本覆盖 14:43:18 至 14:47:28 UTC，5/5 存活且健康，3/5 具备 Grid 下单条件。样本行情年龄为 1.302–4.232 s。末次账户恢复操作耗时 0.830 s，最近成功行情与账户对账均在 14:48:13–15 UTC。

新进程 REST 计数为 GET 1,254、POST 1、DELETE 1。唯一记录的 API 错误为一次查询 `ORDER_NOT_FOUND`，随后新订单正常提交并获得回执。恢复尝试累计 3 次，最近一次由 `QUOTE_TIME_REGRESSION` 触发，约 2.9 s 后恢复，截止时连续失败为 0。观测到的单次 API 最大耗时为 1.398 s。35 个账本 POST 中重复为 0，当前零 UNKNOWN、零待对账、零仓位、一笔已确认挂单。成交及资金费累计与 14:45 快照相同，NAV 残差保持 0。

本段全局预算净消耗 1 笔、6.4100000 USDT 名义金额，约 0.19 笔/分、1.24 USDT/分。截止时剩余 68 笔、503.1716000 USDT 名义金额。该速率只覆盖此次短观测。首次恢复完成后的有效订单、现有窗口的自动轮换和三次短暂恢复均沿用既有机制。

启动时成功执行了 `GET /fapi/v1/exchangeInfo` 与签名 `GET /fapi/v1/leverageBracket`。截止点已跨过规则的 300 秒有效期，计数仍各为 1，唯一新 POST 发生在该有效期内。到期以后首次符合规则刷新条件的自然策略动作，以及 24h、72h、7d 的完整窗口，继续由已启用的 Execute 和 C06 观测覆盖。当前证据确认发布、恢复和持续采样已启动。

### 复查入口

```sh
sudo systemctl show factorlab-demo-execute.service \
  -p ExecStart -p ActiveState -p MainPID -p NRestarts -p ExecMainStartTimestamp
sudo systemctl list-timers 'factorlab-demo-*' --all
sudo -u factorlab-demo /opt/factorlab/venv/bin/python -m \
  factorlab.integrations.binance_futures_trading.demo_cloud_runtime report \
  --state-root /var/lib/factorlab-demo/execute-state \
  --monitor-dir /var/lib/factorlab-demo/monitoring \
  --backup-root /var/backups/factorlab-demo
```

检查各窗口时，从发布切换时间划定证据区间，使用 C06 的覆盖率、间隔及 PID 分段字段，并与 journal 和 C07 备份记录对齐。

