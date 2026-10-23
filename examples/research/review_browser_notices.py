"""Review saved public browser notices, preserving their distinct provenance.

Default is strictly offline. --download-attachments archives actual publisher
links using the standard source client, separately from browser observations.
Neither reviewed tables nor attachments are a complete PIT universe.
"""

import argparse
import csv
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
from typing import Optional, Sequence

from factorlab.config import load_config
from factorlab.errors import FactorLabError, ProviderError
from factorlab.reproducibility import RunManifest
from factorlab.universe.browser_observations import read_browser_notice
from factorlab.universe.notice_review import review_csi500_notice
from factorlab.universe.notice_attachments import discover_notice_attachments
from factorlab.universe.official_sources import OfficialAccessDeniedError, OfficialNoticeSource


LOGGER = logging.getLogger(__name__)
FIELDS = ("notice_id", "publish_date", "explicit_after_close_date", "explicit_start_date",
          "conditional_effective_clause", "action", "symbol", "name",
          "source_table_row", "observation_sha256", "response_text_utf8_sha256", "status",
          "point_in_time_universe_verified")


def _root(value: str, config) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else config.repo_root / path).resolve()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/research/csi500_5y.yaml")
    parser.add_argument("--input-directory", required=True)
    parser.add_argument("--download-attachments", action="store_true")
    parser.add_argument("--cache-only", action="store_true", help="Attachment cache only; no HTTP fallback.")
    parser.add_argument("--archive-root", default="data/research/diagnostics/official-browser-attachments")
    args = parser.parse_args(argv)
    if args.cache_only and not args.download_attachments:
        parser.error("--cache-only applies to --download-attachments; default review is already offline")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config(args.config)
    manifest = RunManifest(config, "review-browser-notices", vars(args))
    manifest.payload.update({"diagnostic_only": True, "point_in_time_universe_verified": False,
                             "research_conclusions_permitted": False, "effective_dates_inferred": False,
                             "historical_constituents_generated": False, "starting_membership_baseline_verified": False,
                             "external_timestamp_proof": False, "external_origin_authentication_proof": False,
                             "browser_requests_performed_by_this_run": 0, "network_requests": 0,
                             "verified_cache_reads": 0})
    source = None
    failures = []
    try:
        directory = _root(args.input_directory, config)
        if not directory.is_dir():
            raise ProviderError("browser observation input directory is absent")
        paths = sorted(directory.glob("*.observation.json"))
        if not paths or len(paths) > 1000:
            raise ProviderError("expected between 1 and 1000 explicit browser observation files")
        # Validate all source containers before any possible attachment request.
        observations = [read_browser_notice(path) for path in paths]
        if len({item.notice["id"] for item in observations}) != len(observations):
            raise ProviderError("duplicate browser notice IDs require explicit version selection")
        output = config.paths.results_root / "diagnostics" / "browser-notice-review" / manifest.run_id
        (output / "observations").mkdir(parents=True, exist_ok=False)
        input_evidence = []
        for number, item in enumerate(observations, 1):
            # Unique run directory, exclusive files: preserve the exact input
            # container bytes even if the original local file changes later.
            snapshot = output / "observations" / (str(item.notice["id"]) + ".observation.json")
            with snapshot.open("xb") as stream:
                stream.write(item.container_bytes)
            manifest.add_output("observation_%d" % number, snapshot)
            input_evidence.append({**item.evidence(), "snapshot_path": str(snapshot)})
        manifest.payload["input_evidence"] = input_evidence
        if args.download_attachments:
            source = OfficialNoticeSource(_root(args.archive_root, config), timeout_seconds=10,
                                          max_retries=0, cache_only=args.cache_only)
        summaries, candidates, attachments = [], [], []
        for item in observations:
            notice = item.notice
            summary = {"notice_id": notice["id"], "title": notice["title"],
                       "publish_date": notice["publishDate"], "source_evidence": item.evidence()}
            try:
                review = review_csi500_notice(notice)
                summary["review"] = asdict(review)
                for row in review.changes:
                    candidates.append({"notice_id": review.notice_id, "publish_date": review.publish_date,
                                       "explicit_after_close_date": review.explicit_after_close_date,
                                       "explicit_start_date": review.explicit_start_date,
                                       "conditional_effective_clause": review.conditional_effective_clause,
                                       **row, "observation_sha256": item.container_sha256,
                                       "response_text_utf8_sha256": item.response_text_sha256,
                                       "status": review.status, "point_in_time_universe_verified": False})
                LOGGER.info("Notice %d: %s; %d candidate change events", notice["id"], review.status, len(review.changes))
            except FactorLabError as exc:
                summary["review"] = {"status": "REVIEW_FAILED", "error": str(exc), "point_in_time_universe_verified": False}
                failures.append({"notice_id": notice["id"], "stage": "review", "error": str(exc)})
            summaries.append(summary)
            for enclosure in discover_notice_attachments(notice):
                record = {"notice_id": notice["id"], "publisher_url": enclosure["fileUrl"],
                          "label": enclosure["fileName"], "publisher_create_at": enclosure.get("createAt"),
                          "discovery_sources": enclosure["discovery_sources"],
                          "status": "DISCOVERED_NOT_DOWNLOADED", "point_in_time_universe_verified": False}
                if source is not None:
                    try:
                        artifact = source.attachment(enclosure["fileUrl"])
                        record.update({"status": "ARCHIVED_NOT_INTERPRETED", "path": str(artifact.path),
                                       "source_evidence": artifact.receipt})
                    except ProviderError as exc:
                        skipped = isinstance(exc, OfficialAccessDeniedError) and not exc.request_performed
                        record.update({"status": "SKIPPED_AFTER_SITE_DENIAL" if skipped else "FAILED", "error": str(exc)})
                        failures.append({"notice_id": notice["id"], "stage": "attachment", "error": str(exc),
                                         "publisher_url": enclosure["fileUrl"], "skipped_after_site_denial": skipped})
                        LOGGER.warning("Attachment for notice %d: %s", notice["id"], exc)
                attachments.append(record)
        candidate_path = output / "candidate_changes.csv"
        with candidate_path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(candidates)
        summary_path = output / "review.json"
        summary_path.write_text(json.dumps({"notices": summaries, "attachments": attachments},
                                          ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest.add_output("candidate_changes", candidate_path)
        manifest.add_output("review", summary_path)
        manifest.payload.update({"observation_count": len(observations), "candidate_change_events": len(candidates),
                                 "notice_ids": [item.notice["id"] for item in observations],
                                 "inline_candidate_tables": sum(bool(row.get("review", {}).get("changes")) for row in summaries),
                                 "manual_review_required_notices": [row["notice_id"] for row in summaries if
                                     row["review"]["status"] != "PARSED_SOURCE_CANDIDATE_NOT_PIT"],
                                 "attachments_archived_not_interpreted": sum(row["status"] == "ARCHIVED_NOT_INTERPRETED" for row in attachments),
                                 "discovered_attachments": len(attachments),
                                 "output_artifacts": {path.name: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                                      for path in (candidate_path, summary_path)},
                                 "network_requests": source.network_requests if source else 0,
                                 "verified_cache_reads": source.cache_reads if source else 0})
        manifest.add_failures("source_review", failures)
        manifest.finish("partial" if failures else "success")
        LOGGER.info("Source candidate review only; no starting baseline, PIT, dataset or Forward signal generated.")
        LOGGER.info("Manifest: %s", manifest.path)
        return 1 if failures else 0
    except Exception as exc:
        manifest.payload.update({"network_requests": source.network_requests if source else 0,
                                 "verified_cache_reads": source.cache_reads if source else 0})
        manifest.add_failures("source_review", failures + [{"error": str(exc)}])
        manifest.finish("failed", "%s: %s" % (type(exc).__name__, exc))
        if isinstance(exc, (FactorLabError, OSError)):
            LOGGER.error("Browser source review failed: %s", exc)
        else:
            LOGGER.exception("Unexpected browser source review error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
