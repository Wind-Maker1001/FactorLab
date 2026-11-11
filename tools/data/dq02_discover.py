"""Discover physical audit units without treating discovery as content validation."""
from pathlib import Path
import hashlib
import json
import zipfile


def discover():
    jobs = []
    archives = []
    for source in ('CSMAR', 'CNRDS'):
        for path in sorted(Path('E:/' + source + ' Raw').iterdir()):
            if not path.is_file() or path.suffix.lower() not in {'.zip', '.7z'}:
                continue
            stat = path.stat()
            identity = dict(bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
            if not zipfile.is_zipfile(path):
                archives.append(dict(source=source, archive=str(path), identity=identity, status='UNREADABLE', reason='Not a ZIP container; requires alternate archive decoder'))
                continue
            with zipfile.ZipFile(path) as archive:
                dictionaries = {i.filename: archive.read(i).decode('utf-8-sig', errors='replace') for i in archive.infolist() if i.filename.lower().endswith('.txt')}
                members = [i for i in archive.infolist() if i.filename.lower().endswith(('.xlsx', '.xls', '.csv', '.parquet'))]
                archives.append(dict(source=source, archive=str(path), identity=identity, status='DISCOVERED', members=len(members)))
                for member in members:
                    ident = hashlib.sha256((source + '/' + path.name + '/' + member.filename).encode()).hexdigest()[:24]
                    jobs.append(dict(id=ident, source=source, archive=str(path), member=member.filename,
                                     bytes=member.file_size, crc=member.CRC, identity=identity,
                                     dictionaries=dictionaries, table=Path(member.filename).stem))
    return jobs, archives


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--private-root', required=True)
    args = p.parse_args()
    root = Path(args.private_root)
    root.mkdir(parents=True, exist_ok=True)
    jobs, archives = discover()
    (root / 'discovery.json').write_text(json.dumps(dict(jobs=jobs, archives=archives), ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(work_units=len(jobs), archives=len(archives), unreadable=sum(a['status']=='UNREADABLE' for a in archives), workbook_bytes=sum(j['bytes'] for j in jobs))))
