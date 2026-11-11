"""Profile existing audit differences using bounded monthly native checkpoints."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
from decimal import Decimal, ROUND_HALF_UP
import json
import struct
from pathlib import Path
import time
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import psutil

pa.set_cpu_count(1)
pa.set_io_thread_count(1)
FIELDS = ['open', 'high', 'low', 'close', 'volume', 'amount']


def load_month(root, month, receipts, keys=None):
    result = {}
    for source in ['CSMAR', 'CNRDS']:
        parts = []
        for name, receipt in receipts.items():
            if receipt['source'] != source:
                continue
            path = root / 'monthly' / name / (month + '.parquet')
            if not path.exists():
                continue
            frame = pq.read_table(path).to_pandas()
            if keys is not None:
                # CNRDS exchange resolution in the original audit is inherited from CSMAR.
                codes = set(keys.symbol.str.split('.').str[0])
                frame = frame.loc[frame.code.isin(codes)]
            frame['receipt'] = name
            parts.append(frame)
        result[source] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    cs = result['CSMAR']
    cn = result['CNRDS']
    mapping = cs[['code', 'symbol']].drop_duplicates()
    cn = cn.drop(columns='symbol').merge(mapping, on='code', how='inner')
    result['CNRDS'] = cn
    return result


def write_csv(path, rows, fields=None):
    rows = list(rows)
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def partition(args):
    root, work = Path(args.native), Path(args.work)
    work.mkdir(parents=True, exist_ok=False)
    paths = sorted(root.glob('audit-full-*-anomalies-complete.csv'))
    # 2021 annual output predates the final per-month rerun; never mix vintages.
    paths = [p for p in paths if p.name != 'audit-full-2021-01-01-2021-12-31-anomalies-complete.csv']
    counts = Counter()
    manifest = []
    for path in paths:
        count = 0
        for frame in pd.read_csv(path, dtype=str, chunksize=100000, keep_default_na=False):
            selected = frame.loc[(frame.scope == 'remaining') & (frame.sources == 'CSMAR|CNRDS') & (frame.classification == 'MISMATCH') & frame.field.isin(FIELDS)]
            for month, rows in selected.groupby(selected.date.str[:7], sort=False):
                target = work / (month + '.csv')
                rows.to_csv(target, mode='a', header=not target.exists(), index=False)
            counts.update(selected.field)
            count += len(selected)
        manifest.append(dict(name=path.name, bytes=path.stat().st_size, selected_rows=count))
        print(path.name, count, flush=True)
    (work / 'index-manifest.json').write_text(json.dumps(dict(inputs=manifest, counts=counts), indent=2), encoding='utf-8')
    print(dict(counts), flush=True)


def diagnostic(args):
    root = Path(args.native)
    receipts = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in root.glob('*-*.json') if p.name.startswith(('CSMAR-', 'CNRDS-'))}
    for month in ['2018-12', '2019-05', '2019-06', '2019-07']:
        data = load_month(root, month, receipts)
        cs, cn = (data[s] for s in ['CSMAR', 'CNRDS'])
        merged = cs.merge(cn, on=['date', 'symbol'], suffixes=('_cs', '_cn'))
        unequal = merged.open_cs.map(Decimal) != merged.open_cn.map(Decimal)
        print('MONTH', month, 'open differences', int(unequal.sum()), flush=True)
        for _, row in merged.loc[unequal].head(4).iterrows():
            print(json.dumps({k: row[k] for k in ['date', 'symbol','open_cs','open_cn','close_cs','close_cn','amount_cs','amount_cn','name_cs','name_cn','filling_cs','volume_cs','volume_cn','adj_close_cs','adj_close_cn','native_record_cs','native_record_cn']}, ensure_ascii=False), flush=True)
        amt = merged.loc[merged.amount_cs.notna() & merged.amount_cn.notna() & ~merged.amount_cs.isin(['', '-', '--']) & ~merged.amount_cn.isin(['', '-', '--'])]
        amt = amt.loc[amt.amount_cs.map(Decimal) != amt.amount_cn.map(Decimal)]
        print('AMOUNT EXAMPLES', amt[['date','symbol','amount_cs','amount_cn','post_amount_cs']].head(6).to_json(orient='records'), flush=True)


def number(value):
    return None if value is None or value in {'', '-', '--'} else Decimal(value)


def classify_amount(left, right):
    if abs(left - right) <= 1 and left == left.to_integral_value() and right == right.to_integral_value():
        return 'PRECISION_OR_ROUNDING', 'INTEGER_QUANTIZATION_COMPATIBLE_NOT_PROVEN'
    rounded = Decimal.from_float(struct.unpack('f', struct.pack('f', float(left)))[0])
    if rounded == right:
        return 'PRECISION_OR_ROUNDING', 'EXACT_BINARY32_IMAGE_NOT_PIPELINE_PROOF'
    for places in [0, 1, 2]:
        quantum = Decimal(10) ** -places
        if left.quantize(quantum, rounding=ROUND_HALF_UP) == right or right.quantize(quantum, rounding=ROUND_HALF_UP) == left:
            return 'PRECISION_OR_ROUNDING', 'DECIMAL_QUANTIZATION_COMPATIBLE_NOT_PROVEN'
    return 'UNRESOLVED', 'RESIDUAL_NATIVE_VALUE_DIFFERENCE'


def new_stat():
    return dict(count=0, positive=0, negative=0, abs_sum=Decimal(0), abs_max=Decimal(0), rel_sum=Decimal(0), rel_max=Decimal(0), zero_reference=0,
                le_1=0, le_10=0, gt_10=0, ratio_power10=0, filled=0, zero_volume=0,
                cs_digits=Counter(), cn_digits=Counter(), cs_exponent=Counter(), cn_exponent=Counter(), reasons=Counter())


def add_stat(stat, left, right, filled, zero):
    delta = left - right
    absolute = abs(delta)
    stat['count'] += 1
    stat['positive' if delta > 0 else 'negative'] += 1
    stat['abs_sum'] += absolute
    stat['abs_max'] = max(stat['abs_max'], absolute)
    if left:
        relative = absolute / abs(left)
        stat['rel_sum'] += relative
        stat['rel_max'] = max(stat['rel_max'], relative)
    else:
        stat['zero_reference'] += 1
    stat['le_1' if absolute <= 1 else 'le_10' if absolute <= 10 else 'gt_10'] += 1
    stat['ratio_power10'] += int(bool(left and absolute >= abs(left) * Decimal('.9') and any(right == left * (Decimal(10) ** k) for k in [-4,-3,-2,-1,1,2,3,4])))
    stat['filled'] += filled
    stat['zero_volume'] += zero
    for prefix, value in [('cs', left), ('cn', right)]:
        norm = value.normalize()
        stat[prefix + '_digits'][len(norm.as_tuple().digits)] += 1
        stat[prefix + '_exponent'][norm.as_tuple().exponent] += 1


def flatten_stat(key, stat):
    dimension, group = key
    row = dict(dimension=dimension, group=group)
    for name, value in stat.items():
        row[name] = json.dumps(value, sort_keys=True) if isinstance(value, Counter) else str(value) if isinstance(value, Decimal) else value
    row['mean_abs'] = str(stat['abs_sum'] / stat['count'])
    nonzero = stat['count'] - stat['zero_reference']
    row['mean_relative_to_CSMAR'] = str(stat['rel_sum'] / nonzero) if nonzero else ''
    return row


def profile(args):
    started, cpu_started = time.perf_counter(), time.process_time()
    root, work, output = Path(args.native), Path(args.work), Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    receipts = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in root.glob('*-*.json') if p.name.startswith(('CSMAR-', 'CNRDS-'))}
    amounts = defaultdict(new_stat)
    classes, totals = Counter(), Counter()
    ohlc, cases, raw_cases, monthly = [], [], [], []
    field_counts = Counter()
    identities = []
    for name, receipt in receipts.items():
        identities.append({k: receipt[k] for k in ['source','member','sha256','dictionary_member','dictionary_sha256','code_sha256']})
    for path in sorted(work.glob('????-??.csv')):
        month_started = time.perf_counter()
        index = pd.read_csv(path, dtype=str, keep_default_na=False)
        data = load_month(root, path.stem, receipts)
        lookup = {}
        for source, frame in data.items():
            frame = frame.copy()
            frame['locator'] = frame.receipt.map(lambda name: receipts[name]['archive'] + '/' + receipts[name]['member']) + ':' + frame.sheet + ':' + frame.row
            lookup[source] = {row.locator: row for row in frame.itertuples(index=False)}
        price_groups = defaultdict(dict)
        case_seen = Counter()
        for entry in index.itertuples(index=False):
            cs, cn = lookup['CSMAR'][entry.left_locator], lookup['CNRDS'][entry.right_locator]
            left, right = number(getattr(cs, entry.field)), number(getattr(cn, entry.field))
            if left == right or left is None or right is None:
                raise ValueError('index/native disagreement: ' + str(entry))
            field_counts[entry.field] += 1
            filled = int(cs.filling == '1')
            zero = int(number(cs.volume) == 0 or number(cn.volume) == 0)
            if entry.field == 'amount':
                classification, reason = classify_amount(left, right)
                if filled or zero:
                    classification, reason = 'FILL_OR_STATUS_DIFFERENCE', 'ZERO_OR_FILLED_ASSOCIATION_NOT_CAUSAL_PROOF'
                price = number(cs.close)
                price_bucket = 'MISSING' if price is None else '<5' if price < 5 else '5-20' if price < 20 else '20-100' if price < 100 else '>=100'
                size_bucket = '<1m' if abs(left) < 1000000 else '1m-10m' if abs(left) < 10000000 else '10m-100m' if abs(left) < 100000000 else '100m-1b' if abs(left) < 1000000000 else '>=1b'
                for key in [('all','all'),('year',path.stem[:4]),('month',path.stem),('security',entry.symbol),('price_CSMAR',price_bucket),('amount_CSMAR',size_bucket),('native_batch',cs.receipt + '|' + cn.receipt)]:
                    stat = amounts[key]
                    add_stat(stat, left, right, filled, zero)
                    stat['reasons'][reason] += 1
            elif entry.field in FIELDS[:4]:
                classification, reason = 'UNRESOLVED', 'NATIVE_OHLC_PRICE_BASIS_CONFLICT'
                price_groups[(entry.date,entry.symbol)][entry.field] = (cs,cn,left,right)
            else:
                classification, reason = 'UNRESOLVED', 'NATIVE_VOLUME_CONFLICT'
            classes[(entry.field,classification,reason)] += 1
            sample_key = (entry.field,reason)
            if case_seen[sample_key] < 2:
                case_seen[sample_key] += 1
                cid = f'{path.stem}-{entry.field}-{len(cases):05d}'
                cases.append(dict(case_id=cid, sources='CSMAR|CNRDS', date=entry.date, symbol=entry.symbol, field=entry.field,
                    classification=classification, feature=reason, signed_CSMAR_minus_CNRDS=str(left-right), relative_to_CSMAR=str(abs(left-right)/abs(left)) if left else '',
                    left_locator=entry.left_locator, right_locator=entry.right_locator, left_sha256=receipts[cs.receipt]['sha256'],right_sha256=receipts[cn.receipt]['sha256'], confidence='OBSERVED_NATIVE_VALUES; CAUSAL_MECHANISM_UNVERIFIED', normalization='NONE'))
                raw_cases.append(dict(case_id=cid, left=cs._asdict(), right=cn._asdict()))
        buckets = defaultdict(Counter)
        symbols = defaultdict(set)
        days = defaultdict(set)
        for (date,symbol), records in price_groups.items():
            mask = '|'.join(f for f in FIELDS[:4] if f in records)
            cs, cn, _, _ = next(iter(records.values()))
            key = (path.stem,mask)
            buckets[key]['security_days'] += 1
            symbols[key].add(symbol)
            days[key].add(date)
            buckets[key]['filled_days'] += int(cs.filling == '1')
            cv, nv = number(cs.volume), number(cn.volume)
            buckets[key]['zero_volume_days'] += int(cv == 0 or nv == 0)
            buckets[key]['volume_equal_days'] += int(cv is not None and cv == nv)
            ca, na = number(cs.amount), number(cn.amount)
            buckets[key]['amount_equal_days'] += int(ca is not None and ca == na)
            offsets = {l-r for _,_,l,r in records.values()}
            buckets[key]['same_additive_offset_days'] += int(len(records)==4 and len(offsets)==1)
            buckets[key]['cn_close_equals_cs_adjusted_cent_days'] += int(number(cs.adj_close) is not None and number(cn.close) == number(cs.adj_close).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
            buckets[key]['cs_close_equals_cn_adjusted_cent_days'] += int(number(cn.adj_close) is not None and number(cs.close) == number(cn.adj_close).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
            if cv and ca is not None and number(cs.low) is not None:
                buckets[key]['cs_vwap_outside_native_range_days'] += int(not number(cs.low) <= ca/cv <= number(cs.high))
            if nv and na is not None and number(cn.low) is not None:
                buckets[key]['cn_vwap_outside_native_range_days'] += int(not number(cn.low) <= na/nv <= number(cn.high))
        for key, count in buckets.items():
            ohlc.append(dict(month=key[0],mismatch_mask=key[1],**count,securities=len(symbols[key]),dates=len(days[key]),first_date=min(days[key]),last_date=max(days[key])))
        # Full affected-security/month counts, not just published representative examples.
        for symbol in sorted({k[1] for k in price_groups}):
            records = {k:v for k,v in price_groups.items() if k[1] == symbol}
            monthly.append(dict(month=path.stem,symbol=symbol,security_days=len(records),first_date=min(k[0] for k in records),last_date=max(k[0] for k in records),field_mismatches=sum(len(v) for v in records.values())))
        print(path.stem, len(index), round(time.perf_counter()-month_started,2), flush=True)
        totals['index_rows'] += len(index)
    write_csv(output/'amount-difference-profile.csv',(flatten_stat(k,v) for k,v in amounts.items()))
    ohlc_fields = ['month','mismatch_mask','security_days','filled_days','zero_volume_days','volume_equal_days','amount_equal_days','same_additive_offset_days','cn_close_equals_cs_adjusted_cent_days','cs_close_equals_cn_adjusted_cent_days','cs_vwap_outside_native_range_days','cn_vwap_outside_native_range_days','securities','dates','first_date','last_date']
    write_csv(output/'ohlc-difference-profile.csv',({k:r.get(k,0) for k in ohlc_fields} for r in ohlc),ohlc_fields)
    write_csv(output/'ohlc-affected-security-month.csv',monthly)
    write_csv(output/'root-cause-classification.csv',(dict(field=f,classification=c,feature=r,count=n,confidence='NUMERIC_RELATION_CONFIRMED; SUPPLIER_CAUSE_UNVERIFIED',normalization='NONE',sources='CSMAR|CNRDS',date_range='2015-01-01/2026-10-20',evidence='existing exact difference index + native checkpoint lexemes + version-bound dictionaries',impact='see research-impact.csv',needed_evidence='supplier export precision/vintage; independent transaction or corporate-action evidence') for (f,c,r),n in classes.items()))
    write_csv(output/'representative-cases.csv',cases)
    (work/'representative-native-rows.json').write_text(json.dumps(raw_cases,ensure_ascii=False,indent=2),encoding='utf-8')
    index_manifest = json.loads((work/'index-manifest.json').read_text())
    if dict(field_counts) != index_manifest['counts']:
        raise ValueError('profile denominator differs from partitioned index')
    seconds = time.perf_counter()-started
    result = dict(field_counts=field_counts,classes=[dict(field=f,classification=c,feature=r,count=n) for (f,c,r),n in classes.items()],
        resource=dict(seconds=seconds,cpu_seconds=time.process_time()-cpu_started,average_cpu_cores=(time.process_time()-cpu_started)/seconds,throughput_rows_per_second=totals['index_rows']/seconds,peak_process_bytes=getattr(psutil.Process().memory_info(),'peak_wset',psutil.Process().memory_info().rss),workers=1,arrow_threads=1),ohlc_security_days=sum(r['security_days'] for r in ohlc),normalization_applied=False,
        restrictions=['NO LIVE MONEY','PIT_UNVERIFIED','HFQ_UNVERIFIED','FORMAL_ADMISSION_BLOCKED'])
    (output/'summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    (output/'input-manifest.json').write_text(json.dumps(dict(baseline_main=args.baseline,prior_pr=85,prior_pr_merge='f013210be4eca4e3bdff08b023350b8851c8a1ed',native=identities,difference_index=index_manifest,selection='remaining + CSMAR|CNRDS + MISMATCH + six fields; final 2021 monthly indexes only',checkpoint_policy='read only existing monthly partitions; no workbook rescans',public_policy='no native absolute values or complete raw records'),indent=2,ensure_ascii=False),encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['partition', 'diagnostic', 'profile'])
    parser.add_argument('--output')
    parser.add_argument('--baseline')
    parser.add_argument('--native', required=True)
    parser.add_argument('--work', required=True)
    args = parser.parse_args()
    globals()[args.command](args)

if __name__ == '__main__':
    main()
