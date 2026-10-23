"""Offline TDX DAY audit CLI, isolated from production FactorLab commands."""

import argparse
from pathlib import Path

from factorlab.data.tdx import TdxError
from factorlab.data.tdx_staging import run_audit


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读TDX DAY审计与RAW staging；不认证正式研究数据")
    parser.add_argument("--vipdoc-root", required=True, type=Path, help="实际vipdoc目录，不能传安装目录的猜测名称")
    parser.add_argument("--report-root", type=Path, default=Path("results/diagnostics/tdx-local-data"))
    parser.add_argument("--convert", action="store_true", help="写独立RAW staging，不注册正式Provider")
    parser.add_argument("--staging-root", type=Path, default=Path("data/research/staging/tdx"))
    parser.add_argument("--start", default="2020-01-02")
    parser.add_argument("--end", default="2026-10-09")
    parser.add_argument("--research-start", default="2021-09-13")
    parser.add_argument("--research-end", default="2026-09-11")
    parser.add_argument("--warmup-days", default=130, type=int)
    parser.add_argument("--calendar", type=Path, help="仅本地CSV/Parquet参考日期；不自动声称独立核验")
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    try:
        result = run_audit(args.vipdoc_root, args.report_root, args.staging_root if args.convert else None,
                           args.start, args.end, args.research_start, args.research_end, args.warmup_days,
                           args.calendar, args.run_id,
                           progress=lambda done, total: print("已审计 %s/%s" % (done, total), flush=True))
    except (TdxError, OSError, ValueError) as exc:
        print("TDX审计失败：%s" % exc)
        return 2
    print("报告：%s；状态：%s；正式研究合格：False" % (result["report_dir"], result["status"]))
    return 0 if result["status"] == "engineering_complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
