"""Changing PIT keys and an independent checker trust boundary; synthetic only."""

from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

import pandas as pd
import pytest

from factorlab.data.admission import assert_formal_market_admission
from factorlab.data.corporate_actions import select_events
from factorlab.data.evidence import table_sha256
from factorlab.data.market_qualification import load_qualification, publish_qualification
from factorlab.data.providers.base import MarketQualificationEvidence
from factorlab.errors import ProviderError
from tests.research.market_data_fixture import qualification_inputs


ROOT = Path(__file__).resolve().parents[2]


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def changing_pit(tmp_path_factory):
    root = tmp_path_factory.mktemp("changing-pit-software-only")
    original, _, dates, stocks, benchmark = qualification_inputs(root)
    first = dates[4]
    symbol, security_id = "000001.SZ", "synthetic-new-listing"
    raw = original.raw.loc[original.raw.date.ge(first)].assign(symbol=symbol).reset_index(drop=True)
    spec = original.adjustments["fixture-security"]
    coverage = replace(spec.view.snapshot.coverage[0], security_id=security_id,
                       start=first.date().isoformat())
    snapshot = replace(spec.view.snapshot, coverage=(coverage,))
    view = select_events(snapshot, effective_through=dates[-1].date().isoformat(),
                         knowledge_as_of=original.signal_as_of[dates[-1]])
    reference = spec.reference.loc[spec.reference.date.ge(first)].assign(symbol=symbol).reset_index(drop=True)
    new_spec = replace(spec, raw=raw, view=view,
                       anchor=replace(spec.anchor, security_id=security_id, date=first.date().isoformat()),
                       signal_as_of={day: original.signal_as_of[day] for day in dates[4:]},
                       reference=reference,
                       contract=replace(spec.contract, reference_sha256=table_sha256(reference)))
    new_identity = replace(original.identity.intervals[0], security_id=security_id, symbol=symbol,
                           start=first.date().isoformat(), listing_date=first.date().isoformat())
    new_units = replace(original.units[0], symbol=symbol, start=first.date().isoformat())
    keys = raw[["date", "symbol"]]
    inputs = replace(
        original, raw=pd.concat([original.raw, raw], ignore_index=True),
        turnover=pd.concat([original.turnover, keys.assign(turnover=0.01)], ignore_index=True),
        identity=replace(original.identity, intervals=original.identity.intervals + (new_identity,)),
        units=original.units + (new_units,),
        adjustments=dict(original.adjustments, **{security_id: new_spec}),
        status=pd.concat([original.status, keys.assign(trade_status=1, is_st=False)], ignore_index=True),
        listings=pd.concat([original.listings, pd.DataFrame({"symbol": [symbol], "listing_date": [first]})], ignore_index=True),
        membership=pd.concat([original.membership.loc[original.membership.date.lt(first)],
                              keys.assign(is_member=True)], ignore_index=True),
    )
    stocks = pd.concat([stocks, stocks.loc[stocks.date.ge(first)].assign(symbol=symbol)], ignore_index=True)
    stocks = stocks.sort_values(["date", "symbol"]).reset_index(drop=True)
    required = inputs.raw[["date", "symbol"]].copy()
    manifest = publish_qualification(inputs, required, dates, stocks=stocks, benchmark=benchmark,
                                     output_root=root / "qualification")
    report = load_qualification(manifest, stocks=stocks, benchmark=benchmark)[0]
    assert all(report.stages[name] == "PASS" for name in ("RAW", "ADJUSTMENT", "STATUS", "PIT")), report.issues
    assert report.synthetic_only and not report.formal_admission
    evidence = MarketQualificationEvidence(manifest, hashlib.sha256(manifest.read_bytes()).hexdigest(),
                                           inputs.source_version, stocks, benchmark)
    return inputs, dates, stocks, benchmark, evidence


def pit_request(changing_pit):
    inputs, dates, stocks, benchmark, evidence = changing_pit
    return SimpleNamespace(get_research_qualification=lambda: evidence), dict(
        membership=inputs.membership, calendar=dates, bars=stocks, benchmark=benchmark,
        research_start=dates[0], research_end=dates[-1], status=inputs.status, listings=inputs.listings,
    )


def test_mid_calendar_listing_and_pit_change_reach_financial_gate_without_fabricated_rows(changing_pit):
    provider, request = pit_request(changing_pit)
    # The valid engineering/PIT shape must reach the unchanged financial refusal.
    # This never turns synthetic evidence into a financial positive control.
    with pytest.raises(ProviderError, match="SOFTWARE_ONLY"):
        assert_formal_market_admission(provider, **request)


