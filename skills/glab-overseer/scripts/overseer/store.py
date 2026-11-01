"""Atomic JSON writes, the append-only event log and the project mutation lock.

The lock is advisory between cooperating supervisor processes and relies on
`O_EXCL` atomicity on the shared filesystem. There is no stale-lock reaping:
only `overseer.py unlock --force --reason TEXT` removes a lock.
"""
import json
import os
import socket
import tempfile
import time
import uuid

from . import paths

_MISSING = object()


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fsync_dir(d):
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json_atomic(path, data):
    """Write JSON via a temp file in the same directory, fsync, `os.replace`."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(d)


def read_json(path, default=_MISSING):
    """Read JSON; return `default` when the file is missing (raise if none given)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        if default is _MISSING:
            raise
        return default


def append_event(root, kind, **fields):
    """Append one line to `.overseer/events.jsonl` and fsync it."""
    paths.ensure_state_dir(root)
    rec = {"time": utc_now(), "kind": kind}
    rec.update(fields)
    line = json.dumps(rec, sort_keys=True) + "\n"
    fd = os.open(paths.events_path(root), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return rec


def read_events(root):
    try:
        with open(paths.events_path(root), encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return []


class LockHeld(RuntimeError):
    def __init__(self, holder):
        self.holder = holder or {}
        h = self.holder
        super().__init__(
            "project is locked by '%s' (host %s, pid %s, since %s); "
            "only 'overseer.py unlock --force --reason TEXT' removes a lock"
            % (h.get("command", "unknown"), h.get("host", "unknown"),
               h.get("pid", "unknown"), h.get("time", "unknown"))
        )


def read_lock(root):
    try:
        with open(paths.lock_path(root), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except ValueError:
        return {}  # lock exists but its holder record is unreadable


class ProjectLock(object):
    """Project mutation lock, usable as a context manager."""

    def __init__(self, root, command):
        self.root = root
        self.command = command
        self.token = uuid.uuid4().hex
        self.held = False

    def acquire(self):
        paths.ensure_state_dir(self.root)
        record = {
            "command": self.command, "host": socket.gethostname(),
            "pid": os.getpid(), "time": utc_now(), "token": self.token,
        }
        try:
            fd = os.open(paths.lock_path(self.root), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            raise LockHeld(read_lock(self.root))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f)
            f.flush()
            os.fsync(f.fileno())
        self.held = True
        return self

    def release(self):
        if not self.held:
            return
        self.held = False
        cur = read_lock(self.root)
        if cur and cur.get("token") == self.token:
            try:
                os.unlink(paths.lock_path(self.root))
            except FileNotFoundError:
                pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def force_unlock(root, reason):
    """Remove the lock and record an event. Returns the removed holder or None."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("unlock requires a non-blank --reason")
    holder = read_lock(root)
    if holder is None:
        return None
    try:
        os.unlink(paths.lock_path(root))
    except FileNotFoundError:
        return None
    append_event(root, "unlock", reason=reason.strip(), previous_holder=holder)
    return holder
