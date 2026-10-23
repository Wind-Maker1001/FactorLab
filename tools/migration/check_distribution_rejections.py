"""Executable archive attack checks; mutate synthetic local copies, never old evidence."""
import argparse
import io
import json
from pathlib import Path
import shutil
import stat
import tarfile
import warnings
import zipfile

from scan_distribution import scan


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('distributions',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    wheel=next(args.distributions.glob('*.whl'))
    source=next(args.distributions.glob('*.tar.gz'))
    cases={
        'credential':('factorlab/test_fixture.py',b"key = 'ghp_"+b'A'*36+b"'\n"),
        'legacy_import':('factorlab/test_fixture.py',b'import qbot.engine\n'),
        'parent_path':('../outside.py',b'# synthetic attack\n'),
        'unbound_golden':('tests/research/fixtures/factor_golden/real.parquet',b'synthetic unbound sentinel'),
        'duplicate_zip':('duplicate',b''),
        'zip_link':('factorlab/link.py',b'../../outside'),
        'tar_link':('link',b''),
    }
    results=[]
    for label,(name,body) in cases.items():
        directory=output/label
        directory.mkdir()
        shutil.copyfile(wheel,directory/wheel.name)
        shutil.copyfile(source,directory/source.name)
        if label=='tar_link':
            target=directory/source.name
            old=directory/'original.tar.gz'
            target.rename(old)
            with tarfile.open(old) as original, tarfile.open(target,'w:gz') as mutated:
                for entry in original:
                    mutated.addfile(entry,original.extractfile(entry) if entry.isfile() else None)
                entry=tarfile.TarInfo('factorlab-0.1.0.dev0/linked.py')
                entry.type=tarfile.SYMTYPE
                entry.linkname='../../outside'
                mutated.addfile(entry)
            old.rename(directory/'original.archive')
        else:
            with warnings.catch_warnings(), zipfile.ZipFile(directory/wheel.name,'a') as mutated:
                warnings.simplefilter('ignore',UserWarning)
                if label=='duplicate_zip':
                    member=mutated.namelist()[0]
                    mutated.writestr(member,mutated.read(member))
                elif label=='zip_link':
                    entry=zipfile.ZipInfo(name)
                    entry.create_system=3
                    entry.external_attr=(stat.S_IFLNK | 0o777)<<16
                    mutated.writestr(entry,body)
                else:
                    mutated.writestr(name,body)
        rejected=scan(directory,directory/'scan.json')
        if not rejected:
            raise AssertionError('archive attack accepted: '+label)
        results.append({'case':label,'status':'PASS_REJECTED'})
    (output/'receipt.json').write_text(json.dumps({'status':'PASS','cases':results,
        'scope':'seven synthetic archive mutations; not financial certification'},indent=2),encoding='utf-8')
    print('PASS:',len(results),'synthetic archive attacks rejected')


if __name__=='__main__':
    main()
