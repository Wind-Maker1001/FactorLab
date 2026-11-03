"""P2-01E: compare real borrowed references against native Python calls."""
from collections import OrderedDict, UserDict, defaultdict
from dataclasses import FrozenInstanceError
import gc
import inspect
from pathlib import Path
import pickle
import runpy
from types import MappingProxyType
import weakref

import numpy as np
import pandas as pd
import pytest

from factorlab.models.factor_score import FactorScoreModel
from factorlab.models.factory import (
    DEFAULT_CATALOG, InvalidModelSpec, LegacyModelRequest, ModelCatalogSnapshot,
    ModelRegistration, TrustedLegacyReference, create_legacy_model,
    effective_spec_from_legacy,
)
from factorlab.models.ridge import RidgeResearchModel


def request(weights, *, profile='legacy_csi500_v1', keyword=False, kwargs=None,
            model='factor_score', version='legacy-v1'):
    arguments = dict(kwargs or {})
    if keyword:
        arguments['weights'] = weights
    return LegacyModelRequest(model, version, profile, arguments,
                              () if keyword else (weights,))


@pytest.mark.parametrize('profile', ['legacy_csi500_v1', 'cross_market_fixed_v1'])
@pytest.mark.parametrize('kind', [dict, OrderedDict])
@pytest.mark.parametrize('keyword', [False, True])
def test_explicit_reference_reused_at_each_constructor_entry(profile, kind, keyword):
    original = kind([('REV1', -1.0), ('MOM20', 1.0)])
    received = []

    class Spy(FactorScoreModel):
        def __init__(self, *args, **kwargs):
            weights = kwargs['weights'] if keyword else args[0]
            assert weights is original
            received.append((args, kwargs, weights))
            super().__init__(*args, **kwargs)

    r = request(TrustedLegacyReference(original), profile=profile, keyword=keyword)
    models = [create_legacy_model(r, constructor_type=Spy) for _ in range(2)]
    assert models[0] is not models[1]
    assert received[0][2] is received[1][2] is original
    assert all(len(a) == (0 if keyword else 1) for a, _, _ in received)
    assert all(tuple(k) == (('weights',) if keyword else ()) for _, k, _ in received)
    assert all(m.weights is not original for m in models)
    assert all(type(m.weights) is OrderedDict for m in models)
    assert vars(models[0]) == vars(FactorScoreModel(original))


@pytest.mark.parametrize('normalization', [False, True, np.bool_(True)])
def test_p202_two_real_folds_preserve_runner_reference_and_explicit_keyword(monkeypatch, normalization):
    from factorlab.validation import walk_forward as wf
    # Original test data generation is reused read-only, with no Oracle edits.
    fixture = runpy.run_path(str(Path(__file__).with_name('test_walk_forward_stage_diagnostics.py')))
    runner = wf.WalkForwardRunner(fixture['_splitter'](), model_name='factor_score',
        feature_names=('REV1', 'MOM20'), top_k=2, minimum_cross_section=4,
        **dict(fixture['_options']('factor_score'), normalize_by_absolute_weight=normalization))
    received = []
    models = []

    class PositionalOnly(FactorScoreModel):
        def __init__(self, weights, /, *, normalize_by_absolute_weight=False):
            assert weights is runner.factor_weights
            received.append((weights, normalize_by_absolute_weight))
            super().__init__(weights, normalize_by_absolute_weight=normalize_by_absolute_weight)
            models.append(self)

    class PublicInterfaceProbe(FactorScoreModel):
        def __new__(cls, *args, **kwargs):
            assert len(args) == 1
            assert tuple(kwargs) == ('normalize_by_absolute_weight',)
            return create_legacy_model(request(TrustedLegacyReference(args[0]), kwargs=kwargs),
                                       constructor_type=PositionalOnly)

    monkeypatch.setattr(wf, 'FactorScoreModel', PublicInterfaceProbe)
    dates, frame = fixture['_dataset']()
    result = runner.run(frame, calendar=dates)
    assert len(received) == len(result.fold_summary) == 2
    assert received[0][0] is received[1][0] is runner.factor_weights
    assert all(n is runner.normalize_by_absolute_weight for _, n in received)
    assert models[0] is not models[1]


