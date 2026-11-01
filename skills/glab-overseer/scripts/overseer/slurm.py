"""Slurm backend: submission, status evidence, abort and the shared-storage handshake.

A Slurm invocation is an ordinary U3 invocation with backend "slurm": intent.json first,
launch.json (with `job_id`) after `sbatch`. The generated run.sh runs inside the batch job.
Status evidence is combined by the pure `judge`; every consulted source and its raw outcome
is recorded in the invocation's evidence.json. `sbatch` and `scancel` are never repeated
when their outcome is ambiguous: the token (job comment) is searched in `squeue` instead.
"""
import calendar
import os
import re
import secrets
import time
import uuid

from . import approval, env as E, execution as X, paths, plan as P, store

QUERY_TIMEOUT = 20
SACCT_TIMEOUT = 5
SBATCH_TIMEOUT = 60
HANDSHAKE_MAX_AGE = 7 * 86400
TMP_ROOTS = ("/tmp", "/var/tmp")
JOBID_RE = re.compile(r"^([0-9]+)(;[A-Za-z0-9_.-]+)?$")
EXIT_RE = re.compile(r"^[0-9]+:[0-9]+$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._,-]*$")
MEM_RE = re.compile(r"^[0-9]+[KMGTkmgt]?$")
GRES_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:.,/=-]*$")
QUEUED = ("PENDING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "RESV_DEL_HOLD", "SPECIAL_EXIT")
RUNNING = ("RUNNING", "COMPLETING", "CONFIGURING", "SUSPENDED", "STOPPED", "RESIZING", "SIGNALING",
           "STAGE_OUT", "UPDATE_DB")
TERMINAL = ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED",
            "BOOT_FAIL", "DEADLINE", "REVOKED")
LOG_PATTERNS = (("time_limit", re.compile(r"DUE TO TIME LIMIT", re.I)),
                ("out_of_memory", re.compile(r"oom[-_ ]?kill|out of memory|OUT_OF_MEMORY", re.I)),
                ("cancelled", re.compile(r"CANCELLED AT|DUE TO (?:PREEMPTION|NODE FAILURE)", re.I)))
REJECTION_RE = re.compile("|".join((
    "invalid partition", "invalid account", "invalid qos", "requested node configuration is not available",
    "requested time limit", "memory specification", "job violates accounting/qos policy",
    "access/permission denied", "invalid feature specification", "invalid generic resource")), re.I)
NO_COMMENT = ("", "(null)")
CACHE_OK = ("not_known", "timed_out", "error")  # never after a mismatch: the id may belong to another job
LOG_FULL_MAX = 256 * 1024 * 1024
LOG_EDGE = 16 * 1024 * 1024


class SlurmError(X.ExecutionError):
    pass


class SbatchRejected(X.LaunchRejected):
    """sbatch exited non-zero: nothing was submitted (recorded as a launch error)."""


class SbatchAmbiguous(X.ExecutionError):
    """sbatch timed out or printed something unparseable and squeue shows no such job."""


# ---- backend choice and preconditions --------------------------------------

def disabled(base=None):
    """True only when OVERSEER_DISABLE_SLURM is `1`, `true` or `yes` (case-insensitive)."""
    return E.slurm_disabled(base)


def _survey_slurm(root):
    try:
        s = store.read_json(os.path.join(paths.state_dir(root), "survey.json"), {})
    except ValueError:
        return {}
    s = s.get("slurm") if isinstance(s, dict) else None
    return s if isinstance(s, dict) else {}


def choose_backend(root, base=None):
    """slurm when the survey reports Slurm usable and OVERSEER_DISABLE_SLURM is unset, else local."""
    return "slurm" if not disabled(base) and _survey_slurm(root).get("usable") is True else "local"


def local_agent_problem(root, base=None):
    """Why an agent may not run locally here, else None.

    Decided from the current host at launch time: sbatch on the current PATH and no SLURM_JOB_ID.
    survey.json and the disable flag are ignored.
    """
    base = os.environ if base is None else base
    if base.get("SLURM_JOB_ID"):
        return None
    if E.resolve_exe("sbatch", base.get("PATH", "")):
        return ("this is a Slurm host and no job allocation is active: an agent invocation must not run "
                "on the login node; dispatch it through the slurm backend or run inside an allocation")
    return None


