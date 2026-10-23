"""Strict review of already extracted publisher workbook values, never PIT.

No spreadsheet SDK is imported. Extraction/source integrity must be verified by
the caller; supported typed cells are interpreted without padding identifiers,
filling merged cells or assigning an effective market session.
"""

from dataclasses import dataclass, replace
import re
from typing import Any, Dict, Sequence, Tuple

from factorlab.data.schema import normalize_symbol
from factorlab.errors import ProviderError
from factorlab.universe.notice_review import NoticeReview, mentions_csi500, review_csi500_notice


WORKBOOK_TABLE_CONTRACT = "csindex-csi500-paired-typed-values-v1"
_HEADERS = {
    6: (["指数代码", "指数简称", "调出", None, "调入", None],
        [None, None, "证券代码", "证券简称", "证券代码", "证券简称"]),
    10: (["指数代码", "指数简称", "调出", None, None, None, "调入", None, None, None],
         [None, None] + ["证券代码", "证券简称"] * 4),
}


@dataclass(frozen=True)
class WorkbookNoticeReview:
    review: NoticeReview
    worksheets: Tuple[Dict[str, Any], ...]
    body_mentions_csi500: bool
    explicit_empty_target_slots: Tuple[Dict[str, Any], ...]
    contract: str = WORKBOOK_TABLE_CONTRACT
    extraction_completeness_proved_by_parser: bool = False


def review_csi500_workbook_values(notice: Dict[str, Any], sheets: Sequence[Dict[str, Any]]) -> WorkbookNoticeReview:
    """Interpret every supplied worksheet under two actually observed layouts.

    Unknown worksheets/headers/rows fail, including in non-target tables, so an
    unsupported layout cannot become negative CSI500 evidence. Only literal
    ``000905`` + ``中证500`` matches. ``-,-`` is an explicit empty security slot,
    not a guessed ticker. Missing/partially empty slots are rejected.
    """
    base = review_csi500_notice(notice)
    if not isinstance(sheets, (list, tuple)) or not 1 <= len(sheets) <= 20:
        raise ProviderError("workbook review requires 1 to 20 complete extracted worksheets")
    changes, summaries, empty = [], [], []
    names = set()
    total_rows = 0
    for sheet in sheets:
        if not isinstance(sheet, dict):
            raise ProviderError("invalid extracted worksheet")
        name, values = sheet.get("sheet"), sheet.get("values")
        if name not in ("Sheet1", "指数样本调整名单") or name in names:
            raise ProviderError("unknown or duplicate worksheet; independent review required")
        names.add(name)
        if not isinstance(values, list) or not 3 <= len(values) <= 20000:
            raise ProviderError("complete supported worksheet rows are required")
        total_rows += len(values)
        if total_rows > 20000:
            raise ProviderError("workbook exceeds bounded row count")
        if not isinstance(values[0], list) or len(values[0]) not in _HEADERS:
            raise ProviderError("unrecognized workbook direction/header")
        width = len(values[0])
        first, second = _HEADERS[width]
        if values[0] != first or values[1] != second:
            raise ProviderError("unrecognized workbook direction/header; no assumed column order")
        target_rows = []
        indices = set()
        for row_number, row in enumerate(values[2:], 3):
            if not isinstance(row, list) or len(row) != width or any(
                    not isinstance(cell, str) or not cell.strip() or len(cell) > 200 for cell in row):
                raise ProviderError("missing, untyped or malformed workbook row at %s!%d" % (name, row_number))
            index, index_name = row[:2]
            if not re.fullmatch(r"[0-9A-Z]{6}", index):
                raise ProviderError("index identifiers must retain their literal six characters")
            if (index == "000905") != (index_name == "中证500"):
                raise ProviderError("CSI500 index code/name disagreement")
            indices.add(index)
            if index != "000905":
                continue
            target_rows.append(row_number)
            before = len(changes)
            slots = [("REMOVE", column) for column in range(2, 2 + (width - 2) // 2, 2)]
            slots += [("ADD", column) for column in range(2 + (width - 2) // 2, width, 2)]
            for action, column in slots:
                code, security_name = row[column:column + 2]
                cell_range = "%s%d:%s%d" % (chr(65 + column), row_number, chr(66 + column), row_number)
                location = {"source_sheet": name, "source_row": row_number, "source_range": cell_range}
                if (code, security_name) == ("-", "-"):
                    empty.append({"action": action, **location})
                    continue
                if not re.fullmatch(r"[0-9]{6}", code) or security_name == "-":
                    raise ProviderError("invalid or unresolved CSI500 security slot at %s!%s" % (name, cell_range))
                changes.append({"action": action, "symbol": normalize_symbol(code), "name": security_name, **location})
            if len(changes) == before:
                raise ProviderError("CSI500 target row has no explicit change events")
        summaries.append({"sheet": name, "header_range": "A1:%s2" % chr(64 + width),
                          "data_rows": len(values) - 2, "unique_index_codes": len(indices),
                          "csi500_source_rows": tuple(target_rows)})
    groups = {action: [row["symbol"] for row in changes if row["action"] == action] for action in ("REMOVE", "ADD")}
    if any(len(values) != len(set(values)) for values in groups.values()) or set(groups["ADD"]) & set(groups["REMOVE"]):
        raise ProviderError("duplicate or overlapping CSI500 workbook change symbols")
    if base.stated_change_count is not None and any(len(values) != base.stated_change_count for values in groups.values()):
        raise ProviderError("workbook does not match explicit stated CSI500 change count")
    if base.changes:
        def identities(rows):
            return {(row["action"], row["symbol"], re.sub(r"\s+", "", row["name"])) for row in rows}
        if identities(base.changes) != identities(changes):
            raise ProviderError("workbook and inline HTML change identities disagree")
    status = "NO_CSI500_ENTRIES_IN_SUPPORTED_TABLES"
    if changes:
        status = ("PARSED_SOURCE_CANDIDATE_NOT_PIT" if
                  bool(base.explicit_after_close_date) != bool(base.explicit_start_date) and
                  not base.conditional_effective_clause else "EFFECTIVE_DATE_REVIEW_REQUIRED")
    return WorkbookNoticeReview(replace(base, changes=tuple(changes), status=status), tuple(summaries),
                                mentions_csi500(notice["content"]), tuple(empty))