def test_p203_original_make_model_expression_and_omission(monkeypatch):
    from factorlab.cross_market import research
    original_inputs, received = [], []

    class PositionalOnly(FactorScoreModel):
        def __init__(self, weights, /):
            assert weights is original_inputs[-1]
            received.append(weights)
            super().__init__(weights)

    class PublicInterfaceProbe(FactorScoreModel):
        def __new__(cls, *args, **kwargs):
            assert len(args) == 1 and kwargs == {}
            original_inputs.append(args[0])
            r = request(TrustedLegacyReference(args[0]), profile='cross_market_fixed_v1')
            return create_legacy_model(r, constructor_type=PositionalOnly)

    monkeypatch.setattr(research, 'FactorScoreModel', PublicInterfaceProbe)
    model = research.make_model('FactorScore', ('REV1', 'MOM20'))
    assert received[0] is original_inputs[0]
    assert not model.normalize_by_absolute_weight


@pytest.mark.parametrize('keyword', [False, True])
def test_default_snapshot_remains_frozen_and_fresh_per_construction(keyword):
    original = {'a': 1}
    r = request(original, keyword=keyword)
    original['a'] = 8
    received = []

    class Spy(FactorScoreModel):
        def __init__(self, *args, **kwargs):
            weights = kwargs['weights'] if keyword else args[0]
            received.append(weights)
            assert weights is not original and weights == {'a': 1}
            super().__init__(*args, **kwargs)

    create_legacy_model(r, constructor_type=Spy)
    create_legacy_model(r, constructor_type=Spy)
    assert received[0] is not received[1]
    public = r.constructor_arguments['weights'] if keyword else r.positional_arguments[0]
    public['a'] = 99
    assert create_legacy_model(r).weights == {'a': 1}


def test_only_borrowed_graph_is_live_and_shared_with_other_argument_aliases():
    weights = {'a': [1]}
    weights['cycle'] = weights
    separate = [10]
    r = request(TrustedLegacyReference(weights), kwargs={
        'alias': weights, 'child': weights['a'], 'other': separate, 'other_alias': separate})
    weights['a'].append(2)
    separate.append(20)
    calls = []

    class Spy(FactorScoreModel):
        def __init__(self, w, /, *, alias, child, other, other_alias):
            assert w is alias is weights and w['cycle'] is w
            assert child is weights['a'] and child == [1, 2]
            assert other is other_alias and other is not separate and other == [10]
            calls.append(other)
            super().__init__({'a': 1})

    create_legacy_model(r, constructor_type=Spy)
    create_legacy_model(r, constructor_type=Spy)
    assert calls[0] is not calls[1]


def test_default_joint_snapshot_cycles_and_cross_channel_aliases():
    weights = {'a': 1}; loop = [weights]; loop.append(loop)
    r = request(weights, kwargs={'alias': weights, 'loop': loop})
    calls = []

    class Spy(FactorScoreModel):
        def __init__(self, w, /, *, alias, loop):
            assert w is alias is loop[0] and loop[1] is loop
            assert w is not weights
            calls.append(w)
            super().__init__(w)

    for _ in range(2): create_legacy_model(r, constructor_type=Spy)
    assert calls[0] is not calls[1]


@pytest.mark.parametrize('model,version,profile', [
    ('ridge', 'legacy-v1', 'legacy_csi500_v1'),
    ('lightgbm', 'legacy-v1', 'cross_market_fixed_v1'),
    ('xgboost', 'legacy-v1', 'legacy_csi500_v1'),
    ('unknown', 'legacy-v1', 'legacy_csi500_v1'),
    ('factor_score', 'future', 'legacy_csi500_v1'),
    ('factor_score', 'legacy-v1', 'research_v2'),
    ('factor_score', 'legacy-v1', 'unapproved'),
])
def test_reference_scope_rejected_at_request_creation(model, version, profile):
    with pytest.raises(InvalidModelSpec, match='approved legacy profile'):
        request(TrustedLegacyReference({'a': 1}), model=model, version=version, profile=profile)


@pytest.mark.parametrize('value', [[], (), UserDict(a=1), defaultdict(int, a=1),
    MappingProxyType({'a': 1}), np.array([1]), object(), 'borrow', None])
def test_unsupported_root_types_rejected_without_protocols(value):
    with pytest.raises(InvalidModelSpec, match='exact dict or OrderedDict'):
        TrustedLegacyReference(value)


