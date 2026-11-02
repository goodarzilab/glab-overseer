#!/usr/bin/env python3
"""Evaluate the acceptance claims of one run from `.overseer/` state alone (no network).

    check_run.py PROJECT_ROOT PLAN_ID --expect FILE [--json]

Prints `PASS|FAIL|INCOMPLETE <claim id> <evidence path>` per claim, or a JSON summary. Exit 1 on any FAIL,
3 on INCOMPLETE without FAIL, else 0. A missing record is INCOMPLETE; a contradicting one is FAIL.

Expectation keys (all optional): run; backend ("slurm"|"local"); code_units, job_units; overlap;
integration_units; job_sources (squeue,
scontrol, sacct, wrapper); sacct ("answered"|"unavailable"|"any"); uncertain_units; recovery {unit,
merge_log}; shim_log (file under the project root of logged Slurm calls); author_family and
reviewer_family ("claude"|"google"): the model families the run must use, defaulting by `run` to
author claude and reviewer google for run1 and run2, author google and reviewer claude for run3;
any other family in a review round is FAIL.

Claims: `overlap` is emitted only when the plan has two or more code units (a job-only plan has nothing
to overlap, so no claim is made and the exit code is unaffected). A record that is missing is
INCOMPLETE; so is one whose JSON has the wrong type, with the reason in the detail; check_run never
raises on a malformed record.
"""
import argparse
import calendar
import json
import os
import re
import sys
import time

PASS, FAIL, INCOMPLETE = "PASS", "FAIL", "INCOMPLETE"
FAMILY = {"claude": "claude", "agy": "google"}
RUN_FAMILIES = {"run1": ("claude", "google"), "run2": ("claude", "google"), "run3": ("google", "claude")}
SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def ts(s):
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def sub(d, key):
    """d[key] when it is a dict, else {}."""
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, dict) else {}


def sha(v):
    return bool(SHA_RE.match(str(v)))


