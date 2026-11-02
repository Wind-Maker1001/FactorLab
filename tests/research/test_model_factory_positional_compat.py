"""P2-01D: original Python calls are the oracle, including their failures."""
from collections import OrderedDict, UserDict, defaultdict
from dataclasses import FrozenInstanceError
import inspect
import pickle
from types import MappingProxyType

import numpy as np
import pandas as pd
import pytest

from factorlab.models.factory import (
    LegacyModelRequest, create_legacy_model, effective_spec_from_legacy,
    InvalidModelSpec,
)
from factorlab.models.factor_score import FactorScoreModel
from factorlab.models.ridge import RidgeResearchModel
from factorlab.models.lightgbm import LightGBMResearchModel
from factorlab.models.xgboost import XGBoostResearchModel


def request(args=(), kwargs=None, name='factor_score'):
    return LegacyModelRequest(name, 'legacy-v1', 'research_v2',
                              {} if kwargs is None else kwargs, args)


@pytest.mark.parametrize('args,kwargs', [
    (({'b': '-1', 'a': 2},), {}),
    (({'a': 1},), {'normalize_by_absolute_weight': False}),
    (({'a': 1},), {'normalize_by_absolute_weight': True}),
    ((), {'weights': {'a': 1}}),
    ((), {'weights': {'a': 1}, 'normalize_by_absolute_weight': False}),
])
def test_factor_score_exact_call_shape_single_construction_and_state(args, kwargs):
    calls = []

    class Spy(FactorScoreModel):
        def __init__(self, *a, **k):
            calls.append((a, k))
            super().__init__(*a, **k)

    actual = create_legacy_model(request(args, kwargs), constructor_type=Spy)
    assert len(calls) == 1 and calls[0] == (args, kwargs)
    assert type(actual) is Spy and not actual.is_fitted
    assert vars(actual) == vars(FactorScoreModel(*args, **kwargs))


@pytest.mark.parametrize('kwargs', [{}, {'normalize_by_absolute_weight': False}])
def test_p202_p203_independent_original_call_probes_and_positional_only(kwargs):
    # P2-02 explicitly passes normalization; P2-03 omits it. Neither caller is migrated here.
    calls = []

    class PositionalOnly(FactorScoreModel):
        def __init__(self, weights, /, *, normalize_by_absolute_weight=False):
            calls.append(weights)
            super().__init__(weights, normalize_by_absolute_weight=normalize_by_absolute_weight)

    weights = OrderedDict([('REV1', -1), ('MOM20', 1)])
    expected = PositionalOnly(weights, **kwargs)
    actual = create_legacy_model(request((weights,), kwargs), constructor_type=PositionalOnly)
    assert type(actual) is type(expected) is PositionalOnly
    assert len(calls) == 2 and vars(actual) == vars(expected)


@pytest.mark.parametrize('args,kwargs', [
    ((), {}), (({},), {}), (({'a': 0},), {}), (({'a': 'bad'},), {}),
    (({'a': True},), {}), (({'a': np.inf},), {}), ((None,), {}),
    (({'a': 1},), {'weights': {'b': 2}}),
    (({'a': 1}, False), {}), (({'a': 1},), {'unknown': 1}),
    (({'a': 1},), {'normalize_by_absolute_weight': None}),
    ((), {1: 'bad'}),
])
def test_original_native_errors_and_cause_are_not_rewritten(args, kwargs):
    with pytest.raises(Exception) as direct:
        FactorScoreModel(*args, **kwargs)
    with pytest.raises(type(direct.value)) as adapted:
        create_legacy_model(request(args, kwargs))
    a, b = direct.value, adapted.value
    assert str(a) == str(b) and type(a.__cause__) is type(b.__cause__)
    assert str(a.__cause__) == str(b.__cause__)
    assert a.__suppress_context__ == b.__suppress_context__


def test_multiple_positional_arguments_mixed_keywords_and_joint_aliases():
    calls = []

    class Multi(RidgeResearchModel):
        def __init__(self, alpha, names, /, *, alias):
            calls.append((alpha, names, alias))
            assert names is alias
            super().__init__(alpha, feature_names=names)

    names = ['b', 'a']
    r = request(('0.5', names), {'alias': names}, 'ridge')
    names.reverse()
    first = create_legacy_model(r, constructor_type=Multi)
    second = create_legacy_model(r, constructor_type=Multi)
    assert len(calls) == 2 and calls[0][1] is not calls[1][1]
    assert first.requested_feature_names == second.requested_feature_names == ('b', 'a')
    assert first.alpha == 0.5


def test_constructor_exception_identity_cause_traceback_and_once():
    cause, sentinel = ValueError('cause'), RuntimeError('original constructor failure')
    calls = []

    class Failing(FactorScoreModel):
        def __init__(self, weights, /):
            calls.append(weights)
            raise sentinel from cause

    with pytest.raises(RuntimeError) as caught:
        create_legacy_model(request(({'a': 1},)), constructor_type=Failing)
    assert caught.value is sentinel and caught.value.__cause__ is cause
    assert len(calls) == 1
    frames = []
    tb = caught.value.__traceback__
    while tb:
        frames.append(tb.tb_frame.f_code)
        tb = tb.tb_next
    assert frames[-1] is Failing.__init__.__code__
    assert sum(f is create_legacy_model.__code__ for f in frames) == 1


