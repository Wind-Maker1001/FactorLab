"""Optional immutable research experiment records; not financial admission."""
__all__ = ["ExperimentTracker"]


def __getattr__(name):
    if name == 'ExperimentTracker':
        from .tracker import ExperimentTracker
        return ExperimentTracker
    raise AttributeError(name)
