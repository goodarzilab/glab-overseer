"""Judgment: `judge` and `resolve`.

`judgment.json` of an attempt holds status DONE, FAILED or UNCERTAIN with the reasons.
Failure evidence always wins; an unanswered question is UNCERTAIN, never DONE.
A code judgment binds to the sealed candidate head and is valid only while the
worktree HEAD still equals it (`valid_judgment`). A job judgment binds to the token
of the invocation it judged. Writes expect the caller to hold `store.ProjectLock`.
Output validators and the test command run through `env.run_tool` with the worker
environment: they are plan content, approved with the plan.
"""
import hashlib
import json
import os

from . import approval, env as E, execution as X, paths, store, workspace as W

TEST_TIMEOUT = 1800
VALIDATE_TIMEOUT = 300
RELEVANT = {"code": ("author", "fixer"), "job": ("job",)}
EXIT = {"DONE": 0, "FAILED": 1, "UNCERTAIN": 4}


class JudgeError(X.ExecutionError):
    pass


def _unit_attempt(root, plan_id, unit):
    st = approval.load_unit_state(root, plan_id, unit["id"])
    n = st["counters"]["attempts"]
    if not X._int(n):
        raise JudgeError("unit %s has no attempt to judge" % unit["id"])
    return st, n, paths.attempt_dir(root, plan_id, unit["id"], n)


def _invocations(root, plan_id, unit, n):
    kinds = RELEVANT[unit["kind"]]
    return [dict(i, status=X.invocation_status(i["path"], True))
            for i in X.iter_invocations(root, plan_id, unit["id"]) if i["attempt"] == n and i["kind"] in kinds]


def execution_evidence(invs):
    """(class, evidence) from the attempt's invocations: class is success, failure or uncertain.

    A queued or running invocation refuses; the latest invocation decides success or failure,
    but any UNCERTAIN or CANCELLING one makes the attempt uncertain.
    """
    if not invs or invs[-1]["status"]["state"] == "NOT_LAUNCHED":
        raise JudgeError("nothing was launched for this attempt")
    for i in invs:
        if i["status"]["state"] in ("QUEUED", "RUNNING"):
            raise JudgeError("invocation %s is %s; judge after it is terminal" % (i["seq"], i["status"]["state"]))
    last = invs[-1]
    s = last["status"]
    if s["state"] == "RESOLVED":
        raise JudgeError("invocation %s was resolved by the orchestrator; see judgment.json" % last["seq"])
    ev = {"invocation": last["seq"], "state": s["state"], "rc": s.get("rc"), "signal": s.get("signal"),
          "verdict": s.get("verdict"), "evidence": s.get("evidence"), "reason": s.get("reason")}
    if any(i["status"]["state"] in ("UNCERTAIN", "CANCELLING") for i in invs):
        return "uncertain", ev
    if s["state"] == "EXITED" and s.get("rc") == 0 and not s.get("signal") and s.get("verdict") in (None, "COMPLETED"):
        return "success", ev
    return "failure", ev


# ---- output predicates -------------------------------------------------------

def _predicate(o, default_min, size, read, run_validator):
    """Problems of one output against min_bytes, sha256 and the validator."""
    problems = []
    mb = o.get("min_bytes", default_min)
    if not (type(mb) is int and mb >= 0):
        return ["invalid min_bytes"]
    if size < mb:
        problems.append("min_bytes: %d bytes, need %d" % (size, mb))
    sha = o.get("sha256")
    if sha is not None and hashlib.sha256(read()).hexdigest() != sha:
        problems.append("sha256 mismatch")
    v = o.get("validate")
    if v is not None and not problems:
        if not (isinstance(v, str) and v.strip()):
            problems.append("invalid validator")
        else:
            rc = run_validator(v)
            if rc != 0:
                problems.append("validator exited %s" % ("(timeout)" if rc is None else rc))
    return problems


def _validate_runner(cwd, file_path):
    def run(cmd):
        r = E.run_tool(["bash", "-c", cmd], E.worker_env(extras={"OVERSEER_OUTPUT": file_path}), VALIDATE_TIMEOUT, cwd=cwd)
        return r["rc"]
    return run


