import copy
import json
import os
import unittest

from helpers import TmpCase, plan_copy

from overseer import approval as A, paths, plan as P, store


def pfile(root, name="plan.json"):
    return os.path.join(paths.plan_dir(root, "demo"), name)


class ApproveTests(TmpCase):
    def approve(self, plan=None, approver="tester"):
        return A.approve_plan(self.root, plan or plan_copy(), approver)

    def test_writes_files_and_drafts(self):
        res = self.approve()
        self.assertEqual(res["status"], "approved")
        self.assertEqual(res["titles"], ["Add alpha", "Add beta", "Crunch"])
        for n in ("plan.json", "issues.json", "approval.json"):
            self.assertTrue(os.path.exists(pfile(self.root, n)), n)
        issues = store.read_json(pfile(self.root, "issues.json"))
        self.assertEqual(set(issues), {"drafts", "filed"})
        body = issues["drafts"]["beta"]["body"]
        self.assertIn("<!-- overseer plan=demo unit=beta -->", body)
        self.assertIn("- beta is fast", body)
        self.assertIn("- `src/beta.py`", body)
        self.assertIn("- alpha", body)
        ap = store.read_json(pfile(self.root, "approval.json"))
        self.assertEqual(ap["approver"], "tester")
        self.assertEqual(ap["digest"], res["digest"])
        self.assertEqual(len(ap["digest"]), 64)
        self.assertEqual(ap["boundary"]["repo"], "lab-org/demo-repo")
        self.assertEqual(ap["boundary"]["caps"], {"workers": 3, "attempts": 2, "review_rounds": 3})
        self.assertEqual(ap["boundary"]["slurm"]["partition"], "batch-cpu")
        A.require_approval(self.root, "demo")

    def test_blank_approver_refused(self):
        for bad in ("", "   ", None):
            with self.assertRaises(A.ApprovalError):
                self.approve(approver=bad)
        self.assertFalse(os.path.exists(paths.plans_dir(self.root)))

    def test_invalid_plan_refused(self):
        p = plan_copy()
        p["units"][0]["kind"] = "x"
        with self.assertRaises(A.PlanInvalid):
            self.approve(p)

    def test_reapprove_identical_changes_nothing(self):
        self.approve()
        def read(n):
            with open(pfile(self.root, n)) as f:
                return f.read()
        snap = dict((n, read(n)) for n in ("plan.json", "issues.json", "approval.json"))
        events = store.read_events(self.root)
        self.assertEqual(self.approve(approver="someone else")["status"], "unchanged")
        for n, text in snap.items():
            self.assertEqual(read(n), text)
        self.assertEqual(store.read_events(self.root), events)
        self.assertFalse(os.path.exists(pfile(self.root, "approval_history.json")))

    def test_reapprove_changed_appends_history(self):
        first = self.approve()
        p = plan_copy()
        p["units"][0]["prompt"] = "Write alpha differently."
        second = self.approve(p, approver="other")
        self.assertEqual(second["status"], "updated")
        self.assertNotEqual(first["digest"], second["digest"])
        hist = store.read_json(pfile(self.root, "approval_history.json"))
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["approval"]["digest"], first["digest"])
        self.assertEqual(hist[0]["approval"]["approver"], "tester")
        self.assertEqual(store.read_json(pfile(self.root, "approval.json"))["approver"], "other")

    def test_digest_covers_every_semantic_field(self):
        base = plan_copy()
        def digest(p):
            n = P.normalize_plan(p)
            return P.approval_digest(n, P.render_drafts(n))
        d0 = digest(base)
        edits = [
            lambda p: p.update(repo="lab-org/other"),
            lambda p: p.update(target_branch="dev"),
            lambda p: p["scope"].append("extra/**"),
            lambda p: p.update(caps={"workers": 2}),
            lambda p: p["slurm"].update(partition="other-cpu"),
            lambda p: p.update(test_globs=["tests/**"]),
            lambda p: p["units"][0].update(title="New title"),
            lambda p: p["units"][0].update(prompt="changed"),
            lambda p: p["units"][0].update(claims=["a", "b"]),
            lambda p: p["units"][0].update(test_command="true"),
            lambda p: p["units"][0].update(outputs=["src/alpha.py"]),
            lambda p: p["units"][0].update(scope=["src/alpha.py"]),
            lambda p: p["units"][1].update(needs=[]),
            lambda p: p["units"][2].update(command="python other.py"),
            lambda p: p["units"][2].update(slurm={"time": "02:00:00"}),
            lambda p: p["units"][2].update(outputs=[{"path": "result.txt", "min_bytes": 5}]),
        ]
        seen = {d0}
        for i, edit in enumerate(edits):
            p = copy.deepcopy(base)
            edit(p)
            d = digest(p)
            self.assertNotIn(d, seen, "edit %d did not change the digest" % i)
            seen.add(d)
        reordered = copy.deepcopy(base)
        reordered["scope"].reverse()
        self.assertEqual(digest(reordered), d0)

    def test_unit_json_and_filed_issues_do_not_change_digest(self):
        res = self.approve()
        st = A.load_unit_state(self.root, "demo", "alpha")
        st["execution"] = {"state": "running"}
        st["counters"]["attempts"] = 1
        st["tracker"] = {"issue": 12}
        A.save_unit_state(self.root, "demo", "alpha", st)
        issues = store.read_json(pfile(self.root, "issues.json"))
        issues["filed"] = {"alpha": 12}
        store.write_json_atomic(pfile(self.root, "issues.json"), issues)
        self.assertEqual(A.require_approval(self.root, "demo")["digest"], res["digest"])
        self.assertEqual(self.approve()["status"], "unchanged")

    def test_unit_slurm_override_kept_for_code_units_and_in_digest(self):
        p = plan_copy()
        p["units"][0]["slurm"] = {"time": "02:00:00", "mem_per_cpu": "2G"}
        self.approve(p)
        stored = store.read_json(pfile(self.root))
        self.assertEqual(stored["units"][0]["slurm"], {"time": "02:00:00", "mem_per_cpu": "2G"})
        A.require_approval(self.root, "demo")
        stored["units"][0]["slurm"]["time"] = "99:00:00"
        store.write_json_atomic(pfile(self.root), stored)
        with self.assertRaises(A.ApprovalError):
            A.require_approval(self.root, "demo")

    def test_tampered_plan_refused(self):
        self.approve()
        plan = store.read_json(pfile(self.root))
        plan["units"][0]["scope"] = ["src/**", "tests/**"]
        store.write_json_atomic(pfile(self.root), plan)
        with self.assertRaisesRegex(A.ApprovalError, "no longer match"):
            A.require_approval(self.root, "demo")

    def test_tampered_drafts_refused(self):
        self.approve()
        issues = store.read_json(pfile(self.root, "issues.json"))
        issues["drafts"]["alpha"]["title"] = "Something else"
        store.write_json_atomic(pfile(self.root, "issues.json"), issues)
        with self.assertRaises(A.ApprovalError):
            A.require_approval(self.root, "demo")

    def test_missing_approval_refused(self):
        with self.assertRaises(A.ApprovalError):
            A.require_approval(self.root, "demo")
        with self.assertRaises(paths.InvalidId):
            A.require_approval(self.root, "../demo")

    def test_reapprove_repairs_tampered_plan(self):
        self.approve()
        plan = store.read_json(pfile(self.root))
        plan["target_branch"] = "evil"
        store.write_json_atomic(pfile(self.root), plan)
        self.assertEqual(self.approve()["status"], "approved")
        A.require_approval(self.root, "demo")

    def test_unit_state_facets(self):
        self.approve()
        st = A.load_unit_state(self.root, "demo", "alpha")
        for k in ("authorization", "execution", "evidence", "closure", "tracker", "lineage", "counters"):
            self.assertIn(k, st)
        self.assertEqual(st["counters"], {"attempts": 0, "review_rounds": 0})
        self.assertEqual(st["authorization"]["plan_digest"],
                         store.read_json(pfile(self.root, "approval.json"))["digest"])
        self.assertTrue(os.path.exists(os.path.join(paths.unit_dir(self.root, "demo", "alpha"), "unit.json")))

    def test_cli_approve_show(self):
        r = self.cli("plan", "approve", self.write_plan(), "--approver", "  ")
        self.assertEqual(r.returncode, 1)
        r = self.cli("plan", "approve", self.write_plan(), "--approver", "tester")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertIn("plan: demo", lines)
        self.assertIn("issue: Add alpha", lines)
        self.assertIn("issues: 3", lines)
        self.assertIsNone(store.read_lock(self.root))
        r = self.cli("plan", "show", "demo")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("plan: demo", r.stdout)
        self.assertIn("approver: tester", r.stdout)
        self.assertIn("approval: valid", r.stdout)
        self.assertIn("unit alpha [code] Add alpha", r.stdout)
        for f in ("authorization", "execution", "evidence", "closure", "tracker"):
            self.assertIn("  %s: " % f, r.stdout)
        self.assertEqual(self.cli("plan", "show", "missing").returncode, 1)


if __name__ == "__main__":
    unittest.main()