def test_custom_container_hooks_never_called():
    class Hostile(dict):
        def __copy__(self): raise AssertionError('copy hook')
        def __deepcopy__(self, memo): raise AssertionError('deepcopy hook')
        def __reduce__(self): raise AssertionError('reduce hook')
        def items(self): raise AssertionError('custom iteration')
    with pytest.raises(InvalidModelSpec): TrustedLegacyReference(Hostile(a=1))


@pytest.mark.parametrize('borrow', [False, True])
def test_opaque_values_are_not_inspected_through_custom_class_property(borrow):
    class Opaque:
        @property
        def __class__(self):
            raise AssertionError('reference recognition invoked caller code')

    opaque = Opaque()
    weights = {'a': 1}
    r = request(TrustedLegacyReference(weights) if borrow else weights,
                kwargs={'opaque': opaque})

    class Spy(FactorScoreModel):
        def __init__(self, w, /, *, opaque):
            assert type(opaque) is Opaque
            super().__init__(w)

    create_legacy_model(r, constructor_type=Spy)


@pytest.mark.parametrize('where', ['second_positional', 'other_keyword', 'nested_list', 'nested_dict', 'borrowed_graph'])
def test_marker_cannot_escape_to_other_slots_or_nested_values(where):
    marker = TrustedLegacyReference({'a': 1})
    args, kwargs = ({'a': 1},), {}
    if where == 'second_positional': args += (marker,)
    elif where == 'other_keyword': kwargs['normalize_by_absolute_weight'] = marker
    elif where == 'nested_list': kwargs['other'] = [marker]
    elif where == 'nested_dict': args = ({'a': marker},)
    else: args = (TrustedLegacyReference({'nested': marker}),)
    with pytest.raises(InvalidModelSpec, match='weights arguments'):
        LegacyModelRequest('factor_score', 'legacy-v1', 'legacy_csi500_v1', kwargs, args)


@pytest.mark.parametrize('kwargs', [{'reference_mode': True}, {'trusted_reference': True}, {'copy': False}])
def test_string_configuration_cannot_enable_borrowing(kwargs):
    weights = {'a': 1}; r = request(weights, kwargs=kwargs); seen = []
    class Spy(FactorScoreModel):
        def __init__(self, *a, **k):
            assert a[0] is not weights
            seen.append(a[0]); super().__init__(*a, **k)
    with pytest.raises(TypeError): create_legacy_model(r, constructor_type=Spy)
    assert len(seen) == 1


def test_native_duplicate_keyword_error_not_prebound_or_retried():
    weights = {'a': 1}
    r = request(TrustedLegacyReference(weights), kwargs={'weights': TrustedLegacyReference(weights)})
    with pytest.raises(TypeError) as direct: FactorScoreModel(weights, weights=weights)
    with pytest.raises(TypeError) as adapted: create_legacy_model(r)
    assert str(direct.value) == str(adapted.value)
    assert type(direct.value.__cause__) is type(adapted.value.__cause__)


@pytest.mark.parametrize('weights,kwargs', [({}, {}), ({'a': 0}, {}), ({'a': 'bad'}, {}),
    ({'a': True}, {}), ({'a': np.inf}, {}), ({'a': 1}, {'normalize_by_absolute_weight': None})])
def test_native_validation_errors_and_causes_retain_construction_stage(weights, kwargs):
    r = request(TrustedLegacyReference(weights), kwargs=kwargs)  # no eager numeric validation
    with pytest.raises(Exception) as direct: FactorScoreModel(weights, **kwargs)
    with pytest.raises(type(direct.value)) as adapted: create_legacy_model(r)
    assert str(adapted.value) == str(direct.value)
    assert type(adapted.value.__cause__) is type(direct.value.__cause__)
    assert str(adapted.value.__cause__) == str(direct.value.__cause__)


def test_mutation_failure_preserves_same_exception_cause_and_no_retry():
    weights = {'a': 1}; cause = ValueError('cause'); failure = RuntimeError('constructor failed')
    calls = []
    class Mutating(FactorScoreModel):
        def __init__(self, w, /):
            assert w is weights
            calls.append(w); w['a'] += 1
            raise failure from cause
    r = request(TrustedLegacyReference(weights))
    with pytest.raises(RuntimeError) as error: create_legacy_model(r, constructor_type=Mutating)
    assert error.value is failure and error.value.__cause__ is cause
    assert len(calls) == 1 and weights['a'] == 2


