"""Independent command; no formal configuration, Forward or trading imports."""

import argparse
from dataclasses import replace
import json
from pathlib import Path

from .contracts import DiscoveryConfig
from .private_inputs import load_private_inputs
from .receipts import code_evidence, digest, verify_receipt, write_receipt
from .runner import _destination, run_frames, run_synthetic


def main():
    parser = argparse.ArgumentParser(
        description="Bounded exploratory factor discovery. NO LIVE MONEY."
    )
    parser.add_argument("--mode", choices=("synthetic", "private"), default="synthetic")
    parser.add_argument("--development-start")
    parser.add_argument("--development-end")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--frozen-receipt", type=Path)
    parser.add_argument(
        "--output-root", type=Path, default=Path("results/exploratory/factor-discovery")
    )
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        receipt = verify_receipt(args.verify)
        print(
            json.dumps(
                {
                    "receipt_verified": True,
                    "status": receipt["status"],
                    "version": receipt["run_version"],
                }
            )
        )
        return 0
    config = DiscoveryConfig()
    changes = {}
    for name in ("development_start", "development_end"):
        if getattr(args, name):
            changes[name] = getattr(args, name)
    config = replace(config, **changes)
    if args.mode == "synthetic":
        result = run_synthetic(config, output_root=args.output_root / "synthetic")
        receipt, destination = result.receipt, result.destination
    else:
        status, loaded, reason = load_private_inputs(args.manifest, args.frozen_receipt)
        if status == "SUCCESS":
            bars, benchmark, calendar, labels, evidence, universe = loaded
            result = run_frames(
                bars,
                benchmark,
                calendar,
                config,
                evidence=evidence,
                labels=labels,
                universe=universe,
                output_root=args.output_root / "private",
                real_data_status=status,
                real_data_reason=reason,
                private_input_paths=(args.manifest, args.frozen_receipt),
            )
            receipt, destination = result.receipt, result.destination
        else:
            payload = {
                "status": status,
                "real_data_status": status,
                "completion_reason": reason,
                "real_data_reason": reason,
                "config": config.as_dict(),
                "code_and_environment": code_evidence(),
            }
            payload["run_version"] = digest(payload)
            destination = _destination(
                args.output_root / "private", payload["run_version"]
            )
            (destination / "summary.md").write_text(
                "# 真实探索数据未运行\n\nNO LIVE MONEY。\n\n%s：%s\n\n缺失真实证据不能用合成结果替代。\n"
                % (status, reason),
                encoding="utf-8",
            )
            receipt = write_receipt(destination, payload)
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "real_data_status": receipt.get("real_data_status"),
                "counts": receipt.get("counts"),
                "destination": str(destination),
            },
            ensure_ascii=False,
        )
    )
    return 0 if receipt["status"] in ("SOFTWARE_MVP_SUCCESS", "NOT_RUN") else 1


if __name__ == "__main__":
    raise SystemExit(main())
