"""Read-only public SSE index notice archive; not a constituent data importer."""

import argparse
import hashlib
from html.parser import HTMLParser
import json
import logging
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urljoin

from factorlab.config import load_config
from factorlab.errors import FactorLabError
from factorlab.reproducibility import RunManifest
from factorlab.universe.official_sources import OfficialNoticeSource


class _Links(HTMLParser):
    """Keep actual page link targets; do not invent URLs from an article date."""

    def __init__(self):
        super().__init__()
        self.links = []
        self._anchor = None
        self.title = []
        self._in_title = False
        self._title_captured = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self._anchor = {"href": values["href"], "text": ""}
        if tag == "title" and not self._title_captured:
            self._in_title = True

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor["text"] += data
        if self._in_title:
            self.title.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            self.links.append(self._anchor)
            self._anchor = None
        if tag == "title" and self._in_title:
            self._in_title = False
            self._title_captured = True


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/research/csi500_5y.yaml")
    parser.add_argument("--url", required=True)
    parser.add_argument("--archive-root", default="data/research/diagnostics/official-notices/sse-index-notices")
    parser.add_argument("--cache-only", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger = logging.getLogger(__name__)
    config = load_config(args.config)
    manifest = RunManifest(config, "collect-exchange-article", {"url": args.url, "cache_only": args.cache_only})
    root = Path(args.archive_root)
    if not root.is_absolute():
        root = config.repo_root / root
    manifest.payload.update({"diagnostic_only": True, "point_in_time_universe_verified": False,
                             "research_conclusions_permitted": False, "effective_dates_inferred": False,
                             "historical_constituents_generated": False})
    source = None
    try:
        source = OfficialNoticeSource(root, cache_only=args.cache_only, max_retries=0, timeout_seconds=10)
        artifact = source.exchange_article(args.url)
        reader = _Links()
        reader.feed(artifact.content.decode("utf-8"))
        # Page links are evidence/discovery only; an href is not a downloaded,
        # verified attachment. In particular, never download nav/account links.
        files = [{"url": urljoin(args.url, row["href"]), "label": row["text"].strip()}
                 for row in reader.links if row["href"].lower().split("?", 1)[0].endswith(
                     (".pdf", ".xls", ".xlsx", ".zip", ".csv"))]
        manifest.payload.update({"title": "".join(reader.title).strip(),
                                 "source_evidence": artifact.receipt, "linked_files_not_downloaded": files,
                                 "network_requests": source.network_requests, "verified_cache_reads": source.cache_reads,
                                 "article_content_sha256": hashlib.sha256(artifact.content).hexdigest()})
        manifest.add_output("article", artifact.path)
        manifest.finish()
        logger.info("Archived public exchange article (%d bytes); %d file links, not PIT", len(artifact.content), len(files))
        logger.info("Manifest: %s", manifest.path)
        return 0
    except Exception as exc:
        manifest.payload.update({"network_requests": source.network_requests if source else 0,
                                 "verified_cache_reads": source.cache_reads if source else 0})
        manifest.finish("failed", "%s: %s" % (type(exc).__name__, exc))
        if isinstance(exc, (FactorLabError, OSError)):
            logger.error("Exchange source archive failed: %s", exc)
        else:
            logger.exception("Unexpected exchange source archive failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
