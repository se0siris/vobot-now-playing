"""Single-instance guard, backed by a named shared memory segment.

Only one copy should run: the Mini Dock serves one client exchange at a time and
refuses overlapping pushes with a 'busy' reply, so a second copy just fights the
first for the device.

The segment holds the owner's PID, so a second launch can say who is already
running. On Windows the kernel reference-counts the mapping and frees it when
the last handle closes, so a hard crash cannot leave a stale lock behind.
"""

import getpass
import logging
import struct

from PyQt5.QtCore import QCoreApplication, QSharedMemory

from constants import APP_NAME

logger = logging.getLogger(__name__)

PID_FORMAT = '=i'
PID_SIZE = struct.calcsize(PID_FORMAT)

# The segment is owned by whichever QSharedMemory object created it, and dies
# with it - so the winning instance has to hold on to this for its whole life.
_segment: QSharedMemory | None = None


def _key() -> str:
    # Keyed per user: two people signed in to the same machine each get their
    # own dock and should not block one another.
    return f'{APP_NAME} - Instance Check - {getpass.getuser()}'


def claim() -> int | None:
    """Try to become the one running instance.

    Returns None if this process now owns the lock, otherwise the PID of the
    instance already running - or 0 if another instance holds the lock but its
    PID could not be read.
    """
    global _segment

    segment = QSharedMemory(_key())
    if segment.create(PID_SIZE, QSharedMemory.ReadWrite):
        _write_pid(segment)
        _segment = segment
        return None

    if segment.error() != QSharedMemory.AlreadyExists:
        # Something else went wrong - out of handles, sandbox policy, and so on.
        # Refusing to start over an unclear failure is worse than allowing it.
        logger.warning('Could not claim the instance lock (%s); starting anyway', segment.errorString())
        return None

    return _read_pid(segment)


def release() -> None:
    """Drop the lock. Only needed for tests; exiting does this anyway."""
    global _segment
    if _segment is not None:
        _segment.detach()
        _segment = None


def _write_pid(segment: QSharedMemory) -> None:
    pid = int(QCoreApplication.applicationPid())
    segment.lock()
    try:
        segment.data()[:PID_SIZE] = struct.pack(PID_FORMAT, pid)
    finally:
        segment.unlock()
    logger.debug('Instance lock claimed by pid %d', pid)


def _read_pid(segment: QSharedMemory) -> int:
    if not segment.attach(QSharedMemory.ReadOnly):
        logger.warning('Another instance holds the lock but it could not be read (%s)', segment.errorString())
        return 0

    segment.lock()
    try:
        view = memoryview(segment.data())
        pid = struct.unpack(PID_FORMAT, view[:PID_SIZE])[0]
    except Exception:
        logger.warning('Could not read the owning pid', exc_info=True)
        pid = 0
    finally:
        segment.unlock()
        segment.detach()

    return pid
