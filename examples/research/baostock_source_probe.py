"""Supervised, anonymous BaoStock 0.9.4 historical-response diagnostic.

This does NOT certify point-in-time index membership.  Matching the requested
date and 500 unique codes proves only the narrow query response contract; an
``updateDate`` may be an announcement rather than a membership-effective date.
No formal universe, status, market cache, model, or order is written.

The published wheel is zip-imported only in an isolated child.  Its SDK has no
native deadline and may loop when recv returns empty bytes, so the parent uses
subprocess.run(timeout=...) to kill and reap a stalled child.  Installed 0.8.9,
proxy settings, sockets, accounts and API keys are never modified.
"""

import argparse
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from email.parser import BytesParser
import hashlib
import importlib.metadata
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, Optional, Sequence, Tuple
import zipfile

from qbot.research.config import ResearchConfig, load_config
from qbot.research.errors import ConfigurationError, DataQualityError, FactorLabError
from qbot.research.reproducibility import RunManifest, set_random_seed


SDK_VERSION = "0.9.4"
SDK_CLIENT_VERSION = "00.9.40"
PUBLISHED_WHEEL_SHA256 = "0bf71c6069ab5890ff3596632f9c3f8f1fbc6bfcac582c2f9d6a5c11ab2cfaf8"
DEFAULT_WHEEL = "data/research/diagnostics/baostock-sdk/baostock-0.9.4-py3-none-any.whl"
DEFAULT_DATES = ("2023-12-08", "2023-12-11", "2024-01-02")
PUBLIC_HOST = "public-api.baostock.com"
PUBLIC_PORT = 10030
SOURCE_URLS = ("https://pypi.org/project/baostock/0.9.4/",
               "https://pypi.org/pypi/baostock/0.9.4/json")
WARNING = "DIAGNOSTIC ONLY: historical query responses are not verified point-in-time membership; no research conclusions."
LOGGER = logging.getLogger(__name__)


class _DiscardOutput(io.TextIOBase):
    def write(self, text: str) -> int:
        return len(text)


def _installed_version() -> Optional[str]:
    try:
        return importlib.metadata.version("baostock")
    except importlib.metadata.PackageNotFoundError:
        return None


def _dates(values: Sequence[str]) -> Tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not 2 <= len(values) <= 4:
        raise ConfigurationError("Provide 2 to 4 distinct historical dates")
    result = []
    for value in values:
        try:
            parsed = date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("Probe dates must use YYYY-MM-DD") from exc
        if parsed.isoformat() != value or parsed > date.today():
            raise ConfigurationError("Probe dates must be canonical historical YYYY-MM-DD dates")
        result.append(value)
    if len(set(result)) != len(result):
        raise ConfigurationError("Probe dates must be distinct")
    return tuple(result)


