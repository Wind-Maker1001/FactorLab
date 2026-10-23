"""Discover actual metadata and HTML file links, without fetching or inferring PIT.

Older public notices have an empty enclosureList but real attachments in their
HTML. Distinct linked file revisions remain distinct; none is picked as the
historically available revision simply because it is newer.
"""

from html.parser import HTMLParser
from pathlib import PurePosixPath
import re
from typing import Any, Dict, List
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from qbot.research.errors import ProviderError


class _FileLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.anchor = None
        self.ordinal = 0
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if self.hidden or tag != "a":
            return
        if self.anchor is not None:
            raise ProviderError("nested notice anchors require independent review")
        names = [key for key, value in attrs]
        if len(names) != len(set(names)):
            raise ProviderError("duplicate notice anchor attributes require independent review")
        values = dict(attrs)
        self.ordinal += 1
        self.anchor = {"href": values.get("href"), "title": values.get("title") or "",
                       "text": [], "source_link_ordinal": self.ordinal}

    def handle_data(self, data):
        if not self.hidden and self.anchor is not None:
            self.anchor["text"].append(data)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
            return
        if not self.hidden and tag == "a" and self.anchor is not None:
            self.links.append(self.anchor)
            self.anchor = None


def _url_identity(url: str) -> str:
    """Same encoded request, not a guessed resource: do not decode slash/query."""
    try:
        parsed = urlsplit(url)
        wire = urlunsplit((parsed.scheme, parsed.netloc, quote(parsed.path, safe="/%:@"),
                          quote(parsed.query, safe="=&%+,:") , parsed.fragment))
    except ValueError as exc:
        raise ProviderError("malformed notice file URL") from exc
    return re.sub(r"%[0-9a-fA-F]{2}", lambda match: match.group(0).upper(), wire)


def body_file_links(content: str, page_url: str) -> List[Dict[str, Any]]:
    """Only literal file hrefs; ordinary website links and scripts are not files."""
    if not isinstance(content, str) or len(content.encode("utf-8")) > 2 * 1024 * 1024:
        raise ProviderError("notice HTML for file discovery is invalid or oversized")
    parser = _FileLinks()
    parser.feed(content)
    if parser.anchor is not None:
        raise ProviderError("unclosed notice anchor requires independent review")
    result = []
    for row in parser.links:
        href = row["href"]
        if not isinstance(href, str) or not href.strip():
            continue
        resolved = urljoin(page_url, href)
        try:
            suffix = PurePosixPath(unquote(urlsplit(resolved).path)).suffix.lower()
        except ValueError as exc:
            raise ProviderError("malformed notice href") from exc
        if suffix not in (".xlsx", ".xls", ".pdf", ".zip", ".csv"):
            continue
        visible = " ".join("".join(row["text"]).split())
        result.append({"href": href, "resolved_url": resolved,
                       "label": visible or row["title"], "label_from_title": not bool(visible) and bool(row["title"]),
                       "visible_label_present": bool(visible), "source_link_ordinal": row["source_link_ordinal"]})
    return result


def discover_notice_attachments(notice: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Merge metadata/HTML aliases; never merge distinct dated archive objects."""
    if (not isinstance(notice, dict) or type(notice.get("id")) is not int or notice["id"] < 1 or
            not isinstance(notice.get("enclosureList"), list)):
        raise ProviderError("file discovery requires a validated notice identity/enclosureList")
    page_url = "https://www.csindex.com.cn/#/about/newsDetail?id=%d" % notice["id"]
    files = []
    by_identity = {}

    def add(url, label, created_at, origin):
        if not isinstance(url, str) or not url or not isinstance(label, str):
            raise ProviderError("invalid notice attachment metadata")
        key = _url_identity(url)
        if key in by_identity:
            by_identity[key]["discovery_sources"].append(origin)
            return
        item = {"fileUrl": url, "fileName": label, "createAt": created_at, "discovery_sources": [origin]}
        files.append(item)
        by_identity[key] = item

    for number, enclosure in enumerate(notice["enclosureList"], 1):
        if not isinstance(enclosure, dict):
            raise ProviderError("invalid notice enclosure metadata")
        add(enclosure.get("fileUrl"), enclosure.get("fileName"), enclosure.get("createAt"),
            {"kind": "enclosureList", "source_ordinal": number, "publisher_url": enclosure.get("fileUrl")})
    for link in body_file_links(notice.get("content"), page_url):
        add(link["resolved_url"], link["label"], None, {"kind": "body_href", **link})
    return files
