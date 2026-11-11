"""Aggregate completed DQ02 receipts; exact cross-partition checks remain disk-backed."""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import shutil
import sys
import time
import zlib
sys.path.insert(0,str(Path(__file__).resolve().parent))
from dq02_native import MISSING,dump


def write_csv(path,rows):
    fields=list(dict.fromkeys(k for row in rows for k in row)) or ['status']
    with path.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k:json.dumps(v,ensure_ascii=False,sort_keys=True) if isinstance(v,(list,dict)) else v for k,v in row.items()})


def table_name(source,table,archive=''):
    if source=='BigQuant':
        return table
    if source=='CNRDS':
        if archive.startswith('CNSP_'):
            return '个股日回报率'
        if archive.startswith('IORD_'):
            return '基金重仓流通股'
        return Path(archive).stem
    return re.sub(r'\d+$','',table)


def assemble(root):
    discovery=json.loads((root/'discovery.json').read_text(encoding='utf-8'))
    receipts=[json.loads(p.read_text(encoding='utf-8')) for p in root.glob('*/receipt.json')]
    inherited=json.loads((root/'inherited.json').read_text(encoding='utf-8'))
    completed={r['id'] for r in receipts}
    receipts.extend(r for r in inherited if r['id'] not in completed)
    accounted={r['id'] for r in receipts}
    missing=[j['id'] for j in discovery['jobs'] if j['id'] not in accounted]
    if missing:
        raise ValueError('Native scan units have no completed receipt: '+','.join(missing))
    native=dict(receipts=receipts,wall_seconds=None,calibration=json.loads((root/'calibration.json').read_text(encoding='utf-8')))
    bigquant=dict(receipts=[],wall_seconds=None,calibration={'status':'DEFERRED_SCOPE'})
    bqjobs=[]
    tables=defaultdict(list)
    for r in receipts:
        archive=r.get('archive',Path(r.get('job',{}).get('archive','')).name)
        tables[(r['source'],table_name(r['source'],r['table'],archive))].append(r)
    return native,bigquant,discovery,bqjobs,tables


