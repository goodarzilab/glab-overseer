import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACC = os.path.join(REPO, "acceptance")
ENTRY = os.path.join(REPO, "skills", "glab-overseer", "scripts", "overseer.py")
sys.path.insert(0, ACC)

import check_run as C  # noqa: E402
import make_fixture as M  # noqa: E402

A, B, D = "a" * 40, "b" * 40, "c" * 40
PID = "p1"
UNITS = "plans/%s/units" % PID


def rj(path):
    with open(path) as f:
        return json.load(f)


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


def mf(argv):
    with contextlib.redirect_stdout(io.StringIO()):
        return M.main(argv)


def validate(plan):
    return subprocess.run([sys.executable, ENTRY, "plan", "validate", plan], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)


def git(cwd, *a):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.invalid")
    return subprocess.run(["git"] + list(a), cwd=cwd, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make(self, name="f", *extra):
        out = os.path.join(self.tmp, name)
        mf([out, "--repo", "acme/throwaway", "--partition", "part", "--time", "00:30:00", "--mem", "4G", "--cpus", "2"] + list(extra))
        return out

    def test_repo_and_plan(self):
        out = self.make("f", "--plan-id", "run-a")
        self.assertEqual(len(git(out, "log", "--format=%s", "main").stdout.splitlines()), 1)
        self.assertEqual(git(out, "status", "--porcelain").stdout, b"")  # plan files are git-ignored
        r = validate(os.path.join(out, "plan.json"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        plan = rj(os.path.join(out, "plan.json"))
        self.assertEqual((plan["plan_id"], plan["slurm"]["cpus"]), ("run-a", 2))
        self.assertNotIn("account", plan["slurm"])
        self.assertEqual([u["kind"] for u in plan["units"]], ["code", "code", "job"])

    def test_target_branch(self):
        out = self.make("f", "--plan-id", "run-b", "--target", "run1")
        self.assertEqual(git(out, "symbolic-ref", "--short", "HEAD").stdout.strip(), b"run1")
        self.assertEqual(rj(os.path.join(out, "plan.json"))["target_branch"], "run1")
        r = validate(os.path.join(out, "plan.json"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with self.assertRaises(SystemExit):
            self.make("g", "--target", "Bad/Branch")

    def test_tests_fail_at_base(self):
        out = self.make()
        for pat in ("test_slug.py", "test_count.py"):
            r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", pat], cwd=out,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(r.returncode, 0, pat)

    def test_job_script_and_validator(self):
        out, o = self.make(), os.path.join(self.tmp, "o")
        subprocess.check_call([sys.executable, "scripts/summarize.py"], cwd=out, env=dict(os.environ, OVERSEER_OUT=o))
        csv_path = os.path.join(o, "result.csv")
        with open(csv_path) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "name,words,lines")
        self.assertGreaterEqual(len(lines), 3)
        cmd = rj(os.path.join(out, "plan.json"))["units"][2]["outputs"][0]["validate"]
        self.assertEqual(subprocess.run(["bash", "-c", cmd], env=dict(os.environ, OVERSEER_OUTPUT=csv_path)).returncode, 0)

    def test_changelog_edits_merge_cleanly(self):
        out = self.make()
        p = os.path.join(out, "CHANGELOG.md")
        for br, section, line in (("a", "slug", "- slugify fixed\n"), ("b", "count", "- word_count added\n")):
            git(out, "checkout", "-q", "-b", br, "main")
            with open(p) as f:
                s = f.read()
            end = "\nThat is the end of the %s section." % section
            with open(p, "w") as f:
                f.write(s.replace(end, line.rstrip("\n") + "\n" + end, 1))
            git(out, "commit", "-qam", br)
        git(out, "checkout", "-q", "a")
        git(out, "merge", "-q", "--no-edit", "b")

    def test_placeholders_plan_only_and_no_slurm(self):
        g = os.path.join(self.tmp, "g")
        mf([g, "--no-slurm", "--repo", "acme/throwaway"])
        self.assertNotIn("slurm", rj(os.path.join(g, "plan.json")))
        self.assertEqual(validate(os.path.join(g, "plan.json")).returncode, 0)
        h = os.path.join(self.tmp, "h")
        mf([h])
        tmpl = json.dumps(rj(os.path.join(h, "plan.template.json")))
        for ph in M.PLACEHOLDERS:
            self.assertIn(ph, tmpl)
        self.assertNotEqual(validate(os.path.join(h, "plan.json")).returncode, 0)
        mf([h, "--plan-only", "--repo", "acme/t", "--partition", "p", "--account", "acc", "--time", "01:00:00",
            "--mem", "2G", "--cpus", "1", "--plan-id", "later"])
        plan = rj(os.path.join(h, "plan.json"))
        self.assertEqual((plan["plan_id"], plan["slurm"]["account"]), ("later", "acc"))
        self.assertEqual(validate(os.path.join(h, "plan.json")).returncode, 0)

    def test_job_only_refusals(self):
        out = self.make("j", "--job-only", "--plan-id", "accept-d3")
        plan = rj(os.path.join(out, "plan.json"))
        self.assertEqual([u["id"] for u in plan["units"]], ["summarize", "summarize-lost"])
        self.assertEqual(validate(os.path.join(out, "plan.json")).returncode, 0)
        with self.assertRaises(SystemExit):
            mf([out])  # refuses a non-empty directory


def build_tree(root, backend="slurm", sacct=False):
    """A complete passing run: slug and count code units, summarize job."""
    state = os.path.join(root, ".overseer")
    pd = os.path.join(state, "plans", PID)
    wj(os.path.join(pd, "plan.json"), {"units": [{"id": "slug", "kind": "code"}, {"id": "count", "kind": "code"},
                                                 {"id": "summarize", "kind": "job"}]})
    when = {"slug": ("10:00:00", "10:05:00", "10:20:00"), "count": ("10:01:00", "10:06:00", "10:30:00")}
    events = []
    for i, u in enumerate(("slug", "count")):
        ud, ad = os.path.join(pd, "units", u), os.path.join(pd, "units", u, "attempts", "a1")
        inv = os.path.join(ad, "inv", "01-author")
        wj(os.path.join(inv, "intent.json"), {"argv": ["/usr/bin/claude", "-p"], "backend": backend})
        wj(os.path.join(inv, "launch.json"), {"job_id": "123"} if backend == "slurm" else {})
        wj(os.path.join(inv, "start.json"), {"time": "2026-11-01T%sZ" % when[u][0]})
        wj(os.path.join(inv, "exit.json"), {"rc": 0, "end_time": "2026-11-01T%sZ" % when[u][1]})
        wj(os.path.join(ad, "judgment.json"), {"status": "DONE", "orchestrator_resolved": False, "integrated": u == "count",
                                              "outputs": [{"path": "textkit/%s.py" % u, "ok": True}]})
        wj(os.path.join(ad, "reviews", "r1", "round.json"), {"state": "complete", "reviewer_family": "google",
                                                             "same_family_fallback": False, "calls": [{"state": "ok", "family": "google"}]})
        wj(os.path.join(ad, "nonvacuous.json"), {"status": "PASS"})
        if u == "count":
            wj(os.path.join(ad, "integration.json"), {"integration_base": B, "integrated_candidate": D})
        wj(os.path.join(ud, "merge.json"), {"state": "merged", "pr_head": A, "merge_commit": B, "number": 5 + i,
                                            "integration_base": B if u == "count" else None, "merged_at": "2026-11-01T%sZ" % when[u][2]})
        wj(os.path.join(ud, "unit.json"), {"closure": {"state": "merged"}, "tracker": {"state": "closed", "issue": 1 + i}})
        events += [{"kind": "merge_intent", "unit_id": u}, {"kind": "merged", "unit_id": u}]
    ud = os.path.join(pd, "units", "summarize")
    ad, inv = os.path.join(ud, "attempts", "a1"), os.path.join(ud, "attempts", "a1", "inv", "01-job")
    wj(os.path.join(inv, "intent.json"), {"argv": ["python3"], "backend": backend})
    wj(os.path.join(inv, "exit.json"), {"rc": 0, "end_time": "2026-11-01T10:40:00Z"})
    if backend == "slurm":
        wj(os.path.join(inv, "evidence.json"), {"sources": {"squeue": {"outcome": "absent"}, "scontrol": {"outcome": "known"},
                                                            "sacct": {"outcome": "known" if sacct else "timed_out"}},
                                                "sacct_ever_answered": sacct})
    wj(os.path.join(ad, "judgment.json"), {"status": "DONE", "outputs": [{"path": "result.csv", "ok": True}]})
    wj(os.path.join(ud, "unit.json"), {"tracker": {"state": "closed", "issue": 3}})
    with open(os.path.join(state, "events.jsonl"), "w") as f:
        f.write("".join(json.dumps(e) + "\n" for e in events))


class CheckRunTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.root, True))
        build_tree(self.root)

    def p(self, u, *rest):
        return os.path.join(self.root, ".overseer", UNITS, u, *rest)

    def a1(self, u, *rest):
        return self.p(u, "attempts", "a1", *rest)

    def chk(self, expect=None):
        s = C.evaluate(self.root, PID, {"backend": "slurm"} if expect is None else expect)
        return s, dict((c["id"], c["status"]) for c in s["claims"])

    def upd(self, path, **kw):
        d = rj(path)
        d.update(kw)
        wj(path, d)

    def test_all_pass(self):
        s, st = self.chk()
        self.assertEqual(s["counts"], {"PASS": len(s["claims"]), "FAIL": 0, "INCOMPLETE": 0}, s["claims"])
        self.assertNotIn("slug.integration", st)
        self.assertTrue(all(not os.path.isabs(c["evidence"]) for c in s["claims"]))

    def test_overlap(self):
        self.upd(self.a1("count", "inv", "01-author", "start.json"), time="2026-11-01T10:05:00Z")
        self.assertEqual(self.chk()[1]["overlap"], "FAIL")
        os.unlink(self.a1("count", "inv", "01-author", "exit.json"))
        self.assertEqual(self.chk()[1]["overlap"], "INCOMPLETE")

    def test_judgment(self):
        p = self.a1("slug", "judgment.json")
        for kw, want in (({"status": "FAILED"}, "FAIL"), ({"status": "DONE", "orchestrator_resolved": True}, "FAIL"),
                         ({"status": "DONE", "orchestrator_resolved": False, "outputs": [{"path": "x", "ok": False}]}, "FAIL")):
            self.upd(p, **kw)
            self.assertEqual(self.chk()[1]["slug.judgment"], want, kw)
        os.unlink(p)
        self.assertEqual(self.chk()[1]["slug.judgment"], "INCOMPLETE")

    def test_review(self):
        p = self.a1("slug", "reviews", "r1", "round.json")
        self.upd(p, same_family_fallback=True)
        self.assertEqual(self.chk()[1]["slug.review"], "FAIL")
        self.upd(p, same_family_fallback=False, calls=[{"state": "ok", "family": "claude"}])
        self.assertEqual(self.chk()[1]["slug.review"], "FAIL")
        self.upd(p, state="failed")
        self.assertEqual(self.chk()[1]["slug.review"], "INCOMPLETE")

    def test_nonvacuity(self):
        p = self.a1("slug", "nonvacuous.json")
        self.upd(p, status="NOT_APPLICABLE")
        self.assertEqual(self.chk()[1]["slug.nonvacuity"], "PASS")
        self.upd(p, status="FAIL")
        self.assertEqual(self.chk()[1]["slug.nonvacuity"], "FAIL")
        wj(self.a1("slug", "reviews", "orchestrator_decision.json"), {"orchestrator_decision": True})
        s, st = self.chk()
        self.assertEqual((st["slug.nonvacuity"], len(s["orchestrator_resolved"])), ("PASS", 1))
        os.unlink(p)
        self.assertEqual(self.chk()[1]["slug.nonvacuity"], "INCOMPLETE")

    def test_merge_and_tracker(self):
        self.upd(self.p("slug", "merge.json"), merge_commit=None)
        self.assertEqual(self.chk()[1]["slug.merge"], "FAIL")
        os.unlink(self.p("slug", "merge.json"))
        self.assertEqual(self.chk()[1]["slug.merge"], "INCOMPLETE")
        for tr, want in (({"state": "pending", "issue": 1}, "FAIL"), ({"state": "open", "issue": 1}, "INCOMPLETE")):
            self.upd(self.p("slug", "unit.json"), tracker=tr)
            self.assertEqual(self.chk()[1]["slug.tracker"], want)

    def test_integration(self):
        p = self.a1("count", "integration.json")
        os.unlink(p)
        self.assertEqual(self.chk()[1]["count.integration"], "FAIL")
        wj(p, {"integration_base": B, "integrated_candidate": D})
        self.upd(self.a1("count", "judgment.json"), integrated=False)
        self.assertEqual(self.chk()[1]["count.integration"], "FAIL")
        with open(os.path.join(self.root, ".overseer", "events.jsonl"), "w") as f:  # now slug merged last
            f.write("".join(json.dumps({"kind": "merged", "unit_id": u}) + "\n" for u in ("count", "slug")))
        self.assertEqual(self.chk()[1]["slug.integration"], "FAIL")
        os.unlink(self.p("slug", "merge.json"))
        self.assertEqual(self.chk()[1]["integration"], "INCOMPLETE")

    def test_job_sources_and_sacct(self):
        s, st = self.chk({"job_sources": ["scontrol", "wrapper"], "sacct": "unavailable"})
        self.assertEqual((st["summarize.sources"], st["summarize.sacct"]), ("PASS", "PASS"))
        self.assertIn("scontrol", [c for c in s["claims"] if c["id"] == "summarize.sources"][0]["detail"])
        self.assertEqual(self.chk({"job_sources": ["sacct"]})[1]["summarize.sources"], "FAIL")
        self.assertEqual(self.chk({"sacct": "answered"})[1]["summarize.sacct"], "INCOMPLETE")
        build_tree(self.root, sacct=True)
        st = self.chk({"sacct": "answered", "job_sources": ["sacct"]})[1]
        self.assertEqual((st["summarize.sacct"], st["summarize.sources"]), ("PASS", "PASS"))
        self.assertEqual(self.chk({"sacct": "unavailable"})[1]["summarize.sacct"], "INCOMPLETE")
        for n in ("evidence.json", "exit.json"):
            os.unlink(self.a1("summarize", "inv", "01-job", n))
        self.assertEqual(self.chk()[1]["summarize.sources"], "INCOMPLETE")

    def test_uncertain_resolved(self):
        exp = {"uncertain_units": ["summarize"]}
        self.assertEqual(self.chk(exp)[1]["summarize.resolved"], "INCOMPLETE")
        self.upd(self.a1("summarize", "judgment.json"), orchestrator_resolved=True, reason="outputs verified", evidence="3 rows")
        s, st = self.chk(exp)
        self.assertEqual(st["summarize.resolved"], "PASS")
        self.assertEqual(s["orchestrator_resolved"][0]["unit"], "summarize")

    def test_local_backend(self):
        shutil.rmtree(self.root)
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.root, True))
        build_tree(self.root, backend="local")
        exp = {"backend": "local", "shim_log": "shim.log"}
        self.assertEqual(self.chk(exp)[1]["no_slurm"], "INCOMPLETE")  # named log absent
        open(os.path.join(self.root, "shim.log"), "w").close()
        self.assertEqual(self.chk(exp)[1]["no_slurm"], "PASS")
        self.assertEqual(self.chk({"backend": "local"})[1]["no_slurm"], "INCOMPLETE")
        with open(os.path.join(self.root, "shim.log"), "w") as f:
            f.write("sbatch --version\n")
        self.assertEqual(self.chk(exp)[1]["no_slurm"], "FAIL")
        self.assertEqual(self.chk({"backend": "slurm"})[1]["backend"], "FAIL")

    def test_recovery(self):
        exp = {"recovery": {"unit": "slug", "merge_log": "gh.log"}}
        self.assertEqual(self.chk(exp)[1]["recovery.slug"], "INCOMPLETE")
        with open(os.path.join(self.root, "gh.log"), "w") as f:
            f.write("gh pr view 5\ngh pr merge 5 --squash\ngh pr view 5\n")
        self.assertEqual(self.chk(exp)[1]["recovery.slug"], "INCOMPLETE")  # no KILLED: not exercised
        with open(os.path.join(self.root, "gh.log"), "w") as f:
            f.write("gh pr view 5\ngh pr merge 5 --squash\nKILLED 77\ngh pr view 5\n")
        self.assertEqual(self.chk(exp)[1]["recovery.slug"], "PASS")
        with open(os.path.join(self.root, "gh.log"), "a") as f:
            f.write("gh pr merge 5 --squash\n")
        self.assertEqual(self.chk(exp)[1]["recovery.slug"], "FAIL")

    def test_cli_and_missing_plan(self):
        self.assertEqual(C.evaluate(self.root, "nope", {})["claims"][0]["status"], "INCOMPLETE")
        ex = os.path.join(self.root, "expect.json")
        wj(ex, {"backend": "slurm"})
        cmd = [sys.executable, os.path.join(ACC, "check_run.py"), self.root, PID, "--expect", ex]
        r = subprocess.run(cmd + ["--json"], stdout=subprocess.PIPE, universal_newlines=True)
        d = json.loads(r.stdout)
        self.assertEqual((r.returncode, d["counts"]["PASS"]), (0, len(d["claims"])))
        r = subprocess.run(cmd, stdout=subprocess.PIPE, universal_newlines=True)
        self.assertTrue(all(len(l.split()) >= 3 and l.split()[0] in ("PASS", "FAIL", "INCOMPLETE") for l in r.stdout.splitlines()))