def root_problem(root, base=None):
    base = os.environ if base is None else base
    real = os.path.realpath(root)
    for t in list(TMP_ROOTS) + ([base["TMPDIR"]] if base.get("TMPDIR") else []):
        t = os.path.realpath(t)
        if real == t or real.startswith(t.rstrip(os.sep) + os.sep):
            return "project root %s is under %s, which is private to each Slurm job" % (real, t)
    return None


def resources(plan, unit):
    """Merged, validated Slurm resources of a unit; refuses naming what the approved plan must add."""
    def block(o):
        b = (o or {}).get("slurm")
        return b if isinstance(b, dict) else {}
    r = P.merge_slurm(block(plan), block(unit))
    missing, bad = [], []

    def text(key, rx, optional=False):
        v = r.get(key)
        if v is None:
            if not optional:
                missing.append(key)
        elif not (isinstance(v, str) and rx.match(v)):
            bad.append(key)
    text("partition", NAME_RE)
    text("account", NAME_RE, True)
    text("qos", NAME_RE, True)
    text("gres", GRES_RE, True)
    t = r.get("time")
    if t is None:
        missing.append("time")
    elif P.parse_walltime(t) is None:
        bad.append("time (finite walltime required)")
    mem = [k for k in ("mem", "mem_per_cpu") if k in r]
    if not mem:
        missing.append("mem or mem_per_cpu")
    elif len(mem) > 1:
        bad.append("mem and mem_per_cpu (give one)")
    elif not (isinstance(r[mem[0]], str) and MEM_RE.match(r[mem[0]])):
        bad.append(mem[0])
    c = r.get("cpus")
    if c is None:
        missing.append("cpus")
    elif type(c) is not int or c < 1:
        bad.append("cpus")
    if missing or bad:
        parts = []
        if missing:
            parts.append("missing: " + ", ".join(missing))
        if bad:
            parts.append("invalid: " + ", ".join(bad))
        raise SlurmError("Slurm dispatch refused (%s); the approved plan must declare these values in its "
                         "slurm defaults or the unit's slurm block" % "; ".join(parts))
    out = {"partition": r["partition"], "time": str(r["time"]).strip(), "cpus": c}
    for k in ("account", "qos", "gres", "mem", "mem_per_cpu"):
        if k in r:
            out[k] = r[k]
    return out


