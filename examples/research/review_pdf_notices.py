"""Offline candidate review of PDF links in saved official browser notices.

Only existing SHA-verified publisher attachment caches are read. No HTTP
fallback, index effective-date inference, PIT publication or Forward activation
is provided. PDF extraction is an optional source-review dependency.
"""

import argparse
import csv
from dataclasses import asdict
import hashlib
import importlib.metadata
import io
import json
import logging
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import unquote, urlsplit

from factorlab.config import load_config
from factorlab.errors import FactorLabError, ProviderError
from factorlab.reproducibility import RunManifest
from factorlab.universe import notice_pdf
from factorlab.universe.browser_observations import read_browser_notice
from factorlab.universe.notice_attachments import discover_notice_attachments
from factorlab.universe.notice_pdf import PDF_TABLE_CONTRACT, review_csi500_pdf_pages
from factorlab.universe.official_sources import OfficialNoticeSource


LOGGER = logging.getLogger(__name__)
FIELDS = ("notice_id", "publish_date", "explicit_after_close_date", "explicit_start_date",
          "conditional_effective_clause", "action", "symbol", "name", "source_page", "source_line",
          "pdf_sha256", "page_text_utf8_sha256", "observation_sha256", "status",
          "point_in_time_universe_verified")


def _root(value: str, config) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else config.repo_root / path).resolve()


