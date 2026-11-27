"""Explicit installed-wheel CPU/CUDA acceptance; outside portable test discovery."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--grid-definition', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    inputs = args.output/'input'
    inputs.mkdir()
    shutil.copyfile(args.grid_definition, inputs/'grid.json')
    tasks = []
    for device in ('cpu', 'local_gpu'):
        for entry in ('gbm', 'bootstrap', 'static_grid'):
            parameters = dict(paths=100000 if entry == 'static_grid' else 1000000,
                              batch_size=4096, seed=1729)
            if entry != 'static_grid':
                parameters.update(steps=252, assets=1)
            tasks.append(dict(id=device+'-'+entry, entry=entry, parameters=parameters,
                inputs={'definition':'grid.json'} if entry == 'static_grid' else {}, device=device,
                memory_budget_mb=8192, gpu_memory_limit_mb=4096, timeout_seconds=120,
                outputs=['report.json', 'terminal.npy' if entry == 'static_grid' else 'samples.npz']))
    # A declared unavailable output exercises a real worker failure after numerical
    # results exist; a following independent job must still complete.
    failure = dict(tasks[0], id='retained-failure', parameters=dict(paths=100,steps=8,assets=1,batch_size=100),
                   outputs=['report.json', 'deliberately-missing.npy'])
    final = dict(tasks[0], id='after-failure', parameters=dict(paths=100,steps=8,assets=1,batch_size=100))
    tasks.extend((failure, final))
    manifest = dict(schema_version=1,commit=args.commit,timeout_seconds=900,budget_cu=0,tasks=tasks)
    path = inputs/'manifest.json'
    path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    command = [sys.executable,'-I','-m','factorlab.experiments.research_batch']
    with (args.output/'selection.json').open('w',encoding='utf-8') as log:
        subprocess.run([*command,'plan','--manifest',str(path)],stdout=log,check=True)
    result = subprocess.run([*command,'local','--manifest',str(path),'--output',str(args.output/'batch')])
    if result.returncode != 1:
        raise RuntimeError('Partial-success acceptance must return nonzero')
    report=json.loads((args.output/'batch/report.json').read_text(encoding='utf-8'))
    states=[o['state'] for o in report['outcomes']]
    if states != ['SUCCEEDED']*6+['FAILED','SUCCEEDED']:
        raise RuntimeError('Unexpected real-run outcomes: '+str(states))
    pids=[json.loads((args.output/'batch'/t['id']/'environment.json').read_text())['pid'] for t in tasks]
    if len(set(pids)) != len(pids):
        raise RuntimeError('Workers did not use independent processes')
    if not (args.output/'batch/retained-failure/samples.npz').is_file():
        raise RuntimeError('Failed worker lost completed numerical outputs')
    print(json.dumps(dict(state=report['state'],outcomes=states,pids=pids)))


if __name__=='__main__':
    main()
