"""Immutable, point-in-time inference for linear strategy artifacts."""
from dataclasses import dataclass
from datetime import datetime
from math import fsum, isfinite
import json

from factorlab.trading.contracts import utc


LINEAR_MODEL_SCHEMA = 'FACTORLAB_LINEAR_MODEL_V1'


class ModelNotEffective(ValueError):
    """The decision time precedes the package's frozen model activation time."""


def model_effective_at(declaration):
    fields = {'file', 'model_id', 'version', 'trained_through', 'effective_at'}
    if (not isinstance(declaration, dict) or set(declaration) != fields
            or any(not isinstance(value, str) or not value.strip() for value in declaration.values())
            or not declaration['file'].endswith('.json')
            or declaration['file'].casefold() == 'manifest.json'):
        raise ValueError('invalid linear model declaration')
    trained_through = datetime.fromisoformat(declaration['trained_through'])
    effective_at = datetime.fromisoformat(declaration['effective_at'])
    utc(trained_through)
    utc(effective_at)
    if trained_through >= effective_at:
        raise ValueError('model effective time must follow the training cutoff')
    return effective_at


def require_model_effective(event_time, effective_at):
    utc(event_time)
    if event_time < effective_at:
        raise ModelNotEffective('decision precedes model effective time ' + effective_at.isoformat())


def _coefficient(value):
    if type(value) not in {int, float}:
        raise ValueError('linear model parameters must be JSON numbers')
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError('linear model parameters must be finite') from error
    if not isfinite(result):
        raise ValueError('linear model parameters must be finite')
    return result


@dataclass(frozen=True)
class LinearModel:
    model_id: str
    version: str
    effective_at: datetime
    features: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float

    @classmethod
    def read(cls, declaration, data):
        effective_at = model_effective_at(declaration)
        payload = json.loads(data)
        if (not isinstance(payload, dict) or set(payload) != {'schema', 'features', 'coefficients', 'intercept'}
                or payload['schema'] != LINEAR_MODEL_SCHEMA):
            raise ValueError('unsupported linear model artifact schema')
        features = payload['features']
        coefficients = payload['coefficients']
        if (not isinstance(features, list) or not features
                or any(not isinstance(name, str) or not name.strip() for name in features)
                or len(set(features)) != len(features) or not isinstance(coefficients, list)
                or len(coefficients) != len(features)):
            raise ValueError('linear model requires unique features and matching coefficients')
        return cls(declaration['model_id'], declaration['version'], effective_at, tuple(features),
                   tuple(_coefficient(value) for value in coefficients), _coefficient(payload['intercept']))

    def require_event_time(self, event_time):
        require_model_effective(event_time, self.effective_at)

    def predict(self, features, *, event_time):
        self.require_event_time(event_time)
        values = [float(features[name]) for name in self.features]
        if any(not isfinite(value) for value in values):
            raise ValueError('linear model inputs must be finite')
        score = self.intercept + fsum(value * weight for value, weight in zip(values, self.coefficients))
        if not isfinite(score):
            raise ValueError('linear model output must be finite')
        return score
