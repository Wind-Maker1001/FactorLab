# Trusted risk selection performance

Actions [43728021733](https://github.com/Wind-Maker1001/FactorLab/actions/runs/43728021733) selected risk from 15:06:51.949 to 15:08:41.182 UTC. Sealing succeeded; artifact upload was cancelled at the two-minute job boundary. This is distinct from a classification or identity failure.

The replay subject is `336d64f80fceb90e6973b340fe049bf9363ca63a`, with tree `b8b856ac9c33ea95027a90cdf770439fd1673ee0` and parents `ac3505faa13f2cc9533fb13ba16551c4d521cd36`, `e1af57acd48cb27738d5b8315df129c5156bcaa5`. The original analyzer comes directly from the first parent, not a reconstructed policy approximation.

## Measurement

Local Windows measurements are computation evidence, not hosted Runner acceptance. The final instrumented, non-cProfile comparison measured 146.469 seconds before and 27.646 seconds after (81.1% reduction). Inclusive phase measurements:

| Phase | Before seconds | After seconds |
| --- | ---: | ---: |
| Git subprocesses | 0.262 | 0.315 |
| Tree enumeration | 0.148 | 0.172 |
| Blob acquisition and decoding | 0.707 | 0.758 |
| Consumer graph, including AST and dynamic calls | 145.478 | 26.549 |
| Output receiver proof, included in graph | 11.587 | 11.472 |
| Closure traversal | 0.00143 | 0.00144 |
| Classification | 0.00243 | 0.00354 |
| JSON serialization | 0.00159 | 0.00205 |

Rows overlap: Git calls are included in tree/blob acquisition; output receiver proof is included in graph construction. Unchanged selection orchestration includes formal-loader handling and decision assembly. Closure and classification are not meaningful bottlenecks for this subject.

Original cProfile evidence identified 587,589,353 string suffix checks and approximately 7.88 million dependency path parses. The dependency/literal Cartesian scan, not Git, sealing or output serialization, dominated. Evidence JSON already generated no consumer literals under the original policy, but still incurred dependency metadata traversal. Actual source/configuration references to evidence remain relevant.

The complete 110,725-byte risk plans and sealed validation identities matched byte-for-byte within the comparison process, with no excluded fields. A separate-process byte check initially failed: original set traversal can reorder reason records between interpreter hash seeds. This is not removed from the strict original/optimized comparison; that comparison uses the same process and requires identical bytes and digest. Independent-repeat comparisons may normalize reason-list ordering only, preserving every record and all fields.

Targeted verification: `python -m pytest -q tests/security/test_pr_risk.py tests/security/test_pr_identity.py`: **112 passed in 118.09 seconds**. The first run had one incorrect new test expectation: the original conservative module substring rule treats module `x` as matching `prefix`; the expectation was corrected rather than narrowing that rule.

## Reproduction and adoption

With the recorded subject available in the repository object database:

```sh
python -I docs/validation/ci-risk-selection/benchmark.py --repository . --output measurement.json
```

The benchmark obtains the original analyzer using Git, evaluates both versions on the exact subject, validates both issued identities against the recorded event/run fields, and requires identical plan and sealed-identity bytes. It does not execute candidate loaders or rewrite evidence.

The existing workflow uses the analyzer from trusted PR base. This optimization therefore requires its own `CI control review` through the `ci-control-review` environment and reviewed adoption on trusted base before rerunning PR #175. Candidate code must not substitute the trusted analyzer. The two-minute timeout remains unchanged. PR #174 changes a separate region of the shared risk test file for sharding contracts; no workflow changes are included here. PR #176 owns targeted historical isolation independently.

Hosted acceptance requires trusted selection and artifact upload to finish on the adopted base, followed by the existing gates. This local replay does not establish the end-to-end 900-second CI target. No automatic merging, Ruleset changes, evidence deletion or financial authorization. NO LIVE MONEY.
