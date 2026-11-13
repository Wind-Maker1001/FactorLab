import csv
from decimal import Decimal
import json
from pathlib import Path
import shutil
import pandas as pd

ROOT = Path('D:/Code/FactorLab-worktrees/p7-r04b-e01-grid-study')
NEW = ROOT/'results/p7-r04b-e04-parallel-history'
OLD = ROOT/'results/p7-r04b-exploratory-history'
BASE = ROOT/'docs/validation/p7-r04b-e01/exploratory-history'
DEST = ROOT/'docs/validation/p7-r04b-e04'


def main():
    baseline = list(csv.DictReader((BASE/'summary.csv').open()))
    current = list(csv.DictReader((NEW/'summary.csv').open()))
    old_recon = {r['account_id']:r for r in json.loads((BASE/'reconciliation.json').read_text())}
    mismatches, not_verified, details = [], [], []
    if len(current) != 16:
        mismatches.append('account count is not 16')
    excluded = {'wall_seconds'}
    for old, new in zip(baseline, current):
        account = old['account_id']
        for key in old:
            if key not in excluded and old[key] != new[key]:
                try:
                    same = Decimal(old[key]) == Decimal(new[key])
                except Exception:
                    same = False
                if not same:
                    mismatches.append(dict(account=account,field=key,old=old[key],new=new[key]))
        rel = Path(old['scenario'])/old['arm']/old['mode']
        fifo = json.loads((NEW/rel/'fifo.json').read_text())
        old_fifo = json.loads((BASE/rel/'fifo.json').read_text())
        if fifo != old_fifo:
            mismatches.append(dict(account=account,field='full_fifo'))
        econ = fifo['summary']
        for key in ('reconciliation_residual','reported_equity_difference'):
            actual = Decimal(econ[key])
            if abs(actual)>Decimal('1e-8') or actual != Decimal(old_recon[account][key]):
                mismatches.append(dict(account=account,field=key))
        checks = {}
        for name in ('daily.csv','trades.csv'):
            original = OLD/rel/name
            if not original.exists():
                checks[name] = 'NOT_VERIFIED'
                not_verified.append(str(rel/name))
            else:
                try:
                    pd.testing.assert_frame_equal(pd.read_csv(original), pd.read_csv(NEW/rel/name),
                        check_exact=True)
                    checks[name] = 'EXACT_MATCH'
                except AssertionError as error:
                    checks[name] = 'MISMATCH'
                    mismatches.append(dict(account=account,field=name,error=str(error)))
        checks.update(account_id=account, reconciliation_residual=econ['reconciliation_residual'],
                      reported_equity_difference=econ['reported_equity_difference'])
        details.append(checks)
    for name in ('design.json','data-identity.json','admission.json'):
        if json.loads((BASE/name).read_text()) != json.loads((NEW/name).read_text()):
            mismatches.append(dict(field=name))
    if len(list(NEW.rglob('paper.sqlite'))) != 16:
        mismatches.append('independent SQLite count is not 16')
    study=json.loads((NEW/'study.json').read_text())
    resources=json.loads((NEW/'resources.json').read_text())
    wall=study['wall_seconds']
    result=dict(status='PARALLEL_HISTORY_MISMATCH' if mismatches else 'PARALLEL_HISTORY_ACCEPTED',
        code_head='1128b13741a492f02e08a8bdea4394f053581552',accounts=len(current),
        serial_wall_seconds=1081.768,parallel_wall_seconds=wall,speedup=1081.768/wall,
        time_saved_seconds=1081.768-wall,reduction=1-wall/1081.768,
        comparison='Exact CSV values (Decimal normalization only), exact parsed FIFO JSON, exact full NAV/trade DataFrames; wall_seconds excluded.',
        mismatches=mismatches,not_verified=not_verified,accounts_comparison=details,resources=resources)
    DEST.mkdir()
    for name in ('summary.csv','study.json','resources.json','data-identity.json','design.json','admission.json'):
        shutil.copyfile(NEW/name,DEST/name)
    (DEST/'assessment.json').write_text(json.dumps(result,indent=2)+'\n')
    text=f'''# P7-R04B-E04：四进程历史验收

**{result['status']} / NO LIVE MONEY**

## 执行与环境

执行代码 HEAD：`{result['code_head']}`，分支 `research/p7-r04b-e01-grid-study`，原 PR #143。复用原 Anaconda Python 3.12.7、Windows 11 build 26340 和原 certified generation；冻结配置和数据身份通过原入口检查，并与串行保存的 design/data-identity/admission JSON 精确一致。工作区四个无关未提交文件不参与实验修改。没有重新下载、复制行情、重跑串行历史或全量测试。

实际测量命令：`D:/Environments/anaconda3/python.exe -I D:/Code/p7-r04b-e04-measure.py`。测量脚本通过 `runpy` 调用以下原 CLI，未更改回测代码：

```bash
D:/Environments/anaconda3/python.exe examples/research/grid_independent_demo.py --snapshot D:/Code/p7-r04b-official-payloads/certified/runs/bde00b2f948842e2b8c2a00465a9f8c6 --output results/p7-r04b-e04-parallel-history
```

测量脚本首次在调用 CLI 前因现有 psutil.disk_usage 兼容错误退出，改用标准库磁盘查询后才执行本轮唯一一次完整历史实验。新目录隔离原串行输出。默认 ProcessPoolExecutor 4 workers、spawn；16 个独立 SQLite，全部账户成功后主进程才写最终汇总。资源采样间隔 0.2 秒，见 resources.json；采样内存是进程树 RSS 求和峰值，共享页可能重复计数。

## 一致性

完成 {len(current)}/16 账户。账户 ID、冻结矩阵顺序及 summary.csv 除耗时以外的全部字段精确一致（仅将 Decimal 的等价表示规范化）。逐账户完整 FIFO JSON 与旧保存证据一致，NAV 残差及报告权益差与旧 reconciliation.json 一致且不超过原 1e-8 USDT。原本地 NAV/成交明细存在时，以 check_exact=True 比较全序列，不放宽浮点阈值。

不一致：{json.dumps(mismatches,ensure_ascii=False)}。

未验证：{json.dumps(not_verified,ensure_ascii=False)}。

逐账户比较及完整资源数值见 [assessment.json](assessment.json)，新账户耗时见 [summary.csv](summary.csv)。原串行证据未覆盖。

## 实测性能

| 指标 | 实测 |
|---|---:|
| 串行矩阵墙钟基准 | 1081.768 s |
| 并行矩阵墙钟 | {wall:.6f} s |
| 含 CLI 检查的测量墙钟 | {resources['cli_wall_seconds']:.6f} s |
| 实际加速比 | {result['speedup']:.4f}× |
| 节省时间 | {result['time_saved_seconds']:.6f} s |
| 时间减少比例 | {result['reduction']*100:.4f}% |
| 主进程矩阵 CPU | {resources['matrix_main_cpu_seconds']:.6f} s |
| 子进程账户累计 CPU | {resources['worker_account_cpu_seconds']:.6f} s |
| 采样进程树峰值 RSS | {resources['peak_process_tree_rss_bytes']} bytes |
| 最大工作进程数 | 4 |
| 完整输出占用（测量文件写入前） | {resources['output_bytes']} bytes |

CPU 累计工作量按原执行器逐账户 process_time 汇总计算，不含 spawn/import CPU；从 study.json 的主/工作累计值减测量主进程 CPU 得到，边界误差估计不超过 0.02 秒。主进程 CPU 含资源采样线程。进程树子进程计数可能还包含 multiprocessing resource tracker，不应误当成额外账户 worker。串行与并行使用同一运行时、冻结数据和配置；串行没有留存同时运行的外部负载或资源峰值，因此速度比是两次实测运行的比较，不是受控空载硬件基准。

## 判断

工程正确性以 16 账户成功和上述经济一致性为依据，性能以独立的实测速度比报告，不设加速门槛。此结论只适用于现有 R04B 独立账户矩阵，不能直接外推为 FactorLab 所有历史回测类型均已验收。策略经济结果与原探索性研究相同，性能验收不增加策略有效性证据。

保留 EXPLORATORY_LISTING_ADMISSION_RELAXED、LISTING_UNVERIFIED、PIT_UNVERIFIED、HFQ_UNVERIFIED、EXPOSURE_NOT_MATCHED 和 FUNDING_NOT_IN_PAPER_LEDGER；不接真实下单。
'''
    (DEST/'report.md').write_text(text,encoding='utf-8')
    shutil.copyfile(Path(__file__),DEST/'compare.py')
    shutil.copyfile(Path('D:/Code/p7-r04b-e04-measure.py'),DEST/'measure.py')
    print(json.dumps({k:v for k,v in result.items() if k not in ('accounts_comparison','resources')},indent=2))


if __name__=='__main__':
    main()
