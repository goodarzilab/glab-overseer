"""Execution core and local backend: attempts, invocations, slots, wait and abort.

Layout under an attempt: `inv/<seq>-<kind>/` holds one record set per invocation:
intent.json (written BEFORE the launch), launch.json (after), start.json and
exit.json (written by the generated run.sh, keyed by token and argv digest),
cancel.json (abort), plus run.sh, rec.py, stdout.log, stderr.log, run.log.

Functions that write state expect the caller to hold `store.ProjectLock`.
Liveness is never one signal: RUNNING needs a live pid whose start identity
matches launch.json; EXITED needs an exit.json matching token and argv digest;
anything else with no live process is UNCERTAIN and holds its slot until an
operator resolves or aborts it. Reconciliation never relaunches.
"""
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid

from . import approval, env as E, paths, store

KINDS = ("author", "fixer", "job")
ACTIVE = ("QUEUED", "RUNNING", "UNCERTAIN")
BLOCKING_CLASSES = ("QUOTA", "AUTH")
SIGNALS = ("TERM", "INT", "HUP")
BAD = object()  # a record file exists but is unreadable or not an object
ATTEMPT_RE = re.compile(r"^a([1-9][0-9]*)$")
INV_RE = re.compile(r"^([0-9]{2,})-(author|fixer|job)$")


class ExecutionError(ValueError):
    pass


class SlotsFull(ExecutionError):
    pass


def _load(path):
    """Record dict; None when absent; BAD when present but unusable."""
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return BAD
    return v if isinstance(v, dict) else BAD


def _int(v, lo=1):
    return type(v) is int and v >= lo


def argv_digest(argv):
    return hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode("utf-8")).hexdigest()


# ---- process identity ------------------------------------------------------

def _ps_exe():
    return E.resolve_exe("ps", os.environ.get("PATH", ""))


def proc_start(pid, allow_zombie=False):
    """Start identity of `pid` (Linux: /proc stat starttime; macOS: ps lstart), else None."""
    if not _int(pid):
        return None
    if os.path.isdir("/proc/self"):
        try:
            with open("/proc/%d/stat" % pid) as f:
                data = f.read()
            rest = data[data.rindex(")") + 2:].split()
            if rest[0] == "Z" and not allow_zombie:
                return None
            return rest[19]
        except (OSError, ValueError, IndexError):
            return None
    ps = _ps_exe()
    if ps is None:
        return None
    r = E.run_tool([ps, "-o", "stat=,lstart=", "-p", str(pid)], E.worker_env(), 10)
    parts = r["stdout"].strip().split(None, 1) if r["rc"] == 0 else []
    if len(parts) != 2 or (parts[0].startswith("Z") and not allow_zombie):
        return None
    return parts[1].strip()


_CHILDREN = []  # local launches made by this process, polled so they never linger as zombies


def _reap(pid):
    _CHILDREN[:] = [c for c in _CHILDREN if c.poll() is None]


def identified_alive(pid, start):
    if not (_int(pid) and isinstance(start, str) and start):
        return False
    _reap(pid)
    return proc_start(pid) == start


def find_by_script(script):
    """Pids of live processes running `script` (sh run.sh), by command line."""
    found = []
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    with open("/proc/%s/cmdline" % name, "rb") as f:
                        args = f.read().decode("utf-8", "replace").split("\0")
                except OSError:
                    continue
                if script in args[:2]:
                    found.append(int(name))
        return sorted(found)
    ps = _ps_exe()
    if ps is not None:
        r = E.run_tool([ps, "-axo", "pid=,command="], E.worker_env(), 15)
        for line in r["stdout"].splitlines():
            p = line.strip().split(None, 1)
            if len(p) == 2 and p[0].isdigit() and script in p[1]:
                found.append(int(p[0]))
    return sorted(found)


# ---- invocation records ----------------------------------------------------

def _valid_intent(intent, inv):
    argv = intent.get("argv")
    return bool(isinstance(intent.get("token"), str) and intent["token"]
                and isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)
                and intent.get("argv_sha256") == argv_digest(argv)
                and intent.get("kind") == os.path.basename(inv).split("-", 1)[-1]
                and isinstance(intent.get("backend"), str) and isinstance(intent.get("run_sh"), str))


