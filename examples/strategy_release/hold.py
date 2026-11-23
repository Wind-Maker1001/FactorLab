"""Deployment conformance strategy. No profitability/selection claim, no trade intents."""


def evaluate(context, state):
    if context['event'] == 'probe':
        return dict(state=state, intents=[])
    if context['configuration'].get('inject_failure'):
        raise RuntimeError('deliberate Demo conformance fault')
    return dict(state=dict(bars=state.get('bars', 0) + 1), intents=[])
