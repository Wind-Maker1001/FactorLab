"""Native tree adapters for the public compute policy."""

from contextlib import contextmanager
from importlib import import_module
import json
import sys
from threading import RLock
import warnings

import numpy as np

from factorlab.compute import (
    BackendCapability, BackendStatus, ComputeConfig, ComputeMemoryError,
    ComputeUnavailableError, Device, ResolvedCompute, probe_runtime, resolve_compute,
)


_LIGHTGBM_LOG_LOCK = RLock()


def xgboost_device(booster):
    return json.loads(booster.save_config())["learner"]["generic_param"]["device"]


@contextmanager
def lightgbm_logs(runtime):
    # LightGBM exposes a process-global logger rather than a per-fit sink.
    with _LIGHTGBM_LOG_LOCK:
        basic = runtime.basic
        previous = (basic._LOGGER, basic._INFO_METHOD_NAME, basic._WARNING_METHOD_NAME)
        messages = []

        class Logger:
            def info(self, message):
                messages.append(message)

            def warning(self, message):
                messages.append(message)

        runtime.register_logger(Logger())
        try:
            yield messages
        finally:
            runtime.register_logger(previous[0], previous[1], previous[2])


def _trainer_proof(messages, backend):
    marker = "cuda tree learner" if backend.endswith("cuda") else "gpu trainer"
    if not any(marker in message.lower() for message in messages):
        raise ComputeUnavailableError("Native LightGBM trainer evidence missing: " + "; ".join(messages))
    return {"native_trainer_logs": list(messages)}


def _probe(runtime, family, backend):
    values = np.random.default_rng(42).normal(size=(128, 4))
    target = values[:, 0] + values[:, 1] ** 2
    error_type = runtime.core.XGBoostError if family == "xgboost" else runtime.basic.LightGBMError
    try:
        if family == "xgboost":
            with warnings.catch_warnings(record=True) as diagnostics:
                estimator = runtime.XGBRegressor(n_estimators=2, max_depth=2, n_jobs=1,
                                                 tree_method="hist", device="cuda")
                estimator.fit(values, target)
                device = xgboost_device(estimator.get_booster())
                if not device.startswith("cuda"):
                    return BackendStatus(False, f"Native device is {device}; " +
                                         "; ".join(str(item.message) for item in diagnostics))
                prediction = estimator.get_booster().predict(runtime.DMatrix(values))
        else:
            with lightgbm_logs(runtime) as messages:
                estimator = runtime.LGBMRegressor(n_estimators=2, num_leaves=3,
                    min_child_samples=1, verbosity=1, n_jobs=1, max_bin=15,
                    device_type="cuda" if backend.endswith("cuda") else "gpu")
                estimator.fit(values, target)
                prediction = estimator.predict(values)
            _trainer_proof(messages, backend)
        if np.shape(prediction) != (len(values),) or not np.isfinite(prediction).all():
            return BackendStatus(False, "Native probe produced invalid predictions")
    except ComputeUnavailableError as exc:
        return BackendStatus(False, str(exc))
    except error_type as exc:
        message = str(exc).lower()
        if any(marker in message for marker in (
            "not compiled with gpu", "not compiled with cuda", "not enabled in this build",
            "no opencl device", "no gpu device", "no visible gpu", "must have at least one device",
            "cuda driver", "cudaerror", "cuda error", "clgetplatformids", "clgetdeviceids",
            "cl_mem_object_allocation_failure", "out of memory", "gpu tree learner was not enabled",
        )):
            return BackendStatus(False, str(exc))
        raise
    return BackendStatus(True, runtime_available=True)


def resolve_tree_compute(family: str, config: ComputeConfig) -> ResolvedCompute:
    def cpu(_):
        return probe_runtime(family, lambda runtime: BackendStatus(True, runtime_available=True))

    def gpu(backend):
        return lambda _: probe_runtime(family, lambda runtime: _probe(runtime, family, backend))

    if family == "xgboost":
        backends = ("xgboost-cuda",)
    else:
        backends = (("lightgbm-opencl", "lightgbm-cuda") if sys.platform == "win32"
                    else ("lightgbm-cuda", "lightgbm-opencl"))
    capabilities = tuple(BackendCapability(Device.GPU, name, gpu(name)) for name in backends)
    return resolve_compute(capabilities + (BackendCapability(Device.CPU, family + "-cpu", cpu),), config)