def _handshake_evidence(root, rec):
    """Epoch time the batch job wrote in its reply, if the recorded token's directory lies under this
    root and holds the nonce and a matching reply; else None."""
    tok = rec.get("token")
    if not (isinstance(tok, str) and paths.valid_id(tok)):
        return None
    base = os.path.realpath(os.path.join(paths.state_dir(root), "handshake"))
    d = os.path.realpath(os.path.join(base, tok))
    if os.path.dirname(d) != base:
        return None
    try:
        with open(os.path.join(d, "nonce")) as f:
            nonce = f.read()
    except OSError:
        return None
    reply = X._load(os.path.join(d, "reply.json"))
    if not (nonce and isinstance(reply, dict) and reply.get("nonce") == nonce
            and isinstance(reply.get("host"), str) and reply["host"] and reply["host"] == rec.get("host")):
        return None
    try:
        return calendar.timegm(time.strptime(reply.get("time"), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def check_handshake(root, now=None):
    """Refuse unless a handshake for this root succeeded in the last 7 days.

    The age comes from the UTC time the batch job wrote into its reply, not from the mutable record.
    Same-user tampering with local state is outside the cooperative boundary (C20).
    """
    rec = X._load(os.path.join(paths.state_dir(root), "handshake.json"))
    stamp = _handshake_evidence(root, rec) if isinstance(rec, dict) else None
    age = None if stamp is None else (now or time.time()) - stamp
    if not (age is not None and rec.get("root") == os.path.realpath(root) and -300 <= age <= HANDSHAKE_MAX_AGE):
        raise SlurmError("no successful Slurm handshake for %s in the last 7 days; run `overseer.py "
                         "slurm-handshake`" % os.path.realpath(root))


def preflight(root, plan, unit, base=None):
    """Everything Slurm dispatch needs before any attempt or submission; returns the resources."""
    res = resources(plan, unit)
    problem = root_problem(root, base)
    if problem:
        raise SlurmError(problem)
    check_handshake(root)
    return res


# ---- scheduler queries -------------------------------------------------------

def _raw(r):
    return {"rc": r["rc"], "timed_out": r["timed_out"], "stdout": r["stdout"][:1500], "stderr": r["stderr"][:1500]}


def _tool(argv, timeout):
    return E.run_tool(argv, E.worker_env(), timeout)


def query_squeue():
    r = _tool(["squeue", "--me", "-h", "-o", "%i|%j|%k|%T|%r"], QUERY_TIMEOUT)
    if r["timed_out"] or r["rc"] != 0:
        return dict(_raw(r), outcome="timed_out" if r["timed_out"] else "error", rows=[])
    rows = []
    for line in r["stdout"].splitlines():
        if not line.strip():
            continue
        p = line.split("|")
        if len(p) < 5 or not p[0].strip():  # names may contain '|': anchor on the trailing fields
            return dict(_raw(r), outcome="unparseable", rows=[])
        rows.append({"id": p[0].strip(), "name": "|".join(p[1:-3]), "comment": p[-3], "state": p[-2].strip()})
    return dict(_raw(r), outcome="ok", rows=rows)


def job_name(intent):
    return "overseer-%s-a%d-%s" % (intent["unit_id"], intent["attempt"], intent["token"][:8])


def _verified(s):
    """A scheduler record that is bound to our token (a comment-less record is identity unknown)."""
    return bool(s) and s.get("outcome") in ("listed", "known") and s.get("identity") != "unknown"


def _owned(job_id, token):
    """(job carries our full token per squeue or scontrol, squeue view); used before any scancel."""
    sq = squeue_view(query_squeue(), job_id, token)
    return _verified(sq) or _verified(query_scontrol(job_id, token)), sq


def squeue_view(sq, job_id, token):
    if sq["outcome"] != "ok":
        return dict((k, v) for k, v in sq.items() if k != "rows")
    for row in sq["rows"]:
        if row["id"] == job_id:
            if row["comment"].strip() in NO_COMMENT:  # a site that hides comments: identity unknown, not a mismatch
                return {"outcome": "listed", "state": row["state"], "identity": "unknown"}
            if row["comment"] != token:
                return {"outcome": "mismatch", "comment": row["comment"]}
            return {"outcome": "listed", "state": row["state"]}
    return {"outcome": "absent"}


def query_scontrol(job_id, token):
    r = _tool(["scontrol", "show", "job", job_id], QUERY_TIMEOUT)
    if r["timed_out"]:
        return dict(_raw(r), outcome="timed_out")
    if r["rc"] != 0:
        return dict(_raw(r), outcome="not_known" if "invalid job id" in r["stderr"].lower() else "error")
    kv = {}
    for k, v in re.findall(r"(?:^|\s)([A-Za-z][A-Za-z:/]*)=(\S*)", r["stdout"]):
        kv.setdefault(k, v)
    state = (kv.get("JobState") or "").split("+")[0]
    if kv.get("JobId") != job_id or not state or not EXIT_RE.match(kv.get("ExitCode", "")):
        return dict(_raw(r), outcome="unparseable")
    comment = kv.get("Comment")
    if comment is not None and comment not in NO_COMMENT and comment != token:
        return dict(_raw(r), outcome="mismatch", comment=comment)
    ident = {} if comment == token else {"identity": "unknown"}
    return dict(_raw(r), outcome="known", state=state, exit=kv["ExitCode"], **ident)


def query_sacct(job_id, name):
    r = _tool(["sacct", "-j", job_id, "-n", "-P", "-o", "JobID,State,ExitCode,JobName"], SACCT_TIMEOUT)
    if r["timed_out"]:
        return dict(_raw(r), outcome="timed_out")
    if r["rc"] != 0:
        return dict(_raw(r), outcome="error")
    for line in r["stdout"].splitlines():
        p = line.split("|")
        if len(p) != 4:
            return dict(_raw(r), outcome="unparseable")
        if p[0] == job_id:
            state = p[1].split()[0] if p[1].split() else ""
            if not state or not EXIT_RE.match(p[2]):
                return dict(_raw(r), outcome="unparseable")
            if p[3] != name:
                return dict(_raw(r), outcome="mismatch", name=p[3])
            return dict(_raw(r), outcome="known", state=state, exit=p[2])
    return dict(_raw(r), outcome="not_known")


def _scan_lines(f, limit, found):
    """Scan up to `limit` bytes of f line by line (bounded memory); add matching labels to `found`."""
    tail = b""  # overlap so a pattern split across two reads is still seen
    while limit > 0:
        line = f.readline(min(1 << 16, limit))
        if not line:
            break
        limit -= len(line)
        data = tail + line
        tail = data[-64:]
        text = data.decode("utf-8", "replace")
        for label, rx in LOG_PATTERNS:
            if label not in found and rx.search(text):
                found.add(label)


def scan_logs(adir, job_id):
    """Failure messages (time limit, out of memory, cancellation) in slurm-<id>.out/.err. Failure only.

    Each file is streamed completely. A file over LOG_FULL_MAX is scanned in its first and last
    LOG_EDGE bytes only and reported as `partial`. Returns (hits, per-file state).
    """
    hits, seen = [], {}
    for ext in ("out", "err"):
        name = "slurm-%s.%s" % (job_id, ext)
        path = os.path.join(adir, name)
        seen[name] = "absent"
        found = set()
        try:
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                if size <= LOG_FULL_MAX:
                    _scan_lines(f, size + 1, found)
                    seen[name] = "read"
                else:
                    _scan_lines(f, LOG_EDGE, found)
                    f.seek(size - LOG_EDGE)  # scan the edge from its first byte
                    _scan_lines(f, LOG_EDGE, found)
                    seen[name] = "partial"
        except OSError:
            seen[name] = "unreadable"
            continue
        hits += ["%s@%s" % (label, name) for label, _ in LOG_PATTERNS if label in found]
    return hits, seen


# ---- evidence ----------------------------------------------------------------

def _cls(state):
    if state in QUEUED:
        return "QUEUED"
    if state in RUNNING:
        return "RUNNING"
    return "terminal" if state in TERMINAL else None


def judge(squeue, scontrol, sacct, exit_rec, log_hits, logs_partial=False):
    """Combine evidence: returns (verdict, evidence list).

    Each scheduler argument is None (not consulted) or a dict with `outcome` (listed, known,
    absent, not_known, or any failure outcome) and, when listed or known, `state` and `exit`.
    `exit_rec` is the wrapper record already bound to token and argv digest, or None. Verdicts:
    QUEUED, RUNNING, COMPLETED, FAILED, UNCERTAIN. Failure evidence wins; success needs a
    scheduler COMPLETED with 0:0 or a wrapper rc 0 and no failure; logs only add failure. When
    the logs were only partially scanned (`logs_partial`), success needs scheduler evidence.
    """
    ev, fail, ok, unsure, active = [], [], [], [], None
    for name, s in (("squeue", squeue), ("scontrol", scontrol), ("sacct", sacct)):
        out = s.get("outcome") if s else "not_consulted"
        state = s.get("state") if s and out in ("listed", "known") else None
        ev.append("%s:%s%s" % (name, out, ":" + state if state else ""))
        if out == "mismatch":
            unsure.append("%s record belongs to another job" % name)
        if state is None:
            continue
        c = _cls(state)
        if c in ("QUEUED", "RUNNING"):
            active = active or c  # an active record of unknown identity keeps the invocation active
        elif s.get("identity") == "unknown":
            unsure.append("%s state %r has unknown identity" % (name, state))
        elif c is None:
            unsure.append("%s state %r not recognised" % (name, state))
        elif name == "squeue":
            if state != "COMPLETED":  # a listing carries no exit code
                fail.append("squeue_" + state)
        elif state == "COMPLETED" and s.get("exit") == "0:0":
            ok.append("%s_completed_0:0" % name)
        else:
            fail.append("%s_%s_%s" % (name, state, s.get("exit")))
    if active:
        return active, ev
    if exit_rec is None:
        ev.append("wrapper:none")
    elif exit_rec["rc"] != 0 or exit_rec.get("signal"):
        fail.append("wrapper_rc_%s" % exit_rec["rc"])
        ev.append("wrapper:rc=%s" % exit_rec["rc"])
    else:
        ok.append("wrapper_rc_0")
        ev.append("wrapper:rc=0")
    for h in log_hits:
        fail.append("log_" + h)
        ev.append("log:" + h)
    if fail:
        return "FAILED", ev + ["failure:" + f for f in fail]
    if unsure:
        return "UNCERTAIN", ev + unsure
    if logs_partial and not [o for o in ok if o != "wrapper_rc_0"]:
        return "UNCERTAIN", ev + ["logs only partially scanned and no scheduler success"]
    return ("COMPLETED", ev + ["success:" + o for o in ok]) if ok else ("UNCERTAIN", ev + ["no terminal evidence"])


def _evidence_path(inv):
    return os.path.join(inv, "evidence.json")


def gather(inv, intent, launch):
    """Consult every source; returns (verdict, evidence, sources, exit_rec). Active jobs stop early."""
    jid, tok = launch["job_id"], intent["token"]
    prev = X._load(_evidence_path(inv))
    prev = prev.get("sources", {}) if isinstance(prev, dict) and prev.get("token") == tok else {}
    src = {"squeue": squeue_view(query_squeue(), jid, tok), "scontrol": None, "sacct": None}
    if src["squeue"]["outcome"] != "listed" or _cls(src["squeue"].get("state")) == "terminal":
        sc = src["scontrol"] = query_scontrol(jid, tok)
        if sc["outcome"] in CACHE_OK and _verified(prev.get("scontrol")):
            sc = src["scontrol"] = dict(prev["scontrol"], cached=True)  # scontrol forgets finished jobs
        if sc["outcome"] != "known":
            sa = src["sacct"] = query_sacct(jid, job_name(intent))
            if sa["outcome"] in CACHE_OK and _verified(prev.get("sacct")):
                src["sacct"] = dict(prev["sacct"], cached=True)
    ex = X._load(os.path.join(inv, "exit.json"))
    exit_rec = ex if ex is not None and X._exit_matches(ex, intent) else None
    src["exit"] = {"outcome": "none" if ex is None else ("matched" if exit_rec else "mismatch"),
                   "record": exit_rec}
    hits, seen = scan_logs(os.path.dirname(os.path.dirname(inv)), jid)
    src["logs"] = {"files": seen, "hits": hits}
    partial = "partial" in seen.values()
    verdict, ev = judge(src["squeue"], src["scontrol"], src["sacct"], exit_rec, hits, partial)
    return verdict, ev, src, exit_rec


def _record(inv, intent, launch, verdict, ev, src):
    old = X._load(_evidence_path(inv))
    ever = isinstance(old, dict) and old.get("token") == intent["token"] and old.get("sacct_ever_answered") is True
    sa = src.get("sacct")
    ever = ever or bool(sa and sa.get("outcome") in ("known", "not_known") and not sa.get("cached"))
    store.write_json_atomic(_evidence_path(inv), {
        "token": intent["token"], "argv_sha256": intent["argv_sha256"], "job_id": launch["job_id"],
        "time": store.utc_now(), "verdict": verdict, "evidence": ev, "sources": src,
        "sacct_ever_answered": ever})


def _rc(src, exit_rec):
    if exit_rec and exit_rec["rc"] != 0:
        return exit_rec["rc"]
    for k in ("scontrol", "sacct"):
        s = src.get(k)
        if s and s.get("outcome") == "known" and s.get("exit") not in (None, "0:0"):
            a, b = (int(x) for x in s["exit"].split(":"))
            return a or (128 + b if b else 1)
    return 1


def probe(inv, intent, launch, write=False):
    """PROBES['slurm']: the invocation status for a launched Slurm job (evidence recorded when `write`)."""
    jid = launch["job_id"]
    cancel = X._load(os.path.join(inv, "cancel.json"))
    if isinstance(cancel, dict) and cancel.get("token") == intent["token"] \
            and isinstance(cancel.get("confirmed_at"), str):
        # a confirmed cancellation is a failure for judgment whatever the wrapper recorded
        return {"state": "CANCELLED", "job_id": jid, "verdict": "FAILED"}
    verdict, ev, src, exit_rec = gather(inv, intent, launch)
    if verdict in ("QUEUED", "RUNNING"):
        return {"state": verdict, "job_id": jid}
    if write:
        _record(inv, intent, launch, verdict, ev, src)
    if verdict == "UNCERTAIN":
        return {"state": "UNCERTAIN", "job_id": jid, "reason": "no conclusive Slurm evidence", "evidence": ev}
    return {"state": "EXITED", "rc": 0 if verdict == "COMPLETED" else _rc(src, exit_rec),
            "signal": exit_rec.get("signal") if exit_rec else None, "job_id": jid,
            "verdict": verdict, "evidence": ev}


def find_by_token(token):
    """Search squeue for a job whose comment is the full token.

    Returns ('found', id), ('none',), ('many',), ('unknown',) or ('unsupported',) when every listed
    job has an empty comment (the site does not show comments): the name is never a substitute.
    """
    sq = query_squeue()
    if sq["outcome"] != "ok":
        return ("unknown",)
    ids = [r["id"] for r in sq["rows"] if r["comment"] == token]
    if ids:
        return ("found", ids[0]) if len(ids) == 1 else ("many",)
    if sq["rows"] and all(r["comment"].strip() in ("", "(null)") for r in sq["rows"]):
        return ("unsupported",)
    return ("none",)


def reconcile(inv, intent, write):
    """Find the job of an intent without launch.json by its token; never submits."""
    hit = find_by_token(intent["token"])
    jid, source = (hit[1], "squeue") if hit[0] == "found" else (None, None)
    if jid is None and hit[0] in ("none", "unknown", "unsupported"):
        st = X._load(os.path.join(inv, "start.json"))  # the wrapper recorded its own job id
        if isinstance(st, dict) and st.get("token") == intent["token"] and isinstance(st.get("job_id"), str) \
                and st["job_id"].isdigit():
            jid, source = st["job_id"], "start.json"
    if jid is None:
        return None
    launch = {"token": intent["token"], "backend": "slurm", "job_id": jid, "time": store.utc_now(),
              "reconciled": source}
    if write:
        store.write_json_atomic(os.path.join(inv, "launch.json"), launch)
    return launch


# ---- submission ----------------------------------------------------------------

def sbatch_argv(name, token, chdir, res, script):
    a = ["sbatch", "--parsable", "--job-name", name, "--comment", token, "--chdir", chdir,
         "--output", os.path.join(chdir, "slurm-%j.out"), "--error", os.path.join(chdir, "slurm-%j.err"),
         "--partition", res["partition"]]
    if "account" in res:
        a += ["--account", res["account"]]
    a += ["--time", res["time"]]
    a += ["--mem", res["mem"]] if "mem" in res else ["--mem-per-cpu", res["mem_per_cpu"]]
    a += ["--cpus-per-task", str(res["cpus"])]
    for flag, key in (("--gres", "gres"), ("--qos", "qos")):
        if key in res:
            a += [flag, res[key]]
    return a + [script]


def submit(d, env, argv, token, name):
    """Run sbatch exactly once; returns the job id. `d` receives sbatch.json (argv and raw outcome).

    A non-zero exit is a definite rejection only when stderr matches REJECTIONS. Measured on a
    cluster: sbatch exited 1 with "Batch job submission failed: Socket timed out on send/recv
    operation" and the job was submitted and ran anyway. Every other failure is ambiguous: squeue
    is searched for the token, and otherwise the invocation stays UNCERTAIN (later reconciliation
    uses squeue and the wrapper's own start.json job id).
    """
    r = E.run_tool(argv, env, SBATCH_TIMEOUT)
    rec = dict(_raw(r), argv=argv, time=store.utc_now())
    out = os.path.join(d, "sbatch.json")
    if not r["timed_out"] and r["rc"] != 0 and REJECTION_RE.search(r["stderr"]):
        store.write_json_atomic(out, dict(rec, outcome="rejected"))
        raise SbatchRejected("sbatch rejected the job (rc %s): %s" % (r["rc"], r["stderr"].strip()[:500]))
    m = JOBID_RE.match(r["stdout"].strip()) if r["rc"] == 0 else None
    if m:
        store.write_json_atomic(out, dict(rec, outcome="submitted"))
        return m.group(1)
    hit = find_by_token(token)  # ambiguous: reconcile by token, never call sbatch again
    if hit[0] == "found":
        store.write_json_atomic(out, dict(rec, outcome="recovered_by_squeue"))
        return hit[1]
    store.write_json_atomic(out, dict(rec, outcome="ambiguous", squeue=hit[0]))
    raise SbatchAmbiguous("sbatch outcome is ambiguous (%s) and squeue shows no single job with token %s (%s); "
                          "the invocation stays UNCERTAIN and sbatch is not repeated"
                          % ("timed out" if r["timed_out"] else "exit %s: %s" % (r["rc"], r["stderr"].strip()[:200]),
                             token, hit[0]))


def make_spawn():
    """The `spawn` for launch_invocation: submits run.sh with the resources recorded in the intent."""
    def spawn(inv, run_sh, envd, cwd):
        intent = X._load(os.path.join(inv, "intent.json"))
        adir = os.path.dirname(os.path.dirname(inv))
        argv = sbatch_argv(job_name(intent), intent["token"], adir, intent["slurm"], run_sh)
        return {"job_id": submit(inv, envd, argv, intent["token"], job_name(intent))}
    return spawn


def launch(root, plan_id, unit_id, attempt, kind, argv, cwd, stdin_text=None, extras=None):
    """Submit an author, fixer or job invocation as a batch job (preflight, then U3's launch)."""
    plan = approval.effective_plan(root, plan_id)
    res = preflight(root, plan, X._unit_of(plan, unit_id))
    return X.launch_invocation(root, plan_id, unit_id, attempt, kind, argv, cwd, backend="slurm",
                               stdin_text=stdin_text, extras=extras, spawn=make_spawn(),
                               extra_intent={"slurm": res, "env_extras": dict(extras or {})})


# ---- abort -----------------------------------------------------------------------

def _root_of(inv, intent):
    """Project root from the invocation path, checked against the known layout; None if it differs."""
    tail = [paths.STATE_DIRNAME, "plans", intent["plan_id"], "units", intent["unit_id"], "attempts",
            "a%d" % intent["attempt"], "inv", os.path.basename(inv)]
    parts = os.path.abspath(inv).split(os.sep)
    if parts[-len(tail):] != tail:
        return None
    return os.sep.join(parts[:-len(tail)]) or os.sep


def cancel_check(inv, intent, cancel):
    """Confirm an outstanding cancel: the job left squeue and CANCELLED or a wrapper exit is on record."""
    launch = X._load(os.path.join(inv, "launch.json"))
    if not (launch is not None and X._valid_launch(launch, intent)):
        return False
    _, _, src, exit_rec = gather(inv, intent, launch)
    cancelled = any(_verified(s) and s["state"].startswith("CANCELLED") for s in (src["scontrol"], src["sacct"]))
    sc = src["scontrol"]
    active = bool(sc) and sc.get("outcome") == "known" and _cls(sc["state"]) in ("QUEUED", "RUNNING")
    if not (src["squeue"]["outcome"] == "absent" and not active and (cancelled or exit_rec is not None)):
        return False
    cancel["confirmed_at"] = store.utc_now()
    store.write_json_atomic(os.path.join(inv, "cancel.json"), cancel)
    root = _root_of(inv, intent)
    if root:
        store.append_event(root, "cancel_confirmed", plan_id=intent["plan_id"], unit_id=intent["unit_id"],
                           attempt=intent["attempt"], invocation=os.path.basename(inv))
    return True


def abort(root, inv, intent, confirm_wait=30.0, sleep=time.sleep):
    launch = X._load(os.path.join(inv, "launch.json"))
    ref = {"plan_id": intent["plan_id"], "unit_id": intent["unit_id"], "attempt": intent["attempt"],
           "invocation": os.path.basename(inv)}
    cpath = os.path.join(inv, "cancel.json")
    if launch is None or not X._valid_launch(launch, intent):
        reason = "no job id on record for this invocation; a job may still run"
        store.write_json_atomic(cpath, {"token": intent["token"], "requested_at": store.utc_now(),
                                        "unidentified": reason})
        store.append_event(root, "cancel_unidentified", reason=reason, **ref)
        return "UNIDENTIFIED"
    cancel = X._load(cpath)
    if not X._cancel_pending(cancel, intent):
        cancel = {"token": intent["token"], "requested_at": store.utc_now()}
        store.write_json_atomic(cpath, cancel)  # the request is recorded before scancel runs
        store.append_event(root, "cancel_requested", **ref)
    prev = cancel.get("scancel")
    if prev is None or prev.get("retryable") is True:  # a timed-out scancel is ambiguous: never repeated
        mine, sq = _owned(launch["job_id"], intent["token"])
        if not mine:  # the recorded id may belong to another job now: never scancel on a bare id
            reason = "job %s cannot be shown to carry this invocation's token; not cancelled" % launch["job_id"]
            store.write_json_atomic(cpath, dict(cancel, unidentified=reason))
            store.append_event(root, "cancel_unidentified", reason=reason, **ref)
            return "UNIDENTIFIED"
        if sq["outcome"] == "absent":  # already left the queue: nothing to cancel, only to confirm
            cancel["scancel"] = {"time": store.utc_now(), "skipped": "job already left squeue", "retryable": False}
        else:
            r = _tool(["scancel", launch["job_id"]], QUERY_TIMEOUT)
            cancel["scancel"] = {"time": store.utc_now(), "rc": r["rc"], "timed_out": r["timed_out"],
                                 "stderr": r["stderr"][:500], "retryable": not r["timed_out"] and r["rc"] != 0}
        store.write_json_atomic(cpath, cancel)
    end = time.monotonic() + confirm_wait
    while True:
        if cancel_check(inv, intent, cancel):
            return X.invocation_status(inv, True)["state"]
        if time.monotonic() >= end:
            return "UNCONFIRMED"
        sleep(1.0)


# ---- handshake -----------------------------------------------------------------------

_JOB = """#!/bin/sh
D=%s
n=$(cat "$D/nonce") || exit 3
h=$(hostname 2>/dev/null || uname -n)
printf '{"nonce": "%%s", "host": "%%s", "time": "%%s"}\\n' "$n" "$h" "$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)" > "$D/reply.tmp" && mv "$D/reply.tmp" "$D/reply.json"
"""


def _approved_plan(root, plan_id):
    if plan_id is None:
        pdir = paths.plans_dir(root)
        ids = [p for p in sorted(os.listdir(pdir)) if paths.valid_id(p)
               and os.path.exists(os.path.join(pdir, p, "plan.json"))] if os.path.isdir(pdir) else []
        if len(ids) != 1:
            raise SlurmError("pass --plan PLAN_ID (%d plans found)" % len(ids))
        plan_id = ids[0]
    approval.require_approval(root, plan_id)
    return approval.effective_plan(root, plan_id)


def handshake(root, plan_id=None, timeout=600.0, poll=5.0, sleep=time.sleep):
    """Prove controller and batch job see the same project root; records the verified root and time."""
    root = os.path.realpath(root)
    problem = root_problem(root)
    if problem:
        raise SlurmError(problem)
    if choose_backend(root) != "slurm":
        raise SlurmError("Slurm is not usable here (run `overseer.py survey`; OVERSEER_DISABLE_SLURM must be unset)")
    res = resources(_approved_plan(root, plan_id), None)
    token, nonce = uuid.uuid4().hex, secrets.token_hex(16)
    d = os.path.join(paths.state_dir(root), "handshake", token)
    paths.ensure_state_dir(root)
    os.makedirs(d)
    with open(os.path.join(d, "nonce"), "w") as f:
        f.write(nonce)
    script = os.path.join(d, "job.sh")
    with open(script, "w") as f:
        f.write(_JOB % ("'%s'" % d.replace("'", "'\\''")))
    os.chmod(script, 0o755)
    name = "overseer-handshake-" + token[:8]
    store.write_json_atomic(os.path.join(d, "intent.json"), {"token": token, "resources": res, "time": store.utc_now()})
    jid = submit(d, E.worker_env(), sbatch_argv(name, token, d, res, script), token, name)
    store.write_json_atomic(os.path.join(d, "launch.json"), {"token": token, "job_id": jid})
    end, why, reply = time.monotonic() + timeout, None, None
    while True:
        reply = X._load(os.path.join(d, "reply.json"))
        if reply is not None:
            break
        if time.monotonic() >= end:
            why = "no reply from job %s within %gs: the batch job does not see %s" % (jid, timeout, d)
            break
        sleep(min(poll, max(0.0, end - time.monotonic())))
    if why is None and not (isinstance(reply, dict) and reply.get("nonce") == nonce
                            and isinstance(reply.get("host"), str) and reply["host"]):
        why = "reply from job %s does not carry the nonce and a hostname" % jid
    if why:
        if _owned(jid, token)[0]:  # the same identity check as abort: never scancel a bare id
            _tool(["scancel", jid], QUERY_TIMEOUT)
        else:
            why += " (scancel skipped: the job cannot be shown to carry the handshake token)"
        store.write_json_atomic(os.path.join(d, "result.json"), {"ok": False, "reason": why, "time": store.utc_now()})
        raise SlurmError("handshake failed: " + why)
    rec = {"root": root, "time": store.utc_now(), "token": token, "job_id": jid, "host": reply["host"]}
    store.write_json_atomic(os.path.join(d, "result.json"), dict(rec, ok=True))
    store.write_json_atomic(os.path.join(paths.state_dir(root), "handshake.json"), rec)
    store.append_event(root, "slurm_handshake", token=token, host=reply["host"])
    return rec


def configure_handshake(p):
    p.add_argument("--plan", default=None, metavar="PLAN_ID")
    p.add_argument("--timeout", type=float, default=600.0, metavar="S")
    p.add_argument("--poll", type=float, default=5.0, metavar="S")


def run_handshake(args):
    root = paths.find_root(args.root)
    if args.timeout <= 0 or args.poll <= 0:
        raise SlurmError("--timeout and --poll must be positive")
    with store.ProjectLock(root, "slurm-handshake"):
        rec = handshake(root, args.plan, args.timeout, args.poll)
    print("handshake ok: root %s verified by job %s on %s at %s" % (rec["root"], rec["job_id"], rec["host"], rec["time"]))
    return 0


X.PROBES["slurm"] = probe
X.RECONCILE["slurm"] = reconcile
X.CANCEL_CHECK["slurm"] = cancel_check