def test_mutations_between_and_inside_constructions_match_direct_calls():
    weights = {'a': 1}; seen = []
    class Mutating(FactorScoreModel):
        def __init__(self, w, /):
            assert w is weights
            seen.append(w['a']); w['a'] += 1; super().__init__(w)
    r = request(TrustedLegacyReference(weights))
    weights['a'] = 5
    first = create_legacy_model(r, constructor_type=Mutating)
    second = create_legacy_model(r, constructor_type=Mutating)
    assert seen == [5, 6] and weights['a'] == 7
    assert first.weights == {'a': 6} and second.weights == {'a': 7}


def test_borrowed_lifetime_held_by_request_and_released_after_last_owner():
    weights = OrderedDict(a=1); weak = weakref.ref(weights)
    marker = TrustedLegacyReference(weights); r = request(marker)
    del weights, marker
    gc.collect(); assert weak() is not None
    model = create_legacy_model(r)
    assert model.weights is not weak()
    del r
    gc.collect(); assert weak() is None
    assert model.weights == {'a': 1}


def test_reference_properties_are_explicitly_live_but_outer_channels_are_copied():
    weights = {'a': 1}; r = request(TrustedLegacyReference(weights))
    assert r.positional_arguments[0] is weights
    with pytest.raises(FrozenInstanceError): r.model_id = 'ridge'
    with pytest.raises(FrozenInstanceError): TrustedLegacyReference(weights).value = {}
    r.positional_arguments[0]['a'] = 2
    assert create_legacy_model(r).weights == {'a': 2}


@pytest.mark.parametrize('kwargs', [{}, {'normalize_by_absolute_weight': False}])
def test_effective_observer_keeps_original_field_semantics(kwargs):
    r = request(TrustedLegacyReference({'a': 1}), kwargs=kwargs)
    m = create_legacy_model(r)
    s = effective_spec_from_legacy(m, model_id='factor_score', implementation_version='legacy-v1',
                                  profile_id='legacy_csi500_v1', request=r)
    assert s.positional_argument_names == ('weights',)
    assert s.explicit_keyword_arguments == tuple(kwargs)
    assert s.explicit_arguments == ('weights',) + tuple(kwargs)
    assert s.defaults_used == (() if kwargs else ('normalize_by_absolute_weight',))
    assert s.parameters['weights'] == {'a': 1}


def test_original_model_pickle_state_and_predictions_are_unchanged():
    weights = OrderedDict(a=1, b=-2)
    direct = FactorScoreModel(weights)
    adapted = create_legacy_model(request(TrustedLegacyReference(weights)))
    assert pickle.dumps(direct) == pickle.dumps(adapted)
    X = pd.DataFrame({'a': [1., 2., np.nan], 'b': [2., 1., 3.]})
    direct.fit(X); adapted.fit(X)
    pd.testing.assert_series_equal(direct.predict(X), adapted.predict(X), check_exact=True)
    assert pickle.dumps(direct) == pickle.dumps(adapted)


@pytest.mark.parametrize('constructor', [lambda **k: None, RidgeResearchModel, 'FactorScoreModel'])
def test_original_class_requirement_is_preserved(constructor):
    r = request(TrustedLegacyReference({'a': 1}))
    with pytest.raises(InvalidModelSpec, match='registered original class'):
        create_legacy_model(r, constructor_type=constructor)


def test_catalog_cannot_substitute_other_registered_identity():
    original = DEFAULT_CATALOG.resolve('factor_score', 'legacy-v1')
    registration = ModelRegistration('factor_score', 'legacy-v1', original.parameter_defaults,
                                     original.build, legacy_constructor=RidgeResearchModel)
    catalog = ModelCatalogSnapshot({('factor_score', 'legacy-v1'): registration})
    with pytest.raises(InvalidModelSpec, match='registered original model'):
        create_legacy_model(request(TrustedLegacyReference({'a': 1})), catalog=catalog)


def test_four_and_five_argument_request_signature_and_alias_remain_compatible():
    assert tuple(inspect.signature(LegacyModelRequest).parameters) == (
        'model_id', 'implementation_version', 'profile_id', 'constructor_arguments', 'positional_arguments')
    assert create_legacy_model(request(TrustedLegacyReference({'a': 1}), model='FactorScore')).weights == {'a': 1}
