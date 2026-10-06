"""Process locks shared by PDF Desk and its update helper.

Standard library only: the update helper runs a copy of this file without importing PDF Desk.
Each running PDF Desk holds a lock on its own file in cache/running/. The operating system drops a
lock when the process ends in any way (closed, killed, crashed, logged out), so a lock that can be
taken belongs to a copy that isn't running any more.
"""
import errno
import os
import sys

_HELD = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, getattr(errno, "EDEADLK", errno.EACCES)}


def try_lock(fh) -> bool:
    """True if we got the lock, False if someone else holds it. Any other error (a file system
    without locking, for example) is raised, because then nothing can be known about other copies."""
    try:
        if sys.platform.startswith("win"):
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in _HELD:
            return False
        raise


def unlock(fh) -> None:
    try:
        if sys.platform.startswith("win"):
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def is_lock_name(name: str) -> bool:
    stem = name[:-5] if name.endswith(".lock") else ""
    return stem.isascii() and stem.isdecimal()


def others_running(folder: str, skip: str = "") -> list:
    """Lock files in `folder` still held by a running PDF Desk. Files nobody holds are removed.
    Call it only while holding the update lock, so a copy that's just starting can't be missed."""
    found = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return found
    for name in names:
        if not is_lock_name(name) or name == skip:
            continue
        path = os.path.join(folder, name)
        try:
            fh = open(path, "a+b")
        except OSError:
            found.append(name)
            continue
        try:
            free = try_lock(fh)
        except OSError:
            fh.close()
            found.append(name)
            continue
        if free:  # nobody holds it: that copy has ended
            unlock(fh)
            fh.close()
            try:
                os.remove(path)
            except OSError:
                pass
        else:
            fh.close()
            found.append(name)
    return found