def _valid_launch(launch, intent):
    if launch is BAD or launch.get("token") != intent["token"]:
        return False
    if intent["backend"] == "local":
        return bool(_int(launch.get("pid")) and _int(launch.get("pgid"))
                    and isinstance(launch.get("start"), str) and launch["start"])
    return True


def _exit_matches(ex, intent):
    return (ex is not BAD and ex.get("token") == intent["token"]
            and ex.get("argv_sha256") == intent["argv_sha256"]
            and type(ex.get("rc")) is int and ex.get("signal") in (None,) + SIGNALS)


def _reconcile_launch(inv, intent, write):
    """Find a live run.sh for an intent without launch.json; never launches anything."""
    cands = find_by_script(intent["run_sh"])
    st = _load(os.path.join(inv, "start.json"))
    if isinstance(st, dict) and st.get("token") == intent["token"] and st.get("pid") in cands:
        cands = [st["pid"]]
    for pid in cands:
        start = proc_start(pid)
        if start is None:
            continue
        try:
            pgid = os.getpgid(pid)
        except OSError:
            continue
        launch = {"token": intent["token"], "pid": pid, "pgid": pgid, "start": start,
                  "backend": intent["backend"], "time": store.utc_now(), "reconciled": True}
        if write:
            store.write_json_atomic(os.path.join(inv, "launch.json"), launch)
        return launch
    return None


def _probe_local(inv, intent, launch):
    if not identified_alive(launch["pid"], launch["start"]):
        return "GONE"
    st = _load(os.path.join(inv, "start.json"))
    if isinstance(st, dict) and st.get("token") == intent["token"] and st.get("pid") != launch["pid"]:
        return "GONE"
    return "RUNNING"


PROBES = {"local": _probe_local}  # backend -> fn(inv, intent, launch) -> RUNNING|QUEUED|GONE


def invocation_status(inv, write=False):
    """State of one invocation directory: dict with `state` and details.

    States: NOT_LAUNCHED (no intent, nothing started), LAUNCH_FAILED, QUEUED, RUNNING,
    EXITED (with rc, signal), CANCELLED, UNCERTAIN. `write` lets reconciliation record
    a found launch.json.
    """
    intent = _load(os.path.join(inv, "intent.json"))
    if intent is None:
        return {"state": "NOT_LAUNCHED", "reason": "no intent.json"}
    if intent is BAD or not _valid_intent(intent, inv):
        return {"state": "UNCERTAIN", "reason": "intent.json is invalid"}
    ex = _load(os.path.join(inv, "exit.json"))
    if ex is not None and _exit_matches(ex, intent):
        return {"state": "EXITED", "rc": ex["rc"], "signal": ex.get("signal")}
    stale = " (exit.json does not match this invocation)" if ex is not None else ""
    fail = _load(os.path.join(inv, "launch_error.json"))
    if isinstance(fail, dict) and fail.get("token") == intent["token"]:
        return {"state": "LAUNCH_FAILED", "reason": str(fail.get("error"))}
    launch = _load(os.path.join(inv, "launch.json"))
    if launch is None:
        launch = _reconcile_launch(inv, intent, write)
        if launch is None:
            started = os.path.exists(os.path.join(inv, "start.json"))
            return {"state": "UNCERTAIN", "reason": ("process gone before launch.json was written" if started
                    else "intent without launch and no running process") + stale}
    elif not _valid_launch(launch, intent):
        return {"state": "UNCERTAIN", "reason": "launch.json is invalid or belongs to another invocation"}
    probe = PROBES.get(intent["backend"])
    s = probe(inv, intent, launch) if probe else "GONE"
    if s in ("RUNNING", "QUEUED"):
        return {"state": s, "pid": launch.get("pid")}
    cancel = _load(os.path.join(inv, "cancel.json"))
    if isinstance(cancel, dict) and cancel.get("token") == intent["token"] \
            and isinstance(cancel.get("confirmed_at"), str):
        return {"state": "CANCELLED"}
    return {"state": "UNCERTAIN", "reason": "process gone and no matching exit.json" + stale}


