"""One trusted strategy invocation, with JSON input and no execution gateway."""
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys


def invoke(release, context, checkpoint, *, timeout=5):
    result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), str(release)],
        input=json.dumps(dict(context=context, state=checkpoint), allow_nan=False),
        text=True, encoding='utf-8', capture_output=True, timeout=timeout,
        env={key: value for key, value in os.environ.items()
             if key in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LANG'}})
    if result.returncode or len(result.stdout) > 256 * 1024:
        raise ValueError('strategy invocation failed')
    answer = json.loads(result.stdout)
    if (not isinstance(answer, dict) or not isinstance(answer.get('state'), dict)
            or not isinstance(answer.get('intents'), list) or len(answer['intents']) > 1):
        raise ValueError('invalid strategy result')
    json.dumps(answer, allow_nan=False)
    return answer


def main():
    release = Path(sys.argv[1]).resolve()
    m = json.loads((release / 'manifest.json').read_bytes())
    for requirement in m['dependencies']:
        name, version = requirement.split('==')
        if importlib.metadata.version(name) != version:
            raise ValueError('strategy dependency mismatch')
    sys.path.insert(0, str(release))
    module, function = m['entrypoint'].split(':')
    evaluate = getattr(importlib.import_module(module), function)
    request = json.load(sys.stdin)
    request['context']['configuration'] = m['configuration']
    print(json.dumps(evaluate(request['context'], request['state']), allow_nan=False))


if __name__ == '__main__':
    main()
