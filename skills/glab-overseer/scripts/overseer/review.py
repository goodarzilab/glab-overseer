"""Cross-family review, reproduction, adjudication and non-vacuity checks.

Layout under an attempt: `reviews/r<R>/` holds `input.md`, `round.json` (state, candidate, calls),
`review.json` (the locally validated review), `dispositions.json`, and one `c<K>/` directory per
reviewer call (task file, argv, raw stdout and stderr, parsed result). `evidence/rep-<K>.json`
holds reproduction records; `nonvacuous.json` the non-vacuity record.

A review verdict means the named reviewer did not refute the claims; it does not establish
correctness. Every record binds to the candidate head. The reviewer runs synchronously with no
worker slot, in an empty scratch directory outside the project; the project lock is held only
around state writes (intent before the model call, result after). A round whose call outcome is
unknown is never re-run silently: it needs `--retry`, once.
"""
import contextlib
import json
import os
import re
import shutil
import tempfile

from . import adapters, approval, env as E, execution as X, judge as J, models, paths, store, workspace as W

DEFAULT_TIMEOUT = 900.0
MAX_INPUT = 150000
COUNTER_CLAIM = "This change cannot make an honest run fail."
RULINGS = ("dismissed", "fixed", "nonblocking", "blocked", "inconclusive")
NEEDS_EVIDENCE = ("dismissed", "fixed", "nonblocking")
DEFAULT_TEST_GLOBS = ("tests/**", "test_*.py", "*_test.py")
FINDING_KEYS = ("id", "severity", "claim", "file", "line", "description", "reproduction")
_STR = {"type": "string"}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": list(FINDING_KEYS),
            "properties": {"id": _STR, "severity": {"type": "string", "enum": ["blocking", "nonblocking"]},
                           "claim": _STR, "file": _STR, "line": {"type": ["integer", "null"]},
                           "description": _STR, "reproduction": _STR}}}}}
INSTRUCTIONS = """You are an adversarial code reviewer. Try to refute the claims below and the counter-claim.
Judge only what the diff and the stated test result show; do not assume tests that are not shown.
Report each concrete defect as a finding: `severity` is blocking when the change is wrong or breaks a claim,
nonblocking otherwise; give the file, the line (null if unknown), a description, and a command or steps that
reproduce it (empty string if none). Use verdict FAIL when you report any finding you consider blocking
or cannot rule out; PASS only when you found nothing that refutes the claims. A FAIL needs at least one finding.
Reply with a single JSON object {"verdict": "PASS"|"FAIL", "findings": [...]} and nothing else.

"""


class ReviewError(X.ExecutionError):
    pass


# ---- records -----------------------------------------------------------------

def _obj(path):
    d = X._load(path)
    if d is X.BAD:
        raise ReviewError("%s is unreadable or not a JSON object" % path)
    return d


def _rounds(adir):
    """[(R, directory, round.json)] in order; a round directory without a readable round.json is refused."""
    base = os.path.join(adir, "reviews")
    out = []
    for name in sorted(os.listdir(base) if os.path.isdir(base) else [], key=lambda s: (len(s), s)):
        m = re.match(r"^r([1-9][0-9]*)$", name)
        if m:
            rd = _obj(os.path.join(base, name, "round.json"))
            if rd is None:
                raise ReviewError("review round %s has no round.json" % name)
            out.append((int(m.group(1)), os.path.join(base, name), rd))
    return out


def _current(root, plan_id, unit):
    """(judgment, attempt number, attempt dir, worktree) of a valid DONE code judgment, else refuse."""
    if unit["kind"] != "code":
        raise ReviewError("unit %s is not a code unit" % unit["id"])
    n = approval.load_unit_state(root, plan_id, unit["id"])["counters"]["attempts"]
    if not X._int(n):
        raise ReviewError("unit %s has no attempt" % unit["id"])
    j = J.valid_judgment(root, plan_id, unit, n)
    if not j or j.get("status") != "DONE" or not isinstance(j.get("candidate"), str):
        raise ReviewError("unit %s has no DONE judgment bound to the worktree HEAD; run `judge %s`" % (unit["id"], unit["id"]))
    adir = paths.attempt_dir(root, plan_id, unit["id"], n)
    return j, n, adir, W.load_workspace(adir)["worktree"]