def _sorted_dirs(parent, rx):
    try:
        names = os.listdir(parent)
    except OSError:
        return []
    return sorted((n for n in names if rx.match(n)), key=lambda n: int(n.split("-")[0].lstrip("a")))


def iter_invocations(root, plan_id=None, unit_id=None):
    pdir = paths.plans_dir(root)
    for pid in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        if not paths.valid_id(pid) or plan_id not in (None, pid):
            continue
        udir = os.path.join(paths.plan_dir(root, pid), "units")
        for uid in sorted(os.listdir(udir)) if os.path.isdir(udir) else []:
            if not paths.valid_id(uid) or unit_id not in (None, uid):
                continue
            adir = os.path.join(udir, uid, "attempts")
            for a in _sorted_dirs(adir, ATTEMPT_RE):
                for name in _sorted_dirs(os.path.join(adir, a, "inv"), INV_RE):
                    m = INV_RE.match(name)
                    yield {"plan_id": pid, "unit_id": uid, "attempt": int(a[1:]), "seq": name,
                           "kind": m.group(2), "path": os.path.join(adir, a, "inv", name)}


def active_invocations(root, plan_id=None, unit_id=None, write=False):
    out = []
    for i in iter_invocations(root, plan_id, unit_id):
        st = invocation_status(i["path"], write)
        if st["state"] in ACTIVE:
            out.append(dict(i, status=st))
    return out


def slots_in_use(root, write=False):
    """Author, fixer and job invocations QUEUED, RUNNING or UNCERTAIN in every plan."""
    return sum(1 for i in active_invocations(root, write=write) if i["kind"] in KINDS)


# ---- attempts --------------------------------------------------------------

def record_classification(root, plan_id, unit_id, n, cls):
    """Record how an attempt ended (for example QUOTA or AUTH); read by allocate_attempt."""
    if not isinstance(cls, str) or not cls:
        raise ExecutionError("classification must be a non-empty string")
    store.write_json_atomic(os.path.join(paths.attempt_dir(root, plan_id, unit_id, n), "classification.json"),
                            {"class": cls, "time": store.utc_now()})


def _unit_of(plan, unit_id):
    for u in plan["units"]:
        if u["id"] == unit_id:
            return u
    raise ExecutionError("unit %r is not in plan %s" % (unit_id, plan["plan_id"]))


def _counter(st):
    n = st["counters"].get("attempts")
    if not _int(n, 0):
        raise ExecutionError("unit.json counters.attempts is %r, not a non-negative integer" % (n,))
    return n


def allocate_attempt(root, plan_id, unit_id, after_quota=False):
    """Create attempts/a<N> exclusively; returns (N, directory)."""
    approval.require_approval(root, plan_id)
    plan = approval.effective_plan(root, plan_id)
    _unit_of(plan, unit_id)
    st = approval.load_unit_state(root, plan_id, unit_id)
    if st["closure"].get("state") in approval.CLOSED_STATES:
        raise ExecutionError("unit %s is %s" % (unit_id, st["closure"]["state"]))
    last = _counter(st)
    for i in iter_invocations(root, plan_id, unit_id):
        s = invocation_status(i["path"], True)
        if s["state"] in ACTIVE:
            raise ExecutionError("attempt a%d of %s has an invocation %s that is %s"
                                 % (i["attempt"], unit_id, i["seq"], s["state"]))
    if last:
        c = _load(os.path.join(paths.attempt_dir(root, plan_id, unit_id, last), "classification.json"))
        if (c is BAD or (c and c.get("class") in BLOCKING_CLASSES)) and not after_quota:
            raise ExecutionError("attempt a%d of %s ended with %s; pass --after-quota to try again"
                                 % (last, unit_id, "an unreadable classification" if c is BAD else c["class"]))
    n = last + 1
    if n > plan["caps"]["attempts"]:
        raise ExecutionError("unit %s would need attempt %d, above the attempts cap %d"
                             % (unit_id, n, plan["caps"]["attempts"]))
    adir = paths.attempt_dir(root, plan_id, unit_id, n)
    os.makedirs(os.path.dirname(adir), exist_ok=True)
    marker = os.path.join(adir, "allocated.json")
    try:
        os.mkdir(adir)
        store.write_json_atomic(marker, {"attempt": n, "token": uuid.uuid4().hex, "time": store.utc_now()})
    except FileExistsError:
        m = _load(marker)  # a crash between mkdir and the counter bump leaves a marked directory: adopt it
        if not (isinstance(m, dict) and m.get("attempt") == n):
            raise ExecutionError("attempt directory %s already exists and is not a crashed allocation" % adir)
    st["counters"]["attempts"] = n
    st["execution"] = {"state": "allocated", "attempt": n}
    approval.save_unit_state(root, plan_id, unit_id, st)
    store.append_event(root, "attempt_allocated", plan_id=plan_id, unit_id=unit_id, attempt=n)
    return n, adir


