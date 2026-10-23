"""Add raw blob bindings without rewriting the stage-one archive byte identities."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

from extract import SCOPES, REV

ROOT=Path(__file__).resolve().parents[2]
SOURCE=Path(r'D:\Code\FactorLab-extraction')


def main():
    manifest_path=ROOT/'docs/migration/EXTRACTION_FILE_MANIFEST.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    command=['git','-C',str(SOURCE)]
    entries={}
    for line in subprocess.check_output(command+['ls-tree','-r',REV,'--',*SCOPES]).decode('utf-8').splitlines():
        meta,name=line.split('\t',1)
        entries[name]=meta.split()[2]
    objects=subprocess.check_output(command+['cat-file','--batch'],
        input=('\n'.join(i['source_blob'] for i in manifest['files'])+'\n').encode('ascii'))
    exported=subprocess.check_output(command+['archive','--format=tar',REV,*SCOPES])
    with tarfile.open(fileobj=io.BytesIO(exported)) as archive:
        archive_bytes={m.name:archive.extractfile(m).read() for m in archive if m.isfile()}
    cursor=0
    differences=[]
    results=[]
    for item in manifest['files']:
        name=item['source_path']
        assert entries[name]==item['source_blob'], 'path/blob mismatch: '+name
        end=objects.index(b'\n',cursor)
        oid,kind,size=objects[cursor:end].split()
        size=int(size)
        body=objects[end+1:end+1+size]
        cursor=end+1+size+1
        assert kind==b'blob' and oid.decode()==item['source_blob']
        exported_body=archive_bytes[name]
        assert hashlib.sha256(exported_body).hexdigest()==item['source_sha256'], 'recorded archive differs: '+name
        raw_hash=hashlib.sha256(body).hexdigest()
        eol_only=body.replace(b'\r\n',b'\n')==exported_body.replace(b'\r\n',b'\n')
        assert eol_only, 'unexpected export transformation: '+name
        item['source_git_blob_sha256']=raw_hash
        item['source_sha256_contract']='ORIGINAL_STAGE_ONE_GIT_ARCHIVE_EXPORTED_BYTES_PRESERVED'
        if body!=exported_body:
            differences.append(name)
        results.append({'source_path':name,'git_blob':item['source_blob'],'git_blob_sha256':raw_hash,
                        'original_archive_sha256':item['source_sha256'],'exact_blob_equals_archive':body==exported_body,
                        'only_EOL_differences':eol_only})
    manifest['source_identity_clarification']='source_sha256 retains exported git-archive bytes; source_git_blob_sha256 separately binds immutable raw Git bytes'
    manifest_path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    report={'status':'PASS','baseline':REV,'bound_files':len(results),
            'archive_EOL_transformed_files':len(differences),'transformed_paths':differences,'files':results,
            'scope':'Fixed baseline path+Git blob+raw SHA256 and original archive bytes; historical identities are not rewritten.'}
    (ROOT/'docs/migration/evidence/git-blob-archive-binding.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('PASS',len(results),'fixed path/blob bindings;',len(differences),'EOL-only archive transformations')


if __name__=='__main__':
    main()
