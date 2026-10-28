# CI baseline audit

The public baseline is `afac4f9d4c2b123c0b32787ccd1cf67fe965c723`.
All 186 nonpassing nodes from the preserved candidate JUnit were rerun there
under Windows Python 3.14.0 with the same interpreter and dependencies used
for the integration checks. All 186 statuses matched: 50 FAIL and 136 ERROR.
The earlier local control used `9cb0f49`, so it was not sufficient evidence
about the public baseline by itself. This audit adds that missing comparison.

| Primary category | Count |
|---|---:|
| A ENVIRONMENT_DEPENDENCY | 0 |
| B HISTORICAL_ARTIFACT_DEPENDENCY | 186 |
| C EXPECTED_FINANCIAL_REJECTION | 0 |
| D EXISTING_SOFTWARE_DEFECT | 0 |
| E TEST_HARNESS_DEFECT | 0 |
| F UNKNOWN | 0 |

| Historical cause | Count |
|---|---:|
| Missing upstream independent workflow/control root | 140 (136 ERROR, 4 FAIL) |
| Historical publication artifacts not carried | 37 FAIL |
| Original Git objects unavailable | 2 FAIL |
| Pinned attack fixture requires original control/preimages/layout | 7 FAIL |

These are primary causes supported by actual traces. The seven fixtures also
have a layout dependency, but fail first on missing authentic historical
control material; they are classified B, rather than inventing a framework
defect. No setup/collection error is a successful expected refusal. The
matrix preserves node IDs, both statuses, exception types, causes, suggestions,
CI/merge impact and trace identities. Original traces remain in the local audit
package because they contain machine paths.

The baseline rerun selects the 186 failed/error nodes, not the entire baseline
suite. Same status does not prove all code is defect-free, nor change a FAIL
into PASS. The new complete candidate regression and incremental scopes have
their own receipts. Missing history must not be fabricated, historical Golden
files or pins must not be refreshed, and existing tests must not be removed.

Full legacy compatibility remains FAIL. The full remote job propagates its
actual failure code and retains JUnit/logs. A passing incremental job certifies
only its declared selection. No financial authorization follows from either.

PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED / NO LIVE MONEY.
