"""Summarize saved official responses, never execute or redownload them."""
import argparse
import hashlib
import json
from pathlib import Path


def response(directory):
    receipt = json.loads((directory / 'receipt.json').read_bytes())
    payload = (directory / 'response.bin').read_bytes()
    if hashlib.sha256(payload).hexdigest() != receipt['sha256']:
        raise ValueError('saved response does not match retrieval receipt')
    return payload, {k: v for k, v in receipt.items() if k != 'path'}


def summarize(indices, pdfs):
    result = {'schema': 'P3D05_OFFICIAL_RESPONSES_V1', 'indices': [], 'pdf_attempts': [],
              'event_completeness': 'UNKNOWN', 'historical_revision_chain': 'UNKNOWN',
              'first_public_time': 'UNKNOWN'}
    for directory in indices:
        payload, receipt = response(directory)
        data = json.loads(payload)
        rows, page = data['result'], data['pageHelp']
        complete = (page['pageCount'] == page['pageNo'] == 1
                    and len(rows) == page['total']
                    and len({r['URL'] for r in rows}) == len(rows)
                    and all(r['SECURITY_CODE'] == '601857' for r in rows))
        selected = [r for r in rows if 'A股' in r['TITLE'] and '分红' in r['TITLE'] and '实施' in r['TITLE']]
        result['indices'].append({'receipt': receipt, 'query_begin': data['beginDate'],
            'query_end': data['endDate'], 'total': page['total'], 'returned': len(rows),
            'unique_urls': len({r['URL'] for r in rows}), 'page_count': page['pageCount'],
            'pagination_coverage': 'PASS' if complete else 'FAIL',
            'scope': 'CURRENT_ENDPOINT_RESPONSE_ONLY_NOT_HISTORICAL_OR_EVENT_COMPLETENESS',
            'implementation_notices': [{k: r[k] for k in ('TITLE', 'URL', 'SSEDATE', 'ADDDATE')} for r in selected],
            'title_inventory': [{k: r[k] for k in ('TITLE', 'URL', 'SSEDATE')} for r in rows]})
    for directory in pdfs:
        payload, receipt = response(directory)
        result['pdf_attempts'].append({'receipt': receipt,
            'pdf_magic_present': payload.startswith(b'%PDF-'),
            'official_pdf_qualification': 'UNKNOWN',
            'reason': 'Response format alone does not authenticate a notice; inspect original document'})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index-dir', type=Path, action='append', required=True)
    parser.add_argument('--pdf-dir', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.index_dir, args.pdf_dir)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'indices': [(r['returned'], r['pagination_coverage']) for r in result['indices']],
                      'pdf_magic': [r['pdf_magic_present'] for r in result['pdf_attempts']]}))


if __name__ == '__main__':
    main()
