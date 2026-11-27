"""Optional immutable research experiment records; not financial admission."""

__all__ = ["ExperimentTracker"]


def __getattr__(name: str):
    if name == "ExperimentTracker":
        from .tracker import ExperimentTracker
        return ExperimentTracker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
