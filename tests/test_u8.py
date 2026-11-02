import hashlib
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from helpers import ENTRY, plan_copy
from test_u5 import COMMIT, jload, sh
from test_u7 import GhCase, SHIM

from overseer import approval, controller as C, execution as X, github as G, paths, review as R, store, workspace as W


def rd(path, mode="r"):
    with open(path, mode) as f:
        return f.read()


DONE_JSON = "echo '[{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"done\",\"session_id\":\"s\"}]'"
OWN = 'case "$PWD" in */units/ub/*) U=ub;; *) U=ua;; esac; '
AUTHOR = OWN + 'mkdir -p src tests; echo $U > src/$U.py; echo t > tests/test_$U.py; git add src tests; %s; %s' % (COMMIT, DONE_JSON)
SHARED = (OWN + 'case $U in ub) printf "l1\\nl2\\nl3\\nl4\\nL5\\nl6\\nl7\\n";; *) printf "l1\\nl2\\nL3\\nl4\\nl5\\nl6\\nl7\\n";; esac'
          ' > src/shared.txt; git add src; %s; %s' % (COMMIT, DONE_JSON))
CLASH = (OWN + 'case $U in ub) printf "l1\\nl2\\nM3\\nl4\\nl5\\nl6\\nl7\\n";; *) printf "l1\\nl2\\nL3\\nl4\\nl5\\nl6\\nl7\\n";; esac'
         ' > src/shared.txt; git add src; %s; %s' % (COMMIT, DONE_JSON))

MERGE_BLOCK = '''
if a[0] == "repo" and a[1] == "view":
    if "-R" in a or len(a) < 3 or a[2].startswith("-"):  # the real gh takes the repository positionally
        die("unknown shorthand flag: 'R' in -R")
    out(json.dumps({"defaultBranchRef": {"name": st.get("default", "main")}}))
if a[0] == "pr" and a[1] == "merge":
    p = st["prs"][int(a[2]) - 1]
    if fault("merge_timeout_before"):
        die("Post https://api.github.com/graphql: dial tcp 1.2.3.4:443: i/o timeout")
    if opt("--match-head-commit") != pr_json(dict(p))["headRefOid"] or "--squash" not in a:
        die("Head branch was modified. Review and try the merge again.")
    import tempfile
    tmp = tempfile.mkdtemp()
    env = dict(os.environ, GIT_AUTHOR_NAME="gh", GIT_AUTHOR_EMAIL="g@localhost", GIT_COMMITTER_NAME="gh", GIT_COMMITTER_EMAIL="g@localhost")
    def g(*x):
        return subprocess.check_output(["git", "-C", tmp] + list(x), env=env, stderr=subprocess.STDOUT).decode().strip()
    subprocess.check_call(["git", "clone", "-q", st["origin"], tmp])
    g("checkout", "-q", p["baseRefName"])
    g("merge", "--squash", "origin/" + p["headRefName"])
    g("commit", "-q", "-m", p["title"])
    g("push", "-q", "origin", "HEAD:refs/heads/" + p["baseRefName"])
    p["state"], p["mergeCommit"] = "MERGED", {"oid": g("rev-parse", "HEAD")}
    if p["baseRefName"] == st.get("default", "main") and not faults.get("no_autoclose"):
        for x in re.findall(r"Closes #(\\d+)", p["body"]):
            issue(int(x))["state"] = "CLOSED"
    if fault("merge_timeout_after"):
        die("dial tcp 1.2.3.4:443: i/o timeout")
    out()
'''
SHIM2 = SHIM.replace('die("unhandled', MERGE_BLOCK + 'die("unhandled')