def _cap(plan):
    return plan["caps"]["review_rounds"]


def _latest_complete(adir):
    rounds = _rounds(adir)
    if not rounds or rounds[-1][2].get("state") != "complete":
        raise ReviewError("the latest review round has no verdict")
    R, rdir, rd = rounds[-1]
    rv = _obj(os.path.join(rdir, "review.json"))
    if not isinstance(rv, dict) or not isinstance(rv.get("findings"), list):
        raise ReviewError("round r%d has no usable review.json" % R)
    return R, rdir, rd, rv


# ---- input and validation ----------------------------------------------------

def validate_review(obj):
    """(clean review or None, errors). Types are checked, never coerced."""
    if not isinstance(obj, dict) or set(obj) != {"verdict", "findings"}:
        return None, ["review must be an object with exactly verdict and findings"]
    errs = []
    if obj["verdict"] not in ("PASS", "FAIL"):
        errs.append("verdict must be PASS or FAIL")
    fs = obj["findings"]
    if not isinstance(fs, list):
        return None, errs + ["findings must be a list"]
    seen = set()
    for i, f in enumerate(fs):
        tag = "findings[%d]" % i
        if not isinstance(f, dict) or set(f) != set(FINDING_KEYS):
            errs.append("%s must be an object with exactly %s" % (tag, ", ".join(FINDING_KEYS)))
            continue
        if not (isinstance(f["id"], str) and f["id"].strip()) or f["id"] in seen:
            errs.append("%s id must be a non-blank unique string" % tag)
        seen.add(f["id"] if isinstance(f["id"], str) else None)
        if f["severity"] not in ("blocking", "nonblocking"):
            errs.append("%s severity must be blocking or nonblocking" % tag)
        for k in ("claim", "file", "description", "reproduction"):
            if not isinstance(f[k], str):
                errs.append("%s %s must be a string" % (tag, k))
        if not (f["line"] is None or (type(f["line"]) is int and f["line"] >= 0)):
            errs.append("%s line must be a non-negative integer or null" % tag)
    if obj["verdict"] == "FAIL" and not fs and not errs:
        errs.append("a FAIL verdict needs at least one finding")
    return (None, errs) if errs else (obj, [])


def _diff_base(adir, j):
    """The integration base when U8 recorded one (integration.json: integration_base), else the unit's base."""
    rec = _obj(os.path.join(adir, "integration.json"))
    b = rec.get("integration_base") if rec else None
    return b if isinstance(b, str) and b else j["base"]


def build_input(unit, j, diff, earlier):
    lines = ["# Review input: %s" % unit["title"], "", "## Claims"] + ["- %s" % c for c in unit["claims"]]
    lines += ["- %s" % COUNTER_CLAIM, "", "## Test command", "`%s`" % unit["test_command"],
              "Exit code at the candidate: %s" % j.get("test_rc"), "", "## Earlier findings and dispositions"]
    lines += earlier or ["none"]
    lines += ["", "## Diff", "```diff", diff.rstrip("\n"), "```", ""]
    return "\n".join(lines)


def _earlier(rounds):
    out = []
    for R, rdir, rd in rounds:
        rv = _obj(os.path.join(rdir, "review.json")) if rd.get("state") == "complete" else None
        disp = (_obj(os.path.join(rdir, "dispositions.json")) or {}).get("dispositions", {})
        for f in (rv or {}).get("findings", []):
            d = disp.get(f["id"])
            out.append("- r%d %s [%s] %s:%s %s | disposition: %s" % (
                R, f["id"], f["severity"], f["file"], f["line"], f["claim"],
                "%s (%s)" % (d["ruling"], d.get("note", "")) if d else "none"))
    return out


