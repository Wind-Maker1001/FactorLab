"""Field-scoped retrospective evidence; never a bar patch or execution permit."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from factorlab.data.evidence import date, fail, json_bytes, require_hash, sha256, timestamp


SCHEMA = "P9_D01_MARKET_EVIDENCE_V1"
POLICY = "LAST_OBSERVED_RAW_CLOSE_CARRY_ON_CONFIRMED_SUSPENSION"
SAFETY = ("NO LIVE MONEY", "PIT_UNVERIFIED", "HFQ_UNVERIFIED", "FORMAL_ADMISSION_BLOCKED")
SUSPENSION_DATES = ("2023-04-26", "2023-04-27", "2023-04-28", "2023-05-04")
ALLOWED = frozenset(
    [("002408.SZ", d, f) for d in SUSPENSION_DATES
     for f in ("suspension_confirmed", "valuation_mark")]
    + [("000540.SZ", "2023-05-08", f)
       for f in ("is_st", "trading_observed", "execution_price_available")]
    + [("000540.SZ", "2023-05-04", "valuation_mark")]
)
OBSERVATION_DATES = {"002408.SZ": "2023-04-25", "000540.SZ": "2023-04-28"}


def _same(left, right):
    # JSON identities distinguish null, zero and false.
    return json_bytes(left) == json_bytes(right)


def _positive(value):
    if isinstance(value, bool) or value is None:
        fail("positive observed price required")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        fail("invalid observed price: " + str(exc))
    if not result.is_finite() or result <= 0:
        fail("positive observed price required")
    return result


@dataclass(frozen=True)
class CandidateFact:
    security: str
    day: str
    field: str
    original_value: object
    candidate_value: object
    evidence_class: str
    document_sha256: str
    publication_time: str
    known_at_status: str
    verification_status: str
    consumption_scope: str
    limitation: str
    suspension_document_sha256: str | None = None


@dataclass(frozen=True)
class Valuation:
    security: str
    day: str
    raw_close: None
    valuation_price: Decimal
    shares: int
    valuation_source_date: str
    valuation_source_price_sha256: str
    suspension_document_sha256: str
    valuation_policy: str = POLICY
    valuation_policy_version: str = "1"
    valuation_status: str = "MODEL_VALUATION_ASSUMPTION"
    nontradable: bool = True

    @property
    def book_value(self):
        return self.valuation_price * self.shares


@dataclass(frozen=True)
class MarketEvidence:
    """Validated candidates, separate from immutable source observations.

    No execution authorization API is provided. SIGNAL_STATE consumption only
    exposes the targeted issuer-designated ST fact, never a trading permission.
    """
    facts: tuple[CandidateFact, ...]
    source_manifest_sha256: str
    safety: tuple[str, ...] = SAFETY

    def fact(self, security, day, field, *, purpose="AUDIT", cutoff=None):
        key = (security, day, field)
        if key not in ALLOWED:
            fail("candidate outside exact security/date/field scope")
        matches = [f for f in self.facts if (f.security, f.day, f.field) == key]
        if len(matches) != 1:
            fail("exactly one candidate required")
        fact = matches[0]
        if purpose == "AUDIT":
            return fact
        if (purpose != "SIGNAL_STATE" or field != "is_st"
                or fact.known_at_status != "SIGNAL_TIME_STATE_KNOWN"
                or cutoff is None or timestamp(fact.publication_time) > timestamp(cutoff)):
            fail("evidence cannot authorize requested consumption")
        return fact

    def carry(self, security, day, *, shares, raw_close, previous_observation):
        """Value existing shares only; raw fields and execution inputs are untouched."""
        mark = self.fact(security, day, "valuation_mark")
        if security == "000540.SZ":
            suspension_identity = mark.suspension_document_sha256
            if suspension_identity is None:
                fail("independent confirmed suspension required")
        else:
            suspension = self.fact(security, day, "suspension_confirmed")
            if (suspension.candidate_value is not True
                    or suspension.evidence_class != "ISSUER_CONFIRMED_SUSPENSION"
                    or suspension.verification_status != "DOCUMENT_CONFIRMED"):
                fail("independent confirmed suspension required")
            suspension_identity = suspension.document_sha256
        if raw_close is not None or mark.evidence_class != "MODEL_VALUATION_ASSUMPTION":
            fail("independent confirmed suspension and missing raw close required")
        if isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0:
            fail("positive existing holding quantity required")
        p = previous_observation
        if p is None:
            fail("verified pre-suspension raw close observation required")
        if (p["security"] != security or p["date"] != OBSERVATION_DATES[security]
                or date(p["date"]) >= date(day) or p["status"] != "OBSERVED_RAW_CLOSE"
                or p["field"] != "raw_close" or p["volume"] <= 0):
            fail("verified pre-suspension raw close observation required")
        require_hash(p["source_sha256"])
        # The observation identity is bound into the candidate during loading.
        if sha256(json_bytes(p)) != mark.document_sha256:
            fail("carry observation identity differs")
        price = _positive(p["price"])
        if price != _positive(mark.candidate_value):
            fail("carry price differs from observed source")
        return Valuation(security, day, None, price, shares, p["date"],
                         p["source_sha256"], suspension_identity)


def load_sidecar(path, *, source_manifest, originals, documents):
    """Validate bytes, exact original field values and every referenced document.

    ``originals`` maps (security, date, field) to the unmodified source value.
    ``documents`` maps SHA-256 to bytes, including the canonical observation
    capsule used for carry. The caller supplies the actual source manifest bytes.
    Hash binding detects drift; publication/semantic claims require source review.
    """
    payload = json.loads(Path(path).read_bytes())
    digest = sha256(source_manifest)
    if (payload["schema"] != SCHEMA or payload["version"] != 1
            or payload["source_manifest_sha256"] != digest
            or payload["safety"] != list(SAFETY)):
        fail("sidecar schema/version/manifest/safety differs")
    facts, seen = [], set()
    for row in payload["candidates"]:
        key = (row["target_security"], row["target_date"], row["field"])
        date(key[1])
        if key not in ALLOWED or key in seen:
            fail("out-of-scope or conflicting candidate")
        seen.add(key)
        if key not in originals or not _same(originals[key], row["original_value"]):
            fail("original field value differs")
        require_hash(row["source_document_sha256"])
        document = row["source_document_sha256"]
        if document not in documents or sha256(documents[document]) != document:
            fail("missing or changed source document")
        timestamp(row["source_publication_time"])
        timestamp(row["retrospectively_verified_at"])
        if row["consumption_scope"] != "RESEARCH_CANDIDATE_ONLY" or not row["source_uri"] or not row["limitation"]:
            fail("explicit candidate-only scope and provenance required")
        value, evidence = row["candidate_value"], row["evidence_class"]
        suspension_identity = None
        if key == ("000540.SZ", "2023-05-04", "valuation_mark"):
            suspension = row["suspension_evidence"]
            suspension_identity = require_hash(suspension["document_sha256"])
            if (suspension_identity not in documents
                    or sha256(documents[suspension_identity]) != suspension_identity
                    or suspension["security"] != key[0]
                    or suspension["start_date"] != key[1]
                    or suspension["resume_date"] != "2023-05-05"
                    or suspension["evidence_class"] != "ISSUER_CONFIRMED_SUSPENSION"
                    or suspension["verification_status"] != "DOCUMENT_CONFIRMED"
                    or timestamp(suspension["publication_time"]) > timestamp(key[1])):
                fail("independent exact suspension document required")
        if key[2] == "valuation_mark":
            if (evidence != "MODEL_VALUATION_ASSUMPTION" or row["valuation_policy"] != POLICY
                    or row["valuation_policy_version"] != "1" or row["valuation_status"] != evidence
                    or row["nontradable"] is not True or row["original_value"] is not None
                    or row["valuation_source_date"] != OBSERVATION_DATES[key[0]]
                    or _positive(row["valuation_source_price"]) != _positive(value)):
                fail("invalid separated carry valuation policy")
            observation = json.loads(documents[document])
            if (observation["security"] != key[0] or observation["field"] != "raw_close"
                    or observation["status"] != "OBSERVED_RAW_CLOSE"
                    or observation["date"] != row["valuation_source_date"]
                    or observation["source_sha256"] != row["valuation_source_price_identity"]
                    or _positive(observation["price"]) != _positive(value)
                    or observation["volume"] <= 0):
                fail("valuation observation identity conflicts")
            source_hash = require_hash(observation["source_sha256"])
            if source_hash not in documents or sha256(documents[source_hash]) != source_hash:
                fail("original observed price source required")
        elif key[2] == "suspension_confirmed":
            if (value is not True or evidence != "ISSUER_CONFIRMED_SUSPENSION"
                    or row["verification_status"] != "DOCUMENT_CONFIRMED"):
                fail("independent suspension confirmation required")
        elif key[2] == "is_st":
            if (value is not True or evidence != "ISSUER_RISK_DESIGNATION"
                    or row["verification_status"] != "DOCUMENT_CONFIRMED"):
                fail("targeted issuer ST designation required; no enum-wide mapping")
        elif (value is not True or evidence != {
                "trading_observed": "EX_POST_TRADING_CONFIRMED",
                "execution_price_available": "EXECUTION_PRICE_AVAILABLE"}[key[2]]):
            fail("unsupported audit claim")
        known = row["known_at_status"]
        if known not in {"UNKNOWN", "EX_POST_ONLY", "SIGNAL_TIME_STATE_KNOWN"}:
            fail("invalid knowledge status")
        if known == "SIGNAL_TIME_STATE_KNOWN":
            if key[2] != "is_st" or timestamp(row["source_publication_time"]) > timestamp("2023-05-05T15:00:00+08:00"):
                fail("ex-post evidence cannot masquerade as signal-time knowledge")
        facts.append(CandidateFact(*key, row["original_value"], value, evidence, document,
                                   row["source_publication_time"], known, row["verification_status"],
                                   row["consumption_scope"], row["limitation"], suspension_identity))
    return MarketEvidence(tuple(facts), digest)
