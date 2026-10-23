"""Strict review of extracted CSI500 PDF page text, never PIT publication.

The observed layout has REMOVE on the left and ADD on the right, with paired
rows continuing across pages. Extraction is deliberately outside the research
core: this module imports no PDF reader, performs no HTTP and assigns no first
effective trading date. Visual review is still required for source evidence.
"""

from dataclasses import dataclass, replace
import re
from typing import Any, Dict, Sequence, Tuple

from qbot.research.data.schema import normalize_symbol
from qbot.research.errors import ProviderError
from qbot.research.universe.notice_review import NoticeReview, review_csi500_notice


PDF_TABLE_CONTRACT = "csindex-csi500-paired-page-text-v1"
_TARGET = "中证500指数样本调整名单"
_DIRECTION = "调出名单调入名单"
_COLUMNS = frozenset(("证券代码证券名称证券代码证券名称", "股票代码股票名称股票代码股票名称"))
_PAIR = re.compile(r"([0-9]{6})\s+(.+?)\s+([0-9]{6})\s+(.+)")
_CODE_IN_NAME = re.compile(r"(?<![0-9])[0-9]{6}(?![0-9])")


def _heading(value: str) -> str:
    return re.sub(r"\s+", "", value).rstrip("：:")


@dataclass(frozen=True)
class PdfNoticeReview:
    review: NoticeReview
    heading_page: int
    heading_line: int
    termination_page: int
    termination_line: int
    termination_heading: str
    pair_counts_by_page: Tuple[Tuple[int, int], ...]
    contract: str = PDF_TABLE_CONTRACT
    visual_review_performed_by_parser: bool = False


def review_csi500_pdf_pages(notice: Dict[str, Any], pages: Sequence[str]) -> PdfNoticeReview:
    """Review full page strings in order; reject ambiguous layouts and counts.

    Names retain their extracted spelling, including spaces and ST markers.
    A next-index or reserve heading ends the table before any of its rows.
    Source page/line coordinates are one-based in each original page string.
    Missing codes (including ``-``) are errors, never guessed from names.
    """
    if not isinstance(pages, (list, tuple)) or not 1 <= len(pages) <= 100:
        raise ProviderError("PDF review requires between 1 and 100 ordered full page strings")
    if any(not isinstance(page, str) for page in pages):
        raise ProviderError("PDF page text must be a string")
    if sum(len(page.encode("utf-8")) for page in pages) > 2 * 1024 * 1024:
        raise ProviderError("PDF page text exceeds the bounded review size")
    base = review_csi500_notice(notice)
    if base.stated_change_count is None:
        raise ProviderError("PDF table requires an explicit stated CSI500 change count")
    headings = [(page_number, line_number) for page_number, page in enumerate(pages, 1)
                for line_number, line in enumerate(page.splitlines(), 1) if _heading(line) == _TARGET]
    if len(headings) != 1:
        raise ProviderError("PDF requires exactly one explicit CSI500 sample-change heading")
    heading_page, heading_line = headings[0]
    changes = []
    counts: Dict[int, int] = {}
    state = "seeking"
    termination = None
    for page_number, page in enumerate(pages, 1):
        if state in ("direction", "columns", "rows") and not page.strip():
            raise ProviderError("empty extracted page within CSI500 table requires visual review")
        nonempty_seen = False
        for line_number, original in enumerate(page.splitlines(), 1):
            line = original.strip()
            if not line:
                continue
            first_nonempty = not nonempty_seen
            nonempty_seen = True
            compact = _heading(line)
            if state == "seeking":
                if (page_number, line_number) == headings[0]:
                    state = "direction"
                continue
            if state == "finished":
                continue
            if re.fullmatch(r".+指数(?:样本调整|备选)名单", compact) or compact == "备选名单":
                if state != "rows" or not changes:
                    raise ProviderError("CSI500 PDF table ended before complete header/data rows")
                termination = (page_number, line_number, line)
                state = "finished"
                continue
            if state == "direction":
                if compact != _DIRECTION:
                    raise ProviderError("unrecognized PDF direction/header; no assumed column order")
                state = "columns"
                continue
            if state == "columns":
                if compact not in _COLUMNS:
                    raise ProviderError("unrecognized PDF security column header")
                state = "rows"
                continue
            # A repeated header is supported only at the beginning of a new
            # page, and both header lines must again confirm the column order.
            if compact == _DIRECTION and first_nonempty and page_number > heading_page:
                state = "columns"
                continue
            pair = _PAIR.fullmatch(line)
            if pair is None:
                raise ProviderError("unrecognized CSI500 PDF row at page %d line %d" % (page_number, line_number))
            remove_code, remove_name, add_code, add_name = pair.groups()
            for action, code, name in (("REMOVE", remove_code, remove_name), ("ADD", add_code, add_name)):
                if name.strip() == "-" or _CODE_IN_NAME.search(name):
                    raise ProviderError("ambiguous or missing CSI500 PDF security name")
                changes.append({"action": action, "symbol": normalize_symbol(code), "name": name,
                                "source_page": page_number, "source_line": line_number})
            counts[page_number] = counts.get(page_number, 0) + 1
            if len(changes) > 2 * base.stated_change_count:
                raise ProviderError("CSI500 PDF table exceeds explicit stated change count")
    if state not in ("rows", "finished") or not changes:
        raise ProviderError("incomplete CSI500 PDF table")
    groups = {action: [row["symbol"] for row in changes if row["action"] == action]
              for action in ("REMOVE", "ADD")}
    if any(len(values) != len(set(values)) for values in groups.values()) or set(groups["ADD"]) & set(groups["REMOVE"]):
        raise ProviderError("duplicate or overlapping CSI500 PDF change symbols")
    if any(len(values) != base.stated_change_count for values in groups.values()):
        raise ProviderError("CSI500 PDF table does not match explicit stated change count")
    if base.changes:
        def identities(rows):
            return {(row["action"], row["symbol"], re.sub(r"\s+", "", row["name"])) for row in rows}
        if identities(base.changes) != identities(changes):
            raise ProviderError("CSI500 PDF and inline HTML change identities disagree")
    status = ("PARSED_SOURCE_CANDIDATE_NOT_PIT" if
              bool(base.explicit_after_close_date) != bool(base.explicit_start_date) and
              not base.conditional_effective_clause else "EFFECTIVE_DATE_REVIEW_REQUIRED")
    if termination is None:
        termination = (len(pages), len(pages[-1].splitlines()) + 1, "END_OF_DOCUMENT")
    return PdfNoticeReview(replace(base, changes=tuple(changes), status=status), heading_page, heading_line,
                           *termination, tuple(sorted(counts.items())))
