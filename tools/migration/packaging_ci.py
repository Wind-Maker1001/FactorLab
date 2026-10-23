"""Install wheel and sdist in separate core-only environments, outside source cwd."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT=Path(__file__).resolve().parents[2]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('distributions',type=Path)
    p.add_argument('output',type=Path)
    p.add_argument('--uv',type=Path,help='Optional local installer; default CI uses standard pip')
    args=p.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    scripts=('packaging_ci.py','inspect_distributions.py','installed_smoke.py','scan_distribution.py')
    script_hashes=lambda: {name:hashlib.sha256((ROOT/'tools/migration'/name).read_bytes()).hexdigest() for name in scripts}
    before=script_hashes()
    subprocess.run([sys.executable,str(ROOT/'tools/migration/inspect_distributions.py'),
                    str(args.distributions.resolve())],check=True,cwd=ROOT.parent)
    reports=[]
    for suffix in ('.whl','.tar.gz'):
        artifact=next(args.distributions.resolve().glob('*'+suffix))
        kind='wheel' if suffix=='.whl' else 'sdist'
        environment=output/kind
        venv.EnvBuilder(with_pip=True).create(environment)
        binary=environment/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        installer=([str(args.uv.resolve()),'pip','install','--python',str(binary)] if args.uv else
                   [str(binary),'-m','pip','install'])
        commands=[
            installer+['-r',str(ROOT/'requirements/py314-core.txt')],
            installer+['--no-deps',str(artifact)],
            [str(binary),'-m','pip','check'],
            [str(binary),'-I',str(ROOT/'tools/migration/installed_smoke.py'),str(output/(kind+'-smoke.json'))],
            [str(binary),'-I','-m','factorlab.cli','--help'],
            [str(binary),'-I','-m','factorlab.cli','demo','--output',str(output/(kind+'-demo'))],
        ]
        with (output/(kind+'.log')).open('w',encoding='utf-8') as log:
            for command in commands:
                subprocess.run(command,cwd=ROOT.parent,stdout=log,stderr=subprocess.STDOUT,check=True)
        reports.append({'kind':kind,'artifact':artifact.name,'status':'PASS'})
    subprocess.run([sys.executable,str(ROOT/'tools/migration/scan_distribution.py'),
                    str(args.distributions.resolve()),str(output/'publication-scan.json')],check=True,cwd=ROOT.parent)
    after=script_hashes()
    if before!=after:
        raise RuntimeError('packaging validation scripts changed during execution')
    (output/'receipt.json').write_text(json.dumps({'status':'PASS','python':sys.version,'runs':reports,
        'validation_scripts_unchanged':True,'validation_scripts_sha256':after,
        'installer':'uv' if args.uv else 'pip',
        'cwd_outside_source':True,'network_data_downloads':False,
        'formal_admission':'FORMAL_ADMISSION_BLOCKED'},indent=2),encoding='utf-8')
    print('PASS: wheel/sdist core-only installation, imports, CLI, demo and archive scan')


if __name__=='__main__':
    main()