def exact_table(root,label,receipts):
    ident=hashlib.sha256(label.encode()).hexdigest()[:24]
    output=root/'aggregates'
    output.mkdir(exist_ok=True)
    path=output/(ident+'.sqlite')
    source_bytes=sum(Path(r.get('database',r.get('sqlite_path'))).stat().st_size for r in receipts if r['status']=='FULL_CONTENT_SCANNED')
    if shutil.disk_usage(output).free < source_bytes*3 + 12*1024**3:
        raise OSError('Insufficient exact-sort workspace with 12 GiB reserve for '+label)
    if path.exists():
        path.unlink()
    dest=sqlite3.connect(path)
    dest.execute('pragma journal_mode=OFF')
    dest.execute('pragma synchronous=OFF')
    dest.execute('pragma cache_size=-65536')
    dest.execute('create table records(part text,key text,payload text,subject text)')
    dest.execute('create table enums(part text,field text,value text,n integer)')
    total=covered=nulls=0
    temporal_relations=defaultdict(int)
    variants=set()
    for r in receipts:
        if r['status']!='FULL_CONTENT_SCANNED':
            continue
        native='sheets' in r
        source=sqlite3.connect(r['database'] if native else r['sqlite_path'])
        if native:
            configs={s['sheet']:s for s in r['sheets'] if not s.get('empty')}
            def rows():
                nonlocal total,covered,nulls
                for sheet,key,payload,subject in source.execute('select sheet,key,payload,subject from records'):
                    total+=1
                    config=configs[sheet]
                    fields=config['keys']
                    variants.add(tuple(fields))
                    values=json.loads(key) if key is not None else []
                    missing=bool(fields) and any(v is None or v in MISSING for v in values)
                    nulls+=missing
                    actual=json.dumps([fields,values],ensure_ascii=False,separators=(',',':')) if fields and not missing else None
                    covered+=actual is not None
                    # Compare by field names, not physical column order.
                    record=dict(zip(config['headers'],json.loads(zlib.decompress(payload) if isinstance(payload,bytes) else payload)))
                    canonical=json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
                    yield r['id'],actual,zlib.compress(canonical,1),subject
        else:
            fields=r['key_coverage']['fields']
            variants.add(tuple(fields))
            nulls+=r['key_coverage']['null_key_rows']
            def rows():
                nonlocal total,covered
                for key,payload,part in source.execute('select key,payload,partition from rows'):
                    total+=1
                    actual=json.dumps([fields,json.loads(key)],ensure_ascii=False,separators=(',',':')) if key is not None else None
                    covered+=actual is not None
                    record=json.loads(zlib.decompress(payload) if isinstance(payload,bytes) else payload)
                    for earlier,later in (('start_date','end_date'),('report_date','publish_date'),('register_date','ex_date'),('exright_date','allot_listdate')):
                        a,b=record.get(earlier),record.get(later)
                        if isinstance(a,dict) and isinstance(b,dict) and ('$datetime' in a or '$date' in a) and ('$datetime' in b or '$date' in b):
                            temporal_relations[earlier+'>'+later] += next(iter(a.values()))[:10] > next(iter(b.values()))[:10]
                    subject=record.get('instrument',record.get('instrument_id'))
                    yield r['id'],actual,payload,json.dumps(subject) if subject is not None else None
        batch=[]
        for row in rows():
            batch.append(row)
            if len(batch)>=5000:
                dest.executemany('insert into records values(?,?,?,?)',batch)
                batch.clear()
        if batch:
            dest.executemany('insert into records values(?,?,?,?)',batch)
        has_enums = 'enum_stats' in r or any('enum_distinct' in f for s in r.get('sheets',[]) for f in s.get('fields',[]))
        if has_enums:
            query = 'select field,value,sum(n) from enums group by field,value' if native else 'select field,value,count from enums'
            dest.executemany('insert into enums values(?,?,?,?)',((r['id'],field,value,count) for field,value,count in source.execute(query)))
        dest.commit()
        source.close()
    dest.execute('create index key_index on records(key)')
    dest.execute('create index enum_index on enums(part,field,value)')
    duplicate=excess=conflict=overlap=0
    for count,distinct,parts in dest.execute('select count(*),count(distinct payload),count(distinct part) from records where key is not null group by key having count(*)>1'):
        duplicate+=1
        excess+=count-1
        conflict+=distinct>1
        overlap+=parts>1
    subjects=dest.execute('select count(distinct subject) from records').fetchone()[0]
    enum_changes=[]
    ordered = sorted((r.get('member',r.get('job',{}).get('member','')),r['id']) for r in receipts if r['status']=='FULL_CONTENT_SCANNED')
    prior={}
    enum_fields={part:[f for (f,) in dest.execute('select distinct field from enums where part=?',(part,))] for _,part in ordered}
    for member,part in ordered:
        for field in enum_fields[part]:
            if field in prior:
                previous=prior[field]
                added=dest.execute('select count(*) from (select value from enums where part=? and field=? except select value from enums where part=? and field=?)',(part,field,previous,field)).fetchone()[0]
                removed=dest.execute('select count(*) from (select value from enums where part=? and field=? except select value from enums where part=? and field=?)',(previous,field,part,field)).fetchone()[0]
                if added or removed:
                    enum_changes.append(dict(partition=part,previous_partition=previous,field=field,kind='EXACT_CATEGORICAL_SET_CHANGE',added_values=added,removed_values=removed,order='LEXICAL_MEMBER_ORDER',interpretation='OBSERVED_DOMAIN_CHANGE_NOT_AUTOMATIC_ERROR'))
            prior[field]=part
    dest.close()
    path.unlink()  # Per-table sort workspace; source checkpoints remain intact.
    return dict(temporal_relation_anomalies=dict(temporal_relations),enum_changes=enum_changes,checked_rows=total,key_covered_rows=covered,missing_key_rows=nulls,duplicate_keys=duplicate,
                duplicate_excess_rows=excess,conflicting_keys=conflict,overlapping_partition_keys=overlap,
                subjects=subjects,candidate_key_variants=[list(v) for v in sorted(variants)])