class Run(object):
    def __init__(self, root, plan_id, expect):
        self.root, self.plan_id, self.expect = os.path.abspath(root), plan_id, expect
        self.state = os.path.join(self.root, ".overseer")
        self.pdir = os.path.join(self.state, "plans", plan_id)
        self.claims, self.orchestrator = [], []

    def add(self, cid, status, path, detail=""):
        self.claims.append({"id": cid, "status": status, "evidence": os.path.relpath(path, self.root), "detail": detail})

    def load(self, *p):
        """JSON object, None when absent, False when unreadable."""
        try:
            with open(os.path.join(*p), encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return False
        return d if isinstance(d, dict) else False

    def need(self, cid, *p, what="record"):
        """The record, or None after adding INCOMPLETE (absent) or FAIL (unreadable)."""
        d = self.load(*p)
        if not d:
            self.add(cid, INCOMPLETE if d is None else FAIL, os.path.join(*p), "%s %s" % (what, "missing" if d is None else "unreadable"))
            return None
        return d

    def udir(self, uid):
        return os.path.join(self.pdir, "units", uid)

    def adir(self, uid):
        """Directory of the unit's latest attempt (the unit directory when there is none)."""
        base = os.path.join(self.udir(uid), "attempts")
        try:
            ns = sorted(int(x[1:]) for x in os.listdir(base) if re.match(r"^a[1-9][0-9]*$", x))
        except OSError:
            ns = []
        return os.path.join(base, "a%d" % ns[-1]) if ns else self.udir(uid)

    def attempts(self, uid):
        """Directories of every attempt of the unit, oldest first."""
        base = os.path.join(self.udir(uid), "attempts")
        try:
            ns = sorted(int(x[1:]) for x in os.listdir(base) if re.match(r"^a[1-9][0-9]*$", x))
        except OSError:
            ns = []
        return [os.path.join(base, "a%d" % n) for n in ns]

    def invs(self, adir, kinds=("author", "fixer", "job")):
        base = os.path.join(adir, "inv")
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return []
        return [os.path.join(base, n) for n in names if re.match(r"^[0-9]+-(%s)$" % "|".join(kinds), n)]

    def events(self):
        out = []
        try:
            with open(os.path.join(self.state, "events.jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
        return [e for e in out if isinstance(e, dict)]

    def unit_ids(self, key, kind):
        v = self.expect.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, str) and ID_RE.match(x)]
        plan = self.load(self.pdir, "plan.json") or {}
        return [u["id"] for u in plan.get("units") or [] if isinstance(u, dict) and u.get("kind") == kind and ID_RE.match(str(u.get("id")))]

    def local_file(self, key, rel):
        ok = isinstance(rel, str) and rel and not os.path.isabs(rel) and ".." not in rel.split("/")
        return os.path.join(self.root, rel) if ok else None

    def lines(self, path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return [x for x in f.read().splitlines() if x.strip()]
        except (OSError, TypeError):
            return None


def author_family(run, adir):
    for inv in run.invs(adir, ("author",)):
        argv = (run.load(inv, "intent.json") or {}).get("argv")
        if isinstance(argv, list) and argv and isinstance(argv[0], str):
            return FAMILY.get(os.path.basename(argv[0]))
    return None


def check_overlap(run, code):
    ids = run.expect["overlap"] if isinstance(run.expect.get("overlap"), list) else code
    ids = [u for u in ids if isinstance(u, str) and ID_RE.match(u)]
    if len(ids) < 2:
        return  # nothing to overlap: no claim (documented in the module docstring)
    wins, missing, evidence = {}, [], os.path.join(run.adir(ids[0]), "inv")
    for u in ids:
        wins[u] = []
        got = False
        for adir in run.attempts(u):
            for inv in run.invs(adir, ("author",)):
                got = True
                s, e = ts((run.load(inv, "start.json") or {}).get("time")), ts((run.load(inv, "exit.json") or {}).get("end_time"))
                if s is None or e is None:
                    missing.append("%s: %s start.json or exit.json missing or untimed" % (u, os.path.relpath(inv, run.root)))
                else:
                    wins[u].append((s, e))
        if not got:
            missing.append("%s: no author invocation" % u)
    pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]
             if any(x[0] < y[1] and y[0] < x[1] for x in wins[a] for y in wins[b])]
    span = ", ".join("%s %s" % (u, ["%d..%d" % w for w in wins[u]]) for u in ids)
    if pairs:
        run.add("overlap", PASS, evidence, "overlapping %s; %s" % (pairs, span))
    elif missing:
        run.add("overlap", INCOMPLETE, evidence, "; ".join(missing))
    else:
        run.add("overlap", FAIL, evidence, "no two author runs overlapped; " + span)


def check_judgment(run, uid, kind):
    cid, path = "%s.judgment" % uid, os.path.join(run.adir(uid), "judgment.json")
    j = run.need(cid, path, what="judgment")
    if not j:
        return None
    outs = j.get("outputs")
    if outs is not None and not isinstance(outs, list):
        run.add(cid, INCOMPLETE, path, "malformed record: outputs is not a list")
        return j
    outs = outs or []
    if not all(isinstance(o, dict) for o in outs):
        run.add(cid, INCOMPLETE, path, "malformed record: an outputs entry is not an object")
        return j
    bad = [o.get("path") for o in outs if o.get("ok") is not True]
    if j.get("status") != "DONE":
        run.add(cid, FAIL if j.get("status") == "FAILED" else INCOMPLETE, path, "status %s" % j.get("status"))
    elif kind == "code" and j.get("orchestrator_resolved") is True:
        run.add(cid, FAIL, path, "a code unit is DONE only by orchestrator resolution")
    elif bad or not outs:
        run.add(cid, FAIL, path, "outputs failing or not recorded: %s" % (bad or "none"))
    else:
        run.add(cid, PASS, path, "DONE, %d outputs ok" % len(outs))
    return j