def author_sessions(root, plan_id, unit_id, n, family):
    """Session ids found in the stdout of the attempt's author and fixer invocations."""
    out = set()
    for i in X.iter_invocations(root, plan_id, unit_id):
        if i["attempt"] == n and i["kind"] in ("author", "fixer"):
            try:
                with open(os.path.join(i["path"], "stdout.log"), encoding="utf-8", errors="replace") as f:
                    sid = adapters.parse_result(family, 0, f.read(), "")["session_id"]
            except OSError:
                continue
            if sid:
                out.add(sid)
    return out


# ---- review ------------------------------------------------------------------

def _update_round(root, rdir, fn):
    with store.ProjectLock(root, "review"):
        path = os.path.join(rdir, "round.json")
        rd = _obj(path)
        fn(rd)
        store.write_json_atomic(path, rd)
        return rd


def _call(root, rdir, rd, purpose, family, model, task, survey, timeout, author_sids):
    """One reviewer call: intent recorded before it runs, result after. Returns the call record."""
    seq = len(rd["calls"]) + 1
    cdir = os.path.join(rdir, "c%d" % seq)
    os.mkdir(cdir)
    call = {"seq": seq, "purpose": purpose, "family": family, "model": model, "dir": "c%d" % seq,
            "state": "started", "classification": None, "session_id": None, "reason": None}
    scratch = tempfile.mkdtemp(prefix="ovr-review-")
    try:
        try:
            inv = adapters.prepare(family, "reviewer", model, task, scratch, task_dir=cdir, survey=survey, schema=REVIEW_SCHEMA)
        except ValueError as e:
            call.update(state="failed", classification="CONFIG", reason="prepare failed: %s" % e)
            _update_round(root, rdir, lambda r: r["calls"].append(call))
            return call
        store.write_json_atomic(os.path.join(cdir, "argv.json"), {"argv": inv["argv"], "transport": inv["transport"],
                                                                 "schema_flag": inv["schema_flag"], "cwd": scratch})
        call["argv"] = inv["argv"]
        _update_round(root, rdir, lambda r: r["calls"].append(dict(call)))
        r = E.run_tool(inv["argv"], E.worker_env(), timeout, cwd=scratch, stdin=inv["stdin"])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    for name in ("stdout", "stderr"):
        with open(os.path.join(cdir, name + ".txt"), "w", encoding="utf-8") as f:
            f.write(r[name])
    res = adapters.parse_result(family, r["rc"], r["stdout"], r["stderr"])
    call.update(rc=r["rc"], timed_out=r["timed_out"], session_id=res["session_id"], classification=res["classification"],
                reason=res["reason"], schema_flag=inv["schema_flag"])
    review = None
    if res["ok"]:
        errs = []
        for cand in (res["structured"], adapters.extract_json(res["text"])):
            review, errs = validate_review(cand)
            if review:
                break
        if review is None:
            call.update(state="failed", classification="MALFORMED", reason="; ".join(errs[:5]))
        elif not res["session_id"]:
            review = None
            call.update(state="failed", classification="NO_SESSION", reason="the reviewer reported no session id")
        elif res["session_id"] in author_sids:
            review = None
            call.update(state="failed", classification="SESSION_REUSE", reason="reviewer session id equals the author's")
        else:
            call.update(state="ok", classification="OK")
    else:
        call["state"] = "failed"
    store.write_json_atomic(os.path.join(cdir, "parsed.json"), {"call": call, "result": res, "review": review})

    def fin(rd2):
        rd2["calls"][-1] = call
    _update_round(root, rdir, fin)
    return dict(call, review=review)


