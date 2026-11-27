"""Cloud launcher artifact retention without allocating a runtime."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest


def test_failed_execution_bundles_completed_artifacts(tmp_path,monkeypatch):
    source=Path(__file__).resolve().parents[2]/'tools/research/colab_mc_colab_02.py'
    spec=importlib.util.spec_from_file_location('colab_study_launcher',source)
    launcher=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    output=tmp_path/'result'
    monkeypatch.setattr(sys,'argv',[str(source),'--commit','revision','--output',str(output)])
    is_dir=Path.is_dir
    monkeypatch.setattr(Path,'is_dir',lambda p: p==Path('/content') or is_dir(p))
    commands=[]

    def command(*args,**kwargs):
        commands.append(args)
        if '--execute' in args:
            (output/'stages.json').write_text(json.dumps([
                {'stage':'gpu-reliability','status':'COMPLETED'},
                {'stage':'gpu-scale','status':'FAILED'}]))
            (output/'completed-study.json').write_text('{"rows": [1]}')
            raise subprocess.CalledProcessError(1,args)

    monkeypatch.setattr(launcher,'command',command)
    with pytest.raises(subprocess.CalledProcessError):
        launcher.main()
    assert commands[2][-1].endswith('[monte-carlo-cuda,monte-carlo-benchmark]')
    report=json.loads((output/'report.json').read_text())
    assert report['stages'][-1]['status']=='FAILED'
    assert report['environment'] is None
    with np.load(output/'samples.npz',allow_pickle=False) as archive:
        key=next(k for k,v in report['manifest'].items() if v=='completed-study.json')
        assert json.loads(archive[key].tobytes())=={'rows':[1]}
