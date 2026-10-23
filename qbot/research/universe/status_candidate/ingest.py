"""Read frozen candidate files; no ZIP extraction, network or production access."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional
import csv, hashlib, io, json
from .model import Artifact, NativeObservation, EvidenceError, strict, digest

@dataclass(frozen=True)
class VerifiedBytes:
    """Immutable bytes verified against a caller-supplied trusted SHA.

    The path is provenance only. Parsers never use it to reopen the file.
    __fspath__ preserves source-location tooling without a path parser fallback.
    """
    path: Path
    content: bytes
    expected_sha256: str
    mismatch: str

    def __post_init__(self) -> None:
        if type(self.content) is not bytes:
            raise EvidenceError('IMMUTABLE_BYTES_REQUIRED')
        if hashlib.sha256(self.content).hexdigest() != digest(self.expected_sha256):
            raise EvidenceError(self.mismatch)

    def __fspath__(self) -> str:
        return str(self.path)


def read_verified(path: Path, expected_sha256: str, mismatch: str) -> VerifiedBytes:
    # One read; __post_init__ verifies exactly this immutable parsing buffer.
    digest(expected_sha256)
    path = Path(path)
    return VerifiedBytes(path, path.read_bytes(), expected_sha256, mismatch)


def source_bytes(artifact: Artifact) -> VerifiedBytes:
    return read_verified(Path(artifact.path), artifact.sha256,
                         'SOURCE_SHA_MISMATCH:' + artifact.source_id)


def hash_and_head(path: Path):
    """Stream large unparsed artifacts; validate media from the same stream."""
    h = hashlib.sha256()
    head = b''
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            if not head:
                head = block[:100]
            h.update(block)
    return h.hexdigest(), head


def sha256(path: Path) -> str:
    return hash_and_head(path)[0]


def _require_snapshot(snapshot: VerifiedBytes) -> None:
    if not isinstance(snapshot, VerifiedBytes):
        raise EvidenceError('VERIFIED_BYTE_SNAPSHOT_REQUIRED')


def csv_stream(snapshot: VerifiedBytes):
    _require_snapshot(snapshot)
    return io.StringIO(snapshot.content.decode('utf-8-sig'), newline='')


def read_json(snapshot: VerifiedBytes):
    _require_snapshot(snapshot)
    def unique(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise EvidenceError('DUPLICATE_JSON_KEY')
            data[key] = value
        return data
    return json.loads(snapshot.content.decode('utf-8-sig'), object_pairs_hook=unique)


def optional_enum(value: str) -> Optional[int]:
    if value in ['', 'UNKNOWN', 'MISSING']:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise EvidenceError('INVALID_NATIVE_ENUM') from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise EvidenceError('NONINTEGER_NATIVE_ENUM')
    return int(number)

def boolean(value: str) -> Optional[bool]:
    if value.lower() == 'true':
        return True
    if value.lower() == 'false':
        return False
    if value in ['', 'UNKNOWN']:
        return None
    raise EvidenceError('INVALID_BOOLEAN_TEXT')

def native_csv(artifact: Artifact, layout: str) -> List[NativeObservation]:
    if layout not in {'TRDSTA_CONFLICT_V1', 'P0_GAPS_V1'}:
        raise EvidenceError('UNSUPPORTED_NATIVE_LAYOUT')
    output = []
    with csv_stream(source_bytes(artifact)) as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise EvidenceError('DUPLICATE_OR_EMPTY_CSV_HEADER')
        for line, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise EvidenceError('MALFORMED_CSV_ROW')
            if layout == 'TRDSTA_CONFLICT_V1':
                trdsta = optional_enum(row['native_Trdsta'])
                values = dict(native_trdsta=trdsta, native_trdsta_raw=row['native_Trdsta'], native_label='U' if trdsta == 10 else None,
                              native_state_code=optional_enum(row['native_StateCode']), native_filling=optional_enum(row['native_Filling']),
                              native_volume=row['raw_volume'], native_missing=False, native_missing_reason='OBSERVED_FROZEN_CANDIDATE_ROW',
                              native_source_coordinate=row['raw_workbook'] + ':row=' + row['raw_source_row'],
                              native_state_source_coordinate='adj_workbook:row=' + row['adj_source_row'],
                              membership_candidate=boolean(row['membership_candidate']), membership_source_coordinate=row['membership_candidate_source_row'])
            else:
                if row['native_raw_record'] != 'MISSING' or row['native_adj_state_record'] != 'MISSING':
                    raise EvidenceError('GAP_RECORD_IS_NOT_NATIVE_MISSING')
                values = dict(native_trdsta=None, native_trdsta_raw=None, native_label=None, native_state_code=None, native_filling=None,
                              native_volume=None, native_missing=True, native_missing_reason='FROZEN_SCOPE_NATIVE_KEY_ABSENT',
                              native_source_coordinate=row['raw_coordinate_bounds'], native_state_source_coordinate=row['adj_coordinate_bounds'],
                              membership_candidate=boolean(row['MEMBERSHIP_CANDIDATE']), membership_source_coordinate=row['membership_source_row'])
            output.append(NativeObservation(security=row['symbol'], trading_date=row['date'], native_source_sha256=row['raw_zip_sha256'],
                native_state_source_sha256=row['adj_zip_sha256'], record_source_id=artifact.source_id, record_coordinate='CSV_ROW:' + str(line),
                dictionary_binding='UNVERIFIED', source_ids=(artifact.source_id,), **values))
    return output
