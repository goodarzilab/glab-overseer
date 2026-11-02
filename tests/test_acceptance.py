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
        self.upd(self.p("slug", "merge.json"), merged_at="2026-11-01T11:00:00Z")  # now slug merged last
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


if __name__ == "__main__":
    unittest.main()