def aggregate_job(job):
    root,aggregate_root,source,table,receipts=job
    cache=Path(root)/'table-aggregates'
    cache.mkdir(exist_ok=True)
    identity=hashlib.sha256(json.dumps([(r['id'],r.get('sha256'),r.get('rows'),r.get('code_identity')) for r in receipts],sort_keys=True).encode()).hexdigest()
    checkpoint=cache/(hashlib.sha256((source+'/'+table).encode()).hexdigest()[:24]+'.json')
    if checkpoint.exists():
        saved=json.loads(checkpoint.read_text(encoding='utf-8'))
        if saved['input_identity']!=identity:
            raise ValueError('Aggregate checkpoint input identity changed')
        return source,table
    print('start '+source+'/'+table,flush=True)
    counts=exact_table(Path(aggregate_root),source+'/'+table,receipts)
    dump(checkpoint,dict(input_identity=identity,counts=counts))
    print('completed '+source+'/'+table,flush=True)
    return source,table


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--private-root',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--aggregate-root',type=Path)
    parser.add_argument('--workers',type=int,default=4)
    args=parser.parse_args()
    root,out=Path(args.private_root),Path(args.output)
    out.mkdir(parents=True,exist_ok=True)
    start=time.perf_counter()
    native,bigquant,discovery,bqjobs,tables=assemble(root)
    from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
    aggregate_root=args.aggregate_root or root
    aggregate_root.mkdir(parents=True,exist_ok=True)
    jobs=[]
    for (source,table),receipts in tables.items():
        size=sum(Path(r.get('database',r.get('sqlite_path'))).stat().st_size for r in receipts if r['status']=='FULL_CONTENT_SCANNED')
        jobs.append((size,(str(root),str(aggregate_root),source,table,receipts)))
    jobs.sort(key=lambda item:item[0],reverse=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        active={}
        while jobs or active:
            reserved=sum(active.values())
            free=shutil.disk_usage(aggregate_root).free
            while jobs and len(active)<args.workers:
                eligible=next((i for i,(size,_) in enumerate(jobs) if size*3+reserved+12*1024**3<free),None)
                if eligible is None:
                    break
                size,job=jobs.pop(eligible)
                active[pool.submit(aggregate_job,job)]=size*3
                reserved+=size*3
            if not active:
                raise OSError('No table fits exact-sort disk reserve')
            done,_=wait(active,return_when=FIRST_COMPLETED)
            for future in done:
                future.result()
                del active[future]
    quality=[];fields=[];keys=[];dates=[];drifts=[];numeric=[];coverage=[];unresolved=[];identities=[]
    for (source,table),receipts in sorted(tables.items()):
        print('aggregate '+source+'/'+table,flush=True)
        aggregate_root=args.aggregate_root or root
        aggregate_root.mkdir(parents=True,exist_ok=True)
        cache=root/'table-aggregates'
        cache.mkdir(exist_ok=True)
        identity=hashlib.sha256(json.dumps([(r['id'],r.get('sha256'),r.get('rows'),r.get('code_identity')) for r in receipts],sort_keys=True).encode()).hexdigest()
        checkpoint=cache/(hashlib.sha256((source+'/'+table).encode()).hexdigest()[:24]+'.json')
        if checkpoint.exists():
            saved=json.loads(checkpoint.read_text(encoding='utf-8'))
            if saved['input_identity']!=identity:
                raise ValueError('Aggregate checkpoint input identity changed')
            counts=saved['counts']
        else:
            counts=exact_table(aggregate_root,source+'/'+table,receipts)
            dump(checkpoint,dict(input_identity=identity,counts=counts))
        enum_changes=counts.pop('enum_changes')
        drifts.extend(dict(source=source,table=table,**change) for change in enum_changes)
        complete=[r for r in receipts if r['status']=='FULL_CONTENT_SCANNED']
        inherited=[r for r in receipts if r.get('inherited_rows') is not None]
        failed=[r for r in receipts if r['status']=='UNREADABLE']
        actual=sum(r['rows'] for r in complete)
        status='FULL_CONTENT_SCANNED' if len(complete)==len(receipts) else 'PARTIAL_CONTENT_SCANNED' if complete or inherited else 'UNREADABLE'
        pending=[]
        if inherited:
            pending.append('Inherited market rows/fields/window only; remaining historical and field coverage unknown')
        if failed:
            pending.append('Unreadable partitions')
        if any(not k for k in counts['candidate_key_variants']):
            pending.append('Business primary key unresolved for some schemas')
        pending.append('Supplier version priority and economic unit/scale certification not inferred from statistics')
        base=dict(source=source,table=table)
        keyrow=dict(**base,**counts,key_status='BUSINESS_CANDIDATES_NOT_VENDOR_CERTIFIED',source_version_comparison='WITHIN_SOURCE_ONLY')
        keys.append(keyrow)
        quality.append(dict(**base,status=status,physical_data_rows=actual if not inherited and not failed else None,
                            content_coverage_ratio=1.0 if status=='FULL_CONTENT_SCANNED' else None,
                            worksheet_physical_rows=sum(r.get('physical_rows',r['rows'])+len(r.get('sheets',[])) for r in complete) if source!='BigQuant' else None,
                            worksheet_descriptor_rows=sum(r.get('metadata_rows',0) for r in complete),
                            readable_scanned_rows=actual,inherited_selected_rows=sum(r.get('inherited_rows',0) for r in inherited),
                            discovered_partitions=len(receipts),read_partitions=len(complete),subjects=counts['subjects'],
                            field_count=len({f['field'] for r in complete for s in r.get('sheets',[]) for f in s.get('fields',[])}) if source!='BigQuant' else len({f for r in complete for f in r['fields']}),
                            unfinished_checks=pending))
        schemas=[]
        for r in receipts:
            part=r['id']
            archive=r.get('archive',Path(r.get('job',{}).get('archive','')).name)
            member=r.get('member',r.get('job',{}).get('member'))
            identities.append(dict(**base,evidence_id=part,archive=archive,member=member,identity=r.get('identity'),sha256=r.get('sha256'),status=r['status']))
            coverage.append(dict(**base,partition=part,status=r['status'],discovered_files=1,read_files=int(r['status']=='FULL_CONTENT_SCANNED'),
                                 physical_data_rows=r.get('rows') if r['status']=='FULL_CONTENT_SCANNED' else None,
                                 checked_rows=r.get('rows',0),inherited_selected_rows=r.get('inherited_rows',0),
                                 field_coverage='ALL_PHYSICAL_COLUMNS' if r['status']=='FULL_CONTENT_SCANNED' else 'PRIOR_SELECTED_FIELDS_ONLY' if r in inherited else 'NONE',
                                 key_covered_rows=(r.get('key_coverage',{}).get('covered_rows') if source=='BigQuant' else sum(s['rows'] for s in r.get('sheets',[]) if s.get('keys'))),
                                 denominator_known=r['status']=='FULL_CONTENT_SCANNED',unfinished_checks=pending,reason=r.get('reason','')))
            if r['status']!='FULL_CONTENT_SCANNED':
                unresolved.append(dict(**base,partition=part,status=r['status'],row_count_known=False,known_selected_rows=r.get('inherited_rows'),reason=r.get('reason',r.get('scope','Unknown'))))
                continue
            if source=='BigQuant':
                schema={f['name']:f['type'] for f in r['schema']}
                schemas.append((member,part,schema))
                for field,state in r['fields'].items():
                    row=dict(**base,partition=part,field=field,rows=r['rows'],raw_types={state['type']:r['rows']},null_markers={'ARROW_NULL':state['nulls']},
                             numeric_nonfinite=state['nonfinite'],negative=state['negative'],zero=state['zero'],min=state['min'],max=state['max'],
                             type_conversion='NATIVE_TYPED_NO_COERCION',invalid_values=None,enum_distinct=r['enum_stats'].get(field,{}).get('distinct'),semantic_range_status='NO_VENDOR_RANGE_CONTRACT')
                    fields.append(row)
                    if state['nonfinite'] or state['negative'] or state['zero']:
                        numeric.append(dict(**base,partition=part,field=field,nonfinite=state['nonfinite'],negative=state['negative'],zero=state['zero'],classification='NONFINITE_OR_SIGN_OBSERVATIONS;NEGATIVE_AND_ZERO_NOT_AUTOMATIC_ERRORS'))
                for field,months in r['date_stats'].items():
                    for month,count in months.items():
                        dates.append(dict(**base,partition=part,field=field,year=month[:4],quarter=(int(month[5:])-1)//3+1,month=month,records=count))
            else:
                for sheet in r['sheets']:
                    if sheet.get('empty'):
                        continue
                    schema={f['field']:f['raw_types'] for f in sheet['fields']}
                    schemas.append((member+'/'+sheet['sheet'],part,schema))
                    for f in sheet['fields']:
                        nonnull=f['rows']-sum(f['null_markers'].values())
                        valid=f['date_valid'] if f['expected']=='date' else f['numeric_valid'] if f['expected']=='numeric' else None
                        fields.append(dict(**base,partition=part,sheet=sheet['sheet'],field=f['field'],rows=f['rows'],raw_types=f['raw_types'],null_markers=f['null_markers'],
                                           expected=f['expected'],type_conversion_success_rate=valid/nonnull if valid is not None and nonnull else None,
                                           invalid_values=f['numeric_invalid']+f['date_invalid'],numeric_nonfinite=f['nonfinite'],negative=f['negative'],zero=f['zero'],
                                           min=f['min'],max=f['max'],max_decimal_places=f['max_decimal_places'],date_min=f['date_min'],date_max=f['date_max'],enum_distinct=f.get('enum_distinct'),
                                           formula_cells=f['formula_cells']))
                        if f['numeric_invalid'] or f['nonfinite'] or f['negative'] or f['zero']:
                            numeric.append(dict(**base,partition=part,field=f['field'],invalid_numeric=f['numeric_invalid'],nonfinite=f['nonfinite'],negative=f['negative'],zero=f['zero'],classification='TYPE_FAILURES_AND_STATISTICAL_SIGNS;NO_AUTOMATIC_CORRECTION'))
                        for month,count in f['date_months'].items():
                            dates.append(dict(**base,partition=part,field=f['field'],year=month[:4],quarter=(int(month[5:])-1)//3+1,month=month,records=count))
                    if sheet['report_after_disclosure'] or sheet['ragged_rows']:
                        drifts.append(dict(**base,partition=part,kind='WITHIN_PARTITION_STRUCTURE_OR_DATE_RELATION',report_after_disclosure=sheet['report_after_disclosure'],ragged_rows=sheet['ragged_rows']))
        schemas.sort()
        previous=None
        for member,part,schema in schemas:
            # Type set comparison excludes frequencies, which have their own field results.
            normalized={k:sorted(v) if isinstance(v,dict) else v for k,v in schema.items()}
            if previous is not None:
                before=previous[2]
                added=sorted(set(normalized)-set(before));removed=sorted(set(before)-set(normalized))
                changed=sorted(k for k in normalized.keys()&before.keys() if normalized[k]!=before[k])
                if added or removed or changed:
                    drifts.append(dict(**base,partition=part,previous_partition=previous[1],kind='SCHEMA_DIFFERENCE_IN_LEXICAL_PARTITION_ORDER',first_observed_member=member,added_fields=added,removed_fields=removed,type_changed_fields=changed,interpretation='OBSERVED_NOT_SUPPLIER_ERROR;LEXICAL_ORDER_NOT_HISTORICAL_PUBLICATION_ORDER'))
            previous=(member,part,normalized)
    artifacts={'table-quality.csv':quality,'field-quality.csv':fields,'primary-key-quality.csv':keys,'date-coverage.csv':dates,
               'schema-drift.csv':drifts,'numeric-anomalies.csv':numeric,'scan-coverage.csv':coverage,'unresolved-assets.csv':unresolved}
    for name,rows in artifacts.items():
        write_csv(out/name,rows)
    provenance=dict(status='CONTENT_SCAN_RESULTS',limits=['NO LIVE MONEY','PIT_UNVERIFIED','HFQ_UNVERIFIED','FORMAL_ADMISSION_BLOCKED'],
                    source_inputs=identities,discovered_native_members=len(discovery['jobs']),discovered_bigquant_partitions=len(bqjobs),
                    archives=[dict(source=a['source'],archive=Path(a['archive']).name,identity=a['identity'],status=a['status'],members=a.get('members'),reason=a.get('reason')) for a in discovery['archives']],
                    performance=dict(native_wall_seconds=native['wall_seconds'],bigquant_wall_seconds=bigquant['wall_seconds'],aggregation_wall_seconds=time.perf_counter()-start,native_calibration=native['calibration'],bigquant_calibration=bigquant['calibration']),
                    machine_index={name:dict(format='csv',records=len(rows)) for name,rows in artifacts.items()})
    dump(out/'provenance.json',provenance)
    dump(root/'aggregate-summary.json',dict(tables=len(quality),rows=sum(q['readable_scanned_rows'] for q in quality),full_tables=sum(q['status']=='FULL_CONTENT_SCANNED' for q in quality),partial_tables=sum(q['status']=='PARTIAL_CONTENT_SCANNED' for q in quality),unreadable_tables=sum(q['status']=='UNREADABLE' for q in quality),unresolved_assets=len(unresolved),key_conflicts=sum(k['conflicting_keys'] for k in keys)))


if __name__=='__main__':
    main()