class ReviewRound1Tests(unittest.TestCase):
    """One test per round-1 review finding (F1 to F12); see .swarm/u10-review-r1.txt."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.root, True))
        build_tree(self.root)

    def u(self, u, *rest):
        return os.path.join(self.root, ".overseer", UNITS, u, *rest)

    def claims(self, expect=None):
        s = C.evaluate(self.root, PID, {"backend": "slurm"} if expect is None else expect)
        return dict((c["id"], c) for c in s["claims"]), s

    def st(self, cid, expect=None):
        return self.claims(expect)[0][cid]["status"]

    def test_F1_overlap_counts_earlier_attempts(self):
        a, b = self.u("slug", "attempts", "a1"), self.u("slug", "attempts", "a2")
        shutil.copytree(a, b)
        wj(os.path.join(a, "judgment.json"), {"status": "FAILED"})
        wj(os.path.join(b, "inv/01-author/start.json"), {"time": "2026-11-01T10:10:00Z"})
        wj(os.path.join(b, "inv/01-author/exit.json"), {"rc": 0, "end_time": "2026-11-01T10:15:00Z"})
        self.assertEqual(self.st("overlap"), "PASS")
        os.unlink(os.path.join(a, "inv/01-author/exit.json"))  # an earlier attempt lost its record
        self.assertEqual(self.st("overlap"), "INCOMPLETE")  # no complete overlapping pair, a record missing
        wj(os.path.join(a, "inv/01-author/exit.json"), {"rc": 0, "end_time": "2026-11-01T09:01:00Z"})
        wj(os.path.join(a, "inv/01-author/start.json"), {"time": "2026-11-01T09:00:00Z"})
        self.assertEqual(self.st("overlap"), "FAIL")  # complete records, no pair overlaps

    def test_F2_missing_shim_log_is_incomplete(self):
        shutil.rmtree(self.root)
        self.root = tempfile.mkdtemp()
        build_tree(self.root, backend="local")
        self.assertEqual(self.st("no_slurm", {"backend": "local", "shim_log": "missing.log"}), "INCOMPLETE")
        os.mkdir(os.path.join(self.root, "dir.log"))  # unreadable as a log
        self.assertEqual(self.st("no_slurm", {"backend": "local", "shim_log": "dir.log"}), "INCOMPLETE")
        open(os.path.join(self.root, "empty.log"), "w").close()
        self.assertEqual(self.st("no_slurm", {"backend": "local", "shim_log": "empty.log"}), "PASS")

    def test_F3_sacct_unavailable_needs_evidence(self):
        a = self.u("summarize", "attempts", "a1")
        exp = {"sacct": "unavailable"}
        self.assertEqual(self.st("summarize.sacct", exp), "PASS")
        for n in ("evidence.json", "exit.json"):
            os.unlink(os.path.join(a, "inv", "01-job", n))
        wj(os.path.join(a, "judgment.json"), {"status": "DONE", "outputs": [{"path": "result.csv", "ok": True}],
                                              "orchestrator_resolved": True, "reason": "inspected", "evidence": "output"})
        self.assertEqual(self.st("summarize.sacct", exp), "INCOMPLETE")
        wj(os.path.join(a, "inv", "01-job", "evidence.json"), {"sources": {"scontrol": {"outcome": "known"}}})  # no sacct_ever_answered
        self.assertEqual(self.st("summarize.sacct", exp), "INCOMPLETE")

    def test_F4_later_fallback_round_fails(self):
        wj(self.u("slug", "attempts/a1/reviews/r2/round.json"), {"state": "complete", "reviewer_family": "claude",
                                                                "same_family_fallback": True, "calls": [{"state": "ok", "family": "claude"}]})
        c = self.claims()[0]["slug.review"]
        self.assertEqual(c["status"], "FAIL")
        self.assertIn("r2", c["evidence"])
        # a fallback in an earlier attempt also fails
        shutil.copytree(self.u("slug", "attempts/a1"), self.u("slug", "attempts/a2"))
        shutil.rmtree(self.u("slug", "attempts/a2/reviews/r2"))
        self.assertEqual(self.st("slug.review"), "FAIL")

    def test_F5_run_identity_families(self):
        self.assertEqual(self.st("slug.review", {"run": "run3"}), "FAIL")  # claude author, google reviewer
        self.assertEqual(self.st("slug.review", {"run": "run1"}), "PASS")
        self.assertEqual(self.st("slug.review", {"run": "run2"}), "PASS")
        self.assertEqual(self.st("slug.review", {"author_family": "google"}), "FAIL")
        self.assertEqual(self.st("slug.review", {"reviewer_family": "claude"}), "FAIL")
        self.assertEqual(self.st("slug.review", {"author_family": "claude", "reviewer_family": "google"}), "PASS")
        for u in ("slug", "count"):
            wj(self.u(u, "attempts/a1/inv/01-author/intent.json"), {"argv": ["/usr/bin/agy"], "backend": "slurm"})
            wj(self.u(u, "attempts/a1/reviews/r1/round.json"), {"state": "complete", "reviewer_family": "claude", "same_family_fallback": False,
                                                              "calls": [{"state": "ok", "family": "claude"}]})
        self.assertEqual(self.st("slug.review", {"run": "run3"}), "PASS")
        self.assertEqual(self.st("slug.review", {"run": "run1"}), "FAIL")

    def test_F6_recovery_needs_kill(self):
        exp = {"recovery": {"unit": "slug", "merge_log": "gh.log"}}

        def log(text):
            with open(os.path.join(self.root, "gh.log"), "w") as f:
                f.write(text)
        log("gh pr view 5\ngh pr merge 5 --squash\ngh pr view 5\n")
        self.assertEqual(self.st("recovery.slug", exp), "INCOMPLETE")
        log("gh pr merge 5 --squash\nKILLED 9\ngh pr view 5\n")
        self.assertEqual(self.st("recovery.slug", exp), "PASS")
        log("KILLED 9\ngh pr merge 5 --squash\n")
        self.assertEqual(self.st("recovery.slug", exp), "FAIL")
        log("gh pr merge 5 --squash\nKILLED 9\ngh pr merge 5 --squash\n")
        self.assertEqual(self.st("recovery.slug", exp), "FAIL")
        log("gh pr merge 5 --squash\nKILLED 9\n")
        wj(self.u("slug", "merge.json"), {"state": "merged", "pr_head": A, "merge_commit": None})
        self.assertEqual(self.st("recovery.slug", exp), "FAIL")

    def test_F7_F8_runbook_text(self):
        with open(os.path.join(ACC, "RUNBOOK.md")) as f:
            text = f.read()
        run2 = text[text.index("## 3. Run 2"):text.index("## 4. Run 3")]
        self.assertIn("steps 1 to 5 and 7 to 17", run2)
        self.assertNotIn("1 to 14, 16", run2)
        steps = text[text.index("## 1. Run 1"):text.index("## 2. ")]
        self.assertNotIn("accept-run1", steps)
        self.assertIn("$PLAN", steps)
        self.assertIn("--plan-id $PLAN", text)
        run3 = text[text.index("## 4. Run 3"):text.index("## 5. ")]
        self.assertIn("PLAN=accept-run3", run3)

    def test_F9_job_only_plan_has_no_overlap_claim(self):
        pd = os.path.join(self.root, ".overseer", "plans", PID)
        wj(os.path.join(pd, "plan.json"), {"units": [{"id": "summarize", "kind": "job"}]})
        shutil.rmtree(self.u("slug"))
        shutil.rmtree(self.u("count"))
        cl, s = self.claims()
        self.assertNotIn("overlap", cl)
        self.assertEqual(s["counts"]["INCOMPLETE"] + s["counts"]["FAIL"], 0, s["claims"])

    def test_F10_missing_unit_json_is_incomplete_merge(self):
        os.unlink(self.u("slug", "unit.json"))
        cl = self.claims()[0]
        self.assertEqual((cl["slug.merge"]["status"], cl["slug.tracker"]["status"]), ("INCOMPLETE", "INCOMPLETE"))
        with open(self.u("slug", "unit.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(self.st("slug.merge"), "INCOMPLETE")

    def test_F11_job_variables_documented(self):
        with open(os.path.join(ACC, "RUNBOOK.md")) as f:
            text = f.read()
        self.assertIn("OVERSEER_OUT", text)
        self.assertIn("OVERSEER_OUTPUT", text)
        line = [l for l in text.splitlines() if "OVERSEER_OUTPUT" in l][0]
        self.assertIn("OVERSEER_OUT", line.replace("OVERSEER_OUTPUT", ""))  # one sentence names both

    def test_F12_malformed_records_never_crash(self):
        p = self.u("slug", "attempts/a1/judgment.json")
        wj(p, {"status": "DONE", "orchestrator_resolved": False, "outputs": ["textkit/slug.py"]})
        self.assertEqual(self.st("slug.judgment"), "INCOMPLETE")
        self.assertIn("outputs entry", self.claims()[0]["slug.judgment"]["detail"])
        wj(p, {"status": "DONE", "outputs": "textkit/slug.py"})
        self.assertEqual(self.st("slug.judgment"), "INCOMPLETE")
        wj(self.u("slug", "unit.json"), {"closure": "merged", "tracker": 5})
        cl, s = self.claims({"backend": "slurm", "job_sources": 3, "uncertain_units": 7, "sacct": "unavailable"})
        self.assertEqual(cl["slug.merge"]["status"], "FAIL")
        self.assertEqual(cl["slug.tracker"]["status"], "INCOMPLETE")
        for k in ("slug.review", "count.review", "summarize.sources"):
            self.assertIn(cl[k]["status"], ("PASS", "FAIL", "INCOMPLETE"))
        for name, val in (("round.json", {"state": "complete", "calls": 5}), ("nonvacuous.json", {"status": []})):
            wj(self.u("slug", "attempts/a1/reviews/r1" if name == "round.json" else "attempts/a1", name), val)
            self.claims()
        wj(self.u("count", "attempts/a1/inv/01-author/intent.json"), {"argv": [5]})
        wj(self.u("summarize", "attempts/a1/inv/01-job/evidence.json"), {"sources": [1], "sacct_ever_answered": "x"})
        self.assertTrue(self.claims({"overlap": [1, 2], "recovery": {"unit": "slug", "merge_log": 4}, "integration_units": [9]})[1]["claims"])


class ReviewRound2Tests(unittest.TestCase):
    """One test per round-2 review finding (F1 to F7); see .swarm/u10-review-r2.txt."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.root, True))
        build_tree(self.root)
        with open(os.path.join(ACC, "RUNBOOK.md")) as f:
            self.book = f.read()

    def u(self, u, *rest):
        return os.path.join(self.root, ".overseer", UNITS, u, *rest)

    def claims(self, expect=None):
        s = C.evaluate(self.root, PID, {"backend": "slurm"} if expect is None else expect)
        return dict((c["id"], c) for c in s["claims"]), s

    def section(self, start, end):
        return self.book[self.book.index(start):self.book.index(end)]

    def test_F1_missing_plan_json_is_incomplete(self):
        pj = os.path.join(self.root, ".overseer", "plans", PID, "plan.json")
        os.unlink(pj)
        cl, s = self.claims()
        self.assertEqual(cl["plan"]["status"], "INCOMPLETE")
        self.assertGreater(s["counts"]["INCOMPLETE"], 0)
        with open(pj, "w") as f:
            f.write("[1")
        self.assertEqual(self.claims()[0]["plan"]["status"], "INCOMPLETE")
        ex = os.path.join(self.root, "expect.json")
        wj(ex, {"backend": "slurm"})
        r = subprocess.run([sys.executable, os.path.join(ACC, "check_run.py"), self.root, PID, "--expect", ex],
                           stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(r.returncode, 3)

    def test_F2_review_must_cover_current_attempt(self):
        base = self.u("slug", "attempts")
        shutil.copytree(os.path.join(base, "a1"), os.path.join(base, "a2"))
        shutil.rmtree(os.path.join(base, "a2", "reviews"))
        c = self.claims({"run": "run1"})[0]["slug.review"]
        self.assertEqual(c["status"], "INCOMPLETE")
        self.assertIn("a2", c["detail"])
        wj(os.path.join(base, "a2", "reviews", "r1", "round.json"), {"state": "complete", "reviewer_family": "google",
                                                                    "same_family_fallback": False, "calls": [{"state": "ok", "family": "google"}]})
        self.assertEqual(self.claims({"run": "run1"})[0]["slug.review"]["status"], "PASS")
        # an earlier-attempt fallback still fails
        wj(os.path.join(base, "a1", "reviews", "r2", "round.json"), {"state": "complete", "reviewer_family": "claude",
                                                                    "same_family_fallback": True, "calls": [{"state": "ok", "family": "claude"}]})
        self.assertEqual(self.claims({"run": "run1"})[0]["slug.review"]["status"], "FAIL")

    def test_F2_merged_attempt_is_the_reviewed_one(self):
        base = self.u("slug", "attempts")
        shutil.copytree(os.path.join(base, "a1"), os.path.join(base, "a2"))
        shutil.rmtree(os.path.join(base, "a2", "reviews"))
        wj(os.path.join(base, "a1", "judgment.json"), {"status": "DONE", "candidate": "c1", "outputs": [{"path": "x", "ok": True}]})
        wj(os.path.join(base, "a2", "judgment.json"), {"status": "DONE", "candidate": "c2", "outputs": [{"path": "x", "ok": True}]})
        m = rj(self.u("slug", "merge.json"))
        wj(self.u("slug", "merge.json"), dict(m, candidate="c1"))
        self.assertEqual(self.claims({"run": "run1"})[0]["slug.review"]["status"], "PASS")  # a1 was merged and reviewed

    def test_F3_sacct_needs_an_entry(self):
        ev = self.u("summarize", "attempts/a1/inv/01-job/evidence.json")
        for srcs in ({"scontrol": {"outcome": "known"}}, {"scontrol": {"outcome": "known"}, "sacct": None}):
            wj(ev, {"sources": srcs, "sacct_ever_answered": False})
            self.assertEqual(self.claims({"sacct": "unavailable"})[0]["summarize.sacct"]["status"], "INCOMPLETE")
        wj(ev, {"sources": {"scontrol": {"outcome": "known"}, "sacct": {"outcome": "error"}}, "sacct_ever_answered": False})
        self.assertEqual(self.claims({"sacct": "unavailable"})[0]["summarize.sacct"]["status"], "PASS")

    def test_F4_merge_order_is_event_order(self):
        for u in ("slug", "count"):
            p = self.u(u, "merge.json")
            wj(p, dict(rj(p), merged_at="2026-11-01T10:20:00Z"))
        cl = self.claims({})[0]
        self.assertEqual(cl["count.integration"]["status"], "PASS")
        self.assertNotIn("slug.integration", cl)
        with open(os.path.join(self.root, ".overseer", "events.jsonl"), "w") as f:  # count first, slug second, equal timestamps
            f.write("".join(json.dumps({"kind": "merged", "unit_id": u}) + "\n" for u in ("count", "slug")))
        self.assertEqual(self.claims({})[0]["slug.integration"]["status"], "FAIL")
        os.unlink(os.path.join(self.root, ".overseer", "events.jsonl"))
        self.assertEqual(self.claims({})[0]["integration"]["status"], "INCOMPLETE")

    def test_F5_sections_change_directory(self):
        d3 = self.section("## 2. ", "## 3. ")
        r2 = self.section("## 3. ", "## 4. ")
        r3 = self.section("## 4. ", "## 5. ")
        for text, proj, plan in ((d3, "$PROJ2", "accept-d3"), (r2, "$PROJ3", "accept-run2"), (r3, "$PROJ4", "accept-run3")):
            self.assertIn("cd %s && PLAN=%s" % (proj, plan), text)

    def test_F6_each_run_pushes_its_target(self):
        for sec in (self.section("## 2. ", "## 3. "), self.section("## 3. ", "## 4. "), self.section("## 4. ", "## 5. ")):
            self.assertIn("git remote add origin https://github.com/$REPO.git", sec)
        self.assertIn("git push -u origin d3", self.section("## 2. ", "## 3. "))
        self.assertIn("git push -u origin run2", self.section("## 3. ", "## 4. "))
        self.assertIn("git push -u origin run3", self.section("## 4. ", "## 5. "))
        self.assertEqual(self.book.count("gh repo create"), 1)

    def test_F7_run2_path_has_tools(self):
        sec = self.section("## 3. ", "## 4. ")
        loop = [l for l in sec.splitlines() if l.startswith("for t in python3")][0].split()
        for tool in ("tee", "node", "readlink", "tr", "awk"):
            self.assertIn(tool, loop)
        self.assertIn("PATH=$BIN:$SH claude --version && PATH=$BIN:$SH agy --version", sec)


if __name__ == "__main__":
    unittest.main()
