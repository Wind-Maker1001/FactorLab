"""Cross-process exclusive cache publication lock."""
from contextlib import contextmanager
import logging
import os
import time
from factorlab.errors import CacheError

LOGGER = logging.getLogger(__name__)


@contextmanager
def publication_lock(root, key, timeout=10.0):
    lock_path = root / (".%s.write.lock" % key)
    if lock_path.is_symlink() or lock_path.resolve().parent != root:
        raise CacheError("cache writer lock path escapes its root")
    # Windows byte-range locks can cover a byte beyond EOF. Acquire that
    # native lock before initializing a newly created file: otherwise two
    # buffered handles can both observe length zero, and one may flush its
    # pending byte after the other has already locked that byte. FileIO
    # avoids carrying a deferred write into the unlock/close sequence.
    try:
        handle = lock_path.open("a+b", buffering=0)
    except OSError as exc:
        raise CacheError("cannot open cache writer lock for %s" % key) from exc
    with handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - exercised on POSIX CI
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise CacheError(
                        "cache writer lock timeout for %s" % key
                    ) from exc
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        protected_error = None
        try:
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
            except OSError as exc:
                raise CacheError(
                    "cannot initialize cache writer lock for %s" % key
                ) from exc
            yield
        except BaseException as exc:
            protected_error = exc
            raise
        finally:
            try:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - exercised on POSIX CI
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                # Closing the unbuffered handle is the final OS-lock
                # release. Preserve a primary body/initialization failure,
                # but never report success if explicit unlocking failed.
                if protected_error is None:
                    raise CacheError(
                        "cannot release cache writer lock for %s" % key
                    ) from exc
                LOGGER.warning(
                    "cannot release cache writer lock for %s while handling %s: %s",
                    key, type(protected_error).__name__, exc,
                )