def _write_json(path: Path, payload) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _extract_pages(content: bytes):
    if not content.startswith(b"%PDF-") or not 1 <= len(content) <= 20 * 1024 * 1024:
        raise ProviderError("PDF source is absent, oversized or lacks its file signature")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ProviderError("optional PDF review requires dev/requirements-source-review.txt") from exc
    reader = PdfReader(io.BytesIO(content), strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 100:
        raise ProviderError("PDF source must be unencrypted and have 1 to 100 pages")
    return [page.extract_text() or "" for page in reader.pages]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/research/csi500_5y.yaml")
    parser.add_argument("--input-directory", required=True, help="Saved *.observation.json containers.")
    parser.add_argument("--archive-root", required=True, help="Existing publisher PDF objects and request receipts.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config(args.config)
    manifest = RunManifest(config, "review-pdf-notices", vars(args))
    manifest.payload.update({"diagnostic_only": True, "point_in_time_universe_verified": False,
                             "research_conclusions_permitted": False, "historical_constituents_generated": False,
                             "starting_membership_baseline_verified": False, "effective_dates_inferred": False,
                             "historical_availability_proved": False, "visual_review_performed_by_this_run": False,
                             "network_requests": 0, "verified_cache_reads": 0, "pdf_table_contract": PDF_TABLE_CONTRACT,
                             "extraction_method": "pypdf.PdfReader(strict=True).pages[].extract_text()",
                             "parser_module_sha256": hashlib.sha256(Path(notice_pdf.__file__).read_bytes()).hexdigest(),
                             "extractor_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    try:
        manifest.payload["package_versions"]["pypdf"] = importlib.metadata.version("pypdf")
    except importlib.metadata.PackageNotFoundError:
        manifest.payload["package_versions"]["pypdf"] = None
    source = None
    failures, reviews, candidates, inputs = [], [], [], []
    try:
        directory, archive = _root(args.input_directory, config), _root(args.archive_root, config)
        if not directory.is_dir() or not archive.is_dir():
            raise ProviderError("PDF review requires existing observation and attachment-cache directories")
        paths = sorted(directory.glob("*.observation.json"))
        if not 1 <= len(paths) <= 100:
            raise ProviderError("expected between 1 and 100 explicit browser observation files")
        observations = [read_browser_notice(path) for path in paths]
        if len({item.notice["id"] for item in observations}) != len(observations):
            raise ProviderError("duplicate notice IDs require explicit source-version selection")
        output = config.paths.results_root / "diagnostics" / "pdf-notice-review" / manifest.run_id
        snapshots = output / "inputs"
        snapshots.mkdir(parents=True, exist_ok=False)
        source = OfficialNoticeSource(archive, cache_only=True, max_retries=0)
        for item in observations:
            notice_id = item.notice["id"]
            observation_path = snapshots / (str(notice_id) + ".observation.json")
            with observation_path.open("xb") as stream:
                stream.write(item.container_bytes)
            input_row = {**item.evidence(), "observation_snapshot": str(observation_path)}
            inputs.append(input_row)
            try:
                links = [link for link in discover_notice_attachments(item.notice)
                         if Path(unquote(urlsplit(link["fileUrl"]).path)).suffix.lower() == ".pdf"]
                if len(links) != 1:
                    raise ProviderError("PDF review requires exactly one explicit publisher PDF link per notice")
                artifact = source.attachment(links[0]["fileUrl"])
                pdf_hash = hashlib.sha256(artifact.content).hexdigest()
                pdf_path = snapshots / (str(notice_id) + ".pdf")
                with pdf_path.open("xb") as stream:
                    stream.write(artifact.content)
                receipt_path = snapshots / (str(notice_id) + ".receipt.json")
                request_bytes = json.dumps(artifact.receipt["request"], sort_keys=True, separators=(",", ":")).encode("utf-8")
                receipt_source = archive / (hashlib.sha256(request_bytes).hexdigest() + ".receipt.json")
                receipt_bytes = receipt_source.read_bytes()
                if json.loads(receipt_bytes.decode("utf-8")) != artifact.receipt:
                    raise ProviderError("publisher receipt changed after verified cache read")
                with receipt_path.open("xb") as stream:
                    stream.write(receipt_bytes)
                input_row.update({"pdf_sha256": pdf_hash, "pdf_snapshot": str(pdf_path),
                                  "publisher_url": links[0]["fileUrl"], "publisher_receipt": artifact.receipt,
                                  "receipt_snapshot": str(receipt_path), "receipt_input_path": str(receipt_source),
                                  "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest()})
                pages = _extract_pages(artifact.content)
                text_hashes = [hashlib.sha256(page.encode("utf-8")).hexdigest() for page in pages]
                text_path = snapshots / (str(notice_id) + ".pages.json")
                _write_json(text_path, {"pdf_sha256": pdf_hash, "pages": pages, "page_text_utf8_sha256": text_hashes})
                input_row.update({"extracted_pages_snapshot": str(text_path), "page_count": len(pages),
                                  "page_text_utf8_sha256": text_hashes})
                result = review_csi500_pdf_pages(item.notice, pages)
                reviews.append({"notice_id": notice_id, "source_evidence": input_row, "pdf_review": asdict(result)})
                reviewed = result.review
                for row in reviewed.changes:
                    candidates.append({"notice_id": notice_id, "publish_date": reviewed.publish_date,
                                       "explicit_after_close_date": reviewed.explicit_after_close_date,
                                       "explicit_start_date": reviewed.explicit_start_date,
                                       "conditional_effective_clause": reviewed.conditional_effective_clause,
                                       **row, "pdf_sha256": pdf_hash,
                                       "page_text_utf8_sha256": text_hashes[row["source_page"] - 1],
                                       "observation_sha256": item.container_sha256, "status": reviewed.status,
                                       "point_in_time_universe_verified": False})
                LOGGER.info("PDF notice %d: %d candidate events; %s", notice_id, len(reviewed.changes), reviewed.status)
            except Exception as exc:
                failures.append({"notice_id": notice_id, "error": "%s: %s" % (type(exc).__name__, exc)})
                LOGGER.warning("PDF notice %d failed: %s", notice_id, exc)
        candidate_path = output / "candidate_changes.csv"
        with candidate_path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(candidates)
        review_path = output / "review.json"
        _write_json(review_path, {"pdf_reviews": reviews, "failures": failures})
        manifest.add_output("candidate_changes", candidate_path)
        manifest.add_output("review", review_path)
        manifest.payload.update({"input_evidence": inputs, "observation_count": len(observations),
                                 "candidate_change_events": len(candidates), "pdfs_reviewed": len(reviews),
                                 "network_requests": source.network_requests, "verified_cache_reads": source.cache_reads,
                                 "manual_effective_date_review_required": [row["notice_id"] for row in reviews if
                                     row["pdf_review"]["review"]["status"] != "PARSED_SOURCE_CANDIDATE_NOT_PIT"],
                                 "output_artifacts": {str(path.relative_to(output)): {"path": str(path),
                                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                     for path in sorted(output.rglob("*")) if path.is_file()}})
        manifest.add_failures("pdf_source_review", failures)
        manifest.finish("partial" if failures else "success")
        LOGGER.info("Manifest: %s; offline source candidates only, no PIT publication.", manifest.path)
        return 1 if failures else 0
    except Exception as exc:
        manifest.payload.update({"input_evidence": inputs, "network_requests": source.network_requests if source else 0,
                                 "verified_cache_reads": source.cache_reads if source else 0})
        manifest.add_failures("pdf_source_review", failures + [{"error": "%s: %s" % (type(exc).__name__, exc)}])
        manifest.finish("failed", "%s: %s" % (type(exc).__name__, exc))
        if isinstance(exc, (FactorLabError, OSError)):
            LOGGER.error("Offline PDF review failed: %s", exc)
        else:
            LOGGER.exception("Unexpected offline PDF review error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
