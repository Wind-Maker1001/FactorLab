"""Reported quantitative lock events, not proof of universal trading eligibility."""
from dataclasses import dataclass


@dataclass(frozen=True)
class QuantitativeObservation:
    states: tuple[tuple[str, str, str], ...] = ()
    error: str | None = None

    def applicable_locks(self, symbol):
        return tuple((scope, name) for scope, name, state in self.states
                     if scope in ('ACCOUNT', symbol) and state == 'LOCKED')

    def report(self, symbol):
        scopes = {}
        for scope in ('ACCOUNT', symbol):
            rows = {name: state for owner, name, state in self.states if owner == scope}
            scopes[scope] = rows or {'status': 'NOT_REPORTED' if self.error is None else 'UNKNOWN'}
        return {'status': 'UNKNOWN' if self.error else 'OBSERVED', 'error': self.error,
                'missing_indicator_state': 'UNKNOWN' if self.error else 'NOT_REPORTED',
                'scopes': scopes}


def observe_quantitative(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('indicators'), dict):
        return QuantitativeObservation(error='MALFORMED_RESPONSE')
    states = {}
    error = None
    for scope, rows in payload['indicators'].items():
        if not isinstance(scope, str) or not scope or not isinstance(rows, list):
            error = 'MALFORMED_RESPONSE'
            continue
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('indicator'), str)
                    or not row['indicator'] or type(row.get('isLocked')) is not bool):
                error = 'MALFORMED_RESPONSE'
                continue
            key = (scope, row['indicator'])
            state = 'LOCKED' if row['isLocked'] else 'NOT_LOCKED'
            if key in states and states[key] != state:
                error = 'CONFLICTING_FIELDS'
                states[key] = 'LOCKED'
            else:
                states[key] = state
    return QuantitativeObservation(tuple((*key, value) for key, value in states.items()), error)


def account_can_trade(*accounts):
    flags = [account['canTrade'] for account in accounts
             if isinstance(account, dict) and 'canTrade' in account]
    if not flags or any(type(flag) is not bool for flag in flags):
        return None
    return flags[0] if all(flag is flags[0] for flag in flags) else None