def check_review(run, uid):
    cid, base_ev = "%s.review" % uid, os.path.join(run.adir(uid), "reviews")
    want_a, want_r = run.expect.get("author_family"), run.expect.get("reviewer_family")
    if not (want_a and want_r) and run.expect.get("run") in RUN_FAMILIES:
        da, dr = RUN_FAMILIES[run.expect["run"]]
        want_a, want_r = want_a or da, want_r or dr
    rounds, fams = [], [author_family(run, a) for a in run.attempts(uid)]
    unit_fam = next((f for f in fams if f), None)
    for adir, afam in zip(run.attempts(uid), fams):
        base = os.path.join(adir, "reviews")
        try:
            names = sorted((x for x in os.listdir(base) if re.match(r"^r[1-9][0-9]*$", x)), key=lambda s: (len(s), s))
        except OSError:
            names = []
        for n in names:
            rd = run.load(base, n, "round.json")
            if rd and rd.get("state") == "complete":
                calls = rd.get("calls") if isinstance(rd.get("calls"), list) else []
                ok = [c for c in calls if isinstance(c, dict) and c.get("state") == "ok"]
                rounds.append({"name": "%s/%s" % (os.path.basename(adir), n), "family": (ok[-1] if ok else {}).get("family") or rd.get("reviewer_family"),
                               "fallback": rd.get("same_family_fallback") is True, "author": afam or unit_fam,
                               "path": os.path.join(base, n, "round.json")})
    if not rounds or unit_fam is None:
        return run.add(cid, INCOMPLETE, base_ev, "no completed review round" if not rounds else "author family not in inv/*/intent.json")
    for r in rounds:
        if r["fallback"] or (r["family"] and r["family"] == r["author"]):
            return run.add(cid, FAIL, r["path"], "round %s is a same-family fallback (reviewer %s, author %s)" % (r["name"], r["family"], r["author"]))
    wrong = [f for f in fams if f and want_a and f != want_a]
    if wrong:
        return run.add(cid, FAIL, base_ev, "author family %s, run requires %s" % (wrong[0], want_a))
    for r in rounds:
        if want_r and r["family"] and r["family"] != want_r:
            return run.add(cid, FAIL, r["path"], "round %s reviewer family %s, run requires %s" % (r["name"], r["family"], want_r))
    good = [r for r in rounds if r["family"] and r["family"] != r["author"]]
    if good:
        run.add(cid, PASS, good[-1]["path"], "%d round(s), none a fallback; %s by %s reviewed a %s author" % (
            len(rounds), good[-1]["name"], good[-1]["family"], good[-1]["author"]))
    else:
        run.add(cid, INCOMPLETE, rounds[-1]["path"], "no review round records a reviewer family")


def check_nonvacuity(run, uid):
    cid, adir = "%s.nonvacuity" % uid, run.adir(uid)
    nv = run.need(cid, adir, "nonvacuous.json", what="non-vacuity record")
    if not nv:
        return
    status = nv.get("status")
    dpath = os.path.join(adir, "reviews", "orchestrator_decision.json")
    if status in ("PASS", "NOT_APPLICABLE"):
        run.add(cid, PASS, os.path.join(adir, "nonvacuous.json"), "status %s" % status)
    elif (run.load(dpath) or {}).get("orchestrator_decision") is True:
        run.orchestrator.append({"unit": uid, "item": "non-vacuity %s overruled by orchestrator decision" % status})
        run.add(cid, PASS, dpath, "status %s overruled by an orchestrator decision" % status)
    else:
        run.add(cid, FAIL, os.path.join(adir, "nonvacuous.json"), "status %s" % status)


def check_merge(run, uid):
    path = os.path.join(run.udir(uid), "merge.json")
    m = run.need("%s.merge" % uid, path, what="merge record")
    if not m:
        return None
    cid, upath = "%s.merge" % uid, os.path.join(run.udir(uid), "unit.json")
    unit = run.load(upath)
    own = m.get("state") == "merged" and sha(m.get("pr_head")) and sha(m.get("merge_commit"))
    desc = "state %s, pr_head %s, merge_commit %s" % (m.get("state"), str(m.get("pr_head"))[:12], str(m.get("merge_commit"))[:12])
    if not unit:
        run.add(cid, INCOMPLETE if own else FAIL, upath if own else path,
                "unit.json %s: closure not shown; %s" % ("missing" if unit is None else "unreadable", desc))
        return m
    closed = sub(unit, "closure").get("state") == "merged"
    run.add(cid, PASS if own and closed else FAIL, path, "%s, closure %s" % (desc, "merged" if closed else "not merged"))
    return m


