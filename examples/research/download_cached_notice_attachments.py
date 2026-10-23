"""Download literal links from verified cached notices, never request bodies.

This explicit-subset source task can complete an attachment audit without
reopening a denied catalogue/detail workflow. It does not infer CSI500 relevance
from the title, interpret attachments or publish point-in-time membership.
"""

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional, Sequence

from factorlab.config import load_config
from factorlab.errors import FactorLabError, ProviderError
from factorlab.reproducibility import RunManifest
from factorlab.universe.notice_attachments import discover_notice_attachments
from factorlab.universe.official_sources import OfficialAccessDeniedError, OfficialNoticeSource


LOGGER = logging.getLogger(__name__)


def _root(value, config):
    path = Path(value)
    return (path if path.is_absolute() else config.repo_root / path).resolve()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/research/csi500_5y.yaml")
    parser.add_argument("--source-archive", required=True, help="Existing verified publisher JSON cache; strictly offline.")
    parser.add_argument("--attachment-root", required=True, help="Separate attachment cache; resume without replacing bytes.")
    parser.add_argument("--notice-ids", type=int, nargs="+", required=True)
    parser.add_argument("--cache-only", action="store_true", help="Attachments also offline; no HTTP fallback.")
    parser.add_argument("--timeout-seconds", type=float, default=10)
    parser.add_argument("--max-retries", type=int, default=0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config(args.config)
    manifest = RunManifest(config, "download-cached-notice-attachments", vars(args))
    manifest.payload.update({"diagnostic_only": True, "point_in_time_universe_verified": False,
                             "research_conclusions_permitted": False, "historical_constituents_generated": False,
                             "effective_dates_inferred": False, "historical_availability_proved": False,
                             "body_network_requests": 0, "attachment_network_requests": 0,
                             "verified_body_cache_reads": 0, "verified_attachment_cache_reads": 0,
                             "attachment_selection": "all literal links in explicitly selected cached notices; no title filter",
                             "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    bodies, attachments = None, None
    failures, summaries = [], []
    try:
        ids = args.notice_ids
        if not 1 <= len(ids) <= 20 or len(set(ids)) != len(ids) or any(i < 1 for i in ids):
            raise ProviderError("select 1 to 20 unique positive notice IDs")
        source_root, attachment_root = _root(args.source_archive, config), _root(args.attachment_root, config)
        if not source_root.is_dir() or source_root == attachment_root:
            raise ProviderError("existing source archive and separate attachment root are required")
        bodies = OfficialNoticeSource(source_root, cache_only=True, max_retries=0)
        # Validate every cached source before any network request. A missing body
        # is not a reason to fetch it, choose another transport or skip its gap.
        selected = [(notice, artifact, discover_notice_attachments(notice))
                    for notice, artifact in (bodies.notice(i) for i in ids)]
        output = config.paths.results_root / "diagnostics" / "cached-notice-attachments" / manifest.run_id
        snapshots = output / "inputs"
        snapshots.mkdir(parents=True, exist_ok=False)
        for notice, artifact, links in selected:
            snapshot = snapshots / (str(notice["id"]) + ".notice.json")
            with snapshot.open("xb") as stream:
                stream.write(artifact.content)
            request = json.dumps(artifact.receipt["request"], sort_keys=True, separators=(",", ":")).encode("utf-8")
            receipt_path = source_root / (hashlib.sha256(request).hexdigest() + ".receipt.json")
            receipt_bytes = receipt_path.read_bytes()
            if json.loads(receipt_bytes.decode("utf-8")) != artifact.receipt:
                raise ProviderError("cached source receipt changed after validation")
            receipt_snapshot = snapshots / (str(notice["id"]) + ".receipt.json")
            with receipt_snapshot.open("xb") as stream:
                stream.write(receipt_bytes)
            summaries.append({"notice_id": notice["id"], "title": notice["title"], "publish_date": notice["publishDate"],
                              "body_snapshot": str(snapshot), "source_body_path": str(artifact.path),
                              "source_body_sha256": artifact.receipt["content_sha256"], "source_receipt": artifact.receipt,
                              "receipt_snapshot": str(receipt_snapshot), "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                              "attachments": []})
        attachments = OfficialNoticeSource(attachment_root, cache_only=args.cache_only,
                                           timeout_seconds=args.timeout_seconds, max_retries=args.max_retries)
        for summary, (_, _, links) in zip(summaries, selected):
            for link in links:
                record = {"publisher_url": link["fileUrl"], "label": link["fileName"],
                          "publisher_create_at": link.get("createAt"), "discovery_sources": link["discovery_sources"],
                          "point_in_time_universe_verified": False}
                try:
                    artifact = attachments.attachment(link["fileUrl"])
                    record.update({"status": "ARCHIVED_NOT_INTERPRETED", "path": str(artifact.path),
                                   "source_evidence": artifact.receipt})
                except FactorLabError as exc:
                    skipped = isinstance(exc, OfficialAccessDeniedError) and not exc.request_performed
                    record.update({"status": "SKIPPED_AFTER_SITE_DENIAL" if skipped else "FAILED", "error": str(exc)})
                    failures.append({"notice_id": summary["notice_id"], "publisher_url": link["fileUrl"],
                                     "error": str(exc), "skipped_after_site_denial": skipped})
                    LOGGER.warning("Notice %d attachment failed: %s", summary["notice_id"], exc)
                summary["attachments"].append(record)
        result = output / "attachments.json"
        with result.open("x", encoding="utf-8") as stream:
            json.dump({"notices": summaries, "failures": failures}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        manifest.add_output("attachments", result)
        manifest.add_output("attachment_archive", attachment_root)
        manifest.payload.update({"body_network_requests": bodies.network_requests,
                                 "attachment_network_requests": attachments.network_requests,
                                 "verified_body_cache_reads": bodies.cache_reads,
                                 "verified_attachment_cache_reads": attachments.cache_reads,
                                 "archived_not_interpreted": sum(r["status"] == "ARCHIVED_NOT_INTERPRETED"
                                     for n in summaries for r in n["attachments"]),
                                 "output_artifacts": {str(p.relative_to(output)): {"path": str(p),
                                     "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                                     for p in sorted(output.rglob("*")) if p.is_file()}})
        manifest.add_failures("cached_notice_attachments", failures)
        manifest.finish("partial" if failures else "success")
        LOGGER.info("Manifest: %s; source evidence only, no body requests or PIT publication.", manifest.path)
        return 1 if failures else 0
    except Exception as exc:
        manifest.payload.update({"body_network_requests": bodies.network_requests if bodies else 0,
                                 "attachment_network_requests": attachments.network_requests if attachments else 0,
                                 "verified_body_cache_reads": bodies.cache_reads if bodies else 0,
                                 "verified_attachment_cache_reads": attachments.cache_reads if attachments else 0})
        manifest.add_failures("cached_notice_attachments", failures + [{"error": "%s: %s" % (type(exc).__name__, exc)}])
        manifest.finish("failed", "%s: %s" % (type(exc).__name__, exc))
        LOGGER.error("Cached-notice attachment task failed: %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
