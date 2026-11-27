"""Dispatch registered tasks into one local batch and one bounded Colab job."""

from copy import deepcopy
from importlib.resources import files
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
from time import monotonic

from factorlab.experiments.research_batch import run_batch, save, unpack_results
from factorlab.experiments.research_selection import probe_resources, select_task
from factorlab.experiments.research_tasks import input_path, load_manifest
from factorlab.experiments.research_workloads import admission_task


def wsl_path(path):
    path = PureWindowsPath(str(path))
    if not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ':':
        raise ValueError('WSL bridge requires an absolute Windows drive path')
    return '/mnt/' + path.drive[0].lower() + '/' + '/'.join(path.parts[1:])


def stage(manifest, selected, root, source):
    root.mkdir()
    derived = deepcopy(manifest)
    derived['tasks'] = selected
    for task in selected:
        for name in task['inputs'].values():
            if name == 'manifest.json':
                raise ValueError('Input conflicts with staged manifest.json')
            origin = input_path(source, name)
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(origin, target)
    path = root / 'manifest.json'
    save(path, derived)
    return path


def dispatch(manifest_path, output, wheel=None, cloud=None, wsl_distro=None,
             controller_python=None, colab_executable='colab', gpu='L4'):
    manifest_path = Path(manifest_path).resolve()
    manifest = load_manifest(manifest_path)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = monotonic()
    report = dict(state='RUNNING', placements=[], local={'state': 'NOT_STARTED'},
                  cloud={'state': 'NOT_STARTED'})
    journal = output / 'state.json'
    save(journal, report)
    try:
        groups = {'local': [], 'cloud': []}
        for task in manifest['tasks']:
            if task['device'] == 'colab':
                chosen = {'device': 'colab', 'reason': 'explicit Colab; controller validates live account'}
            else:
                metadata = dict(cloud, budget_cu=manifest['budget_cu']) if cloud is not None else None
                chosen = select_task(admission_task(task, manifest_path.parent),
                    probe_resources(include_gpu=task['device'] != 'cpu'), metadata)
                if chosen['device'] == 'colab' and (not metadata or metadata.get('authorized') is not True):
                    raise ValueError('Automatic Colab placement requires authorization')
            selected = dict(task, device=chosen['device'])
            groups['cloud' if chosen['device'] == 'colab' else 'local'].append(selected)
            report['placements'].append(dict(id=task['id'], selection=chosen))
        if groups['cloud'] and wheel is None:
            raise ValueError('A built wheel is required for Colab dispatch')
        if groups['cloud'] and os.name == 'nt' and not wsl_distro:
            raise ValueError('Windows Colab dispatch requires an explicit WSL distribution')
        save(journal, report)
        if groups['local']:
            local_manifest = stage(manifest, groups['local'], output / 'local-input', manifest_path.parent)
            report['local']['state'] = 'RUNNING'
            save(journal, report)
            try:
                report['local'] = run_batch(local_manifest, output / 'local',
                    timeout=manifest['timeout_seconds'] - (monotonic() - started))
            except Exception as exc:
                report['local'] = dict(state='FAILED', error=f'{type(exc).__name__}: {exc}')
            save(journal, report)
        if groups['cloud']:
            remaining = manifest['timeout_seconds'] - (monotonic() - started)
            if remaining <= 120:
                report['cloud'] = dict(state='NOT_STARTED', reason='DISPATCH_TIME_BUDGET')
            else:
                derived = dict(manifest, timeout_seconds=remaining)
                cloud_manifest = stage(derived, groups['cloud'], output / 'cloud-input', manifest_path.parent)
                controller = Path(str(files('factorlab').joinpath('resources/colab_job.py')))
                job = output / 'cloud-job'
                translate = wsl_path if os.name == 'nt' else lambda path: str(path)
                command = [controller_python or ('python3' if os.name == 'nt' else sys.executable),
                    translate(controller), 'submit', '--wait', '--job', translate(job),
                    '--wheel', translate(Path(wheel).resolve()), '--manifest', translate(cloud_manifest),
                    '--colab', colab_executable, '--gpu', gpu]
                if os.name == 'nt':
                    command = ['wsl.exe', '--distribution', wsl_distro, '--exec', *command]
                report['cloud'] = dict(state='RUNNING', job=str(job))
                save(journal, report)
                try:
                    # The supervised unit owns its deadline and cleanup. Allow its
                    # bounded stop/recovery reserve before timing out the bridge.
                    subprocess.run(command, check=True, timeout=remaining+150)
                    report['cloud']['state'] = 'SUCCEEDED'
                except (OSError, subprocess.SubprocessError) as exc:
                    report['cloud'].update(state='FAILED', error=f'{type(exc).__name__}: {exc}')
                finally:
                    if (job / 'job.json').is_file():
                        report['cloud']['controller'] = json.loads((job / 'job.json').read_text(encoding='utf-8'))
                    results = job / 'results'
                    if (results / 'report.json').is_file() and (results / 'samples.npz').is_file():
                        report['cloud']['batch'] = unpack_results(results, output / 'cloud')
                        controller_state = report['cloud'].get('controller', {})
                        if (report['cloud']['batch']['state'] == 'PARTIAL'
                                and controller_state.get('released') is True):
                            report['cloud']['state'] = 'PARTIAL'
            save(journal, report)
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        states = [report[key]['state'] for key in ('local', 'cloud')]
        active = [report[key]['state'] for key in ('local', 'cloud')
                  if any(p['selection']['device'] == 'colab' if key == 'cloud'
                         else p['selection']['device'] != 'colab' for p in report['placements'])]
        report['state'] = ('FAILED' if 'error' in report else
            'SUCCEEDED' if active and all(s == 'SUCCEEDED' for s in active) else
            'PARTIAL' if any(s in ('SUCCEEDED', 'PARTIAL') for s in states) else 'FAILED')
        report['elapsed_seconds'] = monotonic() - started
        save(journal, report)
    return report