# ---- launching -------------------------------------------------------------

_REC = '''import json, os, socket, sys, tempfile, time
def w(path, rec):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".rec.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
a = sys.argv
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
if a[1] == "start":
    w(a[2] + "/start.json", {"token": a[3], "pid": int(a[4]), "host": socket.gethostname(), "time": now})
else:
    w(a[2] + "/exit.json", {"token": a[3], "argv_sha256": a[4], "rc": int(a[5]), "signal": a[6] or None, "end_time": now})
'''


def _run_sh(inv, intent, stdin_path):
    q = shlex.quote
    return """#!/bin/sh
PY=%s; REC=%s; INV=%s; TOKEN=%s; SHA=%s
sig=; rc=1
fin() { trap - EXIT; "$PY" -I "$REC" exit "$INV" "$TOKEN" "$SHA" "$rc" "$sig"; }
trap fin EXIT
trap 'sig=TERM; rc=$?; exit "$rc"' TERM
trap 'sig=INT; rc=$?; exit "$rc"' INT
trap 'sig=HUP; rc=$?; exit "$rc"' HUP
cd %s || { rc=126; exit 126; }
"$PY" -I "$REC" start "$INV" "$TOKEN" "$$"
%s <%s >"$INV/stdout.log" 2>"$INV/stderr.log"
rc=$?
exit "$rc"
""" % (q(sys.executable), q(os.path.join(inv, "rec.py")), q(inv), q(intent["token"]), q(intent["argv_sha256"]),
       q(intent["cwd"]), " ".join(q(a) for a in intent["argv"]), q(stdin_path))


def _write_scripts(inv, intent, stdin_text):
    stdin_path = os.devnull
    if stdin_text is not None:
        stdin_path = os.path.join(inv, "stdin.txt")
        with open(stdin_path, "w", encoding="utf-8") as f:
            f.write(stdin_text)
    with open(os.path.join(inv, "rec.py"), "w") as f:
        f.write(_REC)
    with open(intent["run_sh"], "w") as f:
        f.write(_run_sh(inv, intent, stdin_path))
    os.chmod(intent["run_sh"], 0o755)


def _spawn_local(inv, run_sh, envd, cwd):
    with open(os.path.join(inv, "run.log"), "ab") as log:
        p = subprocess.Popen([run_sh], cwd=cwd, env=envd, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True)
    _CHILDREN.append(p)
    return {"pid": p.pid}


