"""Render aggregate native quality results without publishing supplier row data."""
import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path


def read(path):
    with path.open(encoding='utf-8',newline='') as stream:
        return list(csv.DictReader(stream))


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--private-root',type=Path,required=True)
    args=p.parse_args()
    out=args.output
    tables=read(out/'table-quality.csv')
    keys=read(out/'primary-key-quality.csv')
    fields=read(out/'field-quality.csv')
    drift=read(out/'schema-drift.csv')
    coverage=read(out/'scan-coverage.csv')
    receipts=[json.loads(p.read_text(encoding='utf-8')) for p in args.private_root.glob('*/receipt.json')]
    totals=defaultdict(lambda:dict(members=0,rows=0,cpu=0,peak=0,work_seconds=0))
    for r in receipts:
        t=totals[r['source']]
        t['members']+=1;t['rows']+=r['rows'];t['cpu']+=r['cpu_seconds'];t['peak']=max(t['peak'],r['peak_memory_bytes']);t['work_seconds']+=r['wall_seconds']
    conflict=[r for r in keys if int(r['conflicting_keys'])]
    invalid=[r for r in fields if r.get('invalid_values') and int(r['invalid_values'])]
    nulls=[r for r in fields if sum(json.loads(r['null_markers']).values())]
    lines=['# DQ-02：CSMAR/CNRDS 结构化内容质量审计','',
           '本轮按调整后的执行范围优先交付 CSMAR/CNRDS。BigQuant 暂停，其本次私有扫描中间文件已删除；原始数据未修改，不将此前扫描结果计入完成范围。','',
           '## 实际覆盖','',
           '|来源|本次完成成员|实际检查数据行|累计工作进程 CPU 秒|单进程峰值 MiB|','|---|---:|---:|---:|---:|']
    for source,t in sorted(totals.items()):
        lines.append(f"|{source}|{t['members']:,}|{t['rows']:,}|{t['cpu']:.2f}|{t['peak']/2**20:.2f}|")
    lines+=['',f'物理成员分母为 {len(coverage)}。各表精确行数、成员分母、已读数及状态见 `table-quality.csv`、`scan-coverage.csv`。工作簿标题行及 CSMAR 字段描述行与数据行分开记录。',
            '', '43 个原生日线成员继承旧行情证据：CSMAR 15 个、CNRDS 28 个。逐成员 SHA-256 与原证据核对；仅继承原先实际选择的行、字段和时间窗口，不将特定窗口外推至历史全集。这些范围标记 `PARTIAL_CONTENT_SCANNED`，未检查行数分母保持未知。',
            '', '## 精确键与冲突','',f'发现 {len(conflict)} 个表候选键有不同完整内容的冲突。候选键不是供应商认证的唯一性合同；财务报表保留报表类型、修订与披露维度，一对多和事件表保留业务维度。完整键变体及缺失、重复、冲突、跨分区重叠数量见 `primary-key-quality.csv`。','',
            '|来源|表|冲突键|重复键|跨分区重叠键|','|---|---|---:|---:|---:|']
    for r in conflict:
        lines.append(f"|{r['source']}|{r['table']}|{r['conflicting_keys']}|{r['duplicate_keys']}|{r['overlapping_partition_keys']}|")
    lines+=['','没有 keep-first/keep-last；不删除供应商记录，不自动选择版本。无已确立业务键的范围单独列为未完成键检查。',
            '', '## 字段与分区异常','',f'共有 {len(invalid)} 个分区—字段统计存在数值或日期解析失败；{len(nulls)} 个分区—字段统计观察到空值/空标记。完整统计见 `field-quality.csv`，数值观察见 `numeric-anomalies.csv`。负值和零值是统计观察，不自动视为财务错误；未根据分布修改数据。',
            '', f'记录 {len(drift)} 项结构、类型、类别集合或日期关系观察，见 `schema-drift.csv`。字段类型按原始单元格类型统计，类别集合精确比较。分区首次观察以文件名排序定位，不冒充供应商历史发布时间。列集合变化、枚举变化不直接解释为供应商错误。',
            '', '所有识别出的日期字段输出年月和季度计数，见 `date-coverage.csv`。没有以每日连续性要求判断季度财务或事件数据，也没有把证券非每日记录视为缺失交易日。',
            '', '## 尚未完成的规则与影响','',
            'FULL_CONTENT_SCANNED 表示该范围全部物理数据行和字段实际访问，不表示所有金融规则认证完成。字段字典没有确定类型或取值范围时不编造转换合同。百分比/小数尺度、单位冲突、财务恒等关系、证券状态与行业有效区间、声明分区边界及非空率漂移的完整语义规则仍未全部实现；具体可用统计与未完成项须结合机器文件查看，不报告全项目 PASS。',
            '', '空键、冲突版本和无效报告期会影响财务因子的主体/期间选择；缺值与非数值会影响特征构建和模型训练；有效日期与披露时间问题会影响 PIT 使用；股本及公司行为问题会影响收益和复权解释。本轮不执行跨表证券关联认证，不改变现有模型、因子输入或账务结果。',
            '', '## 性能与运行故障','',
            '初始四进程，原生扫描扩展为八进程；BLAS/OpenMP 每进程限制一线程。最后仅剩单个 XLSX 工作单元时使用单进程流式读取。每成员持久化统计及输入身份。精确汇总采用磁盘 SQLite 索引和分组排序，逐表删除排序工作区，保留成员证据；聚合按表断点恢复。',
            '', '第一次运行因未压缩完整行 JSON/SQLite 索引占满 E: 中止；并非供应商数据不可读。恢复保留已完成统计，迁移部分索引后补扫唯一未完成资产负债表成员。后续扫描完整行采用无损压缩。列式试验百万行从 815,353,856 字节降至 129,463,494 字节，60.819 秒；这是单成员实测，非全库保证。',
            '', '上表 CPU 为各成员记录之和，内存为单进程生命周期峰值，不是整组并发峰值。完整端到端墙钟、时间加权 CPU 利用率、全组峰值内存没有连续采样，保持未知；不能用累计进程秒替代墙钟或宣称达到性能验收。首次四个小成员校准不代表大表吞吐。',
            '', '## 机器证据与验证','',
            '统一索引见 `provenance.json` 的 `machine_index`。`verification.json` 检查物理成员分母、表/分区行数守恒和定向测试；验证通过不升级金融认证。完整行、类别值和私有坐标仅保留私有目录。',
            '', '**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**','']
    (out/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__=='__main__':
    main()
