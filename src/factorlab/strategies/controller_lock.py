"""Nonblocking, process-owned serialization for one deployment instance."""
from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path


@contextmanager
def controller_lock(path, instance_id):
    database = Path(path).resolve()
    key = sha256(instance_id.encode()).hexdigest()
    lock_path = database.with_name(database.name + '.' + key + '.controller.lock')
    with lock_path.open('a+b', buffering=0) as handle:
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        except OSError as error:
            if os.name != 'nt' or error.errno not in {13, 36}:
                raise
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
