# Independent maintainer review

NO LIVE MONEY · PIT_UNVERIFIED · FORMAL_ADMISSION_BLOCKED

The local branch is `codex/factorlab-p0-independent-security`. The accepted
starting branch remains at `98b133841a5cd5ecffcf715e8626b41db2c5b8b6`.
No remote, merge, software publication or PyPI upload is part of this delivery.

## Exact review material

After the evidence commit, the local delivery contains:

- `local-p0/factorlab-p0-review.bundle`: actual Git history and both branch refs.
- `local-p0/factorlab-p0-source-bytes.zip`: exact raw bytes of the final tracked
  worktree, with no ignored raw logs, environments, caches or supplier datasets.
- `local-p0/final-head-binding.json`: final HEAD, tree, bundle/ZIP hashes,
  executed input bindings, final identity checks and preservation observations.
- `local-p0/fixed-review-material`: retained contributor-created verifier and
  anchor copies, with their SHA-256 identities. These are review inputs, not
  independent approval evidence.

Git blobs and physical worktree bytes are distinct identities. The V1 anchor
authenticates exact physical bytes. Git checkout EOL conversion can change
physical bytes while preserving Git blob identity. The raw-byte ZIP therefore
preserves the actually observed source; a reviewer can clone the bundle and
overlay that exact snapshot, or inspect it without Git. Different rendered
bytes must fail integrity rather than being silently rehashed as authorized.
No historical Qbot path or historical receipt is reconstructed from FactorLab
code. The Qbot archive and its real Git objects remain a separate evidence scope.

The verifier is standard-library-only and never imports candidate code. A
reviewer must first independently inspect and retain its code and the anchor
outside contributor write control. Run that retained verifier with `python -I`,
an explicit candidate directory, the retained anchor and an out-of-band digest.
The reviewed candidate-material digest at implementation freeze is
`055461c7a4c9c856778e4a990e13a285afe52fb5f8ff7ec0f6dbc182738f39b2`.
The fixed-verifier file digest is
`4d12491aa114a48d83997feb90dfaf60b094d4151814b914ed8541668b38674c`.
These author-supplied digests demonstrate consistency only; they cannot approve
themselves. Integrity PASS exits with code2 and authorization BLOCKED; integrity
FAIL exits with code1. There is no local approval-import or approval-issue path.

## Review order

1. Verify the frozen baseline bundle/ZIP and unchanged original branch, then
   inspect the final source/test identity manifest and Git diff. Existing
   numerical implementations,208 old test files and35 Golden/reference files
   are preserved; only the existing migration gate delegates to the new refusal.
2. Inspect the fixed verifier, V1 anchor, closed scope and change boundary.
   Check the actual source and trust-control attacks and the rejection sentinel
   assertions. Synthetic tests and same-commit checker/anchor changes cannot
   issue approval.
3. Check the complete software JUnit against actual phase observations and
   input hashes. Interrupted and INTERNALERROR rounds remain failed/incomplete
   observations. Check Python3.13 compatibility separately from full3.14 scope.
4. Read all186 disposition rows and the319-node historical inventory. Check
   original Git-object provenance, unchanged archive content and the retained
   remaining historical FAIL. Its two genuine metadata deltas need independent
   scope/authenticity review; do not rewrite old receipts or conclusions.
5. Authenticate source/data redistribution permissions. Local engineering
   success and existing MIT metadata do not clear upstream or supplier rights.
6. Establish a real independent approval identity, externally held pins and
   change/revocation permissions before designing any later approval protocol.
   No current observation certifies financial qualification, PIT/HFQ evidence,
   tradability or live-money authority.

Raw legacy failure output can expand environment representations. It remains
local and outside Git; the review reports retain hashes and concise causes.
Do not publish raw logs, private archives or the Git history based on software
test success. Root release approval, historical authenticity review and license
permissions remain BLOCKED.
