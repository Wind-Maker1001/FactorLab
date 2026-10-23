"""Compare candidate preparation with the original Git-bound synthetic recipe."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pandas as pd

from frozen_engineering_migration import assert_fixture_functions_equal


ROOT = Path.cwd()
FIXTURE = "tests/research/test_cli_phase2.py"


def module(name, path):
    specification = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(result)
    return result


def equal(actual, expected):
    assert len(actual) == len(expected) == 4
    pd.testing.assert_index_equal(actual[0], expected[0], exact=True)
    assert actual[0].freqstr == expected[0].freqstr
    assert actual[0].to_numpy().tobytes() == expected[0].to_numpy().tobytes()
    for actual_frame, expected_frame in zip(actual[1:], expected[1:]):
        pd.testing.assert_frame_equal(actual_frame, expected_frame, check_exact=True)
        assert actual_frame.attrs == expected_frame.attrs
        for column in actual_frame.columns:
            if actual_frame[column].dtype.kind in "biufcmM":
                assert (
                    actual_frame[column].to_numpy().tobytes()
                    == expected_frame[column].to_numpy().tobytes()
                )


def isolation(builder, expected):
    first = builder()
    equal(first, expected)
    first[1].loc[first[1].index[0], "REV1"] += 999.0
    first[2].loc[first[2].index[0], "adj_open"] += 777.0
    first[3].loc[first[3].index[0], "close"] += 555.0
    equal(builder(), expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timing-repeats", type=int, default=0)
    args = parser.parse_args()
    contracts = args.contract_root
    sys.path.insert(0, str(ROOT))
    manifest = json.loads((contracts / "baseline-collection.json").read_text())
    hashes = json.loads((contracts / "baseline-source-sha256.json").read_text())
    original_bytes = subprocess.check_output(
        ["git", "show", manifest["subject_sha"] + ":" + FIXTURE]
    )
    assert hashlib.sha256(original_bytes).hexdigest() == hashes[FIXTURE]
    original_functions = assert_fixture_functions_equal(
        original_bytes, (ROOT / FIXTURE).read_bytes()
    )
    candidate = module("ci_candidate_synthetic_inputs", ROOT / FIXTURE)
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "original_synthetic_inputs.py"
        source.write_bytes(original_bytes)
        original = module("ci_original_synthetic_inputs", source)
        expected = original._inputs()
        equal(candidate._inputs(), expected)
        isolation(candidate._inputs, expected)
        if hasattr(candidate, "_input_template"):
            template = candidate._input_template(
                1, candidate.np.__version__, pd.__version__
            )
            assert isinstance(template, bytes)
            template_sha = hashlib.sha256(template).hexdigest()
            isolation(candidate._inputs, expected)
            assert (
                hashlib.sha256(
                    candidate._input_template(
                        1, candidate.np.__version__, pd.__version__
                    )
                ).hexdigest()
                == template_sha
            )
        else:
            template_sha = None
        negative_controls = []
        broken = original._inputs()
        broken[1].loc[broken[1].index[0], "REV1"] += 0.000001
        try:
            equal(broken, expected)
        except AssertionError:
            negative_controls.append("changed_value")
        broken = list(original._inputs())
        broken[1] = broken[1].iloc[:-1]
        try:
            equal(broken, expected)
        except AssertionError:
            negative_controls.append("dropped_row")
        shared = original._inputs()
        try:
            isolation(lambda: shared, expected)
        except AssertionError:
            negative_controls.append("shared_mutable_state")
        assert len(negative_controls) == 3
        timings = []
        for _ in range(2 if args.timing_repeats else 0):
            row = {}
            for name, build in (
                ("original", original._inputs),
                ("candidate", candidate._inputs),
            ):
                started = time.perf_counter()
                for _ in range(args.timing_repeats):
                    build()
                row[name + "_seconds"] = time.perf_counter() - started
            timings.append(row)
    cache = getattr(candidate, "_input_template", None)
    cache_info = cache.cache_info()._asdict() if cache else None
    receipt = {
        "kind": "synthetic_fixture_equivalence_not_financial_certification",
        "baseline_sha": manifest["subject_sha"],
        "subject_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "subject_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
        ),
        "baseline_fixture_sha256": hashes[FIXTURE],
        "candidate_fixture_sha256": hashlib.sha256(
            (ROOT / FIXTURE).read_bytes()
        ).hexdigest(),
        "numpy_version": candidate.np.__version__,
        "pandas_version": pd.__version__,
        "calendar_sessions": len(expected[0]),
        "frame_rows": [len(frame) for frame in expected[1:]],
        "frame_content_sha256": [
            hashlib.sha256(
                pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes()
            ).hexdigest()
            for frame in expected[1:]
        ],
        "frame_columns": [list(frame.columns) for frame in expected[1:]],
        "exact_values_dtypes_index_and_attrs_equal": True,
        "original_function_bodies_unchanged": original_functions,
        "mutations_do_not_affect_next_call": True,
        "negative_controls_detected": negative_controls,
        "process_local_cache": cache_info,
        "immutable_template_sha256": template_sha,
        "timing_repeats_per_round": args.timing_repeats,
        "paired_microbenchmarks_not_full_ci_wall_time": timings,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise ValueError("Refuse to overwrite existing fixture evidence")
    args.output.write_bytes(
        (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
    )
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