def launch_invocation(root, plan_id, unit_id, attempt, kind, argv, cwd, backend="local",
                      stdin_text=None, env=None, extras=None, spawn=None):
    """Launch one author, fixer or job invocation; returns its record (intent plus launch plus `dir`).

    The caller holds the project lock and has allocated the attempt. `argv[0]` is
    resolved to an absolute path on the scrubbed worker PATH. `spawn(inv, run_sh, env, cwd)`
    returns launch fields (including `pid` for the local backend); the default is the
    detached local spawn, which other backends replace together with a PROBES entry.
    """
    if kind not in KINDS:
        raise ExecutionError("kind must be one of %s" % ", ".join(KINDS))
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) and "\0" not in a for a in argv)):
        raise ExecutionError("argv must be a non-empty list of strings")
    if not (isinstance(cwd, str) and os.path.isabs(cwd) and os.path.isdir(cwd)):
        raise ExecutionError("cwd must be an existing absolute directory")
    if not (isinstance(backend, str) and backend):
        raise ExecutionError("backend must be a non-empty string")
    if stdin_text is not None and not isinstance(stdin_text, str):
        raise ExecutionError("stdin_text must be a string")
    approval.require_approval(root, plan_id)
    cap = approval.effective_plan(root, plan_id)["caps"]["workers"]
    adir = paths.attempt_dir(root, plan_id, unit_id, attempt)
    if not os.path.isdir(adir):
        raise ExecutionError("attempt a%d of %s is not allocated" % (attempt, unit_id))
    n = slots_in_use(root, True)
    if n >= cap:
        raise SlotsFull("%d of %d worker slots are in use" % (n, cap))
    envd = E.worker_env(base=env, extras=extras)
    exe = E.resolve_exe(argv[0], envd.get("PATH", ""), cwd)
    if exe is None:
        raise ExecutionError("command %r not found (only absolute PATH entries are searched)" % argv[0])
    argv = [exe] + argv[1:]
    invroot = os.path.join(adir, "inv")
    os.makedirs(invroot, exist_ok=True)
    names = _sorted_dirs(invroot, INV_RE)
    seq = "%02d-%s" % ((int(names[-1].split("-")[0]) + 1) if names else 1, kind)
    inv = os.path.join(invroot, seq)
    os.mkdir(inv)
    intent = {"token": uuid.uuid4().hex, "kind": kind, "argv": argv, "argv_sha256": argv_digest(argv),
              "backend": backend, "cwd": cwd, "time": store.utc_now(), "run_sh": os.path.join(inv, "run.sh"),
              "plan_id": plan_id, "unit_id": unit_id, "attempt": attempt}
    _write_scripts(inv, intent, stdin_text)
    store.write_json_atomic(os.path.join(inv, "intent.json"), intent)  # the intent precedes the effect
    store.append_event(root, "invocation_intent", plan_id=plan_id, unit_id=unit_id, attempt=attempt,
                       invocation=seq, backend=backend, token=intent["token"])
    try:
        fields = (spawn or _spawn_local)(inv, intent["run_sh"], envd, cwd)
    except OSError as e:
        store.write_json_atomic(os.path.join(inv, "launch_error.json"), {"token": intent["token"], "error": str(e)})
        raise ExecutionError("launch failed: %s" % e)
    launch = {"token": intent["token"], "backend": backend, "time": store.utc_now()}
    launch.update(fields)
    if backend == "local":
        pid = launch["pid"]
        launch["start"] = proc_start(pid, allow_zombie=True)
        try:
            launch["pgid"] = os.getpgid(pid)
        except OSError:
            launch["pgid"] = pid
    store.write_json_atomic(os.path.join(inv, "launch.json"), launch)
    return dict(intent, launch=launch, dir=inv, seq=seq)


def dispatch_job(root, plan_id, unit, after_quota=False):
    """Run a job unit's command through the local backend with OVERSEER_OUT set."""
    plan = approval.effective_plan(root, plan_id)
    uid = unit["id"]
    if slots_in_use(root, True) >= plan["caps"]["workers"]:
        raise SlotsFull("all %d worker slots are in use" % plan["caps"]["workers"])
    last = _counter(approval.load_unit_state(root, plan_id, uid))
    reuse = False  # an allocated attempt that never launched anything is reused, not burned
    if last:
        reuse = all(invocation_status(i["path"], True)["state"] in ("NOT_LAUNCHED", "LAUNCH_FAILED")
                    for i in iter_invocations(root, plan_id, uid) if i["attempt"] == last) \
            and not os.path.exists(os.path.join(paths.attempt_dir(root, plan_id, uid, last), "classification.json"))
    if reuse:
        n, adir = last, paths.attempt_dir(root, plan_id, uid, last)
    else:
        n, adir = allocate_attempt(root, plan_id, uid, after_quota)
    out = os.path.join(adir, "outputs")
    os.makedirs(out, exist_ok=True)
    sh = E.resolve_exe("sh", E.worker_env().get("PATH", ""))
    if sh is None:
        raise ExecutionError("sh not found on an absolute PATH entry")
    return launch_invocation(root, plan_id, uid, n, "job", [sh, "-c", unit["command"]], root,
                             extras={"OVERSEER_OUT": out})


