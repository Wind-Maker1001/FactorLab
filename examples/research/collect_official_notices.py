"""Archive public CSI notices and their real attachments, without publishing PIT.

All detail pages in the configured publication window are read. By default,
attachments are downloaded only for notices whose title/body mentions CSI500.
The catalogue filter and this attachment selection are recorded explicitly;
neither is proof that all effective membership changes have been found.
"""

import argparse
from datetime import date
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional, Sequence

from qbot.research.config import load_config
from qbot.research.errors import FactorLabError
from qbot.research.reproducibility import RunManifest
from qbot.research.universe.official_sources import OfficialAccessDeniedError, OfficialNoticeSource
from qbot.research.universe.notice_review import mentions_csi500
from qbot.research.universe.notice_attachments import discover_notice_attachments


LOGGER = logging.getLogger(__name__)
WARNING = ("OFFICIAL SOURCE EVIDENCE ONLY: catalogue completeness is not PIT completeness; "
           "no starting constituent baseline, effective membership, security status, "
           "dataset, RankIC, model or Forward signal is authorized by this archive.")


def _date(value: str) -> date:
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc
    if result.isoformat() != value:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/research/csi500_5y.yaml")
    parser.add_argument("--published-start", type=_date, default=date(2021, 1, 1))
    parser.add_argument("--published-end", type=_date, default=date(2026, 9, 11))
    parser.add_argument("--archive-root", help="New directory for a fresh snapshot; reuse for resume.")
    parser.add_argument("--notice-ids", type=int, nargs="+", help="Explicit diagnostic subset, not full-window coverage.")
    parser.add_argument("--timeout-seconds", type=float, default=15)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--cache-only", action="store_true", help="Read verified archive only; no HTTP fallback.")
    args = parser.parse_args(argv)
    if args.published_start > args.published_end:
        parser.error("published-start must not be after published-end")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config(args.config)
    manifest = RunManifest(config, "collect-official-notices", {
        "published_start": args.published_start.isoformat(),
        "published_end": args.published_end.isoformat(),
        "notice_ids": args.notice_ids, "timeout_seconds": args.timeout_seconds,
        "max_retries": args.max_retries, "cache_only": args.cache_only,
    })
    output = config.paths.results_root / "diagnostics" / "official-notices" / manifest.run_id
    output.mkdir(parents=True, exist_ok=False)
    root = Path(args.archive_root) if args.archive_root else (
        config.paths.data_root / "diagnostics" / "official-notices" / manifest.run_id)
    if not root.is_absolute():
        root = config.repo_root / root
    manifest.payload.update({
        "diagnostic_only": True, "warning": WARNING,
        "point_in_time_universe_verified": False, "research_conclusions_permitted": False,
        "catalogue_query": {"indexList": ["csi_index"], "relatedTopics": ["index_rebalance"],
                            "typeList": ["announcement"], "searchInput": ""},
        "attachment_selection": "explicit notice IDs" if args.notice_ids else "title/body mentions 中证500",
        "archive_root": str(root.resolve()),
        "effective_dates_inferred": False, "external_timestamp_proof": False,
    })
    manifest.add_output("source_archive", root.resolve())
    failures = []
    evidence = []
    completed = []
    source = None
    try:
        source = OfficialNoticeSource(root, timeout_seconds=args.timeout_seconds,
                                      max_retries=args.max_retries, cache_only=args.cache_only)
        catalogue, pages = source.catalogue()
        scoped = [row for row in catalogue if args.published_start.isoformat() <=
                  row["publishDate"] <= args.published_end.isoformat()]
        selected = scoped
        if args.notice_ids:
            if len(set(args.notice_ids)) != len(args.notice_ids):
                raise FactorLabError("duplicate explicit notice IDs")
            selected = [row for row in scoped if row["id"] in args.notice_ids]
            if len(selected) != len(args.notice_ids):
                raise FactorLabError("explicit notice IDs are absent from the observed scoped catalogue")
        evidence.extend({"kind": "catalogue", "path": str(item.path), **item.receipt} for item in pages)
        manifest.payload.update({"catalogue_rows": len(catalogue), "scoped_rows": len(scoped),
                                 "selected_rows": len(selected), "catalogue_page_count": len(pages),
                                 "catalogue_query_pagination_complete": True,
                                 "full_scoped_details_requested": args.notice_ids is None})
        catalogue_path = output / "catalogue.json"
        catalogue_path.write_text(json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest.add_output("catalogue", catalogue_path)
        LOGGER.info("Retrieved %d catalogue records; %d in publication window, %d selected", len(catalogue), len(scoped), len(selected))
        for number, listing in enumerate(selected, 1):
            try:
                notice, artifact = source.notice(listing["id"])
                evidence.append({"kind": "notice", "notice_id": listing["id"], "path": str(artifact.path), **artifact.receipt})
                if notice["title"] != listing["title"] or notice["publishDate"] != listing["publishDate"]:
                    raise FactorLabError("official catalogue/detail title or publication date mismatch")
                attachments = []
                mentioned = mentions_csi500(notice["title"] + notice["content"])
                for enclosure in discover_notice_attachments(notice):
                    row = {"label": enclosure["fileName"], "url": enclosure["fileUrl"],
                           "discovery_sources": enclosure["discovery_sources"],
                           "publisher_create_at": enclosure.get("createAt"), "status": "NOT_SELECTED"}
                    if mentioned or args.notice_ids:
                        try:
                            item = source.attachment(enclosure["fileUrl"])
                            evidence.append({"kind": "attachment", "notice_id": listing["id"], "path": str(item.path), **item.receipt})
                            row.update({"status": "ARCHIVED", "path": str(item.path),
                                        "content_sha256": item.receipt["content_sha256"], "bytes": len(item.content)})
                        except FactorLabError as exc:
                            not_requested = isinstance(exc, OfficialAccessDeniedError) and not exc.request_performed
                            row.update({"status": "SKIPPED_AFTER_SITE_DENIAL" if not_requested else "FAILED", "error": str(exc)})
                            failures.append({"notice_id": listing["id"], "attachment": enclosure["fileUrl"], "error": str(exc),
                                             "skipped_after_site_denial": not_requested})
                            LOGGER.warning("Attachment failed for notice %d: %s", listing["id"], exc)
                    attachments.append(row)
                completed.append({"id": notice["id"], "title": notice["title"], "publish_date": notice["publishDate"],
                                  "notice_url": "https://www.csindex.com.cn/#/about/newsDetail?id=%d" % notice["id"],
                                  "mentions_csi500": mentioned, "detail_path": str(artifact.path),
                                  "detail_sha256": artifact.receipt["content_sha256"], "attachments": attachments})
            except FactorLabError as exc:
                failures.append({"notice_id": listing["id"], "error": str(exc),
                                 "skipped_after_site_denial": isinstance(exc, OfficialAccessDeniedError) and not exc.request_performed})
                LOGGER.warning("Notice failed %d: %s", listing["id"], exc)
            if number % 10 == 0 or number == len(selected):
                LOGGER.info("Read %d/%d notices; %d failures", number, len(selected), len(failures))
        for name, records in (("notices", completed), ("source_evidence", evidence)):
            path = output / (name + ".json")
            path.write_text(json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            manifest.add_output(name, path)
        manifest.payload.update({"completed_notices": len(completed),
                                 "network_requests": source.network_requests, "verified_cache_reads": source.cache_reads,
                                 "archived_attachments": sum(r["kind"] == "attachment" for r in evidence),
                                 "csi500_mentioned_notices": sum(r["mentions_csi500"] for r in completed),
                                 "output_artifacts": {path.name: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                                      for path in output.glob("*.json")}})
        manifest.add_failures("official_sources", failures)
        manifest.finish("partial" if failures else "success")
        LOGGER.info("%s", WARNING)
        LOGGER.info("Run manifest: %s", manifest.path)
        return 1 if failures else 0
    except (FactorLabError, OSError) as exc:
        manifest.payload.update({"network_requests": source.network_requests if source else 0,
                                 "verified_cache_reads": source.cache_reads if source else 0})
        manifest.add_failures("official_sources", failures + [{"error": str(exc)}])
        manifest.finish("failed", str(exc))
        LOGGER.error("Official evidence collection failed: %s", exc)
        return 1
    except Exception as exc:
        # Unexpected implementation errors remain fatal, but leave a terminal
        # receipt instead of a misleading indefinitely-running manifest.
        manifest.payload.update({"network_requests": source.network_requests if source else 0,
                                 "verified_cache_reads": source.cache_reads if source else 0})
        manifest.finish("failed", "%s: %s" % (type(exc).__name__, exc))
        LOGGER.exception("Unexpected official evidence collection failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
