"""Strictly offline CSI notice coverage audit; never supplies a PIT universe.

Only an existing validated HTTP archive and explicit browser observation
directories are read. Missing bodies/attachments cannot trigger HTTP fallback.
"""

import argparse
import csv
from datetime import date
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Optional, Sequence

from qbot.research.config import load_config
from qbot.research.errors import FactorLabError, ProviderError
from qbot.research.reproducibility import RunManifest
from qbot.research.universe.browser_observations import read_browser_notice
from qbot.research.universe.official_sources import DETAIL_URL, OfficialNoticeSource
from qbot.research.universe.source_coverage import audit_notice_coverage


LOGGER = logging.getLogger(__name__)


def _date(value):
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")
    return parsed


def _directory(value, config):
    path = Path(value)
    path = path if path.is_absolute() else config.repo_root / path
    if path.is_symlink() or not path.is_dir():
        raise ProviderError("explicit source input must be an existing non-symlink directory")
    return path.resolve()


def _direct_records(source):
    paths = sorted(source.root.glob("*.receipt.json"))
    if len(paths) > 10000:
        raise ProviderError("HTTP archive exceeds bounded receipt discovery size")
    ids = []
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
            raise ProviderError("HTTP request receipt is not a bounded regular file")
        # Discovery only: source.notice subsequently validates the actual
        # request-key receipt, identity, bytes and hash using the shared reader.
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("invalid HTTP archive discovery receipt") from exc
        if not isinstance(receipt, dict) or not isinstance(receipt.get("request"), dict):
            raise ProviderError("HTTP archive discovery receipt has no request identity")
        request = receipt["request"]
        url = request.get("url")
        if not isinstance(url, str):
            raise ProviderError("HTTP archive request URL is absent")
        if not url.startswith(DETAIL_URL):
            continue
        if request.get("method") != "GET" or not re.fullmatch(re.escape(DETAIL_URL) + r"\?id=[1-9]\d*", url):
            raise ProviderError("noncanonical archived notice request")
        ids.append(int(url.rsplit("=", 1)[1]))
    if len(ids) != len(set(ids)):
        raise ProviderError("duplicate HTTP notice identities require explicit version selection")
    result, artifacts = [], []
    for notice_id in sorted(ids):
        notice, artifact = source.notice(notice_id)
        evidence = {"kind": "standard_http_archive_bytes", "input_path": str(artifact.path),
                    "content_sha256": artifact.receipt["content_sha256"],
                    "source_url": artifact.receipt["request"]["url"], "http_receipt": artifact.receipt,
                    "representation": "exact bytes read by the standard HTTP archive; not historical availability proof",
                    "external_origin_authentication_proof": False, "external_timestamp_proof": False,
                    "point_in_time_universe_verified": False}
        result.append((notice, evidence))
        artifacts.append(artifact)
    return result, artifacts


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/research/csi500_5y.yaml")
    parser.add_argument("--archive-root", required=True, help="Existing standard HTTP archive; cache reads only.")
    parser.add_argument("--browser-directories", nargs="+", default=[])
    parser.add_argument("--published-start", type=_date, default=date(2021, 1, 1))
    parser.add_argument("--published-end", type=_date, default=date(2026, 9, 11))
    args = parser.parse_args(argv)
    if args.published_start > args.published_end:
        parser.error("published-start must not be after published-end")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config(args.config)
    manifest = RunManifest(config, "audit-official-coverage", {**vars(args),
        "published_start": args.published_start.isoformat(), "published_end": args.published_end.isoformat()})
    manifest.payload.update({"diagnostic_only": True, "point_in_time_universe_verified": False,
                             "research_conclusions_permitted": False, "historical_constituents_generated": False,
                             "network_requests": 0, "browser_requests_performed_by_this_run": 0})
    source = None
    try:
        root = _directory(args.archive_root, config)
        directories = [_directory(value, config) for value in args.browser_directories]
        if len(directories) != len(set(directories)):
            raise ProviderError("duplicate explicit browser input directories")
        source = OfficialNoticeSource(root, cache_only=True, max_retries=0)
        catalogue, pages = source.catalogue()
        records, artifacts = _direct_records(source)
        observations = []
        for directory in directories:
            paths = sorted(directory.glob("*.observation.json"))
            if not paths or len(paths) > 1000:
                raise ProviderError("explicit browser directory needs between 1 and 1000 observations")
            observations.extend(read_browser_notice(path) for path in paths)
        records.extend((item.notice, item.evidence()) for item in observations)
        review = audit_notice_coverage(catalogue, records, args.published_start, args.published_end)
        output = config.paths.results_root / "diagnostics" / "source-coverage" / manifest.run_id
        (output / "inputs").mkdir(parents=True, exist_ok=False)
        source_inputs = []

        def snapshot(content, original_path, evidence):
            digest = hashlib.sha256(content).hexdigest()
            path = output / "inputs" / (digest + ".json")
            if path.exists():
                if path.read_bytes() != content:
                    raise ProviderError("exclusive source snapshot identity conflict")
            else:
                with path.open("xb") as stream:
                    stream.write(content)
            source_inputs.append({"input_path": str(original_path), "snapshot_path": str(path),
                                  "snapshot_sha256": digest, "source_evidence": evidence})

        for artifact in pages:
            snapshot(artifact.content, artifact.path, {"kind": "catalogue_http_archive", "http_receipt": artifact.receipt})
        for (notice, evidence), artifact in zip(records[:len(artifacts)], artifacts):
            snapshot(artifact.content, artifact.path, evidence)
        for item in observations:
            snapshot(item.container_bytes, item.input_path, item.evidence())
        manifest.payload["source_inputs"] = source_inputs
        review["bound_source_inputs"] = source_inputs
        review["catalogue_query"] = {"indexList": ["csi_index"], "relatedTopics": ["index_rebalance"],
                                     "typeList": ["announcement"], "searchInput": ""}
        review_path = output / "coverage.json"
        with review_path.open("x", encoding="utf-8") as stream:
            json.dump(review, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        csv_path = output / "coverage.csv"
        with csv_path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(review["rows"][0]))
            writer.writeheader()
            writer.writerows(review["rows"])
        attachments_path = output / "attachment_gaps.json"
        with attachments_path.open("x", encoding="utf-8") as stream:
            json.dump(review["attachment_gaps"], stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        summary_path = output / "summary.md"
        with summary_path.open("x", encoding="utf-8") as stream:
            stream.write("# Official notice source coverage (diagnostic only)\n\n"
                         "Observed bodies: {observed_bodies_in_scope}/{scoped_rows}. Missing bodies: {missing}.\n\n"
                         "Discovered attachment references: {discovered_attachments_not_reviewed_by_this_audit}; "
                         "their contents are not reviewed by this audit, including any already archived files.\n\n"
                         "No CSI500 mention or file link is not a zero-adjustment verdict. "
                         "Catalogue-window coverage is not complete PIT, an authenticated historical baseline "
                         "or historical availability proof. No members/dataset/Forward signal generated.\n".format(
                             missing=len(review["missing_notice_ids"]), **review))
        for name, path in (("coverage", review_path), ("coverage_csv", csv_path),
                           ("attachment_gaps", attachments_path), ("summary", summary_path)):
            manifest.add_output(name, path)
        manifest.payload.update({key: review[key] for key in (
            "catalogue_rows", "scoped_rows", "observed_bodies_in_scope", "observed_bodies_outside_scope",
            "body_coverage_complete_for_this_catalogue_window", "missing_notice_ids", "body_mentions_csi500_count",
            "discovered_attachments_not_reviewed_by_this_audit")})
        manifest.payload.update({"network_requests": source.network_requests, "verified_cache_reads": source.cache_reads,
            "output_artifacts": {path.name: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                 for path in (review_path, csv_path, attachments_path, summary_path)}})
        missing = review["missing_notice_ids"]
        manifest.add_failures("missing_notice_bodies", [{"notice_id": value} for value in missing])
        manifest.finish("partial" if missing else "success")
        LOGGER.info("Observed %d/%d bodies; %d missing; 0 HTTP. No PIT promotion.",
                    review["observed_bodies_in_scope"], review["scoped_rows"], len(missing))
        LOGGER.info("Manifest: %s", manifest.path)
        return 1 if missing else 0
    except Exception as exc:
        manifest.payload.update({"network_requests": source.network_requests if source else 0,
                                 "verified_cache_reads": source.cache_reads if source else 0})
        manifest.add_failures("source_coverage", [{"error": str(exc)}])
        manifest.finish("failed", "%s: %s" % (type(exc).__name__, exc))
        if isinstance(exc, (FactorLabError, OSError)):
            LOGGER.error("Source coverage audit failed: %s", exc)
        else:
            LOGGER.exception("Unexpected source coverage audit failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
