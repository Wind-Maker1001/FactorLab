"""Compact evidence remains publishable after moving raw study directories."""
import importlib.util
import json
from pathlib import Path

import numpy as np


def test_compact_evidence_uses_current_study_location(tmp_path):
    path = Path(__file__).resolve().parents[2] / 'tools/research/report_mc05_f06.py'
    spec = importlib.util.spec_from_file_location('f06_report', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / 'moved-study'
    directory = source / 'gbm-1729-168'
    case_directory = directory / 'paper-audits/path-3'
    case_directory.mkdir(parents=True)
    report = dict(status='COMPLETE', steps=168, models=['gbm'],
                  completed_cells=[dict(model='gbm', seed=1729)])
    (source / 'report.json').write_text(json.dumps(report))
    (source / 'definition.json').write_text('{}')
    (directory / 'manifest.json').write_text('{}')
    (directory / 'paper-audits/audit.json').write_text(json.dumps(dict(cases=[
        dict(path_id=3, prices_file='missing-old-drive/prices.npy',
             paper_directory='missing-old-drive/paper')
    ])))
    prices = np.array([[100., 101.]])
    np.save(case_directory / 'prices.npy', prices)
    terminal = np.zeros((64, len(module.COLUMNS)))
    terminal[:, module.COLUMNS.index('equity')] = np.arange(64) + 950
    np.save(directory / 'terminal.npy', terminal)
    destination = tmp_path / 'published'
    _, cases, _ = module.compact_evidence(source, destination)
    np.testing.assert_array_equal(
        np.load(destination / 'selected-prices/gbm-1729-168/path-3.npy'), prices)
    assert cases[0]['prices_file'] == str((case_directory / 'prices.npy').resolve())
    assert cases[0]['paper_directory'] == str((case_directory / 'paper').resolve())