@pytest.mark.parametrize('bad', [RidgeResearchModel, 'FactorScoreModel', lambda *a, **k: None])
def test_wrong_constructor_boundary(bad):
    with pytest.raises(InvalidModelSpec, match='constructor_type'):
        create_legacy_model(request(({'a': 1},)), constructor_type=bad)


@pytest.mark.parametrize('sequence', [tuple, list])
def test_empty_positional_sequence_and_existing_four_argument_initialization(sequence):
    old = LegacyModelRequest('ridge', 'legacy-v1', 'research_v2', {})
    r = request(sequence(), {}, 'ridge')
    assert old.positional_arguments == ()
    assert type(r.positional_arguments) is sequence
    assert vars(create_legacy_model(old)) == vars(create_legacy_model(r))


@pytest.mark.parametrize('bad', [None, 'abc', {'a': 1}, iter([1])])
def test_positional_channel_requires_inert_sequence(bad):
    with pytest.raises(InvalidModelSpec, match='positional_arguments'):
        request(bad)


@pytest.mark.parametrize('kind', [dict, OrderedDict, UserDict, defaultdict, MappingProxyType])
def test_container_types_nested_mutation_and_cross_channel_alias(kind):
    value = defaultdict(list, a=[1]) if kind is defaultdict else kind({'a': [1]})
    pair = (value,)
    r = request((pair, pair), {'value': value, 'pair': pair})
    value['a'].append(2)
    public = r.positional_arguments
    assert public[0] is public[1] and type(public[0]) is tuple
    assert type(public[0][0]) is kind and public[0][0]['a'] == [1]
    public[0][0]['a'].append(3)
    r.constructor_arguments['value']['a'].append(4)
    seen = []

    class Graph(FactorScoreModel):
        def __init__(self, first, second, *, value, pair):
            assert first is second is pair
            assert first[0] is value and value['a'] == [1]
            seen.append(value)
            super().__init__({'a': 1})

    create_legacy_model(r, constructor_type=Graph)
    create_legacy_model(r, constructor_type=Graph)
    assert seen[0] is not seen[1]
    with pytest.raises(FrozenInstanceError):
        r.positional_arguments = ()
    with pytest.raises(TypeError):
        r.constructor_arguments['new'] = 1


@pytest.mark.parametrize('graph_kind', ['list', 'dict', 'tuple', 'object-array', 'structured-array'])
def test_cycles_and_repeated_tuple_identity_survive_joint_snapshots(graph_kind):
    if graph_kind == 'list':
        graph = []; graph.append(graph)
        check = lambda x: x[0] is x
    elif graph_kind == 'dict':
        graph = {}; graph['self'] = graph
        check = lambda x: x['self'] is x
    elif graph_kind == 'tuple':
        child = []; graph = (child,); child.append(graph)
        check = lambda x: x[0][0] is x
    elif graph_kind == 'object-array':
        graph = np.empty(1, dtype=object); graph[0] = graph
        check = lambda x: x[0] is x
    else:
        graph = np.empty(1, dtype=[('item', object)]); graph['item'][0] = graph
        check = lambda x: x['item'][0] is x
    r = request((graph, graph), {'alias': graph})
    assert check(r.positional_arguments[0]) and check(r.constructor_arguments['alias'])

    class Cycle(FactorScoreModel):
        def __init__(self, a, b, *, alias):
            assert a is b is alias and a is not graph and check(a)
            super().__init__({'a': 1})

    create_legacy_model(r, constructor_type=Cycle)


def test_alias_to_outer_keyword_mapping_and_positional_sequence():
    args, kwargs = [], {}
    args.append(kwargs); kwargs['args'] = args; kwargs['root'] = kwargs
    r = request(args, kwargs)

    class Root(FactorScoreModel):
        def __init__(self, mapping, **kw):
            # Python ** creates a new keyword dict; its values retain the joint graph.
            assert mapping is kw['root'] and mapping['args'][0] is mapping
            assert mapping['args'] is kw['args']
            super().__init__({'a': 1})

    create_legacy_model(r, constructor_type=Root)


@pytest.mark.parametrize('dtype', [float, object])
def test_numpy_type_storage_and_mutable_elements_are_defensive(dtype):
    array = np.array([1., 2.], dtype=dtype)
    if dtype is object: array[0] = ['a']
    r = request((array,), {'alias': array})
    if dtype is object: array[0].append('mutated')
    else: array[:] = 99
    public = r.positional_arguments[0]
    assert type(public) is np.ndarray and public.dtype == array.dtype
    if dtype is object:
        assert public[0] == ['a']; public[0].append('public')
        assert r.positional_arguments[0][0] == ['a']
    else:
        np.testing.assert_array_equal(public, [1., 2.]); public[:] = 88
        np.testing.assert_array_equal(r.positional_arguments[0], [1., 2.])