def check_tracker(run, uid):
    path = os.path.join(run.udir(uid), "unit.json")
    st = run.need("%s.tracker" % uid, path, what="unit state")
    if st:
        t = sub(st, "tracker")
        run.add("%s.tracker" % uid, PASS if t.get("state") == "closed" and t.get("issue") else
                (FAIL if t.get("state") == "pending" else INCOMPLETE), path, "tracker %s, issue %s" % (t.get("state"), t.get("issue")))


def check_integration(run, code, merges):
    ids = run.expect.get("integration_units")
    if not isinstance(ids, list):
        merged = sorted((m.get("merged_at") or "", u) for u, m in merges.items() if m and m.get("state") == "merged")
        if len(code) < 2:
            return
        if len(merged) < 2:
            return run.add("integration", INCOMPLETE, run.pdir, "fewer than two merged code units")
        ids = [merged[1][1]]
    for uid in ids:
        adir = run.adir(uid)
        if run.load(adir, "integration.json") is None and merges.get(uid):
            run.add("%s.integration" % uid, FAIL, os.path.join(adir, "integration.json"), "merged without an integration record")
            continue
        integ = run.need("%s.integration" % uid, adir, "integration.json", what="integration record")
        if not integ:
            continue
        j, m = run.load(adir, "judgment.json") or {}, merges.get(uid) or {}
        ok = (sha(integ.get("integration_base")) and sha(integ.get("integrated_candidate")) and j.get("integrated") is True
              and j.get("status") == "DONE" and m.get("integration_base") == integ.get("integration_base"))
        run.add("%s.integration" % uid, PASS if ok else FAIL, os.path.join(adir, "integration.json"),
                "integration base %s rejudged DONE and recorded in the merge" % str(integ.get("integration_base"))[:12])


def job_sources(run, adir):
    """(sources that answered, sacct ever answered, evidence path, sacct did not answer) for the job's last invocation.

    The last item is True only when evidence.json exists, says `sacct_ever_answered` is false and shows no
    answering sacct source."""
    invs = run.invs(adir, ("job",))
    if not invs:
        return [], False, adir, False
    inv = invs[-1]
    ev, ex, ans = run.load(inv, "evidence.json"), run.load(inv, "exit.json") or {}, []
    srcs = sub(ev, "sources") if ev else {}
    for name, s in srcs.items():
        if isinstance(s, dict) and s.get("outcome") in ("known", "listed"):
            ans.append(name)
    if ex.get("rc") == 0:
        ans.append("wrapper")
    ever = bool(ev) and ev.get("sacct_ever_answered") is True
    silent = bool(ev) and ev.get("sacct_ever_answered") is False and sub(srcs, "sacct").get("outcome") not in ("known", "listed", "not_known")
    return sorted(set(ans)), ever, inv, silent


def check_job(run, uid):
    j = check_judgment(run, uid, "job")
    adir = run.adir(uid)
    answered, sacct, inv, silent = job_sources(run, adir)
    want = [w for w in run.expect.get("job_sources") or [] if isinstance(w, str)] if isinstance(run.expect.get("job_sources"), list) else []
    sid = "%s.sources" % uid
    if not answered:
        run.add(sid, INCOMPLETE, inv, "no source answered or no evidence record")
    elif not set(want) <= set(answered):
        run.add(sid, FAIL, inv, "answered %s; expected %s" % (answered, sorted(want)))
    else:
        run.add(sid, PASS, inv, "answered %s; sacct ever answered: %s" % (answered, str(sacct).lower()))
    mode, done = run.expect.get("sacct", "any"), (j or {}).get("status") == "DONE"
    if mode == "answered":
        run.add("%s.sacct" % uid, PASS if sacct else INCOMPLETE, inv, "sacct answered" if sacct else "sacct never answered: the sacct-judged item is incomplete (D3)")
    elif mode == "unavailable":
        ok = done and silent and bool(set(answered) - {"sacct"})
        run.add("%s.sacct" % uid, PASS if ok else INCOMPLETE, inv,
                "judged DONE without sacct from %s" % answered if ok else
                "no evidence.json recording that sacct did not answer, or sacct answered, or job not DONE, or no other source: fallback not shown")
    check_tracker(run, uid)
    jj = run.load(adir, "judgment.json") or {}
    uncertain = run.expect.get("uncertain_units")
    resolved = jj.get("orchestrator_resolved") is True and bool(jj.get("reason")) and bool(jj.get("evidence"))
    if resolved:
        run.orchestrator.append({"unit": uid, "item": "resolved %s: %s" % (jj.get("status"), str(jj.get("reason"))[:120])})
    if isinstance(uncertain, list) and uid in uncertain:
        run.add("%s.resolved" % uid, PASS if resolved else INCOMPLETE, os.path.join(adir, "judgment.json"),
                "orchestrator-resolved %s" % jj.get("status") if resolved else "no orchestrator-resolved judgment")