def run_review(root, plan_id, unit, retry=False, timeout=DEFAULT_TIMEOUT, max_chars=MAX_INPUT):
    with store.ProjectLock(root, "review"):
        plan = approval.effective_plan(root, plan_id)
        j, n, adir, wt = _current(root, plan_id, unit)
        cand = j["candidate"]
        cache = models.load_valid(root)
        if cache is None:
            raise ReviewError("models.json is missing or malformed; run `overseer.py models --host claude|google`")
        host = cache["host"]
        other = [f for f in models.FAMILIES if f != host][0]
        roles = models.roles_for(cache, other)
        if roles is None:
            raise ReviewError("the reviewer family %s is unavailable in models.json (%s)" % (other, cache["choices"][other].get("reason")))
        rounds = _rounds(adir)
        last = rounds[-1] if rounds else None
        reuse = None
        open_round = bool(last and last[2].get("state") in ("started", "failed") and last[2].get("candidate") == cand
                          and not last[2].get("retried"))
        if retry:
            if not open_round:
                raise ReviewError("--retry needs a latest round for this candidate that failed or has an unknown outcome "
                                  "and has not been retried")
            reuse = last
        elif open_round:
            raise ReviewError("round r%d has no verdict; rerun with --retry (once), after which the round is consumed" % last[0])
        if reuse:
            R, rdir, rd = reuse
            with open(os.path.join(rdir, "input.md"), encoding="utf-8") as f:
                text = f.read()
        else:
            R = (last[0] if last else 0) + 1
            if R > _cap(plan):
                raise ReviewError("review rounds are capped at %d; the orchestrator rules alone with "
                                  "`dispositions %s --orchestrator-decision TEXT`" % (_cap(plan), unit["id"]))
            diff = W.git(wt, "diff", "--no-ext-diff", "--no-textconv", "--no-color", _diff_base(adir, j), cand)["stdout"]
            text = build_input(unit, j, diff, _earlier(rounds))
            if len(text) > max_chars:
                raise ReviewError("review input is %d characters, above the limit %d; it is not truncated. Split the unit "
                                  "(`plan split %s --into SPEC.json --reason TEXT`)" % (len(text), max_chars, unit["id"]))
            rdir = os.path.join(adir, "reviews", "r%d" % R)
            os.makedirs(rdir)
            with open(os.path.join(rdir, "input.md"), "w", encoding="utf-8") as f:
                f.write(text)
            rd = {"round": R, "candidate": cand, "base": j["base"], "host": host, "reviewer_family": other,
                  "model": roles["review"], "time": store.utc_now(), "state": "started", "retried": False,
                  "same_family_fallback": False, "calls": [], "verdict": None}
            store.write_json_atomic(os.path.join(rdir, "round.json"), rd)
            st = approval.load_unit_state(root, plan_id, unit["id"])
            st["counters"]["review_rounds"] = R
            approval.save_unit_state(root, plan_id, unit["id"], st)
            store.append_event(root, "review_round_started", plan_id=plan_id, unit_id=unit["id"], round=R, candidate=cand)
        if reuse:
            rd.update(retried=True, state="started")
            store.write_json_atomic(os.path.join(rdir, "round.json"), rd)
        sids = author_sessions(root, plan_id, unit["id"], n, host)
        survey = W._survey(root)
    task = INSTRUCTIONS + text
    plans = [("retry" if reuse else "primary", other, roles["review"])]
    while plans:
        purpose, fam, model = plans.pop(0)
        c = _call(root, rdir, rd, purpose, fam, model, task, survey, timeout, sids)
        rd = _obj(os.path.join(rdir, "round.json"))
        if c["state"] == "ok":
            store.write_json_atomic(os.path.join(rdir, "review.json"), c["review"])
            rd = _update_round(root, rdir, lambda r: r.update(state="complete", verdict=c["review"]["verdict"]))
            break
        if c["classification"] in ("QUOTA", "AUTH") and fam != host and not rd["same_family_fallback"]:
            hroles = models.roles_for(cache, host)
            if hroles:
                why = "%s from %s %s" % (c["classification"], fam, model)
                rd = _update_round(root, rdir, lambda r: r.update(same_family_fallback=True, fallback_reason=why))
                plans.append(("fallback", host, hroles["review"]))
    else:
        rd = _update_round(root, rdir, lambda r: r.update(state="failed"))
    with store.ProjectLock(root, "review"):
        store.append_event(root, "review_round_finished", plan_id=plan_id, unit_id=unit["id"], round=R,
                           state=rd["state"], verdict=rd["verdict"], candidate=cand,
                           same_family_fallback=rd["same_family_fallback"])
    last_call = rd["calls"][-1]
    return {"round": R, "state": rd["state"], "verdict": rd["verdict"], "candidate": cand,
            "reviewer_family": last_call["family"], "model": last_call["model"],
            "session_id": last_call["session_id"], "same_family_fallback": rd["same_family_fallback"],
            "findings": len(_obj(os.path.join(rdir, "review.json"))["findings"]) if rd["state"] == "complete" else None,
            "reason": None if rd["state"] == "complete" else last_call.get("reason") or last_call.get("classification")}


