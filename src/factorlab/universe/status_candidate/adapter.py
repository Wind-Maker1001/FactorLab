"""Generic field-by-field reconciliation with explicit evidence and rejection."""
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import csv, json
from .model import (Artifact, NativeObservation, OfficialEvidence, Relationship, EvidenceError,
                    CertificationRejected, strict, security, day, digest)
from .ingest import (native_csv, read_json, sha256, read_verified,
                     source_bytes, csv_stream, hash_and_head)

ROLE_STATUS = {'IDENTITY': 'PARTIAL', 'STATUS': 'PARTIAL', 'PIT': 'PIT_UNVERIFIED',
               'SOURCE_BINDING': 'SOURCE_BINDING_INCOMPLETE', 'FORMAL': 'FORMAL_ADMISSION_BLOCKED'}

class Adapter:
    def __init__(self):
        raise EvidenceError('USE_PINNED_BUNDLE_LOADER')

    @classmethod
    def load(cls, bundle_path: Path, expected_sha256: str, *, allow_synthetic: bool = False):
        path = Path(bundle_path).resolve(strict=True)
        snapshot = read_verified(path, expected_sha256, 'BUNDLE_SHA_MISMATCH')
        data = read_json(snapshot)
        if set(data) != {'schema', 'scope', 'artifacts', 'native_sources', 'official', 'relationships', 'calendar_source_id', 'forbidden_output_roots'}:
            raise EvidenceError('UNEXPECTED_BUNDLE_FIELDS')
        if data['schema'] != 'fl-status-candidate/v1':
            raise EvidenceError('UNSUPPORTED_BUNDLE_SCHEMA')
        if data['scope'] not in {'REAL_FROZEN_CANDIDATE', 'SYNTHETIC_TEST'} or (data['scope'] == 'SYNTHETIC_TEST' and not allow_synthetic):
            raise EvidenceError('SYNTHETIC_INPUT_NOT_REAL_ADMISSION')
        obj = object.__new__(cls)
        obj.metrics = {'source_hash_checks': 0, 'source_bytes_hashed': 0}
        obj.bundle_sha256 = expected_sha256
        obj.bundle_path = path
        artifacts = [strict(Artifact, row) for row in data['artifacts']]
        obj.artifacts = {a.source_id: a for a in artifacts}
        if len(obj.artifacts) != len(artifacts):
            raise EvidenceError('DUPLICATE_SOURCE_ID')
        obj.verify_sources()
        ca = obj.artifacts.get(data['calendar_source_id'])
        if ca is None or ca.role != 'CALENDAR':
            raise EvidenceError('CALENDAR_BINDING_REQUIRED')
        with csv_stream(source_bytes(ca)) as stream:
            calendar = [day(row['date']) for row in csv.DictReader(stream)]
        if len(set(calendar)) != len(calendar):
            raise EvidenceError('DUPLICATE_CALENDAR_DATE')
        obj.calendar = frozenset(calendar)
        native = []
        for item in data['native_sources']:
            if set(item) != {'source_id', 'layout'}:
                raise EvidenceError('INVALID_NATIVE_SOURCE_CONTRACT')
            artifact = obj.artifacts.get(item['source_id'])
            if artifact is None or artifact.role != 'NATIVE_CANDIDATE':
                raise EvidenceError('NATIVE_SOURCE_ROLE_MISMATCH')
            if item['layout'] == 'CANONICAL_JSON':
                if data['scope'] != 'SYNTHETIC_TEST' or not allow_synthetic:
                    raise EvidenceError('SYNTHETIC_LAYOUT_NOT_REAL_CSMAR')
                native.extend(strict(NativeObservation, x) for x in read_json(source_bytes(artifact)))
            else:
                native.extend(native_csv(artifact, item['layout']))
        obj.native = {}
        lineage = {a.sha256 for a in artifacts if a.role == 'LINEAGE_ZIP'}
        for row in native:
            obj._require_sources(row.source_ids, 'NATIVE_CANDIDATE')
            key = (row.security, row.trading_date)
            if key in obj.native:
                raise EvidenceError('DUPLICATE_NATIVE_KEY')
            if row.trading_date not in obj.calendar:
                raise EvidenceError('NATIVE_DATE_OUTSIDE_BOUND_CALENDAR')
            if data['scope'] != 'SYNTHETIC_TEST' and (row.native_source_sha256 not in lineage or row.native_state_source_sha256 not in lineage):
                raise EvidenceError('NATIVE_ZIP_LINEAGE_UNBOUND')
            obj.native[key] = row
        obj.official = tuple(strict(OfficialEvidence, e) for e in data['official'])
        ids = [e.evidence_id for e in obj.official]
        if len(set(ids)) != len(ids):
            raise EvidenceError('DUPLICATE_EVIDENCE_ID')
        for e in obj.official:
            obj._require_sources(e.source_ids, 'OFFICIAL_DOCUMENT')
            if not set(e.declared_dates) <= obj.calendar:
                raise EvidenceError('OFFICIAL_DATE_NOT_RESEARCH_TRADING_DAY')
        obj.relationships = tuple(strict(Relationship, e) for e in data['relationships'])
        pairs = [(r.old_security, r.new_security) for r in obj.relationships]
        if len(set(pairs)) != len(pairs):
            raise EvidenceError('DUPLICATE_RELATIONSHIP_PAIR')
        for relation in obj.relationships:
            for source_id in relation.source_ids:
                a = obj.artifacts.get(source_id)
                if a is None or a.role not in {'OFFICIAL_DOCUMENT', 'RELATIONSHIP_REVIEW'}:
                    raise EvidenceError('RELATIONSHIP_SOURCE_UNBOUND')
        if not isinstance(data['forbidden_output_roots'], list) or not all(isinstance(v, str) for v in data['forbidden_output_roots']):
            raise EvidenceError('INVALID_OUTPUT_BOUNDARY')
        obj.forbidden_output_roots = tuple(Path(p).resolve() for p in data['forbidden_output_roots'])
        obj.verify_sources()
        if sha256(path) != expected_sha256:
            raise EvidenceError('BUNDLE_CHANGED_DURING_LOAD')
        return obj

    def _require_sources(self, ids: Tuple[str, ...], role: str) -> None:
        for source_id in ids:
            if source_id not in self.artifacts or self.artifacts[source_id].role != role:
                raise EvidenceError('SOURCE_ROLE_MISMATCH')

    def verify_sources(self) -> None:
        for a in self.artifacts.values():
            path = Path(a.path)
            if path.is_file():
                self.metrics['source_hash_checks'] += 1
                self.metrics['source_bytes_hashed'] += path.stat().st_size
            if not path.is_file():
                raise EvidenceError('SOURCE_SHA_MISMATCH:' + a.source_id)
            actual_sha256, head = hash_and_head(path)
            if actual_sha256 != a.sha256:
                raise EvidenceError('SOURCE_SHA_MISMATCH:' + a.source_id)
            if a.media_type == 'PDF' and not head.startswith(b'%PDF'):
                raise EvidenceError('NOT_ORIGINAL_PDF:' + a.source_id)
            if a.media_type == 'ZIP' and not head.startswith(b'PK'):
                raise EvidenceError('NOT_ZIP:' + a.source_id)
            if a.media_type == 'HTML' and b'<' not in head:
                raise EvidenceError('NOT_HTML:' + a.source_id)

    def evaluate(self, symbol: str, trading_date: str) -> Dict[str, Any]:
        self.verify_sources()
        result = self._evaluate(symbol, trading_date)
        self.verify_sources()
        return result

    def _evaluate(self, symbol: str, trading_date: str) -> Dict[str, Any]:
        key = (security(symbol), day(trading_date))
        if trading_date not in self.calendar:
            raise EvidenceError('NON_TRADING_DATE_OR_OUTSIDE_CALENDAR')
        native = self.native.get(key)
        relevant = [e for e in self.official if e.security == symbol and trading_date in e.declared_dates
                    and e.effective_from <= trading_date <= e.effective_to and e.implemented]
        candidates = {'LISTING': [], 'ST_RISK': [], 'SUSPENSION': [], 'HAS_TRADES': []}
        if native is not None and not native.native_missing:
            if native.native_state_code in {0, 1}:
                candidates['SUSPENSION'].append('NOT_SUSPENDED' if native.native_state_code == 0 else 'SUSPENDED')
            if native.native_volume is not None:
                candidates['HAS_TRADES'].append('TRUE' if Decimal(native.native_volume) > 0 else 'FALSE')
        for evidence in relevant:
            candidates[evidence.field].append(evidence.value)
        result = {}
        conflicts = []
        for field, values in candidates.items():
            unique = set(values)
            if len(unique) > 1:
                conflicts.append(field)
            result[field] = sorted(unique)[0] if len(unique) == 1 else 'UNKNOWN'
        # Filling and the exchange calendar are distinct from the risk category.
        if native is not None and native.native_filling == 2:
            conflicts.append('HOLIDAY_FILLING_ON_BOUND_TRADING_DATE')
        if result['SUSPENSION'] == 'SUSPENDED' and result['HAS_TRADES'] == 'TRUE':
            conflicts.append('ALL_DAY_SUSPENSION_WITH_NATIVE_TRADES')
        eligibility = 'FALSE' if result['LISTING'] in {'DELISTED', 'NOT_LISTED'} or result['SUSPENSION'] == 'SUSPENDED' else 'UNKNOWN'
        quality = 'SOURCE_CONFLICT' if conflicts else ('VERIFIED_FOR_DECLARED_OFFICIAL_FIELDS' if relevant else 'NATIVE_CANDIDATE_OR_MISSING')
        refs = []
        for evidence in sorted(relevant, key=lambda e: e.evidence_id):
            refs.append({**asdict(evidence), 'official_sources': [asdict(self.artifacts[sid]) for sid in evidence.source_ids]})
        return {'security': symbol, 'trading_date': trading_date, 'native_observation': asdict(native) if native else None,
                'native_missing': native.native_missing if native else None,
                'native_missing_reason': native.native_missing_reason if native else 'NO_BOUND_NATIVE_CANDIDATE_FOR_KEY',
                'official_evidence': refs, 'membership_candidate': native.membership_candidate if native else None,
                'dictionary_binding': 'UNVERIFIED', 'listing_status': result['LISTING'], 'st_risk_status': result['ST_RISK'],
                'suspension_status': result['SUSPENSION'], 'has_trades': result['HAS_TRADES'],
                'trading_eligibility_candidate': eligibility, 'execution_eligible': 'UNKNOWN',
                'status_evidence_quality': quality, 'source_conflict_status': 'SOURCE_CONFLICT' if conflicts else 'NO_OBSERVED_FIELD_CONFLICT',
                'conflict_fields': sorted(set(conflicts)), 'historical_known_at': 'UNKNOWN', 'pit_status': 'PIT_UNVERIFIED',
                'role_statuses': dict(ROLE_STATUS), 'candidate_scope': 'FIELD_FACTS_ONLY_NOT_FORMAL_QUALIFICATION'}

    def evaluate_all(self) -> List[Dict[str, Any]]:
        self.verify_sources()
        output = [self._evaluate(*key) for key in sorted(self.native)]
        self.verify_sources()
        return output

    def require_relationship(self, old: str, new: str, relation_type: str, issuer_continues: bool) -> str:
        self.verify_sources()
        for r in self.relationships:
            if (r.old_security, r.new_security) == (security(old), security(new)):
                if (r.relation_type, r.issuer_continues) == (relation_type, issuer_continues):
                    return 'CANDIDATE_RELATIONSHIP_FACT_ONLY_NO_PRICE_OR_HOLDING_SPLICE'
                raise CertificationRejected('RELATIONSHIP_KIND_OR_ISSUER_MISMATCH')
        raise CertificationRejected('NO_BOUND_RELATIONSHIP_EVIDENCE')

def require_claim(row: Dict[str, Any], field: str, value: Any, *, adapter: Adapter, role: str = 'CANDIDATE') -> str:
    if role != 'CANDIDATE' or field in {'PIT', 'FORWARD', 'FORMAL', 'execution_eligible'}:
        raise CertificationRejected('FORMAL_OR_EXECUTION_QUALIFICATION_FORBIDDEN')
    adapter.verify_sources()
    actual = adapter._evaluate(row.get('security'), row.get('trading_date'))
    if json.dumps(actual, sort_keys=True) != json.dumps(row, sort_keys=True):
        raise CertificationRejected('OUTPUT_CANDIDATE_TAMPERED')
    if field not in {'listing_status', 'st_risk_status', 'suspension_status', 'has_trades', 'trading_eligibility_candidate'}:
        raise CertificationRejected('UNSUPPORTED_CLAIM_FIELD')
    if row.get('source_conflict_status') == 'SOURCE_CONFLICT':
        raise CertificationRejected('SOURCE_CONFLICT')
    if row.get(field) == 'UNKNOWN' or row.get(field) != value:
        raise CertificationRejected('UNKNOWN_OR_UNSUPPORTED_FIELD_CLAIM')
    return 'CANDIDATE_FIELD_FACT_ONLY'
