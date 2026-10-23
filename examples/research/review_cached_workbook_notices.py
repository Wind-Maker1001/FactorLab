"""Offline source review of cached XLSX notices and read-only extracted cells.

The caller first imports every source worksheet with the bundled artifact
runtime. This task binds the resulting inspection to actual cached source bytes
and checks original workbook sheet names, but does not independently prove all
cell extraction. No HTTP, PIT publication or effective-date inference exists.
"""

import argparse
import csv
from dataclasses import asdict
import hashlib
import io
import json
import logging
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from qbot.research.config import load_config
from qbot.research.errors import ProviderError
from qbot.research.reproducibility import RunManifest
from qbot.research.universe import notice_workbook
from qbot.research.universe.notice_attachments import discover_notice_attachments
from qbot.research.universe.notice_workbook import WORKBOOK_TABLE_CONTRACT, review_csi500_workbook_values
from qbot.research.universe.official_sources import OfficialNoticeSource


LOGGER = logging.getLogger(__name__)
FIELDS = ("notice_id", "publish_date", "explicit_after_close_date", "explicit_start_date", "conditional_effective_clause",
          "action", "symbol", "name", "source_sheet", "source_row", "source_range", "workbook_sha256",
          "inspection_sha256", "status", "point_in_time_universe_verified")


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _root(value, config):
    path = Path(value)
    return (path if path.is_absolute() else config.repo_root / path).resolve()


def _write_json(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _source_sheet_names(content):
    """Bounded OOXML metadata read, without extracting any ZIP member to disk."""
    if not content.startswith(b"PK") or not 1 <= len(content) <= 20 * 1024 * 1024:
        raise ProviderError("XLSX source signature/size invalid")
    with ZipFile(io.BytesIO(content)) as archive:
        members = archive.infolist()
        if (not 1 <= len(members) <= 1000 or len({m.filename for m in members}) != len(members) or
                any(m.flag_bits & 1 for m in members) or sum(m.file_size for m in members) > 20 * 1024 * 1024):
            raise ProviderError("ambiguous, encrypted or oversized XLSX archive")
        info = archive.getinfo("xl/workbook.xml")
        if info.file_size > 1024 * 1024:
            raise ProviderError("oversized workbook metadata")
        metadata = archive.read(info)
    if b"<!DOCTYPE" in metadata.upper() or b"<!ENTITY" in metadata.upper():
        raise ProviderError("unsupported XML declarations in workbook metadata")
    root = ET.fromstring(metadata)
    names = [sheet.get("name") for sheet in root.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheets/"
                                                        "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet")]
    if not names or any(not name for name in names) or len(set(names)) != len(names):
        raise ProviderError("invalid original workbook sheet names")
    return names


def _snapshot(snapshots, name, raw):
    path = snapshots / name
    with path.open("xb") as stream:
        stream.write(raw)
    return {"path": str(path), "sha256": _digest(raw)}