# ---- dispositions and eligibility --------------------------------------------

def _evidence_ok(adir, ref):
    try:
        return os.path.isfile(paths.safe_relpath(adir, ref))
    except paths.UnsafePath:
        return False


def record_dispositions(root, plan_id, unit, data):
    """Validate and store rulings for every finding of the latest round, bound to the candidate head."""
    j, n, adir, wt = _current(root, plan_id, unit)
    R, rdir, rd, rv = _latest_complete(adir)
    if rd["candidate"] != j["candidate"]:
        raise ReviewError("round r%d reviewed %s but the candidate head is now %s; review the new candidate"
                          % (R, rd["candidate"][:12], j["candidate"][:12]))
    if not isinstance(data, dict):
        raise ReviewError("dispositions must be a JSON object keyed by finding id")
    sev = dict((f["id"], f["severity"]) for f in rv["findings"])
    errs = ["finding %s has no ruling" % i for i in sorted(sev) if i not in data]
    errs += ["unknown finding %r" % k for k in sorted(data) if k not in sev]
    clean = {}
    for fid, d in sorted(data.items()):
        if fid not in sev:
            continue
        if not (isinstance(d, dict) and set(d) <= {"ruling", "evidence", "note"} and d.get("ruling") in RULINGS):
            errs.append("%s: needs ruling (%s) and optional evidence list and note" % (fid, ", ".join(RULINGS)))
            continue
        ev, note = d.get("evidence", []), d.get("note", "")
        if not (isinstance(ev, list) and all(isinstance(e, str) for e in ev) and isinstance(note, str)):
            errs.append("%s: evidence must be a list of strings and note a string" % fid)
            continue
        missing = [e for e in ev if not _evidence_ok(adir, e)]
        if missing:
            errs.append("%s: evidence file not found under the attempt root: %s" % (fid, ", ".join(missing)))
        if sev[fid] == "blocking" and d["ruling"] in NEEDS_EVIDENCE and not ev:
            errs.append("%s: a blocking finding ruled %s needs at least one evidence reference" % (fid, d["ruling"]))
        clean[fid] = {"ruling": d["ruling"], "evidence": ev, "note": note}
    if errs:
        raise ReviewError("dispositions refused: " + "; ".join(errs))
    store.write_json_atomic(os.path.join(rdir, "dispositions.json"),
                            {"round": R, "candidate": j["candidate"], "time": store.utc_now(), "dispositions": clean})
    store.append_event(root, "dispositions_recorded", plan_id=plan_id, unit_id=unit["id"], round=R, candidate=j["candidate"])
    return review_state(root, plan_id, unit)


def record_decision(root, plan_id, unit, text):
    if not (isinstance(text, str) and text.strip()):
        raise ReviewError("--orchestrator-decision needs non-blank text")
    plan = approval.effective_plan(root, plan_id)
    j, n, adir, wt = _current(root, plan_id, unit)
    done = len(_rounds(adir))
    if done < _cap(plan):
        raise ReviewError("%d of %d review rounds used; an orchestrator decision is accepted only at the cap" % (done, _cap(plan)))
    store.write_json_atomic(os.path.join(adir, "reviews", "orchestrator_decision.json"),
                            {"orchestrator_decision": True, "text": text.strip(), "candidate": j["candidate"],
                             "rounds": done, "time": store.utc_now()})
    store.append_event(root, "orchestrator_decision", plan_id=plan_id, unit_id=unit["id"], candidate=j["candidate"])
    return review_state(root, plan_id, unit)


