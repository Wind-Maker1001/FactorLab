"""Candidate review of an observed CSI notice layout, never PIT publication.

An explicit after-close date is a source statement, not an inferred first market
session. A correctly parsed change table still does not establish a complete
starting stock pool, source coverage, code history, or historical availability.
"""

from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
import re
from typing import Any, Dict, List, Optional, Tuple

from factorlab.data.schema import normalize_symbol
from factorlab.errors import ProviderError


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


class _TextReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if not self.hidden and tag in ("p", "div", "br", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in ("p", "div", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def visible_html_text(content: str) -> str:
    """Join inline text nodes, preserve block boundaries, ignore scripts/styles."""
    if not isinstance(content, str):
        raise ProviderError("notice HTML content must be a string")
    if len(content.encode("utf-8")) > 2 * 1024 * 1024:
        raise ProviderError("notice HTML content exceeds the bounded review size")
    parser = _TextReader()
    parser.feed(content)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def mentions_csi500(content: str) -> bool:
    """Discovery only: a mention can also name a CSI500 sub-index."""
    return "中证500" in _compact(visible_html_text(content))


class _TableReader(HTMLParser):
    """Keep the preceding block heading and explicit table cells, including spans."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.context: List[str] = []
        self.tables: List[Tuple[str, List[List[str]]]] = []
        self.table: Optional[List[List[str]]] = None
        self.row: Optional[List[str]] = None
        self.cell: Optional[List[str]] = None
        self.span = 1
        self.heading = ""
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if self.hidden:
            return
        if tag == "table":
            if self.table is not None:
                raise ProviderError("nested notice tables are unsupported")
            # Context is already decoded visible text, not HTML. Reparsing it
            # would lose literal angle brackets and whitespace-only context can
            # have no lines at all. Keep the last nonempty preceding block.
            lines = [line.strip() for line in "".join(self.context).splitlines() if line.strip()]
            self.heading = lines[-1] if lines else ""
            self.context = []
            self.table = []
        elif tag == "tr" and self.table is not None:
            if self.row is not None:
                raise ProviderError("unclosed notice table row")
            self.row = []
        elif tag in ("td", "th") and self.table is not None:
            if self.row is None or self.cell is not None:
                raise ProviderError("invalid notice table cell structure")
            values = dict(attrs)
            if values.get("rowspan", "1") != "1":
                raise ProviderError("row-spanned notice tables are unsupported")
            span = values.get("colspan", "1")
            if not isinstance(span, str) or span not in ("1", "2", "3", "4", "5", "6"):
                raise ProviderError("unsupported notice cell colspan")
            self.span = int(span)
            self.cell = []
        elif tag in ("p", "div", "br", "h1", "h2", "h3") and self.table is None:
            self.context.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag in ("td", "th") and self.cell is not None:
            value = "".join(self.cell).strip()
            self.row.extend([value] + [""] * (self.span - 1))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.cell is not None:
                raise ProviderError("unclosed notice table cell")
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            if self.row is not None:
                raise ProviderError("unclosed notice table row")
            self.tables.append((self.heading, self.table))
            self.table = None
        elif tag in ("p", "div", "h1", "h2", "h3") and self.table is None:
            self.context.append("\n")

    def handle_data(self, data):
        if self.hidden:
            return
        if self.cell is not None:
            self.cell.append(data)
        elif self.table is None:
            self.context.append(data)


@dataclass(frozen=True)
class NoticeReview:
    notice_id: int
    publish_date: str
    explicit_after_close_date: Optional[str]
    stated_change_count: Optional[int]
    status: str
    changes: Tuple[Dict[str, Any], ...]
    point_in_time_universe_verified: bool = False
    explicit_start_date: Optional[str] = None
    conditional_effective_clause: Optional[str] = None


def review_csi500_notice(notice: Dict[str, Any]) -> NoticeReview:
    """Parse only the observed, explicitly headed CSI500 change-table contract."""
    if not isinstance(notice, dict) or type(notice.get("id")) is not int or notice["id"] < 1:
        raise ProviderError("notice review requires a positive integer ID")
    publication = notice.get("publishDate")
    if not isinstance(publication, str):
        raise ProviderError("notice review requires an explicit publication date")
    try:
        published = date.fromisoformat(publication)
    except ValueError as exc:
        raise ProviderError("invalid notice publication date") from exc
    if published.isoformat() != publication:
        raise ProviderError("invalid notice publication date")
    content = notice.get("content")
    text = _compact(visible_html_text(content))
    explicit = re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日收(?:市|盘)后", text)
    dates = set()
    for year, month, day in explicit:
        try:
            dates.add(date(int(year), int(month), int(day)))
        except ValueError as exc:
            raise ProviderError("invalid explicit after-close source date") from exc
    if len(dates) > 1:
        raise ProviderError("multiple explicit after-close dates require independent review")
    effective = next(iter(dates)) if dates else None
    if effective and effective < published:
        raise ProviderError("source after-close date precedes publication; independent review required")
    starts = set()
    for year, month, day in re.findall(r"自(\d{4})年(\d{1,2})月(\d{1,2})日起", text):
        try:
            starts.add(date(int(year), int(month), int(day)))
        except ValueError as exc:
            raise ProviderError("invalid explicit start source date") from exc
    if len(starts) > 1:
        raise ProviderError("multiple explicit start dates require independent review")
    start = next(iter(starts)) if starts else None
    if start and start < published:
        raise ProviderError("source start date precedes publication; independent review required")
    clauses = {match.group(0) for match in re.finditer(r"自([^，。；]*?)日起", text)
               if not re.fullmatch(r"自\d{4}年\d{1,2}月\d{1,2}日起", match.group(0))}
    conditional = "；".join(sorted(clauses)) if clauses else None
    numbers = set(re.findall(r"中证500指数更换(\d+)只", text))
    if len(numbers) > 1:
        raise ProviderError("conflicting CSI500 stated change counts")
    count = int(next(iter(numbers))) if numbers else None
    if count is not None and not 1 <= count <= 500:
        raise ProviderError("CSI500 stated change count must be between 1 and 500")
    common = {"notice_id": notice["id"], "publish_date": publication,
              "explicit_after_close_date": effective.isoformat() if effective else None,
              "stated_change_count": count, "explicit_start_date": start.isoformat() if start else None,
              "conditional_effective_clause": conditional}
    reader = _TableReader()
    reader.feed(content)
    if reader.table is not None:
        raise ProviderError("unclosed notice table")
    matching = [table for heading, table in reader.tables
                if _compact(heading).rstrip("：:") == "中证500指数样本调整名单"]
    if not matching:
        return NoticeReview(status="ATTACHMENT_OR_LAYOUT_REVIEW_REQUIRED", changes=(), **common)
    if len(matching) != 1:
        raise ProviderError("multiple CSI500 change tables require independent review")
    rows = matching[0]
    if (len(rows) < 3 or list(map(_compact, rows[0])) != ["调出名单", "", "调入名单", ""] or
            list(map(_compact, rows[1])) != ["证券代码", "证券名称", "证券代码", "证券名称"]):
        raise ProviderError("unrecognized CSI500 table direction/header; no assumed column order")
    changes: List[Dict[str, Any]] = []
    for number, row in enumerate(rows[2:], 3):
        if len(row) != 4 or not all(isinstance(value, str) and value.strip() for value in row):
            raise ProviderError("incomplete CSI500 table row")
        for action, code, name in (("REMOVE", row[0], row[1]), ("ADD", row[2], row[3])):
            if not re.fullmatch(r"\d{6}", code):
                raise ProviderError("CSI500 security identifier must retain six digits")
            changes.append({"action": action, "symbol": normalize_symbol(code), "name": name,
                            "source_table_row": number})
    groups = {action: [row["symbol"] for row in changes if row["action"] == action] for action in ("REMOVE", "ADD")}
    if any(len(values) != len(set(values)) for values in groups.values()) or set(groups["ADD"]) & set(groups["REMOVE"]):
        raise ProviderError("duplicate or overlapping CSI500 change symbols")
    if count is None or count < 1 or len(groups["ADD"]) != count or len(groups["REMOVE"]) != count:
        raise ProviderError("CSI500 table does not match explicit stated change count")
    # Calendar mapping and any conditional event still need independent review.
    # In particular, two different date semantics cannot silently overwrite each other.
    status = "PARSED_SOURCE_CANDIDATE_NOT_PIT" if (bool(effective) != bool(start)) and not conditional else "EFFECTIVE_DATE_REVIEW_REQUIRED"
    return NoticeReview(status=status, changes=tuple(changes), **common)
