# License and publication audit

Status: BLOCKED_SCOPE_UNRESOLVED for public distribution of inherited docs.
Local engineering inspection and testing continue; no remote is configured.

Root LICENSE grants MIT and credits Copyright (c) 2022 UFund-Me. It is copied
unchanged. docs/LICENSE declares GPLv3. Upstream docs/README.md:744-748 also
states Creative Commons Attribution-NonCommercial-ShareAlike 4.0. Applicability
to inherited research docs cannot be resolved by choosing one declaration.
The candidate preserves docs/LICENSE and does not copy docs/research wholesale.
New migration documents describe this task and do not relicense old material.

No upstream data/results directory was extracted. Historical docs contain
real-derived security coverage and quantile summaries; absence of raw OHLC does
not establish redistribution permission. Those files are excluded unless
independently reviewed. Synthetic Golden fixtures remain byte-identical; both
their manifests explicitly identify synthetic-only data.

The extracted minute adapter is limited to qbot/engineering/yhh30m. Nested
third-party trading plugins and their licenses are outside the extracted code.
Preserving the MIT notice is necessary but is not a legal resolution of the
upstream documentation scope. No public repository, release or PyPI upload is
authorized by this task.

NO LIVE MONEY. PIT_UNVERIFIED. FORMAL_ADMISSION_BLOCKED.