def _receipt_bytes(root, artifact):
    request = json.dumps(artifact.receipt["request"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    raw = (root / (_digest(request) + ".receipt.json")).read_bytes()
    if json.loads(raw.decode("utf-8")) != artifact.receipt:
        raise ProviderError("original cache receipt changed after validation")
    return raw


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/research/csi500_5y.yaml")
    parser.add_argument("--source-archive", required=True)
    parser.add_argument("--attachment-root", required=True)
    parser.add_argument("--inspection-directory", required=True, help="Explicit <notice_id>.inspection.json files.")
    parser.add_argument("--inspector-script", required=True, help="Actual read-only extractor, checked against each inspection hash.")
    parser.add_argument("--notice-ids", type=int, nargs="+", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config(args.config)
    manifest = RunManifest(config, "review-cached-workbook-notices", vars(args))
    manifest.payload.update({"diagnostic_only": True, "point_in_time_universe_verified": False,
                             "research_conclusions_permitted": False, "historical_constituents_generated": False,
                             "effective_dates_inferred": False, "historical_availability_proved": False,
                             "extraction_completeness_independently_proved": False, "visual_review_performed_by_this_run": False,
                             "network_requests": 0, "verified_cache_reads": 0, "workbook_table_contract": WORKBOOK_TABLE_CONTRACT,
                             "parser_module_sha256": _digest(Path(notice_workbook.__file__).read_bytes()),
                             "review_script_sha256": _digest(Path(__file__).read_bytes())})
    bodies, attachments = None, None
    failures, reviews, candidates = [], [], []
    try:
        ids = args.notice_ids
        if not 1 <= len(ids) <= 20 or len(set(ids)) != len(ids) or any(i < 1 for i in ids):
            raise ProviderError("select 1 to 20 unique positive notice IDs")
        source_root, attachment_root = _root(args.source_archive, config), _root(args.attachment_root, config)
        directory, inspector = _root(args.inspection_directory, config), _root(args.inspector_script, config)
        if any(not path.is_dir() for path in (source_root, attachment_root, directory)) or not inspector.is_file():
            raise ProviderError("existing source, attachment, inspection directories and inspector script required")
        bodies = OfficialNoticeSource(source_root, cache_only=True, max_retries=0)
        attachments = OfficialNoticeSource(attachment_root, cache_only=True, max_retries=0)
        selected = [bodies.notice(number) for number in ids]
        output = config.paths.results_root / "diagnostics" / "workbook-notice-review" / manifest.run_id
        snapshots = output / "inputs"
        snapshots.mkdir(parents=True, exist_ok=False)
        inspector_bytes = inspector.read_bytes()
        extractor = _snapshot(snapshots, "inspector.mjs", inspector_bytes)
        for notice, body in selected:
            number = notice["id"]
            try:
                links = discover_notice_attachments(notice)
                if len(links) != 1 or Path(unquote(urlsplit(links[0]["fileUrl"]).path)).suffix.lower() != ".xlsx":
                    raise ProviderError("exactly one literal XLSX link required; other files/revisions need separate review")
                artifact = attachments.attachment(links[0]["fileUrl"])
                inspection_path = directory / (str(number) + ".inspection.json")
                if not 1 <= inspection_path.stat().st_size <= 10 * 1024 * 1024:
                    raise ProviderError("inspection JSON size invalid")
                raw = inspection_path.read_bytes()
                inspection = json.loads(raw.decode("utf-8"))
                if (not isinstance(inspection, dict) or inspection.get("diagnostic_only") is not True or
                        inspection.get("point_in_time_universe_verified") is not False or
                        inspection.get("input_sha256") != _digest(artifact.content) or
                        inspection.get("inspector_sha256") != _digest(inspector_bytes) or
                        not isinstance(inspection.get("input_path"), str) or
                        Path(inspection["input_path"]).resolve() != artifact.path):
                    raise ProviderError("inspection/source/extractor identity or evidence flags disagree")
                sheets = inspection.get("sheets")
                names = _source_sheet_names(artifact.content)
                if not isinstance(sheets, list) or any(not isinstance(s, dict) for s in sheets) or names != [s.get("sheet") for s in sheets]:
                    raise ProviderError("extracted worksheet inventory differs from original workbook")
                result = review_csi500_workbook_values(notice, sheets)
                evidence = {"publisher_url": links[0]["fileUrl"], "source_receipt": artifact.receipt,
                            "original_sheet_names": names, "extractor_snapshot": extractor,
                            "body": _snapshot(snapshots, "%d.notice.json" % number, body.content),
                            "body_receipt": _snapshot(snapshots, "%d.body.receipt.json" % number, _receipt_bytes(source_root, body)),
                            "workbook": _snapshot(snapshots, "%d.xlsx" % number, artifact.content),
                            "workbook_receipt": _snapshot(snapshots, "%d.workbook.receipt.json" % number, _receipt_bytes(attachment_root, artifact)),
                            "inspection": _snapshot(snapshots, "%d.inspection.json" % number, raw)}
                reviews.append({"notice_id": number, "source_evidence": evidence, "workbook_review": asdict(result)})
                for change in result.review.changes:
                    reviewed = result.review
                    candidates.append({"notice_id": number, "publish_date": reviewed.publish_date,
                                       "explicit_after_close_date": reviewed.explicit_after_close_date,
                                       "explicit_start_date": reviewed.explicit_start_date,
                                       "conditional_effective_clause": reviewed.conditional_effective_clause, **change,
                                       "workbook_sha256": _digest(artifact.content), "inspection_sha256": _digest(raw),
                                       "status": reviewed.status, "point_in_time_universe_verified": False})
                LOGGER.info("Workbook notice %d: %d candidate events; %s", number, len(result.review.changes), result.review.status)
            except Exception as exc:
                failures.append({"notice_id": number, "error": "%s: %s" % (type(exc).__name__, exc)})
                LOGGER.warning("Workbook notice %d: %s: %s", number, type(exc).__name__, exc)
        if inspector.read_bytes() != inspector_bytes:
            raise ProviderError("inspector script changed during review")
        review_path, candidate_path = output / "review.json", output / "candidate_changes.csv"
        _write_json(review_path, {"workbook_reviews": reviews, "failures": failures})
        with candidate_path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(candidates)
        manifest.add_output("review", review_path)
        manifest.add_output("candidate_changes", candidate_path)
        manifest.payload.update({"network_requests": bodies.network_requests + attachments.network_requests,
                                 "verified_cache_reads": bodies.cache_reads + attachments.cache_reads,
                                 "workbooks_reviewed": len(reviews), "candidate_change_events": len(candidates),
                                 "output_artifacts": {str(p.relative_to(output)): {"path": str(p), "sha256": _digest(p.read_bytes())}
                                     for p in sorted(output.rglob("*")) if p.is_file()}})
        manifest.add_failures("workbook_source_review", failures)
        manifest.finish("partial" if failures else "success")
        LOGGER.info("Manifest: %s; source candidates only, no HTTP or PIT publication.", manifest.path)
        return 1 if failures else 0
    except Exception as exc:
        manifest.payload.update({"network_requests": (bodies.network_requests if bodies else 0) + (attachments.network_requests if attachments else 0),
                                 "verified_cache_reads": (bodies.cache_reads if bodies else 0) + (attachments.cache_reads if attachments else 0)})
        manifest.add_failures("workbook_source_review", failures + [{"error": "%s: %s" % (type(exc).__name__, exc)}])
        manifest.finish("failed", "%s: %s" % (type(exc).__name__, exc))
        LOGGER.error("Offline workbook review failed: %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