# ---- abort -----------------------------------------------------------------

def _signal(launch, sig):
    try:
        if launch["pgid"] == launch["pid"] and launch["pgid"] != os.getpgrp():
            os.killpg(launch["pgid"], sig)
        else:
            os.kill(launch["pid"], sig)
    except ProcessLookupError:
        pass


def abort_invocation(root, inv, grace=10.0, sleep=time.sleep, confirm_wait=5.0):
    """SIGTERM the process group, escalate to SIGKILL after `grace`, confirm only once it is gone.

    cancel.json is written (requested_at) before any signal; confirmed_at is added only
    when the identified process is gone. Returns the resulting state.
    """
    st = invocation_status(inv, True)
    if st["state"] not in ACTIVE:
        return st["state"]
    intent = _load(os.path.join(inv, "intent.json"))
    if intent["backend"] != "local":
        raise ExecutionError("abort is not supported for backend %r here" % intent["backend"])
    launch = _load(os.path.join(inv, "launch.json"))
    if launch is None or not _valid_launch(launch, intent):
        launch = None  # nothing identifiable to signal
    ref = {"plan_id": intent["plan_id"], "unit_id": intent["unit_id"], "attempt": intent["attempt"],
           "invocation": os.path.basename(inv)}
    cpath = os.path.join(inv, "cancel.json")
    cancel = _load(cpath)
    if not (isinstance(cancel, dict) and cancel.get("token") == intent["token"]):
        cancel = {"token": intent["token"], "requested_at": store.utc_now()}
        store.write_json_atomic(cpath, cancel)
        store.append_event(root, "cancel_requested", **ref)

    def alive():
        return launch is not None and identified_alive(launch["pid"], launch["start"])

    if alive():
        _signal(launch, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while alive() and time.monotonic() < deadline:
        sleep(0.1)
    if alive():
        _signal(launch, signal.SIGKILL)
        end = time.monotonic() + confirm_wait
        while alive() and time.monotonic() < end:
            sleep(0.05)
    if alive():
        return "UNCONFIRMED"
    cancel["confirmed_at"] = store.utc_now()
    store.write_json_atomic(cpath, cancel)
    store.append_event(root, "cancel_confirmed", **ref)
    return invocation_status(inv, True)["state"]


# ---- wait ------------------------------------------------------------------

def _snap(root, write):
    return dict(((i["plan_id"], i["unit_id"], i["attempt"], i["seq"]), (i, invocation_status(i["path"], write)))
                for i in iter_invocations(root))


def _snapshot(root):
    try:
        with store.ProjectLock(root, "wait"):
            return _snap(root, True)
    except store.LockHeld:
        return _snap(root, False)


def wait_for_change(root, timeout=1800.0, poll=15.0, emit=None, sleep=time.sleep):
    """Poll until an invocation that was active at the start changes state. 0 on change, 3 on timeout."""
    emit = emit or (lambda line: print(line, flush=True))
    watch = dict((k, v[1]["state"]) for k, v in _snapshot(root).items() if v[1]["state"] in ACTIVE)
    if not watch:
        sys.stderr.write("no active invocations\n")
        return 0
    end = time.monotonic() + timeout
    while True:
        cur = _snapshot(root)
        changed = [k for k in sorted(watch) if k in cur and cur[k][1]["state"] != watch[k]]
        for k in changed:
            i, s = cur[k]
            emit(json.dumps({"plan": k[0], "unit": k[1], "attempt": k[2], "invocation": k[3], "kind": i["kind"],
                             "from": watch[k], "to": s["state"], "rc": s.get("rc"), "signal": s.get("signal")},
                            sort_keys=True))
        if changed:
            return 0
        if time.monotonic() >= end:
            sys.stderr.write("timeout after %gs\n" % timeout)
            return 3
        sleep(min(poll, max(0.0, end - time.monotonic())))


# ---- commands --------------------------------------------------------------

def configure_dispatch(p):
    p.add_argument("--unit", required=True, metavar="UNIT")
    p.add_argument("--plan", default=None, metavar="PLAN_ID")
    p.add_argument("--after-quota", action="store_true", help="allow a new attempt after a QUOTA or AUTH failure")


def run_dispatch(args):
    root = paths.find_root(args.root)
    with store.ProjectLock(root, "dispatch"):
        pid = args.plan or approval.find_plan_for_unit(root, args.unit)
        approval.require_approval(root, pid)
        unit = _unit_of(approval.effective_plan(root, pid), args.unit)
        if unit["kind"] != "job":
            raise ExecutionError("unit %s is a code unit; code workers are launched by the worker flow" % unit["id"])
        rec = dispatch_job(root, pid, unit, args.after_quota)
    print(json.dumps({"plan": pid, "unit": unit["id"], "attempt": rec["attempt"], "invocation": rec["seq"],
                      "pid": rec["launch"].get("pid")}, sort_keys=True))
    return 0


def configure_wait(p):
    p.add_argument("--timeout", type=float, default=1800.0, metavar="S")
    p.add_argument("--poll", type=float, default=15.0, metavar="S")


def run_wait(args):
    if args.timeout < 0 or args.poll <= 0:
        raise ExecutionError("--timeout must be >= 0 and --poll > 0")
    return wait_for_change(paths.find_root(args.root), args.timeout, args.poll)


def configure_abort(p):
    p.add_argument("unit", metavar="UNIT")
    p.add_argument("--plan", default=None, metavar="PLAN_ID")
    p.add_argument("--grace", type=float, default=10.0, metavar="S", help="seconds before SIGKILL")


def run_abort(args):
    root = paths.find_root(args.root)
    with store.ProjectLock(root, "abort"):
        pid = args.plan or approval.find_plan_for_unit(root, args.unit)
        todo = active_invocations(root, pid, args.unit, True)
        if not todo:
            print("nothing to abort for %s" % args.unit)
            return 0
        bad = 0
        for i in todo:
            res = abort_invocation(root, i["path"], args.grace)
            print("%s/a%d/%s: %s" % (args.unit, i["attempt"], i["seq"], res))
            bad += res == "UNCONFIRMED"
    return 1 if bad else 0


def configure_status(p):
    p.add_argument("--json", action="store_true", dest="as_json")


def collect_status(root, write=False):
    plans = {}
    pdir = paths.plans_dir(root)
    for pid in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        if not (paths.valid_id(pid) and os.path.exists(os.path.join(pdir, pid, "plan.json"))):
            continue
        plan = approval.effective_plan(root, pid)
        act = active_invocations(root, pid, write=write)
        units = {}
        for u in plan["units"]:
            st = approval.load_unit_state(root, pid, u["id"])
            units[u["id"]] = dict(
                dict((f, st[f]) for f in approval.FACETS), kind=u["kind"], attempts=st["counters"]["attempts"],
                active=[{"attempt": i["attempt"], "invocation": i["seq"], "kind": i["kind"],
                         "state": i["status"]["state"], "pid": i["status"].get("pid")}
                        for i in act if i["unit_id"] == u["id"]])
        plans[pid] = {"workers_cap": plan["caps"]["workers"], "units": units}
    return {"slots_in_use": slots_in_use(root, write), "plans": plans}


def run_status(args):
    root = paths.find_root(args.root)
    try:
        with store.ProjectLock(root, "status"):
            data = collect_status(root, True)
    except store.LockHeld:
        data = collect_status(root, False)
    if args.as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    for pid, p in data["plans"].items():
        print("plan %s: slots %d/%d" % (pid, data["slots_in_use"], p["workers_cap"]))
        for uid, u in p["units"].items():
            print("  %s (%s) auth=%s exec=%s closure=%s attempts=%d" % (
                uid, u["kind"], u["authorization"].get("state"), u["execution"].get("state"),
                u["closure"].get("state"), u["attempts"]))
            for a in u["active"]:
                print("    a%d/%s %s pid=%s" % (a["attempt"], a["invocation"], a["state"], a["pid"]))
    return 0
