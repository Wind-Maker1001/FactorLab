"""Finite-lifetime procfs/cgroup sampling for a Colab controller, not research work."""

import json
from pathlib import Path
import threading
import time


def counters(path):
    return {line.split()[0]: int(line.split()[1]) for line in path.read_text().splitlines()}


def snapshot():
    mem = counters(Path('/proc/meminfo'))
    vm = counters(Path('/proc/vmstat'))
    return {
        'time': time.time(), 'available_kib': mem['MemAvailable:'],
        'swap_used_kib': mem['SwapTotal:'] - mem['SwapFree:'],
        'pswpin': vm['pswpin'], 'pswpout': vm['pswpout'], 'oom_kill': vm['oom_kill'],
        'memory_pressure': Path('/proc/pressure/memory').read_text(),
        'cpu_pressure': Path('/proc/pressure/cpu').read_text(),
    }


class Resources:
    def __init__(self, root):
        self.root = root
        self.stop = threading.Event()
        relative = next(line.split('::', 1)[1] for line in
                        Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
        self.cgroup = Path('/sys/fs/cgroup') / relative.lstrip('/')
        self.before = snapshot()
        self.minimum = self.before['available_kib']
        self.samples = 0
        self.error = None
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()

    def sample(self):
        try:
            self.sample_until_stopped()
        except Exception as exc:
            self.error = exc

    def sample_until_stopped(self):
        with (self.root / 'resources.jsonl').open('a', encoding='utf-8') as stream:
            while not self.stop.is_set():
                value = snapshot()
                self.minimum = min(self.minimum, value['available_kib'])
                self.samples += 1
                stream.write(json.dumps(value) + '\n')
                stream.flush()
                self.stop.wait(1)

    def finish(self):
        self.stop.set()
        self.thread.join()
        if self.error is not None:
            raise RuntimeError('Resource sampling failed') from self.error
        after = snapshot()
        cpu = counters(self.cgroup / 'cpu.stat')
        wall = after['time'] - self.before['time']
        result = {
            'before': self.before, 'after': after,
            'minimum_available_kib': min(self.minimum, after['available_kib']),
            'samples': self.samples, 'wall_seconds': wall,
            'memory_peak_bytes': int((self.cgroup / 'memory.peak').read_text()),
            'memory_events': counters(self.cgroup / 'memory.events'),
            'cpu_stat': cpu, 'cpu_percent_one_core': cpu['usage_usec'] / 1e6 / wall * 100,
            'memory_pressure': (self.cgroup / 'memory.pressure').read_text(),
            'cpu_pressure': (self.cgroup / 'cpu.pressure').read_text(),
            'disk_bytes': {str(p.relative_to(self.root)): p.stat().st_size
                           for p in self.root.rglob('*') if p.is_file()},
        }
        (self.root / 'resources.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