def code_outputs(wt, candidate, unit):
    """Each declared output at the candidate commit: a regular blob, never a symlink or submodule."""
    results = []
    for o in unit["outputs"]:
        o = o if isinstance(o, dict) else {"path": o}
        p = o["path"]
        problems = []
        ls = W.git(wt, "--literal-pathspecs", "ls-tree", "-z", candidate, "--", p, raw=True)["stdout"]
        ent = [t for t in ls.split(b"\0") if t]
        meta = ent[0].split(b"\t", 1)[0].decode().split() if len(ent) == 1 else None
        if meta is None or ent[0].split(b"\t", 1)[1].decode("utf-8", "replace") != p:
            problems.append("missing at candidate")
        elif meta[1] != "blob" or meta[0] not in ("100644", "100755"):
            problems.append("not a regular file (mode %s, %s)" % (meta[0], meta[1]))
        else:
            sha = meta[2]
            size = int(W.git(wt, "cat-file", "-s", sha)["stdout"].strip())
            full = os.path.join(wt, p)
            problems = _predicate(o, 0, size, lambda: W.git(wt, "cat-file", "blob", sha, raw=True)["stdout"],
                                  _validate_runner(wt, full))
        results.append({"path": p, "ok": not problems, "problems": problems})
    return results


def job_outputs(adir, unit):
    """Each declared output under attempts/a<N>/outputs/ (symlinks must stay inside it)."""
    out = os.path.join(adir, "outputs")
    results = []
    for o in unit["outputs"]:
        o = o if isinstance(o, dict) else {"path": o}
        p = o["path"]
        problems = []
        try:
            full = paths.safe_relpath(out, p)
        except paths.UnsafePath as e:
            full = None
            problems.append(str(e))
        if full is not None:
            if not os.path.isfile(full):
                problems.append("missing")
            else:
                def read(f=full):
                    with open(f, "rb") as fh:
                        return fh.read()
                problems = _predicate(o, 1, os.path.getsize(full), read, _validate_runner(out, full))
        results.append({"path": p, "ok": not problems, "problems": problems})
    return results


# ---- judgment ----------------------------------------------------------------

def _record(root, plan_id, unit, n, adir, j):
    j.update({"plan_id": plan_id, "unit_id": unit["id"], "attempt": n, "kind": unit["kind"], "time": store.utc_now()})
    j.setdefault("orchestrator_resolved", False)
    store.write_json_atomic(os.path.join(adir, "judgment.json"), j)
    st = approval.load_unit_state(root, plan_id, unit["id"])
    st["execution"] = {"state": "judged", "attempt": n, "status": j["status"]}
    st["evidence"] = {"judgment": {"attempt": n, "status": j["status"], "candidate": j.get("candidate"),
                                   "time": j["time"], "orchestrator_resolved": j["orchestrator_resolved"]}}
    approval.save_unit_state(root, plan_id, unit["id"], st)
    store.append_event(root, "judged", plan_id=plan_id, unit_id=unit["id"], attempt=n, status=j["status"],
                       candidate=j.get("candidate"), orchestrator_resolved=j["orchestrator_resolved"])
    return j


def _tracked_changes(wt):
    return [p for _, p, _ in W.parse_status(W.git(wt, "status", "--porcelain=v1", "-z", "--untracked-files=no",
                                                  "--ignore-submodules=none", raw=True)["stdout"])]