def test_opaque_identity_no_copy_reduce_or_early_iterator_protocol():
    events = []

    class Numeric:
        def __float__(self): events.append('float'); return 0.5
        def __deepcopy__(self, memo): raise AssertionError('deepcopy')
        def __reduce_ex__(self, protocol): raise AssertionError('reduce')

    numeric = Numeric()
    names = iter(['b', 'a'])
    r = request((numeric,), {'feature_names': names}, 'ridge')
    assert r.positional_arguments[0] is numeric
    assert r.constructor_arguments['feature_names'] is names and not events
    actual = create_legacy_model(r)
    assert actual.alpha == 0.5 and actual.requested_feature_names == ('b', 'a')
    assert events == ['float'] and list(names) == []


@pytest.mark.parametrize('kwargs', [{}, {'normalize_by_absolute_weight': False}])
def test_effective_observer_explicit_positional_and_omitted_defaults(kwargs):
    r = request(({'a': '1'},), kwargs)
    model = create_legacy_model(r)
    observed = effective_spec_from_legacy(model, model_id='factor_score',
        implementation_version='legacy-v1', profile_id='research_v2', request=r)
    assert observed.positional_argument_names == ('weights',)
    assert observed.explicit_keyword_arguments == tuple(kwargs)
    assert observed.explicit_arguments == ('weights',) + tuple(kwargs)
    assert observed.defaults_used == (() if kwargs else ('normalize_by_absolute_weight',))
    assert observed.parameters['weights'] == {'a': 1.} and observed.status == 'STRICT_REPRESENTABLE'
    unknown = effective_spec_from_legacy(model, model_id='factor_score',
        implementation_version='legacy-v1', profile_id='research_v2')
    assert unknown.explicit_arguments is unknown.positional_argument_names is None
    assert unknown.explicit_keyword_arguments is unknown.defaults_used is None


def test_effective_observer_does_not_invent_variadic_parameter_names():
    class Variadic(FactorScoreModel):
        def __init__(self, *args): super().__init__(args[0])
    r = request(({' ': 1},))
    model = create_legacy_model(r, constructor_type=Variadic)
    result = effective_spec_from_legacy(model, model_id='factor_score',
        implementation_version='legacy-v1', profile_id='research_v2', request=r)
    assert result.positional_argument_names == (None,)
    assert result.status == 'LEGACY_ONLY_UNREPRESENTABLE' and result.strict_spec is None


def test_construction_never_signature_binds(monkeypatch):
    def forbidden(*a, **k): raise AssertionError('pre-binding changes Python errors')
    monkeypatch.setattr(inspect.Signature, 'bind', forbidden)
    assert create_legacy_model(request(({'a': 1},))).weights == {'a': 1.}


@pytest.mark.parametrize('normalize', [False, True])
def test_positional_factor_score_exact_math_pickle_and_save_load(tmp_path, normalize):
    args, kwargs = ({'b': -1, 'a': 2},), {'normalize_by_absolute_weight': normalize}
    direct = FactorScoreModel(*args, **kwargs)
    adapted = create_legacy_model(request(args, kwargs))
    assert pickle.dumps(direct) == pickle.dumps(adapted)
    x = pd.DataFrame({'b': [1., np.nan, 3.], 'a': [4., 5., 6.]})
    direct.fit(x); adapted.fit(x)
    pd.testing.assert_series_equal(direct.predict(x), adapted.predict(x), check_exact=True)
    assert pickle.dumps(direct) == pickle.dumps(adapted)
    a, b = tmp_path/'direct.pkl', tmp_path/'adapted.pkl'
    direct.save(a); adapted.save(b)
    assert a.read_bytes() == b.read_bytes()
    pd.testing.assert_series_equal(FactorScoreModel.load(b).predict(x), direct.predict(x), check_exact=True)


@pytest.mark.parametrize('name,cls', [('ridge', RidgeResearchModel),
    ('lightgbm', LightGBMResearchModel), ('xgboost', XGBoostResearchModel)])
def test_other_registered_original_positional_constructor(name, cls):
    args = ('0.5',) if name == 'ridge' else (8,)
    kwargs = {'feature_names': ['b', 'a']}
    if name != 'ridge':
        # The original tree constructors are keyword-only. Preserve their rejection.
        with pytest.raises(TypeError) as direct:
            cls(*args, **kwargs)
        with pytest.raises(TypeError) as adapted:
            create_legacy_model(request(args, kwargs, name))
        assert str(adapted.value) == str(direct.value)
        return
    actual, expected = create_legacy_model(request(args, kwargs, name)), cls(*args, **kwargs)
    assert type(actual) is cls and vars(actual) == vars(expected)
    assert pickle.dumps(actual) == pickle.dumps(expected)
