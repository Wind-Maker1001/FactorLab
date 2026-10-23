"""Verify final local HEAD against completed jobs; write an external-to-Git receipt."""
import hashlib
import json
from pathlib import Path
import runpy
import subprocess

ROOT=Path(__file__).resolve().parents[2]


def main():
    current=runpy.run_path(str(ROOT/'tools/ci/run_checks.py'),run_name='final_binding_catalog')['validation_inputs']()
    jobs=[]
    for name in ('runtime314-final-r4','runtime313-final-r3','full314-final-r3'):
        receipt=json.loads((ROOT/'local-ci'/name/'receipt.json').read_text(encoding='utf-8'))
        assert receipt['validation_inputs_unchanged'] and receipt['validation_input_sha256']==current
        subprocess.run(['git','diff','--exit-code',receipt['subject_sha'],'HEAD','--','examples/research'],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
        assert not subprocess.check_output(['git','status','--porcelain','--','examples/research'],cwd=ROOT).strip()
        jobs.append({'job':name,'tested_sha':receipt['subject_sha'],'result':receipt['status'],
                     'validation_inputs_match_final_head_worktree':True,'example_git_content_unchanged':True})
    packaging=json.loads((ROOT/'local-ci/packaging314-final-r6/receipt.json').read_text(encoding='utf-8'))
    for name,digest in packaging['validation_scripts_sha256'].items():
        assert hashlib.sha256((ROOT/'tools/migration'/name).read_bytes()).hexdigest()==digest
    status=subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()
    assert not status, 'candidate Git worktree must be clean'
    assert not subprocess.check_output(['git','remote'],cwd=ROOT).strip()
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    report={'status':'PASS_INPUT_BINDING_ONLY','final_head':head,'jobs':jobs,
            'packaging_validation_scripts_match':True,'git_worktree_clean':True,'remotes':[],
            'full_suite_pass':False,'remote_github_actions':'NOT_RUN','linux':'NOT_RUN',
            'formal_admission':'FORMAL_ADMISSION_BLOCKED','public_release':'BLOCKED',
            'scope':'Completed local results bound to unchanged tested inputs at final local HEAD; not financial or remote approval.'}
    output=ROOT/'local-final/final-head-binding.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(report['status'],head)


if __name__=='__main__':
    main()
