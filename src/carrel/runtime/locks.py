from contextlib import contextmanager
import os
from pathlib import Path
import threading


def path_key(path):
    """Stable file identity, including Windows extended-path aliases.

    Windows resolve() may include or omit the extended prefix while a file
    is being replaced. Both spellings must share the same in-process mutex.
    """
    value = str(Path(path).resolve())
    if os.name != 'nt':
        return value
    value = value.replace(chr(92), '/')
    if value.startswith('//?/UNC/'):
        value = '//' + value[8:]
    elif value.startswith('//?/'):
        value = value[4:]
    return os.path.normcase(value)


class KeyedLock:
    def __init__(self):
        self._guard = threading.RLock()
        self._entries = {}

    @contextmanager
    def hold(self, key, *, blocking=True):
        with self._guard:
            lock, count = self._entries.get(key, (threading.RLock(), 0))
            self._entries[key] = (lock, count + 1)
        acquired = lock.acquire(blocking=blocking)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
            with self._guard:
                _, count = self._entries[key]
                if count == 1:
                    del self._entries[key]
                else:
                    self._entries[key] = (lock, count - 1)


paper_locks = KeyedLock()
content_locks = KeyedLock()
cache_locks = KeyedLock()
taxonomy_lock = threading.RLock()