def review_state(root, plan_id, unit):
    """{"state": ELIGIBLE|NOT_ELIGIBLE, "reasons": [...], "orchestrator_decision": bool}."""
    def no(*reasons):
        return {"state": "NOT_ELIGIBLE", "reasons": list(reasons), "orchestrator_decision": False}
    plan = approval.effective_plan(root, plan_id)
    try:
        j, n, adir, wt = _current(root, plan_id, unit)
    except ReviewError as e:
        return no(str(e))
    cand = j["candidate"]
    rounds = _rounds(adir)
    dec = _obj(os.path.join(adir, "reviews", "orchestrator_decision.json"))
    if dec and dec.get("candidate") == cand and len(rounds) >= _cap(plan):
        return {"state": "ELIGIBLE", "reasons": ["orchestrator decision recorded at the round cap"], "orchestrator_decision": True}
    if not rounds:
        return no("no review round")
    R, rdir, rd = rounds[-1]
    if rd.get("state") != "complete":
        return no("round r%d has no verdict (%s)" % (R, rd.get("state")))
    if rd.get("candidate") != cand:
        return no("round r%d reviewed %s, not the current candidate" % (R, str(rd.get("candidate"))[:12]))
    findings = _obj(os.path.join(rdir, "review.json"))["findings"]
    blocking = [f for f in findings if f["severity"] == "blocking"]
    if rd["verdict"] == "PASS" and not blocking:
        return {"state": "ELIGIBLE", "reasons": [], "orchestrator_decision": False}
    disp = _obj(os.path.join(rdir, "dispositions.json"))
    if not disp or disp.get("candidate") != cand:
        return no("round r%d has findings without dispositions bound to the current candidate" % R)
    reasons = []
    for f in findings:
        d = disp["dispositions"].get(f["id"])
        if d is None:
            reasons.append("finding %s has no disposition" % f["id"])
        elif f["severity"] == "blocking":
            if d["ruling"] in ("inconclusive", "blocked"):
                reasons.append("blocking finding %s is %s" % (f["id"], d["ruling"]))
            elif d["ruling"] == "fixed":
                reasons.append("blocking finding %s is ruled fixed; the fix must be reviewed in a later round" % f["id"])
            elif not d["evidence"] or not all(_evidence_ok(adir, e) for e in d["evidence"]):
                reasons.append("blocking finding %s lacks existing evidence" % f["id"])
    return {"state": "NOT_ELIGIBLE" if reasons else "ELIGIBLE", "reasons": reasons, "orchestrator_decision": False}


# ---- disposable worktrees: reproduce and nonvacuous --------------------------

@contextlib.contextmanager
def disposable(wt, commit):
    """A detached worktree at `commit`, always removed. The judged worktree is not touched."""
    parent = tempfile.mkdtemp(prefix="ovr-wt-")
    d = os.path.join(parent, "w")
    try:
        W.git(wt, "worktree", "add", "--detach", d, commit)
        yield d
    finally:
        try:
            W.git(wt, "worktree", "remove", "--force", d)
        except W.GitError:
            with contextlib.suppress(W.GitError):
                W.git(wt, "worktree", "prune")
        shutil.rmtree(parent, ignore_errors=True)


