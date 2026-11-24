"""Filesystem layout, atomic writes and the single-instance lock."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from carrel.runtime.locks import path_key

LOCK_NAME = "server.lock"

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sharing_conflict(exc):
    # Python's buffered I/O sometimes reports EACCES without a winerror value.
    return (getattr(exc, 'winerror', None) in (5, 32, 33)
            or (os.name == 'nt' and isinstance(exc, PermissionError)))


def safe_dirname(paper_id: str) -> str:
    """Map a paper id to a directory name that is valid on every platform.

    Windows forbids ``:`` in path components and arXiv ids use it, so ``:`` becomes
    ``-``. A short hash keeps distinct ids from colliding after that mapping.
    """
    base = _UNSAFE.sub("-", paper_id).strip(" .")
    digest = hashlib.sha256(paper_id.encode("utf-8")).hexdigest()[:8]
    if base != paper_id:
        base = f"{base}-{digest}"
    if len(base) > 120:
        base = f"{base[:100]}-{digest}"
    return base or digest


def paper_dir(data_dir: Path, paper_id: str) -> Path:
    return data_dir / "papers" / safe_dirname(paper_id)


def ensure_paper_dirs(data_dir: Path, paper_id: str) -> Path:
    base = paper_dir(data_dir, paper_id)
    for sub in ("", "parse_raw", "modules"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    return base


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        # Windows readers/indexers can briefly deny FILE_SHARE_DELETE. Keep
        # the complete temp file and retry replacement, never truncate the
        # destination. Persistent permission failures still surface promptly.
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                from carrel.graph.revision import artifact_written
                artifact_written(path)
                break
            except PermissionError as exc:
                if not _sharing_conflict(exc) or attempt == 5:
                    raise
                time.sleep(.02 * 2 ** attempt)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_json(path: Path, default=None, *, strict=False):
    for attempt in range(6):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except OSError as exc:
            if _sharing_conflict(exc) and attempt < 5:
                time.sleep(.02 * 2 ** attempt)
                continue
            if strict:
                raise
            return default
        except json.JSONDecodeError:
            if strict:
                raise
            return default


_held_locks = {}
_lock_guard = threading.RLock()


def acquire_lock(data_dir: Path) -> Path | None:
    """Hold an OS lock on a stable inode, compatible with old PID-only servers.

    The guard file is never unlinked: unlink/replace would let another process
    lock a different inode while the first holder is still alive.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = Path(path_key(data_dir / LOCK_NAME))
    with _lock_guard:
        if lock in _held_locks:
            return None
        guard = lock.with_suffix('.guard')
        fh = guard.open('a+b')
        try:
            if guard.stat().st_size == 0:
                fh.write(b'0')
                fh.flush()
            fh.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if lock.exists() and _pid_alive(lock):
                fh.close()
                return None
            atomic_write_text(lock, str(os.getpid()))
            _held_locks[lock] = fh
            return lock
        except OSError:
            fh.close()
            return None


def _pid_alive(lock: Path) -> bool:
    try:
        pid = int(lock.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        # An exited Windows process object may still be openable while another
        # handle exists. Test its signaled state instead of handle existence.
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not handle:
                return ctypes.get_last_error() == 5  # access denied: be conservative
            try:
                return kernel.WaitForSingleObject(handle, 0) != 0  # signaled = exited
            finally:
                kernel.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def release_lock(lock: Path | None) -> None:
    if lock is None:
        return
    with _lock_guard:
        lock = Path(path_key(lock))
        fh = _held_locks.pop(lock, None)
        if fh is not None:
            try:
                lock.unlink(missing_ok=True)
            finally:
                fh.close()