def all_invocations(run):
    out = []
    base = os.path.join(run.pdir, "units")
    for u in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        ab = os.path.join(base, u, "attempts")
        for a in sorted(os.listdir(ab)) if os.path.isdir(ab) else []:
            out += run.invs(os.path.join(ab, a))
    return out


def check_backend(run):
    want = run.expect.get("backend")
    invs = all_invocations(run)
    if want not in ("slurm", "local"):
        return
    if not invs:
        return run.add("backend", INCOMPLETE, run.pdir, "no invocations")
    seen = sorted(set(str((run.load(i, "intent.json") or {}).get("backend")) for i in invs))
    run.add("backend", PASS if seen == [want] else FAIL, os.path.join(run.pdir, "units"), "backends used %s, expected %s" % (seen, want))
    if want != "local":
        return
    hits = []
    for i in invs:
        hits += [os.path.relpath(os.path.join(i, n), run.root) for n in ("evidence.json", "cancel.json") if os.path.exists(os.path.join(i, n))]
        if (run.load(i, "launch.json") or {}).get("job_id"):
            hits.append(os.path.relpath(os.path.join(i, "launch.json"), run.root))
    if os.path.isdir(os.path.join(run.state, "handshake")):
        hits.append(".overseer/handshake")
    sp = run.local_file("shim_log", run.expect.get("shim_log"))
    calls = run.lines(sp) if sp else None
    if calls:
        hits.append("%s (%d logged calls)" % (run.expect["shim_log"], len(calls)))
    ev = sp or os.path.join(run.state, "events.jsonl")
    if hits:
        run.add("no_slurm", FAIL, ev, "Slurm traces: %s" % hits)
    elif sp is None:
        run.add("no_slurm", INCOMPLETE, ev, "state shows no Slurm use; no shim_log proves no call was attempted")
    elif calls is None:
        run.add("no_slurm", INCOMPLETE, ev, "shim_log %s is missing or unreadable" % run.expect["shim_log"])
    else:
        run.add("no_slurm", PASS, ev, "no job ids, scheduler evidence or logged Slurm calls (shim_log exists and is empty)")


def check_recovery(run, spec):
    uid = spec.get("unit")
    if not (isinstance(uid, str) and ID_RE.match(uid)):
        return
    cid, path = "recovery.%s" % uid, os.path.join(run.udir(uid), "merge.json")
    m = run.need(cid, path, what="merge record")
    if not m:
        return
    kinds = [e.get("kind") for e in run.events() if e.get("unit_id") == uid]
    lp = run.local_file("merge_log", spec.get("merge_log"))
    log = run.lines(lp) if lp else None
    merge_at = [i for i, x in enumerate(log or []) if re.search(r"\bpr\s+merge\b", x)]
    kill_at = [i for i, x in enumerate(log or []) if re.match(r"^\s*KILLED\b", x)]
    problems = (["merge record state %s" % m.get("state")] if m.get("state") != "merged" else []) \
        + (["merge record has no merge_commit"] if not sha(m.get("merge_commit")) else []) \
        + (["%d merged events" % kinds.count("merged")] if kinds.count("merged") != 1 else []) \
        + (["no merge_intent event"] if "merge_intent" not in kinds else []) \
        + (["%d `pr merge` lines logged" % len(merge_at)] if log is not None and len(merge_at) != 1 else []) \
        + (["KILLED precedes the merge line"] if len(merge_at) == 1 and kill_at and kill_at[0] < merge_at[0] else [])
    if problems:
        run.add(cid, FAIL, path, "; ".join(problems))
    elif log is None:
        run.add(cid, INCOMPLETE, lp or path, "merge_log missing or unreadable: no proof of the kill or the call count")
    elif not kill_at:
        run.add(cid, INCOMPLETE, lp, "no KILLED line after the merge call: recovery was not exercised")
    else:
        run.add(cid, PASS, lp, "one logged merge call then KILLED, one merged event, %d intent event(s)" % kinds.count("merge_intent"))