def _verify_wheel(path: Path) -> Dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise DataQualityError("Probe SDK wheel must be a regular local file")
    content = path.read_bytes()
    actual = hashlib.sha256(content).hexdigest()
    if actual != PUBLISHED_WHEEL_SHA256:
        raise DataQualityError("Probe SDK wheel does not match the pinned publisher SHA-256")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            name = "baostock-0.9.4.dist-info/METADATA"
            if archive.namelist().count(name) != 1:
                raise ValueError("missing or duplicated package metadata")
            metadata = BytesParser().parsebytes(archive.read(name))
            if (metadata.get_all("Name") != ["baostock"]
                    or metadata.get_all("Version") != [SDK_VERSION]):
                raise ValueError("wrong package/version")
    except (ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
        raise DataQualityError("Probe SDK wheel metadata is not BaoStock 0.9.4") from exc
    return {"version": SDK_VERSION, "sha256": actual, "path": str(path)}


def _raw_query(sdk: Any, requested_date: str) -> Dict[str, Any]:
    result = sdk.query_zz500_stocks(date=requested_date)
    fields = list(result.fields)
    rows = []
    while result.error_code == "0" and result.next():
        rows.append(list(result.get_row_data()))
        if len(rows) > 10000:
            raise DataQualityError("Unexpectedly large diagnostic constituent response")
    return {
        "requested_date": requested_date, "server_date": getattr(result, "date", None),
        "fields": fields, "rows": rows,
        "error_code": result.error_code, "error_message": result.error_msg,
    }


def _child_probe(wheel: Path, dates: Sequence[str]) -> Dict[str, Any]:
    """No installed SDK import; no credential/log capture or request patches."""
    _verify_wheel(wheel)  # Recheck immediately before executing imported bytes.
    sys.path.insert(0, str(wheel))
    payload = {
        "schema_version": 1, "diagnostic_only": True,
        "point_in_time_universe_verified": False, "sdk_version": SDK_VERSION,
        "sdk_client_version": SDK_CLIENT_VERSION, "sdk_import_path": None,
        "server": {"host": PUBLIC_HOST, "port": PUBLIC_PORT},
        "login": None, "queries": [], "child_exception": None,
    }
    sdk_context = None
    # SDK console output can contain protocol/debug text.  Discard it entirely:
    # it is neither JSON nor a safe evidence/credential collection mechanism.
    with redirect_stdout(_DiscardOutput()), redirect_stderr(_DiscardOutput()):
        try:
            import baostock as sdk
            import baostock.common.contants as constants
            import baostock.common.context as sdk_context
            payload["sdk_import_path"] = sdk.__file__
            expected = (str(wheel) + "/baostock/__init__.py").replace("\\", "/")
            if (os.path.normcase(str(sdk.__file__).replace("\\", "/")) != os.path.normcase(expected)
                    or constants.BAOSTOCK_CLIENT_VERSION != SDK_CLIENT_VERSION
                    or constants.BAOSTOCK_SERVER_IP != PUBLIC_HOST
                    or constants.BAOSTOCK_SERVER_PORT != PUBLIC_PORT
                    or getattr(sdk_context, "apiKey", "0") != "0"):
                raise DataQualityError("Child SDK is not the pinned public anonymous source")
            login = sdk.login()  # Publisher's anonymous default, no arguments/key.
            payload["login"] = {"error_code": login.error_code, "error_message": login.error_msg}
            if login.error_code == "0":
                for value in dates:
                    payload["queries"].append(_raw_query(sdk, value))
        except Exception as exc:
            # Never serialize an exception message containing possible protocol
            # or credential text.  SDK response error fields remain separate.
            payload["child_exception"] = type(exc).__name__
        finally:
            # Close only this process's owned connection, without another
            # unbounded logout RPC, socket monkeypatch or timeout modification.
            connection = getattr(sdk_context, "default_socket", None)
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    payload["child_exception"] = payload["child_exception"] or "SocketCleanupError"
    return payload


def _validated_response(payload: Any, dates: Sequence[str], wheel: Path) -> Dict[str, Any]:
    keys = {"schema_version", "diagnostic_only", "point_in_time_universe_verified",
            "sdk_version", "sdk_client_version", "sdk_import_path", "server",
            "login", "queries", "child_exception"}
    if (not isinstance(payload, dict) or set(payload) != keys
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or payload["diagnostic_only"] is not True
            or payload["point_in_time_universe_verified"] is not False
            or payload["sdk_version"] != SDK_VERSION or payload["sdk_client_version"] != SDK_CLIENT_VERSION
            or payload["server"] != {"host": PUBLIC_HOST, "port": PUBLIC_PORT}):
        raise DataQualityError("Invalid or non-diagnostic child response contract")
    expected = (str(wheel) + "/baostock/__init__.py").replace("\\", "/")
    imported = payload["sdk_import_path"]
    if not isinstance(imported, str) or os.path.normcase(imported.replace("\\", "/")) != os.path.normcase(expected):
        raise DataQualityError("Child did not import the pinned wheel SDK")
    login = payload["login"]
    if (not isinstance(login, dict) or set(login) != {"error_code", "error_message"}
            or any(not isinstance(value, str) for value in login.values())):
        raise DataQualityError("Invalid anonymous login response")
    queries = payload["queries"]
    if not isinstance(queries, list) or len(queries) > len(dates):
        raise DataQualityError("Invalid diagnostic query count")
    if payload["child_exception"] is not None and not isinstance(payload["child_exception"], str):
        raise DataQualityError("Invalid child exception field")
    summaries = []
    for position, query in enumerate(queries):
        if (not isinstance(query, dict)
                or set(query) != {"requested_date", "server_date", "fields", "rows", "error_code", "error_message"}
                or query["requested_date"] != dates[position]
                or not isinstance(query["error_code"], str) or not isinstance(query["error_message"], str)
                or not isinstance(query["server_date"], (str, type(None)))):
            raise DataQualityError("Invalid historical query response fields")
        fields, rows = query["fields"], query["rows"]
        if (not isinstance(fields, list) or any(not isinstance(item, str) for item in fields)
                or len(set(fields)) != len(fields) or not isinstance(rows, list) or len(rows) > 10000
                or any(not isinstance(row, list) or len(row) != len(fields)
                       or any(not isinstance(item, str) for item in row) for row in rows)):
            raise DataQualityError("Invalid complete constituent response rows")
        codes = [row[fields.index("code")] for row in rows] if "code" in fields else []
        updates = Counter(row[fields.index("updateDate")] for row in rows) if "updateDate" in fields else Counter()
        unique = sorted(set(codes))
        digest = hashlib.sha256(("\n".join(unique) + "\n").encode("utf-8")).hexdigest()
        verified = (query["error_code"] == "0" and query["server_date"] == query["requested_date"]
                    and len(rows) == 500 and len(unique) == 500
                    and all(re.fullmatch(r"(?:sh|sz)\.[0-9]{6}", value) for value in codes))
        summaries.append(dict(query, row_count=len(rows), unique_member_count=len(unique),
                              update_date_distribution=dict(sorted(updates.items())),
                              members_sha256=digest, query_contract_verified=verified,
                              point_in_time_universe_verified=False))
    complete = (login["error_code"] == "0" and payload["child_exception"] is None
                and len(summaries) == len(dates)
                and all(item["query_contract_verified"] for item in summaries))
    return dict(payload, queries=summaries, query_contract_verified=complete,
                effective_membership_dates_verified=False)


def _write_diagnostic(path: Path, payload: Dict[str, Any]) -> str:
    content = (json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(content).hexdigest()


def run_probe(config: ResearchConfig, *, sdk_wheel: Path,
              dates: Sequence[str] = DEFAULT_DATES, timeout_seconds: float = 20.0) -> Dict[str, Any]:
    """Read-only source diagnostic; subprocess injection belongs only in tests."""
    selected_dates = _dates(dates)
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60):
        raise ConfigurationError("timeout_seconds must be finite, positive and at most 60")
    root = config.repo_root or Path.cwd()
    wheel = Path(sdk_wheel)
    wheel = wheel if wheel.is_absolute() else root / wheel
    wheel = Path(os.path.abspath(os.fspath(wheel)))
    set_random_seed(config.seed)
    run = RunManifest(config, "baostock-source-probe", {
        "sdk_wheel": str(wheel), "dates": list(selected_dates), "timeout_seconds": float(timeout_seconds),
    })
    safeguards = {
        "diagnostic_only": True, "research_conclusions_permitted": False,
        "point_in_time_universe_verified": False, "effective_membership_dates_verified": False,
        "query_contract_verified": False, "installed_baostock_version": _installed_version(),
        "sdk_wheel": {"version": SDK_VERSION, "published_sha256": PUBLISHED_WHEEL_SHA256},
        "source_urls": list(SOURCE_URLS), "public_server": {"host": PUBLIC_HOST, "port": PUBLIC_PORT},
        "network_controls": {"anonymous_default_login": True, "api_key_used": False,
                             "proxy_settings_modified": False, "socket_settings_modified": False,
                             "subprocess_deadline_seconds": float(timeout_seconds),
                             "sdk_console_output_persisted": False}, "warning": WARNING,
    }
    run.payload.update(safeguards)
    run.add_output("diagnostic_directory", run.path.parent)  # Persist before any SDK/network call.
    diagnostic = dict(safeguards, schema_version=1, run_id=run.run_id, requested_dates=list(selected_dates))
    diagnostic_path = run.path.parent / "diagnostic.json"
    failure = None
    try:
        verified_wheel = _verify_wheel(wheel)
        diagnostic["sdk_wheel"].update(verified_wheel)
        arguments = [sys.executable, "-m", "examples.research.baostock_source_probe",
                     "--child", "--sdk-wheel", str(wheel)]
        for requested in selected_dates:
            arguments.extend(("--date", requested))
        environment = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        child = subprocess.run(arguments, cwd=str(root), env=environment,
                               capture_output=True, text=True, encoding="utf-8",
                               timeout=float(timeout_seconds), check=False)
        if child.returncode != 0:
            raise DataQualityError("Isolated BaoStock diagnostic child failed; raw console output not persisted")
        try:
            response = json.loads(child.stdout)
        except (TypeError, ValueError) as exc:
            raise DataQualityError("Diagnostic child did not return one valid JSON response") from exc
        diagnostic["response"] = _validated_response(response, selected_dates, wheel)
        diagnostic["query_contract_verified"] = diagnostic["response"]["query_contract_verified"]
        if not diagnostic["query_contract_verified"]:
            raise DataQualityError("Historical query contract not verified; this is not point-in-time membership proof")
    except subprocess.TimeoutExpired:
        diagnostic["child_timeout"] = {"timeout_seconds": float(timeout_seconds), "child_killed_and_reaped": True}
        failure = DataQualityError("BaoStock diagnostic subprocess deadline exceeded; child killed and reaped")
    except (OSError, DataQualityError) as exc:
        failure = exc
    diagnostic["status"] = "failed" if failure else "success"
    if failure:
        diagnostic["error_type"] = type(failure).__name__
    digest = _write_diagnostic(diagnostic_path, diagnostic)
    run.payload["query_contract_verified"] = diagnostic["query_contract_verified"]
    run.payload["probe"] = {"status": diagnostic["status"], "query_contract_verified": diagnostic["query_contract_verified"],
                            "diagnostic_sha256": digest, "point_in_time_universe_verified": False}
    run.add_output("diagnostic", diagnostic_path)
    if failure:
        run.add_failures("probe", {"error_type": type(failure).__name__})
        run.finish("failed", "BaoStock source probe failed; see sanitized diagnostic evidence")
        raise failure
    run.finish()
    return {"manifest_path": str(run.path), "manifest": run.payload,
            "diagnostic_path": str(diagnostic_path), "diagnostic": diagnostic}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/research/csi500_smoke.yaml")
    parser.add_argument("--sdk-wheel", default=DEFAULT_WHEEL)
    parser.add_argument("--date", action="append", dest="dates")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        try:
            response = _child_probe(Path(os.path.abspath(args.sdk_wheel)), _dates(args.dates or DEFAULT_DATES))
            print(json.dumps(response, ensure_ascii=False, allow_nan=False))
            return 0
        except Exception:
            # No raw exception/log output: parent records a sanitized exit failure.
            return 1
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        result = run_probe(load_config(args.config), sdk_wheel=Path(args.sdk_wheel),
                           dates=args.dates or DEFAULT_DATES, timeout_seconds=args.timeout_seconds)
    except (FactorLabError, OSError) as exc:
        LOGGER.error("Source probe failed (%s); not a PIT universe", type(exc).__name__)
        return 1
    LOGGER.info("Diagnostic contract checked; PIT remains unverified: %s", result["manifest_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