def reproduce(root, plan_id, unit, finding, command, timeout=600.0):
    if not (isinstance(command, str) and command.strip()):
        raise ReviewError("--command must not be blank")
    j, n, adir, wt = _current(root, plan_id, unit)
    R, rdir, rd, rv = _latest_complete(adir)
    if finding not in [f["id"] for f in rv["findings"]]:
        raise ReviewError("finding %r is not in round r%d" % (finding, R))
    with disposable(wt, j["candidate"]) as d:
        r = E.run_tool(["bash", "-c", command], E.worker_env(), timeout, cwd=d)
    edir = os.path.join(adir, "evidence")
    os.makedirs(edir, exist_ok=True)
    k = 1 + len([x for x in os.listdir(edir) if re.match(r"^rep-[0-9]+\.json$", x)])
    rel = "evidence/rep-%d.json" % k
    store.write_json_atomic(os.path.join(adir, rel), {
        "kind": "reproduce", "finding": finding, "round": R, "command": command, "candidate": j["candidate"],
        "rc": r["rc"], "timed_out": r["timed_out"], "stdout": r["stdout"], "stderr": r["stderr"], "time": store.utc_now()})
    store.append_event(root, "reproduced", plan_id=plan_id, unit_id=unit["id"], finding=finding, rc=r["rc"], evidence=rel)
    return {"evidence": rel, "rc": r["rc"], "timed_out": r["timed_out"]}


def is_test_file(path, globs):
    for g in globs:
        if (paths.glob_match(g, path) if "/" in g else paths.glob_match(g, path.split("/")[-1])):
            return True
    return False


def nonvacuous(root, plan_id, unit, timeout=J.TEST_TIMEOUT):
    """PASS when the tests fail once every non-test change is reverted; FAIL when they still pass."""
    plan = approval.effective_plan(root, plan_id)
    j, n, adir, wt = _current(root, plan_id, unit)
    cand, base = j["candidate"], j["base"]
    globs = plan.get("test_globs") or list(DEFAULT_TEST_GLOBS)
    toks = W.zsplit(W.git(wt, "diff", "--name-status", "-z", "--no-renames", base, cand, raw=True)["stdout"])
    changed = [(toks[i], toks[i + 1]) for i in range(0, len(toks) - 1, 2)]
    tests = [p for _, p in changed if is_test_file(p, globs)]
    others = [(s, p) for s, p in changed if not is_test_file(p, globs)]
    rec = {"candidate": cand, "base": base, "test_files": tests, "restored": [p for _, p in others], "test_rc": None,
           "time": store.utc_now()}
    if not tests or not others:
        rec.update(status="NOT_APPLICABLE", reason="no test file changed" if not tests else "no non-test file changed")
    else:
        with disposable(wt, cand) as d:
            todo = []
            for s, p in others:
                if s == "A":
                    os.unlink(os.path.join(d, p))
                else:
                    todo.append(p)
            for i in range(0, len(todo), 100):
                W.git(d, "--literal-pathspecs", "checkout", base, "--", *todo[i:i + 100])
            r = E.run_tool(["bash", "-c", unit["test_command"]], E.worker_env(), timeout, cwd=d)
        if r["timed_out"] or r["rc"] == 127:
            raise ReviewError("test command %s; no non-vacuity record written" % ("timed out" if r["timed_out"] else "was not found"))
        rec.update(status="PASS" if r["rc"] != 0 else "FAIL", test_rc=r["rc"], stdout_tail=r["stdout"][-2000:],
                   stderr_tail=r["stderr"][-2000:])
    store.write_json_atomic(os.path.join(adir, "nonvacuous.json"), rec)
    store.append_event(root, "nonvacuous", plan_id=plan_id, unit_id=unit["id"], status=rec["status"], candidate=cand)
    return rec


def patch_id(repo, a, b):
    """`git patch-id --stable` of the diff a..b (None for an empty diff). No shell pipe."""
    diff = W.git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color", a, b, raw=True)["stdout"]
    out = W.git(repo, "patch-id", "--stable", stdin=diff)["stdout"].split()
    return out[0] if out else None


# ---- commands ----------------------------------------------------------------

def _unit(root, args):
    pid = args.plan or approval.find_plan_for_unit(root, args.unit)
    approval.require_approval(root, pid)
    return pid, X._unit_of(approval.effective_plan(root, pid), args.unit)


