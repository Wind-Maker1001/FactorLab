# License scope of the software integration

The existing MIT LICENSE and NOTICE are retained byte-for-byte. The historical
Qbot documentation has differing GPL/CC statements; those materials and their
licenses are preserved in the existing public baseline. This integration adds
no copied upstream documentation, vendor implementation, dictionaries,
announcement copies or financial data. Existing notices are not removed or
reassigned to resolve ambiguity.

The added implementation and tests come from this project's local development
chain. All twenty transferred source/test/configuration files are mapped to
their exact original Git blobs in the local audit. The review found imports
of separately installed packages and reuse of this project's existing factor,
model and accounting modules, with no embedded external implementation.
As with any scoped provenance review, this is not a guarantee of originality
against every public implementation.

The only added direct optional dependency is exchange-calendars 4.13.2. Its
installed distribution's complete LICENSE is Apache-2.0, retaining Quantopian
and contributor attribution. Its license-file SHA-256 is
`e35c4578f7db4498e62e278978f6c42e0fce6f21f9993a060b08a33549cd3044`.
The dependency is installed separately; its source is not copied or bundled
inside FactorLab's wheel/sdist. Existing packages retain their own licenses.
No dependency's license is replaced by FactorLab's MIT declaration.

The existing release-license decision applied to older named artifacts and
does not automatically authorize new software or research data. This review
is limited to the reviewed software delta. The new build's module and legal
members are checked independently. The original research-data/history chain
remains blocked for publication until separate permissions are established.

No concrete software-delta license conflict was identified within this scope.
Financial/data redistribution permissions and financial authorization remain
separate. No Release, PyPI upload or main merge is authorized by this document.
