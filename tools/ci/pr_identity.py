"""Bind a trusted PR risk plan to its event, immutable subject and run attempt."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


SCHEMA = 'FACTORLAB_PR_VALIDATION_IDENTITY_V1'


def git(repository, *args):
    return subprocess.check_output(['git', '-C', str(repository), *args], text=True).strip()


def event_identity(event, environ):
    pr = event.get('pull_request')
    return dict(repository=environ['GITHUB_REPOSITORY'],
                run_id=environ['GITHUB_RUN_ID'], run_attempt=environ['GITHUB_RUN_ATTEMPT'],
                event=environ['GITHUB_EVENT_NAME'], event_sha=environ['GITHUB_SHA'],
                pr=event.get('number') if pr else None,
                base=pr['base']['sha'] if pr else environ['GITHUB_SHA'],
                pr_head=pr['head']['sha'] if pr else environ['GITHUB_SHA'])


def subject(repository, expected):
    head = git(repository, 'rev-parse', 'HEAD')
    if expected['pr'] is not None:
        parents = git(repository, 'show', '-s', '--format=%P', head).split()
        if parents != [expected['base'], expected['pr_head']]:
            raise ValueError('candidate merge parents do not match event base and PR head')
    elif head != expected['pr_head']:
        raise ValueError('candidate head does not match event')
    return head


def issue(repository, expected):
    return dict(schema=SCHEMA, **expected, subject=subject(repository, expected))


def validate(repository, expected, identity, raw_plan, pinned_subject, pinned_digest):
    if (identity.get('schema') != SCHEMA or
            any(identity.get(key) != value for key, value in expected.items())):
        raise ValueError('validation identity event or run mismatch')
    if identity.get('subject') != pinned_subject or subject(repository, expected) != pinned_subject:
        raise ValueError('validation identity checkout mismatch')
    digest = hashlib.sha256(raw_plan).hexdigest()
    if digest != pinned_digest or identity.get('risk_sha256') != digest:
        raise ValueError('risk plan issuer digest mismatch')
    plan = json.loads(raw_plan)
    if (plan.get('schema') != 'FACTORLAB_PR_RISK_V2' or
            plan.get('head') != pinned_subject or
            (expected['pr'] is not None and plan.get('base') != expected['base'])):
        raise ValueError('risk plan schema or event subject mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['issue', 'seal', 'verify'])
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--identity', type=Path, required=True)
    parser.add_argument('--risk-plan', type=Path)
    args = parser.parse_args()
    expected = event_identity(json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8')),
                              os.environ)
    if args.action == 'issue':
        identity = issue(args.repository, expected)
        args.identity.write_text(json.dumps(identity, indent=2) + '\n', encoding='utf-8')
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
            stream.write(f"subject={identity['subject']}\n")
    elif args.action == 'seal':
        identity = json.loads(args.identity.read_text(encoding='utf-8'))
        if identity != issue(args.repository, expected):
            raise ValueError('validation identity changed before sealing')
        raw = args.risk_plan.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        identity['risk_sha256'] = digest
        validate(args.repository, expected, identity, raw, identity['subject'], digest)
        args.identity.write_text(json.dumps(identity, indent=2) + '\n', encoding='utf-8')
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as stream:
            stream.write(f'risk_sha256={digest}\n')
    else:
        identity = json.loads(args.identity.read_text(encoding='utf-8'))
        validate(args.repository, expected, identity, args.risk_plan.read_bytes(),
                 os.environ['VALIDATION_SUBJECT'], os.environ['RISK_SHA256'])
        print(json.dumps(dict(status='PASS', checkout=str(args.repository.resolve()), **identity)))


if __name__ == '__main__':
    main()