def _show(d):
    print(json.dumps(d, indent=2, sort_keys=True))


def _plan_arg(p):
    p.add_argument("unit", metavar="UNIT")
    p.add_argument("--plan", default=None, metavar="PLAN_ID")


def configure_review(p):
    _plan_arg(p)
    p.add_argument("--retry", action="store_true", help="retry a round whose reviewer call failed (once)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="S")
    p.add_argument("--max-input-chars", type=int, default=MAX_INPUT, metavar="N")


def run_review_cmd(args):
    root = paths.find_root(args.root)
    if args.timeout <= 0 or args.max_input_chars <= 0:
        raise ReviewError("--timeout and --max-input-chars must be positive")
    pid, unit = _unit(root, args)
    res = run_review(root, pid, unit, args.retry, args.timeout, args.max_input_chars)
    _show(res)
    return 0 if res["state"] == "complete" else 1


def configure_reproduce(p):
    _plan_arg(p)
    p.add_argument("--finding", required=True, metavar="ID")
    p.add_argument("--command", required=True, dest="repro_command", metavar="CMD")
    p.add_argument("--timeout", type=float, default=600.0, metavar="S")


def run_reproduce(args):
    root = paths.find_root(args.root)
    with store.ProjectLock(root, "reproduce"):
        pid, unit = _unit(root, args)
        _show(reproduce(root, pid, unit, args.finding, args.repro_command, args.timeout))
    return 0


def configure_dispositions(p):
    _plan_arg(p)
    p.add_argument("file", nargs="?", default=None, metavar="FILE")
    p.add_argument("--orchestrator-decision", default=None, metavar="TEXT")
    p.add_argument("--state", action="store_true", help="print review eligibility")


def run_dispositions(args):
    root = paths.find_root(args.root)
    if sum(1 for x in (args.file, args.orchestrator_decision, args.state or None) if x is not None) != 1:
        raise ReviewError("give exactly one of FILE, --orchestrator-decision TEXT, --state")
    with store.ProjectLock(root, "dispositions"):
        pid, unit = _unit(root, args)
        if args.file is not None:
            try:
                data = store.read_json(args.file)
            except ValueError as e:
                raise ReviewError("%s is not valid JSON: %s" % (args.file, e))
            res = record_dispositions(root, pid, unit, data)
        elif args.orchestrator_decision is not None:
            res = record_decision(root, pid, unit, args.orchestrator_decision)
        else:
            res = review_state(root, pid, unit)
    _show(res)
    return 0 if res["state"] == "ELIGIBLE" else 1


def configure_fix(p):
    _plan_arg(p)
    p.add_argument("--prompt-file", required=True, metavar="F")


def run_fix(args):
    root = paths.find_root(args.root)
    try:
        with open(args.prompt_file, "rb") as f:
            raw = f.read(W.MAX_FIX_PROMPT + 1)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ReviewError("cannot read --prompt-file: %s" % e)
    if len(raw) > W.MAX_FIX_PROMPT or not text.strip():
        raise ReviewError("--prompt-file must be non-blank and at most %d bytes" % W.MAX_FIX_PROMPT)
    with store.ProjectLock(root, "fix"):
        pid, unit = _unit(root, args)
        if unit["kind"] != "code":
            raise ReviewError("unit %s is not a code unit" % unit["id"])
        rec = W.dispatch_fixer(root, pid, unit, text)
    print(json.dumps({"plan": pid, "unit": unit["id"], "attempt": rec["attempt"], "invocation": rec["seq"]}, sort_keys=True))
    return 0


def configure_nonvacuous(p):
    _plan_arg(p)
    p.add_argument("--timeout", type=float, default=J.TEST_TIMEOUT, metavar="S")


def run_nonvacuous(args):
    root = paths.find_root(args.root)
    with store.ProjectLock(root, "nonvacuous"):
        pid, unit = _unit(root, args)
        rec = nonvacuous(root, pid, unit, args.timeout)
    _show(rec)
    return 0