def _judge_code(root, plan_id, unit, n, adir, cls, ev, timeout):
    j = {"status": "FAILED", "base": None, "candidate": None, "test_rc": None, "outputs": [], "reasons": [],
         "execution": ev, "out_of_scope_dirty": [], "invocation_token": None}
    if cls == "uncertain":  # the author may still be writing: the worktree is left alone
        j["status"] = "UNCERTAIN"
        j["reasons"].append("author execution is uncertain: %s" % (ev.get("reason") or ev.get("state")))
        return j
    if cls == "failure":
        j["reasons"].append("author_failed:state=%s rc=%s signal=%s" % (ev["state"], ev["rc"], ev["signal"]))
        return j
    norm = W.normalize(root, plan_id, unit, adir, n)
    j.update(base=norm["base"], candidate=norm["candidate"], reasons=list(norm["reasons"]),
             out_of_scope_dirty=norm["out_of_scope_dirty"], commits=norm["commits"])
    if not norm["ok"]:
        return j
    wt, cand = W.load_workspace(adir)["worktree"], norm["candidate"]
    r = E.run_tool(["bash", "-c", unit["test_command"]], E.worker_env(), timeout, cwd=wt)
    log = os.path.join(adir, "test.log")
    with open(log, "w", encoding="utf-8") as f:
        f.write(r["stdout"] + ("\n--- stderr ---\n" + r["stderr"] if r["stderr"] else ""))
    j["test_rc"], j["test_log"] = r["rc"], log
    if r["timed_out"]:
        j["reasons"].append("test_timeout:%gs" % timeout)
    elif r["rc"] != 0:
        j["reasons"].append("test_failed:rc=%s" % r["rc"])
    if W.rev(wt, "HEAD") != cand:
        j["reasons"].append("head_moved_by_test")
    changed = _tracked_changes(wt)
    if changed:
        j["reasons"].append("test_modified_tracked:" + ",".join(sorted(changed)))
    if not j["reasons"]:
        j["outputs"] = code_outputs(wt, cand, unit)
        j["reasons"] += ["output_failed:%s: %s" % (o["path"], "; ".join(o["problems"])) for o in j["outputs"] if not o["ok"]]
    if not j["reasons"]:
        j["status"] = "DONE"
    return j


def _judge_job(unit, adir, cls, ev):
    j = {"status": "FAILED", "base": None, "candidate": None, "test_rc": None, "reasons": [], "execution": ev}
    j["outputs"] = job_outputs(adir, unit)
    failed = ["output_failed:%s: %s" % (o["path"], "; ".join(o["problems"])) for o in j["outputs"] if not o["ok"]]
    if cls == "failure":
        j["reasons"] = ["execution_failed:state=%s rc=%s signal=%s verdict=%s" % (ev["state"], ev["rc"], ev["signal"], ev["verdict"])] + failed
    elif cls == "uncertain":
        j["status"] = "UNCERTAIN"
        j["reasons"] = ["execution is uncertain: %s" % (ev.get("reason") or ev.get("state"))]
    elif failed:
        j["reasons"] = failed
    else:
        j["status"] = "DONE"
    return j


def judge_unit(root, plan_id, unit, test_timeout=TEST_TIMEOUT):
    st, n, adir = _unit_attempt(root, plan_id, unit)
    invs = _invocations(root, plan_id, unit, n)
    cls, ev = execution_evidence(invs)
    if unit["kind"] == "code":
        j = _judge_code(root, plan_id, unit, n, adir, cls, ev, test_timeout)
    else:
        j = _judge_job(unit, adir, cls, ev)
    j["invocation_token"] = (X._load(os.path.join(invs[-1]["path"], "intent.json")) or {}).get("token")
    return _record(root, plan_id, unit, n, adir, j)


def valid_judgment(root, plan_id, unit, n=None):
    """The attempt's judgment while it still binds to the evidence, else None.

    Code: the worktree HEAD must equal the sealed candidate. Job: the latest job invocation
    must be the one that was judged.
    """
    if n is None:
        n = approval.load_unit_state(root, plan_id, unit["id"])["counters"]["attempts"]
    adir = paths.attempt_dir(root, plan_id, unit["id"], n)
    j = X._load(os.path.join(adir, "judgment.json"))
    if not isinstance(j, dict) or j.get("status") not in EXIT:
        return None
    if unit["kind"] == "code":
        if not j.get("candidate"):
            return j if j["status"] != "DONE" else None
        try:
            head = W.rev(W.load_workspace(adir)["worktree"], "HEAD")
        except (X.ExecutionError, OSError):
            return None
        return j if head == j["candidate"] else None
    invs = _invocations(root, plan_id, unit, n)
    last = (X._load(os.path.join(invs[-1]["path"], "intent.json")) or {}).get("token") if invs else None
    return j if last and last == j.get("invocation_token") else None


# ---- resolve -----------------------------------------------------------------

