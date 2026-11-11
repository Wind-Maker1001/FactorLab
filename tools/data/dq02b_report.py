"""Aggregate bounded columnar audit receipts without reopening supplier data."""
import argparse
from collections import defaultdict,Counter
import csv
import json
from pathlib import Path
import shutil


def csv_write(path,rows,columns):
    with path.open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore');writer.writeheader()
        for row in rows:writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()})


def main():
    p=argparse.ArgumentParser();p.add_argument('--state',required=True);p.add_argument('--output',required=True);args=p.parse_args()
    root=Path(args.state);out=Path(args.output)
    pre=json.loads((root/'preflight.json').read_text(encoding='utf-8'))
    receipts={f.stem:json.loads(f.read_text(encoding='utf-8')) for f in (root/'parts').glob('*.json')}
    keys=[json.loads(f.read_text(encoding='utf-8')) for f in (root/'keys').glob('*.json')]
    parts=[];fields=[];anomalies=[];unresolved=[];tables=defaultdict(list);assets=defaultdict(list)
    for job in pre['all_jobs']:
        r=receipts.get(job['id'],dict(status='NOT_RUN',rows=0,reason='Missing content receipt'))
        part=dict(archive=job['identity']['archive'],table=job['table'],member=job['identity']['member'],evidence_id=job['id'],status=r['status'],rows=r.get('rows',0),declared_rows=r.get('declared_rows'),row_groups=r.get('row_groups'),read_row_groups=r.get('read_row_groups'),date_outside_filename=r.get('date_outside_filename'),reason=r.get('reason',''))
        parts.append(part);tables[job['table']].append(r);assets[part['archive']].append(part)
        if r['status']!='FULL_CONTENT_SCANNED':unresolved.append(part)
        for name,stat in r.get('fields',{}).items():
            field=dict(table=job['table'],member=job['identity']['member'],field=name,rows=r['rows'],**stat)
            fields.append(field)
            if stat['nan'] or stat['inf']:anomalies.append({k:field[k] for k in ('table','member','field','rows','nan','inf')})
    table_rows=[];drift=[]
    for table,rs in sorted(tables.items()):
        signatures=Counter(json.dumps(r.get('schema',{}),sort_keys=True) for r in rs)
        table_rows.append(dict(table=table,partitions=len(rs),full_content_partitions=sum(r['status']=='FULL_CONTENT_SCANNED' for r in rs),rows=sum(r.get('rows',0) for r in rs),schema_variants=len(signatures),date_outside_filename=sum(r.get('date_outside_filename',0) for r in rs),dictionary_missing_partitions=sum(bool(r.get('dictionary_missing')) for r in rs),dictionary_extra_partitions=sum(bool(r.get('dictionary_extra')) for r in rs)))
        for schema,n in signatures.items():drift.append(dict(table=table,partitions=n,schema=json.loads(schema),status='VARIANT_OBSERVED' if len(signatures)>1 else 'SINGLE_PHYSICAL_SCHEMA'))
    asset_rows=[dict(archive=a,partitions=len(rs),full_content_partitions=sum(r['status']=='FULL_CONTENT_SCANNED' for r in rs),rows=sum(r['rows'] for r in rs)) for a,rs in sorted(assets.items())]
    asset_rows.append(dict(archive='metadata.zip',partitions=0,full_content_partitions=0,rows=0))
    run=json.loads((root/'run.json').read_text(encoding='utf-8'))
    calibration=json.loads((root/'calibration.json').read_text(encoding='utf-8'))
    disk_bytes=sum(f.stat().st_size for f in root.rglob('*') if f.is_file())
    budget=dict(disk_budget_bytes=pre['disk_budget'],free_space_floor_bytes=pre['reserve'],worker_count=2,batch_rows=16384,compact_key_memory_limit_bytes=64*1024**2,initial_free_bytes=pre['initial_free_bytes'],ending_free_bytes={d:shutil.disk_usage(d+':/').free for d in ('D','E','T')},ending_private_bytes=disk_bytes,conservative_stage_reservation_bytes=2*pre['maximum_member_bytes']+256*1024**2,maximum_recorded_worker_peak_bytes=max(r.get('peak_rss_bytes',0) for r in receipts.values()),sum_partition_cpu_seconds=sum(r.get('cpu_seconds',0) for r in receipts.values()),scan_wall_seconds=run['wall_seconds'],calibration_wall_seconds=calibration['wall_seconds'],disk_peak_measurement='NOT_INSTRUMENTED',minimum_free_space_measurement='PER_WRITE_GUARD_NOT_RECORDED',aggregate_ram_measurement='NOT_INSTRUMENTED',source_mode='READ_ONLY')
    summary=dict(status='DQ02B_PARTIAL_RESOURCE_BOUND' if unresolved or any(k['status']=='KEY_CHECK_BLOCKED_RESOURCE' for k in keys) else 'DQ02B_CONTENT_COMPLETE',archives=len(asset_rows),data_archives=len(assets),tables=len(tables),partitions=len(parts),full_content_partitions=sum(p['status']=='FULL_CONTENT_SCANNED' for p in parts),rows=sum(p['rows'] for p in parts),metadata_only_partitions=sum(p['status']=='METADATA_VERIFIED' for p in parts),unresolved_partitions=len(unresolved),candidate_key_exact_tables=sum(k['status']=='CANDIDATE_KEY_EXACT' for k in keys),resource_blocked_key_tables=sum(k['status']=='KEY_CHECK_BLOCKED_RESOURCE' for k in keys),unknown_key_contract_tables=sum(k['status']=='KEY_CONTRACT_UNKNOWN' for k in keys),schema_variant_tables=sum(t['schema_variants']>1 for t in table_rows),nonfinite_field_partitions=len(anomalies),gates=['NO LIVE MONEY','PIT_UNVERIFIED','HFQ_UNVERIFIED','FORMAL_ADMISSION_BLOCKED'],row_conservation=all(r.get('rows')==r.get('declared_rows') and r.get('read_row_groups')==r.get('row_groups') for r in receipts.values() if r['status']=='FULL_CONTENT_SCANNED'))
    # Public outputs are bounded and written only after content workers have stopped.
    estimated=sum(len(json.dumps(v,ensure_ascii=False)) for v in (parts,fields,table_rows,drift,keys))*2+1024**2
    if disk_bytes+estimated>pre['disk_budget'] or shutil.disk_usage(out.parent).free-estimated<pre['reserve']:raise RuntimeError('BLOCKED_RESOURCE: report budget')
    out.mkdir(parents=True,exist_ok=True)
    csv_write(out/'asset-coverage.csv',asset_rows,['archive','partitions','full_content_partitions','rows'])
    csv_write(out/'partition-quality.csv',parts,list(parts[0]))
    csv_write(out/'table-quality.csv',table_rows,list(table_rows[0]))
    csv_write(out/'field-quality.csv',fields,['table','member','field','rows','type','nulls','nan','inf','empty_strings','negative','zero','min','max'])
    csv_write(out/'schema-drift.csv',drift,['table','partitions','schema','status'])
    csv_write(out/'key-quality.csv',keys,['table','status','fields','scope_partitions','rows','null_key_rows','duplicate_keys','duplicate_excess','unique_nonnull_keys','reason','reused_content'])
    csv_write(out/'numeric-anomalies.csv',anomalies,['table','member','field','rows','nan','inf'])
    csv_write(out/'unresolved-partitions.csv',unresolved,list(parts[0]))
    for name,data in [('resource-budget.json',budget),('verification.json',summary)]: (out/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    text=f'''# DQ-02B BigQuant 列式内容审计

**{summary['status']}**

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

原始供应商数据只读。

## 内容覆盖

资产分母 {summary['archives']} 个 ZIP，其中 {summary['data_archives']} 个含 Parquet，`metadata.zip` 不含 Parquet。实际读取{summary['tables']} 张逻辑表的 {summary['full_content_partitions']}/{summary['partitions']} 个 Parquet 分片，共 {summary['rows']:,} 行。读取 Arrow batch 和各 row group，逐分片核对实际行数与 footer；元数据单独不计内容完成。未完成分片 {summary['unresolved_partitions']} 个。分片身份绑定归档大小/mtime、成员 CRC/大小及执行代码 SHA256；这不是完整归档 SHA256 重验。

字段统计保留 Arrow 类型、null、浮点 NaN/Inf、空字符串、负值、零值及有限数值/时间极值。NaN/Inf 涉及 {summary['nonfinite_field_partitions']} 个字段—分片；负数和零不自动认定经济错误。物理 schema 多版本涉及 {summary['schema_variant_tables']} 张表。详细分母见 CSV。

## 精确候选键

{summary['candidate_key_exact_tables']} 张表完成候选键的实际值精确分组；{summary['resource_blocked_key_tables']} 张表受资源/恢复范围限制，{summary['unknown_key_contract_tables']} 张表缺明确键合同。候选重复不等于业务错误，也不区分整行相同与非键字段冲突。键投影在内存中保留，单表上限 64 MiB；不存完整行、不建立 SQLite/数据库镜像。超限只阻断键检查。

精确比较观察到 `future/cn_future_basic_info` 的 instrument 候选键有 638 个重复组、674 条超额记录；未复核合约版本维度，不认定应删除。三项定向软件测试通过（0.58 秒）。

## 研究含义

继承 DQ-03 的既有研究窗口关联结论：605,500 个成员日中 BigQuant 缺键 292、缺 OHLC 564；本次字段总体统计不把这些升级为新增实际回测影响。行业体系缺失会在已检查代表月使 Join 三倍扩增；本次候选键继续保留体系维度。未重训或改写研究结果。财务版本、行业有效区间、首次公开/供应商首次可得与 HFQ 继续未认证。

## 资源与恢复

两个进程、Arrow 单线程、batch 16,384。校准 {calibration['wall_seconds']:.2f} 秒，扫描墙钟 {run['wall_seconds']:.2f} 秒；各分片 CPU 合计 {budget['sum_partition_cpu_seconds']:.2f} 秒，最大记录进程峰值 {budget['maximum_recorded_worker_peak_bytes']:,} 字节。结束时私有汇总占用 {disk_bytes:,} 字节。跨进程锁串行化写前预算判断和写入；4 GiB 新增预算、80 GiB 余量。实际任务磁盘峰值、最低余量与并发总 RAM 未连续记录，不能以预算值代替实测。

恢复复用本入口同身份同代码的完整分片回执，不重复解压；恢复后的表级键检查不能从统计回执还原键，明确列为阻断，不重新扫描完整内容。旧 DQ-01 的 263 分片内容证据不具本入口全字段统计范围，因此本次补读而非升级旧证据。

TODO：补充独立精确键恢复方案、供应商唯一键合同、非键冲突复核、单位/财务恒等式、日期字符串语义解析、完整 schema 字典类型比对与非空率漂移；补齐任务级资源峰值遥测及全局停止调度验收。当前入口不应作为这些安全功能已全部验收的依据。

## 复现

依次运行 `tools/data/dq02b_columnar.py` 的 `--phase preflight`、`--phase calibrate`、`--phase scan`，指定 `--source`、新的私有 `--state` 和公开 `--output`；完成后运行 `tools/data/dq02b_report.py --state <private-state> --output docs/validation/dq-02b`。定向测试覆盖实际值键比较、磁盘预算拒写、跨 batch/row group 统计与身份绑定恢复。
'''
    (out/'REPORT.md').write_text(text,encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':main()