@pytest.mark.parametrize("mutation", ["missing_price", "missing_status", "extra_price", "extra_status", "price", "status", "listing"])
def test_sparse_qualified_keys_still_reject_missing_changed_and_prelisting_rows(changing_pit, mutation):
    provider, request = pit_request(changing_pit)
    if mutation.startswith("missing"):
        field = "bars" if mutation.endswith("price") else "status"
        request[field] = request[field].iloc[:-1]
    elif mutation.startswith("extra"):
        field = "bars" if mutation.endswith("price") else "status"
        extra = request[field].loc[request[field].symbol.eq("000001.SZ")].iloc[:1].assign(date=request["calendar"][0])
        request[field] = pd.concat([request[field], extra], ignore_index=True)
    elif mutation == "price":
        request["bars"] = request["bars"].assign(adj_close=1.0)
    elif mutation == "status":
        request["status"] = request["status"].assign(is_st=True)
    else:
        request["listings"] = request["listings"].assign(listing_date=pd.Timestamp("1900-01-01"))
    with pytest.raises(ProviderError) as error:
        assert_formal_market_admission(provider, **request)
    assert "SOFTWARE_ONLY" not in str(error.value)


def forged_revision(tmp_path):
    security = module_at("original_review_security", ROOT / ".github/scripts/ci/security_admission_revision.py")
    candidate = tmp_path / "candidate"
    subprocess.check_call(["git", "clone", "--shared", "--no-checkout", str(ROOT), str(candidate)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    checker = ".github/scripts/ci/security_admission_revision.py"
    controller = module_at("original_independent_control", ROOT / ".github/scripts/ci/research_checks.py")
    for name in security.FILES | {security.RECEIPT, checker, ".gitattributes", controller.SECURITY_ANCHOR, controller.SECURITY_WORKFLOW} | set(controller.SECURITY_CONTROL_FILES):
        target = candidate / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    changed = candidate / "qbot/research/cli.py"
    changed.write_bytes(changed.read_bytes() + b"\n# unauthorized self-approved change\n")
    receipt = candidate / security.RECEIPT
    payload = json.loads(receipt.read_bytes())
    payload["files"]["qbot/research/cli.py"]["approved_blob"] = security.blob(candidate, "qbot/research/cli.py")
    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    receipt.write_bytes(content)
    path = candidate / checker
    code = re.sub(r'RECEIPT_SHA256 = .*', 'RECEIPT_SHA256 = ' + repr(hashlib.sha256(content).hexdigest()),
                  path.read_text(encoding="utf-8"))
    path.write_text(code, encoding="utf-8")
    return candidate


def test_checker_and_receipt_cannot_jointly_self_authorize(tmp_path):
    candidate = forged_revision(tmp_path)
    controller = module_at("independent_review_controller", ROOT / ".github/scripts/ci/research_checks.py")
    with pytest.raises(ValueError, match="Independent security checker pin mismatch"):
        controller.load_security_revision(candidate)


@pytest.mark.parametrize("mutation", ["append", "execute", "delete"])
def test_changed_checker_is_rejected_before_any_candidate_code_executes(tmp_path, mutation):
    candidate = forged_revision(tmp_path)
    checker = candidate / ".github/scripts/ci/security_admission_revision.py"
    sentinel = candidate / "UNTRUSTED_EXECUTED"
    if mutation == "delete":
        checker.unlink()
    elif mutation == "execute":
        checker.write_text("from pathlib import Path\nPath(" + repr(str(sentinel)) + ").touch()\n", encoding="utf-8")
    else:
        checker.write_bytes(checker.read_bytes() + b"\n# modified checker\n")
    controller = module_at("independent_preexecution_controller", ROOT / ".github/scripts/ci/research_checks.py")
    with pytest.raises(ValueError, match="Independent security checker pin mismatch"):
        controller.load_security_revision(candidate)
    assert not sentinel.exists()


@pytest.mark.parametrize("mutation", ["controller", "anchor", "joint_reanchor"])
def test_independent_controls_cannot_be_authorized_by_resealing_the_candidate(tmp_path, mutation):
    candidate = forged_revision(tmp_path)
    controller = module_at("reviewed_external_control", ROOT / ".github/scripts/ci/research_checks.py")
    anchor = candidate / controller.SECURITY_ANCHOR
    if mutation == "controller":
        control = candidate / ".github/scripts/ci/research_checks.py"
        control.write_bytes(control.read_bytes() + b"\n# altered independent control\n")
    else:
        payload = json.loads(anchor.read_bytes())
        for name in payload["files"]:
            payload["files"][name] = hashlib.sha256((candidate / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        content = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        anchor.write_bytes(content)
        if mutation == "joint_reanchor":
            workflow = candidate / controller.SECURITY_WORKFLOW
            workflow.write_text(re.sub(r"SECURITY_TRUST_ANCHOR_SHA256: '[0-9a-f]{64}'",
                                      "SECURITY_TRUST_ANCHOR_SHA256: '" + hashlib.sha256(content).hexdigest() + "'",
                                      workflow.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(ValueError, match="Independent security checker pin mismatch"):
        controller.load_security_revision(candidate)