class MergeCase(GhCase):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.shims, "gh"), "w") as f:
            f.write(SHIM2 % {"py": sys.executable})
        mock.patch.object(C, "POLL_INTERVAL", 0.01).start()
        mock.patch.object(C, "POLL_SECONDS", 0.05).start()

    def plan(self, target="main", shared=False, caps=None, ua_test=None, ub_test=None):
        p = plan_copy()
        p.update(plan_id="demo", scope=["src/**", "tests/**"], target_branch=target)
        if caps:
            p["caps"] = caps
        mk = lambda uid: {"id": uid, "kind": "code", "title": "Unit " + uid, "prompt": "Write " + uid, "claims": [uid + " works"],
                          "test_command": "test -f src/shared.txt" if shared else "test -f src/%s.py" % uid,
                          "outputs": ["src/shared.txt" if shared else "src/%s.py" % uid],
                          "scope": ["src/shared.txt"] if shared else ["src/%s*" % uid, "tests/test_%s.py" % uid], "needs": []}
        p["units"] = [mk("ua"), mk("ub"), {"id": "jc", "kind": "job", "title": "Job", "command": "true",
                                           "outputs": ["result.txt"], "needs": ["ua", "ub"]}]
        if ua_test:
            p["units"][0]["test_command"] = ua_test
        if ub_test:
            p["units"][1]["test_command"] = ub_test
        approval.approve_plan(self.root, p, "me")
        self.assertEqual(self.cli("issues", "file")[0], 0)
        return p

    def seed_shared(self):
        self.put("src/shared.txt", "l1\nl2\nl3\nl4\nl5\nl6\nl7\n")
        sh(self.root, "add", "-A")
        sh(self.root, *(["-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "shared"]))
        sh(self.root, "push", "-q", "origin", "main")

    def dispatch_both(self, body=AUTHOR):
        self.author(body)
        rc, out, err = self.cli("dispatch")
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(json.loads(l)["unit"] for l in out.splitlines()), ["ua", "ub"])
        for u in ("ua", "ub"):
            self.wait_done(u)
            rc, out, err = self.cli("judge", u)
            self.assertEqual(json.loads(out)["status"], "DONE", out)

    def wtree(self, uid):
        return os.path.join(paths.attempt_dir(self.root, "demo", uid, 1), "worktree")

    def head(self, uid):
        return sh(self.wtree(uid), "rev-parse", "HEAD")

    def fake_review(self, uid, round_=1):
        rdir = os.path.join(paths.attempt_dir(self.root, "demo", uid, 1), "reviews", "r%d" % round_)
        store.write_json_atomic(os.path.join(rdir, "round.json"), {
            "round": round_, "candidate": self.head(uid), "state": "complete", "verdict": "PASS", "retried": False,
            "host": "claude", "calls": [{"state": "ok", "session_id": "reviewer-%s-%d" % (uid, round_)}]})
        store.write_json_atomic(os.path.join(rdir, "review.json"), {"verdict": "PASS", "findings": []})

    def ready(self, uid, round_=1):
        self.fake_review(uid, round_)
        rc, out, err = self.cli("nonvacuous", uid)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def merged_count(self):
        return len(self.calls("pr", "merge"))

    def events(self, kind):
        return [e for e in store.read_events(self.root) if e["kind"] == kind]

    def unit_json(self, uid, name):
        return jload(os.path.join(paths.unit_dir(self.root, "demo", uid), name))

    def facet(self, uid, name):
        return approval.load_unit_state(self.root, "demo", uid)[name]

    def next_lines(self):
        rc, out, err = self.cli("next")
        self.assertEqual(rc, 0, err)
        return out.splitlines()


class TwoUnits(MergeCase):
    def test_parallel_units_merge_in_turn_and_second_carries_its_review(self):
        self.plan()
        self.dispatch_both()
        self.assertEqual(self.next_lines(), ["review ua", "review ub"])  # DONE judgments do not make jc READY
        self.ready("ua")
        self.ready("ub")
        self.assertEqual(self.next_lines(), ["pr ua", "pr ub"])
        ub_first = self.head("ub")
        rc, out, err = self.cli("merge", "ua")
        self.assertEqual(rc, 0, err)
        rec = json.loads(out)
        main = sh(self.origin, "rev-parse", "refs/heads/main")
        mj = self.unit_json("ua", "merge.json")
        self.assertEqual((mj["state"], mj["pr_head"], mj["candidate"], mj["merge_commit"]), ("merged", self.head("ua"), self.head("ua"), main))
        self.assertNotEqual(mj["pr_head"], mj["merge_commit"])  # squash: the two are separate fields
        self.assertEqual((rec["merge_commit"], rec["tracker"]), (main, "closed"))
        self.assertEqual(self.facet("ua", "closure")["state"], "merged")
        self.assertEqual(self.facet("ua", "tracker"), {"issue": 1, "state": "closed", "via": "merge"})
        self.assertEqual(self.gh_state()["issues"][0]["state"], "CLOSED")
        self.assertEqual(self.calls("issue", "close"), [])  # the default branch closes it
        self.assertEqual(self.calls("pr", "merge")[0][-3:], ["--squash", "--match-head-commit", self.head("ua")])
        self.assertEqual(self.next_lines(), ["pr ub"])  # jc still waits for ub: code closes only when merged
        rc, out, err = self.cli("merge", "ub")  # the target advanced: merge it in, re-test, carry the review
        self.assertEqual(rc, 0, err)
        wt = self.wtree("ub")
        self.assertNotEqual(self.head("ub"), ub_first)
        self.assertEqual(sh(wt, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:], [ub_first, mj["merge_commit"]])
        integ = jload(os.path.join(paths.attempt_dir(self.root, "demo", "ub", 1), "integration.json"))
        self.assertEqual((integ["integration_base"], integ["previous_candidate"], integ["integrated_candidate"]),
                         (mj["merge_commit"], ub_first, self.head("ub")))
        j = jload(os.path.join(paths.attempt_dir(self.root, "demo", "ub", 1), "judgment.json"))
        self.assertEqual((j["status"], j["integrated"], j["candidate"]), ("DONE", True, self.head("ub")))
        carry = jload(os.path.join(paths.attempt_dir(self.root, "demo", "ub", 1), "reviews", "carry.json"))
        cf = carry["carried_from"]
        self.assertEqual((cf["candidate"], cf["patch_id"] == cf["integrated_patch_id"], len(cf["patch_id"])), (ub_first, True, 40))
        self.assertFalse(os.path.exists(os.path.join(paths.attempt_dir(self.root, "demo", "ub", 1), "integration_intent.json")))
        self.assertEqual(self.merged_count(), 2)
        self.assertEqual(sh(self.origin, "show", "refs/heads/main:src/ua.py"), "ua")
        self.assertEqual(sh(self.origin, "show", "refs/heads/main:src/ub.py"), "ub")
        self.assertEqual(self.next_lines(), ["dispatch jc"])  # both needs merged now
        self.assertEqual(self.cli("merge", "ub")[0], 0)  # idempotent once merged
        self.assertEqual(self.merged_count(), 2)

    def test_next_is_identical_across_a_restart_and_writes_nothing(self):
        self.plan()
        self.dispatch_both()
        self.ready("ua")
        ncalls = len(self.gh_state()["calls"])
        before = self.cli("next", "--json")[1]
        files = sorted(os.path.join(d, f) for d, _, fs in os.walk(paths.state_dir(self.root)) for f in fs)
        events = len(store.read_events(self.root))
        r = subprocess.run([sys.executable, ENTRY, "next", "--json"], cwd=self.root, env=self.env(), capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, before), r.stderr)
        self.assertEqual(self.cli("next", "--json")[1], before)
        self.assertEqual(sorted(os.path.join(d, f) for d, _, fs in os.walk(paths.state_dir(self.root)) for f in fs), files)
        self.assertEqual(len(store.read_events(self.root)), events)
        self.assertEqual([(a["unit"], a["action"]) for a in json.loads(before)], [("ua", "pr"), ("ub", "review")])
        self.assertEqual(len(self.gh_state()["calls"]), ncalls)  # no network call

    def test_changed_patch_id_requires_a_new_round(self):
        self.seed_shared()
        self.plan(shared=True)
        self.dispatch_both(SHARED)
        self.ready("ua")
        self.ready("ub")
        self.assertEqual(self.cli("merge", "ua")[0], 0)
        rc, out, err = self.cli("merge", "ub")
        self.assertEqual(rc, 1)
        self.assertIn("not the current candidate", err)  # reviewed the old head: a new round is needed
        adir = paths.attempt_dir(self.root, "demo", "ub", 1)
        self.assertFalse(os.path.exists(os.path.join(adir, "reviews", "carry.json")))
        self.assertEqual((self.merged_count(), self.next_lines()), (1, ["review ub"]))
        self.assertEqual(jload(os.path.join(adir, "integration.json"))["integrated_candidate"], self.head("ub"))
        self.ready("ub", 2)  # a second round on the integrated head, and non-vacuity re-run
        self.assertEqual(self.cli("merge", "ub")[0], 0)
        self.assertEqual(self.merged_count(), 2)
        self.assertEqual(sh(self.origin, "show", "refs/heads/main:src/shared.txt").split("\n")[2:5], ["L3", "l4", "L5"])

    def test_integration_conflict_stops_and_leaves_the_worktree_clean(self):
        self.seed_shared()
        self.plan(shared=True)
        self.dispatch_both(CLASH)
        self.ready("ua")
        self.ready("ub")
        self.assertEqual(self.cli("merge", "ua")[0], 0)
        before = self.head("ub")
        rc, out, err = self.cli("merge", "ub")
        self.assertEqual(rc, 5)
        self.assertIn("INTEGRATION_CONFLICT", err)
        self.assertEqual((self.head("ub"), sh(self.wtree("ub"), "status", "--porcelain", "--untracked-files=no")), (before, ""))
        self.assertEqual(sh(self.wtree("ub"), "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False), "")
        self.assertEqual(self.events("INTEGRATION_CONFLICT")[0]["files"], ["src/shared.txt"])
        self.assertEqual(self.merged_count(), 1)
        self.assertFalse(os.path.exists(os.path.join(paths.attempt_dir(self.root, "demo", "ub", 1), "integration_intent.json")))
        self.assertTrue([l for l in self.next_lines() if l.startswith("stop ub: INTEGRATION_CONFLICT")])
        self.assertEqual(self.facet("ub", "closure")["state"], "open")


class Gates(MergeCase):
    def test_missing_or_failing_nonvacuity_blocks_the_merge(self):
        self.plan(caps={"review_rounds": 1})
        self.dispatch_both()
        self.fake_review("ua")
        rc, _, err = self.cli("merge", "ua")
        self.assertEqual((rc, "no non-vacuity record" in err, self.gh_state()["prs"]), (1, True, []))
        self.assertEqual(self.next_lines()[0], "nonvacuous ua")
        self.ready("ua")
        nv = os.path.join(paths.attempt_dir(self.root, "demo", "ua", 1), "nonvacuous.json")
        rec = jload(nv)
        store.write_json_atomic(nv, dict(rec, status="FAIL"))
        rc, _, err = self.cli("merge", "ua")
        self.assertEqual((rc, "non-vacuity FAIL" in err, self.merged_count()), (1, True, 0))
        self.assertEqual(self.cli("dispositions", "ua", "--orchestrator-decision", "tests are vacuous by design; owner agreed")[0], 0)
        self.assertEqual(self.cli("merge", "ua")[0], 0)  # a FAIL overruled at the review cap
        self.assertEqual(self.merged_count(), 1)
        store.write_json_atomic(nv, dict(rec, candidate="0" * 40))  # another candidate's record never counts
        self.assertIn("another candidate", C.nonvacuity_problem(os.path.dirname(nv), self.head("ua"), {"orchestrator_decision": True}))

    def test_review_not_eligible_blocks(self):
        self.plan()
        self.dispatch_both()
        rc, _, err = self.cli("merge", "ua")
        self.assertEqual((rc, "no review round" in err), (1, True))

    def test_dirty_worktree_refuses_before_any_merge_in(self):
        self.plan()
        self.dispatch_both()
        self.ready("ua")
        self.ready("ub")
        self.assertEqual(self.cli("merge", "ua")[0], 0)
        self.put("src/ub.py", "tampered\n", base=self.wtree("ub"))
        before = self.head("ub")
        rc, _, err = self.cli("merge", "ub")
        self.assertEqual((rc, "not clean" in err or "modified" in err, self.head("ub")), (1, True, before))
        self.assertFalse(os.path.exists(os.path.join(paths.attempt_dir(self.root, "demo", "ub", 1), "integration_intent.json")))

    def test_pr_head_changed_remotely_blocks_the_merge(self):
        self.plan()
        self.dispatch_both()
        self.ready("ua")
        self.assertEqual(self.cli("pr", "ua")[0], 0)
        other = os.path.join(self.tmp, "other")
        sh(self.tmp, "clone", "-q", self.origin, other)
        sh(other, "checkout", "-q", "overseer/demo/ua/a1")
        self.put("extra.txt", "x\n", base=other)
        sh(other, "add", "-A")
        sh(other, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "late")
        sh(other, "push", "-q", "origin", "HEAD")
        rc, _, err = self.cli("merge", "ua")
        self.assertEqual((rc, "not an ancestor" in err, self.merged_count()), (1, True, 0))
        self.assertFalse(os.path.exists(os.path.join(paths.unit_dir(self.root, "demo", "ua"), "merge.json")))
        self.fault(wrong_head="0" * 40)  # and a head that reads back differently
        rc, _, err = self.cli("merge", "ua")
        self.assertEqual((rc, self.merged_count()), (1, 0))


class Resume(MergeCase):
    def one(self, **kw):
        self.plan(**kw)
        self.dispatch_both()
        self.ready("ua")

    def test_crash_after_the_merge_call_reads_merged_and_never_merges_again(self):
        self.one()
        with mock.patch.object(C, "_record_merge", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.cli("merge", "ua")
        self.assertEqual((self.merged_count(), self.unit_json("ua", "merge.json")["state"], self.facet("ua", "closure")["state"]),
                         (1, "intent", "open"))
        rc, out, err = self.cli("merge", "ua")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.merged_count(), self.facet("ua", "closure")["state"], self.unit_json("ua", "merge.json")["state"]),
                         (1, "merged", "merged"))
        self.assertEqual(json.loads(out)["merge_commit"], sh(self.origin, "rev-parse", "refs/heads/main"))

    def test_timeout_that_applied_is_reconciled_and_one_that_did_not_is_ambiguous_then_reissued(self):
        self.one()
        self.fault(merge_timeout_after=True)
        rc, _, err = self.cli("merge", "ua")
        self.assertEqual((rc, self.merged_count(), self.facet("ua", "closure")["state"]), (0, 1, "merged"), err)
        self.assertIn("github_ambiguous", [e["kind"] for e in store.read_events(self.root)])

    def test_timeout_before_the_merge_reads_open_and_the_rerun_issues_it_once_more(self):
        self.one()
        self.fault(merge_timeout_before=True)
        rc, _, err = self.cli("merge", "ua")
        self.assertEqual((rc, self.merged_count(), self.unit_json("ua", "merge.json")["state"]), (3, 1, "intent"), err)
        self.assertEqual(self.cli("merge", "ua")[0], 0)
        self.assertEqual((self.merged_count(), self.facet("ua", "closure")["state"]), (2, "merged"))

    def test_interrupted_integration_resumes_from_its_intent(self):
        self.plan()
        self.dispatch_both()
        self.ready("ua")
        self.ready("ub")
        self.assertEqual(self.cli("merge", "ua")[0], 0)
        with mock.patch.object(C.J, "rejudge_integrated", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.cli("merge", "ub")  # the merge-in commit exists, the judgment does not
        adir = paths.attempt_dir(self.root, "demo", "ub", 1)
        merge_commit = self.head("ub")
        self.assertTrue(os.path.exists(os.path.join(adir, "integration_intent.json")))
        self.assertEqual(self.next_lines(), ["merge ub: resume the integration"])
        self.assertEqual(self.cli("merge", "ub")[0], 0)
        self.assertEqual(self.head("ub"), merge_commit)  # not merged in a second time
        self.assertEqual(self.merged_count(), 2)


class Tracker(MergeCase):
    def test_non_default_target_closes_the_issue_explicitly(self):
        sh(self.root, "push", "-q", "origin", "main:refs/heads/release")
        self.plan(target="release")
        self.dispatch_both()
        self.ready("ua")
        rc, out, err = self.cli("merge", "ua")
        self.assertEqual(rc, 0, err)
        st = self.gh_state()
        self.assertEqual((st["issues"][0]["state"], len(self.calls("issue", "close"))), ("CLOSED", 1))
        self.assertEqual(self.facet("ua", "tracker")["via"], "explicit close")
        self.assertEqual(st["prs"][0]["baseRefName"], "release")
        self.assertEqual(sh(self.origin, "show", "refs/heads/release:src/ua.py"), "ua")

    def test_failed_tracker_step_leaves_pending_and_a_rerun_completes_it(self):
        self.plan()
        self.dispatch_both()
        self.ready("ua")
        self.fault(no_autoclose=True)
        rc, out, err = self.cli("merge", "ua")
        self.assertEqual(rc, 0, err)  # the merge record is not lost
        self.assertEqual((self.facet("ua", "closure")["state"], self.facet("ua", "tracker")["state"]), ("merged", "pending"))
        self.assertIn("tracker=pending", self.cli("status")[1])
        self.assertEqual(json.loads(self.cli("status", "--json")[1])["plans"]["demo"]["units"]["ua"]["tracker"]["state"], "pending")
        self.assertIn("tracker ua", self.next_lines()[0])
        st = self.gh_state()
        st["issues"][0]["state"] = "CLOSED"
        self.gh_state(st)
        self.assertEqual(self.cli("merge", "ua")[0], 0)
        self.assertEqual((self.facet("ua", "tracker")["state"], self.merged_count()), ("closed", 1))

    def test_job_unit_closes_on_done_judgment_not_on_exit(self):
        self.plan()
        self.dispatch_both()
        for u in ("ua", "ub"):
            self.ready(u)
            self.assertEqual(self.cli("merge", u)[0], 0)
        rc, out, err = self.cli("dispatch")
        self.assertEqual((rc, [json.loads(l)["unit"] for l in out.splitlines()]), (0, ["jc"]), err)
        self.wait_done("jc")
        self.assertEqual(self.next_lines(), ["judge jc: attempt a1"])  # exited, not closed
        j = self.cli("judge", "jc")
        self.assertEqual(j[0], 1)  # result.txt is missing: FAILED, so a retry, never closure
        self.assertEqual(self.next_lines()[0].split(":")[0], "retry jc")


class OwnerStops(MergeCase):
    def stopped(self, rc, text):
        self.assertEqual(rc, 1)
        self.assertIn("OWNER_STOP", text[1])
        ev = self.events("OWNER_STOP")
        self.assertTrue(ev and text[0] in ev[-1]["reason"], ev)

    def test_digest_mismatch(self):
        self.plan()
        p = os.path.join(paths.plan_dir(self.root, "demo"), "plan.json")
        d = jload(p)
        d["units"][0]["claims"].append("sneaked in")
        store.write_json_atomic(p, d)
        ncalls = len(self.gh_state()["calls"])
        rc, _, err = self.cli("merge", "ua")
        self.stopped(rc, ("plan digest mismatch", err))
        self.assertEqual(len(self.gh_state()["calls"]), ncalls)
        self.assertTrue(self.next_lines()[0].startswith("stop plan:demo: plan digest mismatch"))

    def test_cap_above_the_approved_value_and_scope_widening(self):
        self.plan()
        p = os.path.join(paths.plan_dir(self.root, "demo"), "approval.json")
        a = jload(p)
        b = dict(a["boundary"], caps=dict(a["boundary"]["caps"], workers=1))
        store.write_json_atomic(p, dict(a, boundary=b))
        rc, _, err = self.cli("merge", "ua")
        self.stopped(rc, ("cap workers=3 is above the approved 1", err))
        store.write_json_atomic(p, dict(a, boundary=dict(a["boundary"], scope=["docs/**"])))
        rc, _, err = self.cli("merge", "ua")
        self.stopped(rc, ("widens the approved scope", err))
        self.assertEqual(self.cli("dispatch")[0], 1)  # dispatch-all reports the owner stop: nothing launched
        self.assertEqual(list(X.iter_invocations(self.root)), [])
        store.write_json_atomic(p, a)
        st = approval.load_unit_state(self.root, "demo", "ua")
        st["counters"]["attempts"] = 3
        approval.save_unit_state(self.root, "demo", "ua", st)
        self.assertIn("above the cap", C.owner_problems(self.root, "demo")[0])

    def test_split_widening_scope_is_recorded(self):
        self.plan()
        spec = os.path.join(self.tmp, "spec.json")
        with open(spec, "w") as f:
            json.dump([{"id": "ua1", "outputs": ["src/ua.py"], "scope": ["lib/**"]}], f)
        rc, _, err = self.cli("plan", "split", "ua", "--into", spec, "--reason", "r")
        self.stopped(rc, ("adds scope", err))

    def test_write_outside_the_project_root(self):
        self.plan()
        self.dispatch_both()
        self.ready("ua")
        wsp = os.path.join(paths.attempt_dir(self.root, "demo", "ua", 1), "workspace.json")
        ws = jload(wsp)
        store.write_json_atomic(wsp, dict(ws, worktree=os.path.join(self.tmp, "elsewhere")))
        rc, _, err = self.cli("merge", "ua")
        self.stopped(rc, ("outside the project root", err))
        self.assertEqual(self.calls("pr"), [])
        with self.assertRaises(approval.OwnerStop):
            C.inside_root(self.root, os.path.join(self.root, "..", "x"))

    def test_destructive_git_is_refused_everywhere_and_recorded(self):
        self.plan()
        for argv in (["push", "--force", "origin", "x"], ["push", "-f", "origin", "x"], ["push", "--force-with-lease", "origin", "x"],
                     ["push", "origin", "+x:x"], ["push", "origin", ":main"], ["push", "--delete", "origin", "main"],
                     ["push", "-fu", "origin", "x"], ["reset", "--hard", "HEAD~1"], ["branch", "-D", "main"], ["branch", "-d", "main"],
                     ["rebase", "main"], ["filter-branch", "--all"], ["commit", "--amend", "-m", "x"], ["-C", ".", "rebase", "-i"],
                     ["-c", "a.b=c", "reset", "--hard"], ["update-ref", "-d", "refs/heads/main"]):
            with self.assertRaises(W.DestructiveGit, msg=argv):
                W.git(self.root, *argv)
        for argv in (["push", "origin", "b:b"], ["fetch", "origin", "+refs/heads/m:refs/remotes/origin/m"],
                     ["branch", "--list"], ["-c", "user.name=x", "merge", "--no-ff", "origin/main"], ["worktree", "add", "-b", "b", "p"]):
            self.assertIsNone(W.destructive_reason(argv), argv)
        with mock.patch.object(G, "push_argv", return_value=["push", "--force", "origin", "b:b"]):  # U7's push uses the same helper
            with self.assertRaises(W.DestructiveGit):
                G.push_branch(self.root, self.root, "b", "0" * 40)
        with mock.patch.object(C, "merge_unit", side_effect=lambda *a: W.git(self.root, "branch", "-D", "main")):
            rc, _, err = self.cli("merge", "ua")
        self.stopped(rc, ("branch deletion", err))


class Round1(MergeCase):
    def commit_main(self, rel, text):
        self.put(rel, text)
        sh(self.root, "add", "-A")
        sh(self.root, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "c " + rel)
        sh(self.root, "push", "-q", "origin", "main")

    def test_f1_abandoned_need_never_readies_its_dependent(self):
        self.plan()
        self.dispatch_both()
        self.ready("ub")
        self.assertEqual(self.cli("merge", "ub")[0], 0)
        st = approval.load_unit_state(self.root, "demo", "ua")
        st["closure"] = {"state": "abandoned"}
        approval.save_unit_state(self.root, "demo", "ua", st)
        lines = self.next_lines()
        self.assertNotIn("dispatch jc", lines)
        self.assertTrue([l for l in lines if l.startswith("stop jc")], lines)
        self.assertEqual(self.cli("dispatch")[0], 0)
        self.assertEqual(list(X.iter_invocations(self.root, "demo", "jc")), [])

    def test_f2_nonvacuity_is_rerun_on_the_integrated_candidate(self):
        self.commit_main("src/cfg.txt", "off\n")
        self.plan(caps={"workers": 3, "attempts": 2, "review_rounds": 1},
                  ua_test='read -r v < src/cfg.txt; [ "$v" = on ] || [ -f src/ua.py ]')
        self.dispatch_both()
        self.assertEqual(self.ready("ua")["status"], "PASS")
        self.commit_main("src/cfg.txt", "on\n")  # the target now makes the unit's change unnecessary
        rc, _, err = self.cli("merge", "ua")
        self.assertEqual((rc, "non-vacuity FAIL" in err, self.merged_count()), (1, True, 0), err)
        nv = jload(os.path.join(paths.attempt_dir(self.root, "demo", "ua", 1), "nonvacuous.json"))
        self.assertEqual((nv["status"], nv["candidate"]), ("FAIL", self.head("ua")))
        self.assertEqual(self.cli("dispositions", "ua", "--orchestrator-decision", "owner accepted the redundant change")[0], 0)
        self.assertEqual(self.cli("merge", "ua")[0], 0)
        self.assertEqual(self.merged_count(), 1)

    def test_f3_crash_between_merged_record_and_closure_reconciles(self):
        real = store.write_json_atomic

        def crash(path, data, *a, **k):
            real(path, data, *a, **k)
            if path.endswith("merge.json") and isinstance(data, dict) and data.get("state") == "merged":
                raise KeyboardInterrupt
        self.plan()
        self.dispatch_both()
        self.ready("ua")
        with mock.patch.object(store, "write_json_atomic", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.cli("merge", "ua")
        self.assertEqual((self.unit_json("ua", "merge.json")["state"], self.facet("ua", "closure")["state"]), ("merged", "open"))
        before = len(self.events("integration_intent"))
        rc, out, err = self.cli("merge", "ua")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.merged_count(), self.facet("ua", "closure")["state"], self.facet("ua", "tracker")["state"]),
                         (1, "merged", "closed"))
        self.assertEqual(len(self.events("integration_intent")), before)
        self.assertEqual(json.loads(out)["merge_commit"], sh(self.origin, "rev-parse", "refs/heads/main"))
        self.assertFalse(os.path.exists(os.path.join(paths.attempt_dir(self.root, "demo", "ua", 1), "integration_intent.json")))

    def test_f4_dispatch_all_launches_a_retry_within_the_attempts_cap(self):
        self.plan()
        self.dispatch_both()
        for u in ("ua", "ub"):
            self.ready(u)
            self.assertEqual(self.cli("merge", u)[0], 0)
        self.assertEqual(self.cli("dispatch")[0], 0)
        self.wait_done("jc")
        self.assertEqual(self.cli("judge", "jc")[0], 1)
        rc, out, err = self.cli("dispatch")
        self.assertEqual((rc, [(json.loads(l)["unit"], json.loads(l)["attempt"]) for l in out.splitlines()]), (0, [("jc", 2)]), err)
        self.wait_done("jc")
        self.assertEqual(self.cli("judge", "jc")[0], 1)
        rc, out, err = self.cli("dispatch")  # at the attempts cap: nothing more is launched
        self.assertEqual((rc, out), (0, ""), err)
        self.assertEqual(approval.load_unit_state(self.root, "demo", "jc")["counters"]["attempts"], 2)

    def test_f5_dispatch_all_records_and_reports_owner_stops(self):
        self.plan()
        p = os.path.join(paths.plan_dir(self.root, "demo"), "approval.json")
        a = jload(p)
        store.write_json_atomic(p, dict(a, boundary=dict(a["boundary"], scope=["docs/**"])))
        n = len(self.events("OWNER_STOP"))
        rc, out, err = self.cli("dispatch")
        self.assertNotEqual(rc, 0)
        self.assertIn("widens the approved scope", err)
        ev = self.events("OWNER_STOP")
        self.assertTrue(len(ev) > n)
        self.assertTrue(all("widens the approved scope" in e["reason"] and e["command"] == "dispatch" for e in ev[n:]))
        self.assertEqual(list(X.iter_invocations(self.root)), [])

    def test_f6_alias_cannot_hide_a_destructive_git_command(self):
        repo = os.path.join(self.tmp, "aliasrepo")
        os.makedirs(repo)
        sh(repo, "init", "-q", "-b", "main")
        for i in (1, 2):
            self.put("f.txt", "v%d\n" % i, base=repo)
            sh(repo, "add", "-A")
            sh(repo, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "c%d" % i)
        self.put("f.txt", "dirty\n", base=repo)
        head = sh(repo, "rev-parse", "HEAD")
        for argv in (["-c", "alias.undo=reset --hard", "undo", "HEAD~1"], ["-c", "ALIAS.undo=reset --hard", "undo", "HEAD~1"],
                     ["--config-env=alias.undo=X", "undo"], ["undo", "HEAD~1"], ["-c", "alias.status=!rm -rf x", "status"],
                     ["totally-unknown"]):
            with self.assertRaises(W.DestructiveGit, msg=argv):
                W.git(repo, *argv)
        self.assertEqual((sh(repo, "rev-parse", "HEAD"), rd(os.path.join(repo, "f.txt"))), (head, "dirty\n"))
        for argv in (["status", "--porcelain=v1"], ["-c", "user.name=x", "rev-parse", "HEAD"], ["--literal-pathspecs", "ls-files"]):
            self.assertIsNone(W.destructive_reason(argv), argv)

    def interrupted_integration(self):
        self.plan()
        self.dispatch_both()
        self.ready("ua")
        self.ready("ub")
        self.assertEqual(self.cli("merge", "ua")[0], 0)
        with mock.patch.object(C, "_merge_in", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.cli("merge", "ub")
        self.assertTrue(os.path.exists(os.path.join(paths.attempt_dir(self.root, "demo", "ub", 1), "integration_intent.json")))

    def test_f7_resume_refuses_a_dirty_worktree(self):
        self.interrupted_integration()
        before = self.head("ub")
        self.put("src/ub.py", "tampered\n", base=self.wtree("ub"))
        rc, _, err = self.cli("merge", "ub")
        self.assertEqual((rc, "not clean" in err, self.head("ub")), (1, True, before), err)
        self.assertEqual(rd(os.path.join(self.wtree("ub"), "src/ub.py")), "tampered\n")

    def test_f7_resume_aborts_its_own_half_done_merge_then_completes(self):
        self.interrupted_integration()
        wt = self.wtree("ub")
        sh(wt, "-c", "user.name=t", "-c", "user.email=t@localhost", "merge", "--no-commit", "--no-ff", "origin/main")
        self.assertNotEqual(sh(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False), "")
        rc, _, err = self.cli("merge", "ub")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.merged_count(), 2)


class Round2(MergeCase):
    CFG_UB = 'read -r v < src/cfg.txt; [ "$v" = off ] && [ -f src/ub.py ]'

    def advance_target(self, rel, text):
        """Push a commit to origin/main from a separate clone: the project repo's tracking ref is not refreshed."""
        d = os.path.join(self.tmp, "adv%d" % len(os.listdir(self.tmp)))
        sh(self.tmp, "clone", "-q", self.origin, d)
        self.put(rel, text, base=d)
        sh(d, "add", "-A")
        sh(d, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "adv " + rel)
        sh(d, "push", "-q", "origin", "HEAD:main")

    def seed_cfg(self):
        self.put("src/cfg.txt", "off\n")
        sh(self.root, "add", "-A")
        sh(self.root, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "cfg")
        sh(self.root, "push", "-q", "origin", "main")

    def interrupted(self, where="_merge_in", **kw):
        self.seed_cfg()
        self.plan(**kw)
        self.dispatch_both()
        self.ready("ua")
        self.ready("ub")
        self.assertEqual(self.cli("merge", "ua")[0], 0)
        target = (C, where) if where == "_merge_in" else (C.J, where)
        with mock.patch.object(target[0], target[1], side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.cli("merge", "ub")
        self.assertTrue(os.path.exists(os.path.join(paths.attempt_dir(self.root, "demo", "ub", 1), "integration_intent.json")))

    def test_f1_resume_integrates_the_advanced_target_and_tests_the_combination(self):
        self.interrupted(ub_test=self.CFG_UB)
        self.advance_target("src/cfg.txt", "on\n")  # B's test needs cfg=off; the project tracking ref is stale
        rc, _, err = self.cli("merge", "ub")
        self.assertEqual((rc, self.merged_count()), (1, 1), err)
        self.assertIn("FAILED", err)

    def test_f1_integrated_candidate_is_integrated_again_when_the_target_moved(self):
        self.interrupted(where="rejudge_integrated", ub_test=self.CFG_UB)  # merge-in done, judgment missing
        self.advance_target("src/cfg.txt", "on\n")
        rc, _, err = self.cli("merge", "ub")
        self.assertEqual((rc, self.merged_count()), (1, 1), err)

    def test_f1_target_advancing_before_the_pr_merge_is_refused_then_integrated(self):
        self.seed_cfg()
        self.plan()
        self.dispatch_both()
        self.ready("ua")
        real = C.G.open_pr

        def advance(*a, **k):
            self.advance_target("src/late.txt", "x\n")
            return real(*a, **k)
        with mock.patch.object(C.G, "open_pr", side_effect=advance):
            rc, _, err = self.cli("merge", "ua")
        self.assertEqual((rc, "advanced" in err, self.merged_count()), (1, True, 0), err)
        self.assertEqual(self.cli("merge", "ua")[0], 0)
        self.assertEqual(sh(self.origin, "show", "refs/heads/main:src/late.txt"), "x")

    def test_f2_resume_after_the_tracking_ref_moved_merges_the_current_target(self):
        self.interrupted()
        prev = self.head("ub")
        self.advance_target("src/other.txt", "o\n")
        sh(self.root, "fetch", "-q", "origin")  # origin/main now differs from the intent's base
        rc, _, err = self.cli("merge", "ub")
        self.assertEqual(rc, 0, err)
        self.assertEqual(sh(self.wtree("ub"), "rev-parse", "HEAD^"), prev)  # one integration, against the retargeted intent
        main = sh(self.origin, "show", "refs/heads/main:src/other.txt")
        self.assertEqual((main, self.merged_count()), ("o", 2))
        self.assertEqual(sh(self.wtree("ub"), "rev-parse", "HEAD^2"), sh(self.root, "rev-parse", "refs/remotes/origin/main"))

    def test_f2_merge_in_uses_the_recorded_commit_not_the_tracking_ref(self):
        self.interrupted()
        intent = jload(os.path.join(paths.attempt_dir(self.root, "demo", "ub", 1), "integration_intent.json"))
        self.advance_target("src/other.txt", "o\n")
        sh(self.root, "fetch", "-q", "origin")  # the ref now names a newer commit than the intent
        with mock.patch.object(C, "_fetch_target", return_value=intent["integration_base"]):
            rc, _, err = self.cli("merge", "ub")
        self.assertEqual(rc, 0, err)
        self.assertEqual(sh(self.wtree("ub"), "rev-parse", "HEAD^2"), intent["integration_base"])

    def test_f3_dispatch_all_skips_a_full_unit_and_launches_the_rest(self):
        a = plan_copy()
        a.update(plan_id="demo", scope=["src/**"], target_branch="main")
        a["units"] = [{"id": "j1", "kind": "job", "title": "J1", "command": "true", "outputs": ["r"], "needs": []}]
        b = plan_copy()
        b.update(plan_id="zeta", scope=["src/**"], target_branch="main")
        b["units"] = [{"id": "k1", "kind": "job", "title": "K1", "command": "true", "outputs": ["y"], "needs": []}]
        approval.approve_plan(self.root, a, "me")
        approval.approve_plan(self.root, b, "me")
        real = X.dispatch_job

        def full(root, pid, unit, *args):
            if unit["id"] == "j1":
                raise X.SlotsFull("plan demo has 1 of its 1 worker slots in use")
            return real(root, pid, unit, *args)
        with mock.patch.object(X, "dispatch_job", side_effect=full):
            rc, out, err = self.cli("dispatch")
        self.assertEqual((rc, [json.loads(l)["unit"] for l in out.splitlines()]), (0, ["k1"]), err)
        self.assertIn("skipped j1", err)

    def test_f4_decision_must_name_the_exact_candidate_and_follow_the_fail(self):
        adir = os.path.join(self.tmp, "att")
        nv, dec = os.path.join(adir, "nonvacuous.json"), os.path.join(adir, "reviews", "orchestrator_decision.json")
        store.write_json_atomic(nv, {"candidate": "c2", "status": "FAIL"})
        digest = hashlib.sha256(rd(nv, "rb")).hexdigest()
        rs = {"orchestrator_decision": True}

        def decide(cand, dig):
            store.write_json_atomic(dec, {"orchestrator_decision": True, "candidate": cand, "text": "t", "nonvacuous_sha256": dig})
        decide("c1", digest)  # a carried decision for another candidate
        self.assertIn("non-vacuity FAIL", C.nonvacuity_problem(adir, "c2", rs))
        decide("c2", None)  # right candidate, recorded before any non-vacuity record
        self.assertIn("non-vacuity FAIL", C.nonvacuity_problem(adir, "c2", rs))
        decide("c2", "0" * 64)  # recorded against an earlier record
        self.assertIn("non-vacuity FAIL", C.nonvacuity_problem(adir, "c2", rs))
        decide("c2", digest)
        self.assertIsNone(C.nonvacuity_problem(adir, "c2", rs))

    def test_f5_update_ref_cannot_move_the_target_backward(self):
        repo = os.path.join(self.tmp, "refrepo")
        os.makedirs(repo)
        sh(repo, "init", "-q", "-b", "main")
        for i in (1, 2):
            self.put("f.txt", "v%d\n" % i, base=repo)
            sh(repo, "add", "-A")
            sh(repo, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "c%d" % i)
        head, old = sh(repo, "rev-parse", "HEAD"), sh(repo, "rev-parse", "HEAD~1")
        for argv in (["update-ref", "refs/heads/main", old], ["update-ref", "-d", "refs/heads/main"]):
            with self.assertRaises(W.DestructiveGit, msg=argv):
                W.git(repo, *argv)
        self.assertEqual(sh(repo, "rev-parse", "main"), head)


class Round3(MergeCase):
    def test_f1_reset_soft_cannot_move_the_target_backward(self):
        repo = os.path.join(self.tmp, "resetrepo")
        os.makedirs(repo)
        sh(repo, "init", "-q", "-b", "main")
        for i in (1, 2):
            self.put("f.txt", "v%d\n" % i, base=repo)
            sh(repo, "add", "-A")
            sh(repo, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "c%d" % i)
        head = sh(repo, "rev-parse", "HEAD")
        for argv in (["reset", "--soft", "HEAD~1"], ["reset", "--mixed", "HEAD~1"], ["reset", "HEAD~1"], ["reset", "--hard", "HEAD~1"]):
            with self.assertRaises(W.DestructiveGit, msg=argv):
                W.git(repo, *argv)
        self.assertEqual(sh(repo, "rev-parse", "main"), head)

    def test_f2_equal_timestamps_do_not_let_an_earlier_decision_accept_a_fail(self):
        adir = os.path.join(self.tmp, "att")
        nv, dec = os.path.join(adir, "nonvacuous.json"), os.path.join(adir, "reviews", "orchestrator_decision.json")
        store.write_json_atomic(dec, {"orchestrator_decision": True, "candidate": "c", "text": "t", "nonvacuous_sha256": None})
        store.write_json_atomic(nv, {"candidate": "c", "status": "FAIL"})
        for f in (nv, dec):
            os.utime(f, (1000000000, 1000000000))
        self.assertIn("non-vacuity FAIL", C.nonvacuity_problem(adir, "c", {"orchestrator_decision": True}))

    def test_f2_record_decision_stores_the_digest_of_the_current_record(self):
        self.plan(caps={"workers": 3, "attempts": 2, "review_rounds": 1})
        self.dispatch_both()
        self.fake_review("ua")
        adir = paths.attempt_dir(self.root, "demo", "ua", 1)
        self.assertEqual(self.cli("dispositions", "ua", "--orchestrator-decision", "before any record")[0], 0)
        self.assertIsNone(jload(os.path.join(adir, "reviews", "orchestrator_decision.json"))["nonvacuous_sha256"])
        self.assertEqual(self.cli("nonvacuous", "ua")[0], 0)
        nv = os.path.join(adir, "nonvacuous.json")
        store.write_json_atomic(nv, dict(jload(nv), status="FAIL"))
        rc, _, err = self.cli("merge", "ua")
        self.assertEqual((rc, "non-vacuity FAIL" in err, self.merged_count()), (1, True, 0), err)
        self.assertEqual(self.cli("dispositions", "ua", "--orchestrator-decision", "after the FAIL")[0], 0)
        self.assertEqual(jload(os.path.join(adir, "reviews", "orchestrator_decision.json"))["nonvacuous_sha256"],
                         hashlib.sha256(rd(nv, "rb")).hexdigest())
        self.assertEqual(self.cli("merge", "ua")[0], 0)

    def one_job_plan(self, pid="demo", uid="j1"):
        p = plan_copy()
        p.update(plan_id=pid, scope=["src/**"], target_branch="main")
        p["units"] = [{"id": uid, "kind": "job", "title": uid, "command": "true", "outputs": [uid + ".out"], "needs": []}]
        approval.approve_plan(self.root, p, "me")

    def test_f3_dispatch_all_passes_after_quota_through(self):
        self.one_job_plan()
        n, _ = X.allocate_attempt(self.root, "demo", "j1")
        X.record_classification(self.root, "demo", "j1", n, "QUOTA")
        self.assertEqual([a["action"] for a in C.compute_actions(self.root)], ["stop"])
        rc, out, err = self.cli("dispatch")
        self.assertEqual((rc, out), (0, ""), err)
        rc, out, err = self.cli("dispatch", "--after-quota")
        self.assertEqual((rc, [(json.loads(l)["unit"], json.loads(l)["attempt"]) for l in out.splitlines()]), (0, [("j1", 2)]), err)

    def test_f4_a_skipped_launch_returns_its_reserved_slot_to_the_next_ready_unit(self):
        self.one_job_plan("demo", "j1")
        self.one_job_plan("zeta", "k1")
        real = X.dispatch_job

        def full(root, pid, unit, *args):
            if unit["id"] == "j1":
                raise X.SlotsFull("plan demo has 1 of its 1 worker slots in use")
            return real(root, pid, unit, *args)
        with mock.patch.object(X, "PROJECT_SLOTS", 1), mock.patch.object(X, "dispatch_job", side_effect=full):
            rc, out, err = self.cli("dispatch")
        self.assertEqual((rc, [json.loads(l)["unit"] for l in out.splitlines()]), (0, ["k1"]), err)
        self.assertIn("skipped j1", err)

    def test_f5_a_pass_round_rejected_on_session_independence_needs_a_new_review(self):
        self.plan()
        self.dispatch_both()
        self.fake_review("ua")
        with mock.patch.object(R, "author_sessions", return_value={"reviewer-ua-1"}):
            acts = [(a["unit"], a["action"]) for a in json.loads(self.cli("next", "--json")[1])]
            self.assertIn(("ua", "review"), acts)
            self.assertNotIn(("ua", "dispositions"), acts)
        self.assertNotIn(("ua", "dispositions"), [(a["unit"], a["action"]) for a in json.loads(self.cli("next", "--json")[1])])

    def test_f5_the_same_rejection_at_the_cap_is_a_stop(self):
        self.plan(caps={"workers": 3, "attempts": 2, "review_rounds": 1})
        self.dispatch_both()
        self.fake_review("ua")
        with mock.patch.object(R, "author_sessions", return_value={"reviewer-ua-1"}):
            acts = [(a["unit"], a["action"]) for a in json.loads(self.cli("next", "--json")[1])]
        self.assertIn(("ua", "stop"), acts)

    def test_f6_dispatch_unit_runs_the_owner_checks(self):
        self.plan()
        p = os.path.join(paths.plan_dir(self.root, "demo"), "approval.json")
        a = jload(p)
        store.write_json_atomic(p, dict(a, boundary=dict(a["boundary"], caps=dict(a["boundary"]["caps"], workers=1))))
        n = len(self.events("OWNER_STOP"))
        rc, out, err = self.cli("dispatch", "--unit", "ua")
        self.assertNotEqual(rc, 0)
        self.assertIn("cap workers=3 is above the approved 1", err)
        self.assertEqual((out, list(X.iter_invocations(self.root))), ("", []))
        ev = self.events("OWNER_STOP")
        self.assertTrue(len(ev) > n and "above the approved" in ev[-1]["reason"] and ev[-1]["command"] == "dispatch")


if __name__ == "__main__":
    unittest.main()