def resolve_unit(root, plan_id, unit, as_, reason, evidence):
    if as_ not in ("done", "failed"):
        raise JudgeError("--as must be done or failed")
    for name, v in (("reason", reason), ("evidence", evidence)):
        if not (isinstance(v, str) and v.strip()):
            raise JudgeError("resolve requires a non-blank --%s" % name)
    st, n, adir = _unit_attempt(root, plan_id, unit)
    invs = _invocations(root, plan_id, unit, n)
    for i in invs:
        if i["status"]["state"] in ("QUEUED", "RUNNING"):
            raise JudgeError("invocation %s is %s; only an UNCERTAIN attempt can be resolved" % (i["seq"], i["status"]["state"]))
    targets = [i for i in invs if i["status"]["state"] in ("UNCERTAIN", "CANCELLING")]
    if not targets:
        states = ", ".join("%s=%s" % (i["seq"], i["status"]["state"]) for i in invs) or "no invocations"
        raise JudgeError("attempt a%d of %s is not UNCERTAIN (%s)" % (n, unit["id"], states))
    j = {"status": "DONE" if as_ == "done" else "FAILED", "base": None, "candidate": None, "test_rc": None,
         "outputs": [], "orchestrator_resolved": True, "reason": reason.strip(), "evidence": evidence.strip(),
         "reasons": ["orchestrator resolved as %s: %s" % (as_, reason.strip())], "resolved_at": store.utc_now(),
         "execution": {"state": "UNCERTAIN", "invocations": [i["seq"] for i in targets]},
         "invocation_token": (X._load(os.path.join(invs[-1]["path"], "intent.json")) or {}).get("token")}
    if as_ == "done":
        if unit["kind"] != "job":
            raise JudgeError("code units cannot be resolved as done; resolve the author as failed, or judge a new attempt")
        j["outputs"] = job_outputs(adir, unit)
        bad = ["%s: %s" % (o["path"], "; ".join(o["problems"])) for o in j["outputs"] if not o["ok"]]
        if bad:
            raise JudgeError("refusing --as done; output predicates fail: " + " | ".join(bad))
    # commit order: judgment, unit facets and event first; resolved.json (the slot release) last,
    # so an interrupted resolve is still UNCERTAIN and simply repeats
    _record(root, plan_id, unit, n, adir, j)
    note = {"as": as_, "reason": j["reason"], "evidence": j["evidence"], "time": j["resolved_at"]}
    for i in targets:
        intent = X._load(os.path.join(i["path"], "intent.json"))
        cpath = os.path.join(i["path"], "cancel.json")
        cancel = X._load(cpath)
        if isinstance(cancel, dict) and cancel.get("token") == intent["token"]:
            store.write_json_atomic(cpath, dict(cancel, orchestrator_resolved=note))
        store.write_json_atomic(os.path.join(i["path"], "resolved.json"), dict(note, token=intent["token"]))
    return j


# ---- commands ----------------------------------------------------------------

def _unit_for(root, args):
    pid = args.plan or approval.find_plan_for_unit(root, args.unit)
    approval.require_approval(root, pid)
    return pid, X._unit_of(approval.effective_plan(root, pid), args.unit)


def configure_judge(p):
    p.add_argument("unit", metavar="UNIT")
    p.add_argument("--plan", default=None, metavar="PLAN_ID")
    p.add_argument("--test-timeout", type=float, default=TEST_TIMEOUT, metavar="S")


def run_judge(args):
    root = paths.find_root(args.root)
    if args.test_timeout <= 0:
        raise JudgeError("--test-timeout must be positive")
    with store.ProjectLock(root, "judge"):
        pid, unit = _unit_for(root, args)
        j = judge_unit(root, pid, unit, args.test_timeout)
    print(json.dumps(j, indent=2, sort_keys=True))
    return EXIT[j["status"]]


def configure_resolve(p):
    p.add_argument("unit", metavar="UNIT")
    p.add_argument("--plan", default=None, metavar="PLAN_ID")
    p.add_argument("--as", dest="as_", required=True, choices=("done", "failed"))
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence", required=True)


def run_resolve(args):
    root = paths.find_root(args.root)
    with store.ProjectLock(root, "resolve"):
        pid, unit = _unit_for(root, args)
        j = resolve_unit(root, pid, unit, args.as_, args.reason, args.evidence)
    print(json.dumps({"unit": unit["id"], "attempt": j["attempt"], "status": j["status"],
                      "orchestrator_resolved": True}, sort_keys=True))
    return 0
