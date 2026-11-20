"""Exercise the installed CI-only offline unit on a disposable Linux runner."""
import json
import signal
import subprocess


def systemctl(*args):
    return subprocess.check_output(['sudo', 'systemctl', *args], text=True).strip()


def main():
    signal.alarm(150)
    unit = 'factorlab-demo-ci.service'
    journal = subprocess.Popen(['sudo', 'journalctl', '--follow', '--lines=0',
        '--output=cat', '--unit', unit], stdout=subprocess.PIPE, text=True)
    try:
        systemctl('start', unit)
        def heartbeat(previous_pid=None):
            for line in journal.stdout:
                if line.startswith('{'):
                    value = json.loads(line)
                    if value.get('event') == 'cloud_health' and str(value['pid']) != previous_pid:
                        return value
            raise RuntimeError('journal ended before worker heartbeat')
        assert heartbeat()['event'] == 'cloud_health'
        pid = systemctl('show', unit, '--property=MainPID', '--value')
        # Consume an actual SIGKILL, not a clean service stop.
        systemctl('kill', '--signal=SIGKILL', '--kill-whom=main', unit)
        heartbeat(pid)
        assert systemctl('show', unit, '--property=MainPID', '--value') != pid
        assert int(systemctl('show', unit, '--property=NRestarts', '--value')) >= 1
        systemctl('stop', unit)
        assert systemctl('show', unit, '--property=ActiveState', '--value') == 'inactive'
        print(json.dumps(dict(systemd_sigkill_restart=True, systemd_sigterm_stop=True)))
    finally:
        systemctl('stop', unit)
        journal.terminate()
        journal.communicate(timeout=10)


if __name__ == '__main__':
    main()