def guarded(run, cid, fn, *args):
    """Run one check; a malformed record becomes an INCOMPLETE claim instead of a crash."""
    n = len(run.claims)
    try:
        return fn(run, *args)
    except Exception as e:  # noqa: BLE001 - every claim must print a status
        del run.claims[n:]
        run.add(cid, INCOMPLETE, run.pdir, "malformed record: %s: %s" % (type(e).__name__, e))
        return None


def evaluate(root, plan_id, expect):
    run = Run(root, plan_id, expect)
    if not os.path.isdir(run.pdir):
        run.add("plan", INCOMPLETE, run.pdir, "no state for plan %s" % plan_id)
        return summary(run)
    code = guarded(run, "units", Run.unit_ids, "code_units", "code") or []
    jobs = guarded(run, "units", Run.unit_ids, "job_units", "job") or []
    guarded(run, "overlap", check_overlap, code)
    merges = {}
    for u in code:
        guarded(run, "%s.judgment" % u, check_judgment, u, "code")
        guarded(run, "%s.review" % u, check_review, u)
        guarded(run, "%s.nonvacuity" % u, check_nonvacuity, u)
        merges[u] = guarded(run, "%s.merge" % u, check_merge, u)
        guarded(run, "%s.tracker" % u, check_tracker, u)
    guarded(run, "integration", check_integration, code, merges)
    for u in jobs:
        guarded(run, "%s.job" % u, check_job, u)
    guarded(run, "backend", check_backend)
    if isinstance(expect.get("recovery"), dict):
        guarded(run, "recovery", check_recovery, expect["recovery"])
    run.add("orchestrator_items", PASS, os.path.join(run.pdir, "units"),
            "; ".join("%s: %s" % (i["unit"], i["item"]) for i in run.orchestrator) or "none")
    return summary(run)


def summary(run):
    counts = dict((s, sum(1 for c in run.claims if c["status"] == s)) for s in (PASS, FAIL, INCOMPLETE))
    return {"run": run.expect.get("run"), "plan_id": run.plan_id, "claims": run.claims, "counts": counts,
            "orchestrator_resolved": run.orchestrator}


def main(argv=None):
    ap = argparse.ArgumentParser(description="evaluate acceptance claims from .overseer/ state")
    ap.add_argument("project_root", metavar="PROJECT_ROOT")
    ap.add_argument("plan_id", metavar="PLAN_ID")
    ap.add_argument("--expect", required=True, metavar="FILE")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not ID_RE.match(a.plan_id):
        ap.error("PLAN_ID must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    try:
        with open(a.expect, encoding="utf-8") as f:
            expect = json.load(f)
    except (OSError, ValueError) as e:
        ap.error("cannot read --expect %s: %s" % (a.expect, e))
    if not isinstance(expect, dict):
        ap.error("--expect must hold a JSON object")
    s = evaluate(a.project_root, a.plan_id, expect)
    if a.json:
        print(json.dumps(s, indent=2, sort_keys=True))
    else:
        for c in s["claims"]:
            print("%s %s %s" % (c["status"], c["id"], c["evidence"]))
    return 1 if s["counts"][FAIL] else (3 if s["counts"][INCOMPLETE] else 0)


if __name__ == "__main__":
    sys.exit(main())
