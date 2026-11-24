"""Reproduce the cancellation fix using a credential-free environment (no network tests)."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
REGRESSION = [
    'tests/integrations/test_unified_strategy.py',
    'tests/integrations/test_binance_demo_strategy.py',
    'tests/integrations/test_binance_demo_strategy_session.py',
    'tests/integrations/test_demo_strategy_supervisor.py',
    'tests/integrations/test_binance_demo_grid_bridge.py',
    'tests/research/test_grid_execution.py',
    'tests/research/test_custom_strategy.py',
    'tests/research/test_research_workflow.py',
    'tests/trading/test_strategy_pipeline.py',
    'tests/integrations/test_demo_strategy_budget.py',
    'tests/trading/test_strategy_replay.py',
    'tests/integrations/test_protocol_demo_working_order.py',
    'tests/integrations/test_protocol_research_outstanding.py',
    'tests/integrations/test_protocol_grid_withdrawal.py',
    'tests/integrations/test_protocol_order_outbox.py',
]
SAFETY = [
    'tests/integrations/test_binance_demo_runtime.py',
    'tests/integrations/test_demo_continuous_campaign.py',
    'tests/integrations/test_binance_demo_network.py',
    'tests/integrations/test_binance_bounded_quote.py',
]
FIFO = [
    'tests/integrations/test_binance_demo_fill_closed_loop.py',
    'tests/integrations/test_demo_breakout_exit_f11.py',
    'tests/integrations/test_demo_funding_ledger_f10.py',
    'tests/integrations/test_demo_receipts_m19.py',
]
if __name__ == '__main__':
    mode = sys.argv[1]
    if mode not in {'baseline', 'cancellation', 'regression', 'safety', 'fifo', 'compile'}:
        raise SystemExit('mode: baseline | cancellation | regression | safety | fifo | compile')
    env = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'PATHEXT')
           if key in os.environ}
    git = shutil.which('git')
    env.update(PATH=os.pathsep.join([str(Path(sys.executable).parent), str(Path(git).parent),
                                    str(Path(env.get('SYSTEMROOT', '/')) / 'System32')]),
               PYTHONPATH=str(ROOT / 'src'), PYTHONIOENCODING='utf-8', PYTHONUTF8='1',
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', GIT_CONFIG_COUNT='1',
               GIT_CONFIG_KEY_0='safe.directory', GIT_CONFIG_VALUE_0=str(ROOT))
    tests = REGRESSION if mode == 'regression' else SAFETY if mode == 'safety' else FIFO if mode == 'fifo' else [
        'tests/integrations/test_protocol_cancel_outbox.py']
    args = [*tests, '-q', '-o', 'junit_family=xunit1',
            '--junitxml=docs/validation/p8-s01/cancel-' + mode + '.xml']
    command = [sys.executable, '-m', 'pytest', *args]
    if mode == 'baseline':
        args += ['-k', 'pre_delete_facts_failure or cancel_survives_pause or lost_delete_reply or '
                 '(crash_after_attempt and not True)']
        program = '''
import subprocess, pytest
import factorlab.strategies.decision_runtime as runtime
source = subprocess.check_output(['git', 'show',
    '923a512a2fa8c191eb3451445b11663d60e3b809:src/factorlab/strategies/decision_runtime.py'],
    text=True, encoding='utf-8')
exec(compile(source, runtime.__file__, 'exec'), runtime.__dict__)
raise SystemExit(pytest.main(ARGS))
'''.replace('ARGS', repr(args))
        command = [sys.executable, '-c', program]
    elif mode == 'compile':
        command = [sys.executable, '-m', 'compileall', '-q', 'src', 'tests']
    result = subprocess.run(command, cwd=ROOT, env=env)
    if mode != 'compile':
        # Pytest failure tracebacks include trailing spaces in JUnit text.
        report = ROOT / ('docs/validation/p8-s01/cancel-' + mode + '.xml')
        if report.exists():
            report.write_text('\n'.join(line.rstrip() for line in report.read_text(
                encoding='utf-8').split('\n')), encoding='utf-8')
    raise SystemExit(result.returncode)
