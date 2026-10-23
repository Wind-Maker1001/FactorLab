# Security admission migration

Status: BLOCKED_INDEPENDENT_REVIEW_REQUIRED. No formal financial release is issued.

Upstream control byte identities were actually checked at the fixed baseline:

* trust-anchor.json raw SHA256:
  3a594469a9436f653f2a1fbffc9cc197c8cbc66ccc1120dd25252afc038a559d.
* independent checker SHA256:
  1ce0b775debcb7961fda9acf3a1b9a329ab21f5d7029fd20d9fcf0fcd7cf219f.
* C-01 legacy cc13795d144592b360e560a9b979e183d6b01907;
  source dabdc87b80d3c88ccf56034981a3c5cf0710ed63;
  reviewed source2198d393b75fbccfa05d49ada57cdf6703c12bb8.

The original Git control objects/pins are not rewritten or re-signed to accept
factorlab. Candidate archive exports of three controller files have Windows
EOL differences and must not be represented as the original trusted raw bytes.
Their raw and exported hashes are separately bound in git-blob-archive-binding.json;
they do not establish an independently authorized FactorLab controller. Historical
objects remain in the original repository, while the candidate has an unrelated
local Git root. Missing historical objects/receipts and changed executable paths
therefore cannot pass the old independent protocol. No self-authored PASS field
or new digest grants the missing authority.

New identity protocol FACTORLAB_NAMESPACE_MIGRATION_V1 records baseline, actual
package Python bytes, resource YAML and software version. Source checkout Git
identity is distinct from installed-wheel identity (Git=null) and user-project
Git. Source-only readiness remains strict: no Git checkout means NOT_RUN with
SOURCE_CHECKOUT_REQUIRED. Forward source roots must match the executing package.
Original serialization qbot-* protocol labels are retained as compatibility
formats; they are not Legacy imports or authorization.

The original data admission gate still verifies exact requested tables, dates,
PIT scope, status/listings, manifest pin, replayed source and all six stages:
RAW/ADJUSTMENT/STATUS/PIT/SOURCE_BINDING/FORMAL. It then additionally calls the
unconditionally refusing migration guard. A new adversarial test forges an
all-PASS loader result in memory and confirms the public gate still refuses
MIGRATION_TRUST_ROOT_UNREVIEWED. No issued receipt or source data is changed by
that test. Provider self-reports and synthetic recipes remain unqualified.

New counterexamples also reject a different source tree and changed input files
presented as synthetic. Installed clean-wheel smoke denies network connections,
imports every module and confirms no qbot namespace is available. The product
does not include broker gateways, credentials or order entry points.

Windows timestamp compatibility preserves device/file identity, size, mtime,
creation time, single-link/regular checks and before/after hashing. The CSMAR
attack suite passed227 cases. Security test successes establish software refusal
behavior only; the required independent trust-root migration is still BLOCKED.

Original tests are retained. Full candidate collection/execution is recorded
including failures. The independent CI's complete-upstream-contracts job is a
release requirement and intentionally has no continue-on-error escape. Runtime
domain CI does not count missing upstream history as passing financial review.

33 purely synthetic archive-verifier unit tests were recovered by copying one
unchanged baseline Git blob to a private test-reference location, not by
re-anchoring a financial controller. Its raw SHA256 is
b211d3202b6bf159e5ea585b37469b745584889523b477f13722f0ee834cdb99.
The fixture verifies and compiles exactly the same bytes; a mutation counterexample
refuses any mismatch. The original historical schema/protected-path names stay
inside that reference. Only disposable synthetic Git histories are verified;
PASS_RECORDED_GIT_OBJECTS_ONLY is never full migration replay or financial approval.
The unresolved documentation license scope remains and this reference is excluded
from wheel/sdist.75 historical publication cases still need the independent root.
