"""Publish the bounded 000540 rights investigation and frozen replay receipts."""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
SAFETY = ['NO LIVE MONEY', 'PIT_UNVERIFIED', 'HFQ_UNVERIFIED', 'FORMAL_ADMISSION_BLOCKED']
DOCUMENTS = [
    ('suspension', '2023-56.pdf', '2023-05-18', '2023-05-19',
     'https://static.cninfo.com.cn/finalpage/2023-05-19/1216852113.PDF',
     '特别提示1–2：5月18日收盘0.40元；5月19日开市起停牌；不进入退市整理期。'),
    ('decision', '2023-63.pdf', '2023-06-15', '2023-06-16',
     'https://static.cninfo.com.cn/finalpage/2023-06-16/1217074647.PDF',
     '第二部分：6月15日收到深证上〔2023〕509号决定；复核期间决定不停止执行。'),
    ('delisting', '2023-69.pdf', '2023-06-28', '2023-06-29',
     'https://static.cninfo.com.cn/finalpage/2023-06-29/1217155324.PDF',
     '第一、三部分：6月30日摘牌；6月28日聘请海通；退市登记期间主办券商登记股份并办理确权。'),
    ('registration', 'share-registration-400174.pdf', '2023-07-07', '2023-07-07',
     'https://static.cninfo.com.cn/finalpage/2023-07-07/1217260200.PDF',
     '第一、二、四、六部分：6月30日完成退出登记；400174为确权代码；账户及托管条件；待确权股份明细继续维护。'),
    ('otc_suspension', 'otc-2023-011.pdf', '2023-08-28', '2023-08-28',
     'https://pdf.dfcfw.com/pdf/H2_AN202308281595939419_1.pdf',
     '第一、二部分：7月11日法院受理重整、7月12日披露；8月30日挂牌当日起暂停转让。'),
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish(args):
    replay = json.loads((args.replay / 'replay-result.json').read_bytes())
    output = ROOT / 'docs/validation/p9-d06'
    output.mkdir(exist_ok=False)

    def save(name, data):
        (output / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    documents = [dict(id=key, uri=uri, sha256=digest(args.documents / filename),
        bytes=(args.documents / filename).stat().st_size, signed_date=signed,
        public_date=public, public_time_upper_bound=public + 'T23:59:59.999999+08:00',
        time_precision='DAY', verified_sections=excerpt, source_class='ISSUER_OR_SPONSOR_PRIMARY_DOCUMENT',
        local_retention='EXISTING_D05_ORIGINAL_NOT_COPIED')
        for key, filename, signed, public, uri, excerpt in DOCUMENTS]
    events = [
        dict(id='last_trade', date='2023-05-18', state='A_SHARE_HELD', evidence=['suspension'],
             meaning='最后实际交易日；原冻结观察及发行人均给出0.40元。'),
        dict(id='halt', date='2023-05-19', effective_time='09:30:00+08:00',
             state='SUSPENDED_PRE_DELISTING', evidence=['suspension'], meaning='开市起停牌，不进入退市整理期。'),
        dict(id='decision', date='2023-06-15', state='DELISTING_DECIDED', evidence=['decision'],
             decision_receipt_date='2023-06-15', decision_execution_date='2023-06-15',
             meaning='决定已收到且复核不停止执行；不得与6月30日摘牌混为同一事件。'),
        dict(id='last_listing_day', date='2023-06-29', evidence=['delisting'],
             derivation='6月30日摘牌的前一日；不是最后实际交易日，也不证明该日能够交易。'),
        dict(id='a_share_exit', date='2023-06-30', state='DELISTED_RIGHTS_UNRESOLVED',
             evidence=['delisting', 'registration'], exit_registration_date='2023-06-30',
             meaning='摘牌并终止上市；退出登记完成事实到7月7日公告才独立明确。'),
        dict(id='registration_arrangements', date='2023-07-07', state='OTC_REGISTRATION_PENDING',
             evidence=['registration'], meaning='确权规则公开，不是本账户登记完成日期。'),
        dict(id='reorganization_accepted', date='2023-07-11', public_date='2023-07-12',
             evidence=['otc_suspension'], evidence_level='LATER_PRIMARY_NOTICE_QUOTES_COURT_ACTION',
             meaning='（2023）黔01破申13号及（2023）黔01破6号；受理重整并指定管理人，不等于股份注销或清偿。'),
        dict(id='account_unit_deadline', date='2023-07-21', evidence=['registration'],
             meaning='特定账户/托管情形的T+15申报期限；不是所有普通投资者必须临柜的统一期限。'),
        dict(id='beijing_account_flag', date='2023-07-28', evidence=['registration'],
             status='EXPECTED_DEADLINE_NOT_COMPLETION_PROOF', meaning='预计T+20前申请添加北京市场标识（如需）。'),
        dict(id='otc_listing_and_halt', date='2023-08-30', state='OTC_NONTRADABLE',
             evidence=['otc_suspension'], meaning='400174中天3挂牌当日即暂停转让；不能认作有一笔挂牌成交。'),
    ]
    save('delisting-timeline.json', dict(schema='P9_D06_TIMELINE_V1', documents=documents, events=events,
        timing_rule='公开日期仅支持当日上海日终可得上界；事件生效日不是公开日；不得倒改历史信号。',
        vendor_export_time='INHERITED_FROZEN_MANIFEST_NOT_AN_ISSUER_PUBLICATION_TIME',
        actual_account_processing_time=None, otc_initial_registration_completion_time=None,
        subsequent_disposition='UNKNOWN; no independently verified account settlement or final share cancellation',
        bounded_followup=dict(end_exclusive='2024-03-13', otc_index_query_hits=0,
            interpretation='空镜像索引不证明没有后续公告；不推定持续停牌至窗口末日。'), safety=SAFETY))

    fund = dict(uri='https://paper.cnstock.com/html/2023-05/20/content_1769923.htm',
        sha256=digest(args.acquisition / 'penghua.html'), signed_date='2023-05-19', public_date='2023-05-20',
        public_time_upper_bound='2023-05-20T23:59:59.999999+08:00', effective_from='2023-05-18',
        effective_to=None, price_cny_per_share='0.11', applicable_accounts='鹏华旗下基金，ETF基金除外',
        method='依证监会[2017]13号及鹏华自身股票估值政策/程序，与托管行和会计师事务所协调一致；未披露计算模型。',
        account_applicable=False, before_may19_close_available=False,
        reasoning='5月19日落款不是5月19日公开证据；本研究账户非该基金，不能移植其估值。',
        sensitivity_only=dict(shares=133700, hypothetical_mark_cny='14707.00',
            versus_last_observation_cny='-38773.00', booked=False,
            available_no_earlier_than_public_upper_bound=True))
    save('valuation-evidence.json', dict(schema='P9_D06_VALUATION_V1', account='B_lightgbm/fold-0',
        target=dict(security='000540.SZ', date='2023-05-19', shares=133700),
        last_actual_observation=replay['last_000540_observation'],
        last_observation_book_value_cny='53480.00', last_observation_is_future_fair_value=False,
        candidates=[dict(policy='LAST_OBSERVED_RAW_CLOSE_CARRY', price='0.40', approved=False,
            effective_interval=None, reason='普通停牌政策无退市停牌适用依据；既有000540政策仅2023-05-04单日。'),
            dict(policy='THIRD_PARTY_FUND_VALUATION', approved=False, evidence=fund),
            dict(policy='DELISTING_IMPAIRMENT_OR_FAIR_VALUE_ADJUSTMENT', approved=False,
                 effective_interval=None, account_applicable_rule=None, price=None, status='UNKNOWN')],
        fund_rule_reference=dict(title='中国证监会关于证券投资基金估值业务的指导意见', identifier='[2017]13号',
            evidence_level='REFERENCED_BY_PENGHUA_NOTICE; standalone text not qualified in this investigation',
            factorlab_account_policy=None),
        conclusion=dict(qualified_may19_book_price=None, approved_policy=None, effective_interval=None,
            status='UNQUALIFIED_HELD_CLOSE', no_future_otc_backfill=True), safety=SAFETY))

    save('rights-transition.json', dict(schema='P9_D06_RIGHTS_V1',
        entity='中天金融集团股份有限公司', old_security='000540.SZ', otc_code='400174', otc_name='中天3',
        nature='同一发行人原股份退出深市登记，转退市板块确权登记；不是普通更名或自动可交易别名。',
        legal_security_continuity='PRIMARY_DOCUMENT_CONFIRMED', old_class='人民币普通股/A股',
        rights_class_change='NOT_ESTABLISHED', conversion_ratio=None,
        ratio_reason='公告是原股份确权，不是披露了换股比例的证券交换事件。',
        quantity=dict(frozen_last_holding=133700, contingent_legal_claim_shares=133700,
            assumption='在最后已验收持仓后未发生另行核实的过户、扣划、注销或重整股数调整；不得计作实际登记证明。',
            actual_otc_registered_shares=None, initial_register_reconciled=False,
            source_basis='registration第一、二、六部分；原股份明细及待确权机制，无增股或现金转换授权'),
        account_conditions=dict(original_shenzhen_account='原则上可直接使用；境外托管、沪市配售及未了结业务有例外。',
            ordinary_brokerage_customer='原托管券商具备全国股转经纪资格时维持托管关系，批量申报，无需单独确认托管关系。',
            other_custody_cases='无资格券商、无托管、自营及特殊机构等按公告申报；逾期可暂托管至海通。',
            broker_qualification=None, account_type=None, custody_unit=None,
            required_action_completed=None, actual_registration_evidence=None,
            pending_shares='未确认持有人专用账户的股份由海通维护明细；并非价值为零或股份消灭。',
            trading='完成登记后可查询；按需加挂资金账户等；交易结算资格不等于估值或订单成交。'),
        representation=dict(frozen_ledger_symbol_unchanged='000540.SZ', future_state='OTC_REGISTRATION_PENDING',
            legal_state='OTC_RIGHTS_CONDITIONALLY_RECOGNIZED', account_state='REGISTRATION_UNVERIFIED',
            share_change_postings=0, cash_postings=0, actual_disposal_cash=None,
            otc_value=None, account_valuation=None, normal_a_share_execution_allowed=False,
            later_state_not_posted_to_frozen_may18_account=True),
        reuse=dict(identity='cross_market.identity.IdentityRegistry: separate listing episodes; no market-changing alias',
            corporate_action='strategies.shared_corporate_actions.saved_path_accounting: EVENT_UNRESOLVED / UNSUPPORTED_SHARE_CHANGE',
            implementation='No new lifecycle engine or cash action. Evidence annotations are not executor holdings.'),
        sensitivity_or_actual_account_proof='RESEARCH_LEGAL_CLAIM_CONDITION_ONLY; NOT_BROKER_STATEMENT', safety=SAFETY))

    save('execution-results.json', dict(schema='P9_D06_EXECUTION_V1', **replay,
        window=dict(start='2023-03-13', end_exclusive='2024-03-13', securities=128),
        newly_accepted_sessions=len(replay['newly_qualified_dates']), remaining_sessions=198,
        failed_day_partial_rows_accepted=False, original_accepted_sessions_rewritten=False, safety=SAFETY))
    save('remaining-blockers.json', dict(schema='P9_D06_BLOCKERS_V1', first_actual_blocker=replay['blocker'],
        new_later_blocker_reached=False, qualified_may19_valuation=False,
        required_next_evidence=['适用于本模拟账户且有历史可得时限的退市停牌估值合同、方法及有效日期。',
            '若推进至退市登记，核对账户类型、托管券商资格及登记/确权资格。',
            '若发生重整换股、注销、清偿或出售，仅凭独立条款和账户资格核定股数及现金。'],
        later_rights_are_not_first_actual_execution_blockers=True,
        todo='TODO: obtain an account-applicable historical mark before extending accepted sessions.', safety=SAFETY))

    tests = ET.parse(args.tests).getroot()
    suites = list(tests) if tests.tag == 'testsuites' else [tests]
    resources = json.loads(next(line for line in reversed(args.run_log.read_text(encoding='utf-8').splitlines())
        if line.startswith('{"status":')))['resources']
    save('verification.json', dict(schema='P9_D06_VERIFICATION_V1', status='P9_D06_PARTIAL',
        execution_status=replay['status'], classification_reason='独立原件确立法律权益及确权条件，但未闭合5月19日账户估值。',
        baseline='5fdbba7a00c3ada34b7e5086a87c593d4305c867',
        pr109=dict(state='MERGED', merged_at='2026-11-11T18:11:22Z',
            merge_commit='5e020c1238c366d03090be8ab353a1ca257629d1'),
        private_replay_receipt_sha256=digest(args.replay / 'replay-result.json'),
        private_artifacts=replay['private_receipt_sha256'],
        tests=dict(tests=sum(int(s.get('tests', 0)) for s in suites),
            failures=sum(int(s.get('failures', 0)) for s in suites),
            errors=sum(int(s.get('errors', 0)) for s in suites),
            junit_sha256=digest(args.tests), files=['test_p9_d06.py', 'test_p9_d05.py',
                'test_p9_market_evidence.py', 'test_shared_corporate_actions.py']),
        resources=dict(**resources, maximum_concurrent_python_processes=1, numerical_threads=1,
            test_peak_working_set_bytes=None, temporary_disk_budget_bytes=536870912,
            memory_soft_limit_bytes=4294967296, existing_originals_copied=False,
            selected_shards=len(replay['source_shards']),
            selected_rows=sum(s['selected_rows'] for s in replay['source_shards']),
            uncompressed_shard_bytes=sum(s['uncompressed_bytes'] for s in replay['source_shards'])),
        recovery='首次D06入口在隔离模式下缺少同目录导入路径，未创建账本；修正后成功。随后补记录原生观察身份并生成最终收据。',
        source_gaps=['独立基金估值规则正文未取得合格引用；不赋予本研究账户权限。',
            '全国股转公告入口HTTP403；有界400174镜像公告索引返回0条，不作为事件完整性证明。',
            '7月11日法院事项由8月原件引述，未冒充已读取法院裁定原件。'], safety=SAFETY))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('replay', 'documents', 'acquisition', 'tests', 'run-log'):
        parser.add_argument('--' + name, required=True, type=Path)
    publish(parser.parse_args())
