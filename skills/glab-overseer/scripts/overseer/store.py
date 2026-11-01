"""Atomic JSON writes, the append-only event log and the project mutation lock.

The lock is advisory between cooperating supervisor processes. The holder
record is written to a temporary file in the same directory and published with
`os.link`, which fails with EEXIST when the lock is held, so a contender never
sees a lock without its holder record. There is no stale-lock reaping: only
`overseer.py unlock --force --reason TEXT` removes a lock. `unlock` refuses when
the holder is a live process on this host unless `--even-if-alive` is given.

Accepted limit: a holder on another host cannot be probed, so a forced unlock
of a live remote holder, or a forced unlock racing a release, can let two
supervisors run at once. Operators own that decision.
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


def publish_record(path, record):
    """Atomically create `path` containing `record` (temp file + link); FileExistsError if present."""
    tmp = "%s.%s.tmp" % (path, record["token"])
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.link(tmp, path)
    finally:
        os.unlink(tmp)


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
            publish_record(paths.lock_path(self.root), record)
        except FileExistsError:
            raise LockHeld(read_lock(self.root))
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


class LockAlive(RuntimeError):
    pass


def holder_alive(holder):
    """True when the holder is a live process on this host."""
    if not holder or holder.get("host") != socket.gethostname():
        return False
    pid = holder.get("pid")
    if not isinstance(pid, int) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class UnlockBusy(RuntimeError):
    pass


def _read_record(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except ValueError:
        return {}


def _take_unlock_guard(root, guard):
    rec = {"command": "unlock", "host": socket.gethostname(), "pid": os.getpid(),
           "time": utc_now(), "token": uuid.uuid4().hex}
    for attempt in (1, 2):
        try:
            publish_record(guard, rec)
            return
        except FileExistsError:
            g = _read_record(guard)
            if attempt == 1 and g and g.get("host") == rec["host"] and not holder_alive(g):
                cur = _read_record(guard)
                if cur and cur.get("token") == g.get("token"):
                    try:
                        os.unlink(guard)
                    except FileNotFoundError:
                        pass
                    append_event(root, "unlock_guard_removed", stale_guard=g)
                continue
            raise UnlockBusy("another unlock is in progress (pid %s)" % (g or {}).get("pid", "unknown"))


def force_unlock(root, reason, even_if_alive=False):
    """Remove the lock and record an event. Returns the removed holder or None.

    Forced unlocks are serialized by `lock.unlock`, published like the project
    lock with a holder record. The holder is read and judged only inside that
    section, so a second unlocker sees any new live holder and refuses. A guard
    left by a dead process on this host is removed (with an event) and taken
    once; a live or other-host guard refuses. Accepted limit: a guard left by a
    crashed process on another host needs a manual remove, and two unlockers
    removing the same stale guard at once can still race.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("unlock requires a non-blank --reason")
    paths.ensure_state_dir(root)
    guard = paths.lock_path(root) + ".unlock"
    _take_unlock_guard(root, guard)
    try:
        holder = read_lock(root)
        if holder is None:
            return None
        alive = holder_alive(holder)
        if alive and not even_if_alive:
            raise LockAlive("lock holder '%s' (pid %s) is alive on this host; "
                            "pass --even-if-alive to remove it anyway" % (holder.get("command"), holder.get("pid")))
        try:
            os.unlink(paths.lock_path(root))
        except FileNotFoundError:
            return None
        append_event(root, "unlock", reason=reason.strip(), previous_holder=holder,
                     holder_alive=alive, even_if_alive=bool(even_if_alive))
        return holder
    finally:
        try:
            os.unlink(guard)
        except FileNotFoundError:
            pass