def prediction_compute(family, model, override):
    if family == "lightgbm":
        # The native LightGBM prediction API is CPU even for GPU-trained trees.
        if override is None and not hasattr(model, "compute") and not hasattr(model, "compute_"):
            return None
        config = override or ComputeConfig()
        cpu = BackendCapability(Device.CPU, "lightgbm-cpu", lambda _: probe_runtime(
            family, lambda runtime: BackendStatus(True, runtime_available=True)))
        return resolve_compute((cpu,), config)
    if override is not None:
        return resolve_tree_compute(family, override)
    receipt = getattr(model, "compute_", None)
    if receipt is None:
        return None
    # Reuse the fitted path, never re-resolve into a CPU fallback after training.
    return ResolvedCompute(ComputeConfig(**receipt["requested"]), Device(receipt["device"]),
                           receipt["backend"], BackendStatus(**receipt["probe"]),
                           receipt["fallback"], receipt["fallback_reason"])


def tree_parameters(family: str, resolved: ResolvedCompute | None) -> dict:
    if family == "xgboost":
        return {} if resolved is None else {"device": "cuda" if resolved.device == Device.GPU else "cpu"}
    if resolved is None or resolved.device == Device.CPU:
        return {"deterministic": True, "force_col_wise": True}
    return {"device_type": "cuda" if resolved.backend.endswith("cuda") else "gpu"}


@contextmanager
def native_memory(family: str, gpu: bool):
    if not gpu:
        yield
        return
    runtime = import_module(family)
    error_type = runtime.core.XGBoostError if family == "xgboost" else runtime.basic.LightGBMError
    try:
        yield
    except error_type as exc:
        message = str(exc).lower()
        if any(marker in message for marker in (
            "cudaerrormemoryallocation", "cuda_error_out_of_memory",
            "cl_mem_object_allocation_failure", "cuda out of memory",
            "cudaerror: out of memory", "cuda error: out of memory",
        )):
            raise ComputeMemoryError(str(exc)) from exc
        raise


def fit_native(family, estimator, frame, target, resolved):
    gpu = resolved is not None and resolved.device == Device.GPU
    with native_memory(family, gpu):
        if family == "lightgbm" and gpu:
            with lightgbm_logs(import_module(family)) as messages:
                estimator.fit(frame, target)
            return _trainer_proof(messages, resolved.backend)
        estimator.fit(frame, target, **({"verbose": False} if family == "xgboost" else {}))
        if family == "xgboost" and resolved is not None:
            device = xgboost_device(estimator.get_booster())
            if gpu and not device.startswith("cuda"):
                raise ComputeUnavailableError(f"XGBoost silently selected native device {device}")
            return {"native_device": device}
    return {"native_method": "estimator.fit"}


def predict_native(family, estimator, frame, resolved):
    gpu = resolved is not None and resolved.device == Device.GPU
    with native_memory(family, gpu):
        if family == "xgboost" and resolved is not None:
            runtime = import_module(family)
            booster = estimator.get_booster()
            desired = "cuda" if gpu else "cpu"
            if not xgboost_device(booster).startswith(desired):
                # Device overrides must not rewrite the saved training estimator.
                booster = booster.copy()
                booster.set_param({"device": desired})
            prediction = booster.predict(runtime.DMatrix(frame))
            device = xgboost_device(booster)
            if gpu and not device.startswith("cuda"):
                raise ComputeUnavailableError(f"XGBoost prediction selected native device {device}")
            return prediction, {"native_method": "Booster.predict(DMatrix)", "native_device": device}
        return estimator.predict(frame), {"native_method": "estimator.predict"}


def stage_receipt(resolved, stage, preprocessing_seconds, native_seconds, proof):
    receipt = resolved.to_dict()
    receipt.update(stage=stage, executed=True, preprocessing_seconds=preprocessing_seconds,
                   native_seconds=native_seconds, execution=proof,
                   transfer_sync_timing="included in native_seconds; native host-array API, not independently measurable")
    return receipt
