# Raw Git objects and stage-one archive bytes

Additional actual verification bound all450 selected source paths at the fixed
baseline to their Git blob IDs and raw SHA256. The original extraction used
`git archive`; under the original Windows checkout/export settings,353 exported
files differ from their raw blob bytes only by LF/CRLF. No content other than
line endings differs. The original450 `source_sha256` fields therefore describe
the stage-one exported byte streams, not always the immutable raw Git blob.

The original identity fields are retained unchanged. New
`source_git_blob_sha256` fields independently bind raw bytes; a per-record
`source_sha256_contract` and top-level clarification state the distinction.
The baseline path/blob identities were checked against ls-tree, raw bodies read
with cat-file --batch and exported bytes rechecked against the original digest.
No historical hash or conclusion was replaced to manufacture a pass.

`evidence/git-blob-archive-binding.json` records all450 raw/export bindings and
the353 transformed paths. Frozen reference/Golden byte preservation remains a
separate exact check, not a blanket EOL normalization allowance for those files.
Public artifact digests always hash actual built archive bytes.

`evidence/core-math-ast-comparison.json` additionally compares37 modules under
factors/dataset/labels/models/backtest/metrics/validation/portfolio to raw baseline
blobs. Only Python import package names are normalized. All37 ASTs match;
the result does not cover every metadata/provider module or grant financial
qualification. Cross-version numerical and full behavioral tests are separate.
