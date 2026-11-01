import json
import os
import unittest

from helpers import TmpCase, plan_copy

from overseer import approval as A, paths, store


class SplitTests(TmpCase):
    def setUp(self):
        TmpCase.setUp(self)
        A.approve_plan(self.root, plan_copy(), "tester")
        self.digest = A.require_approval(self.root, "demo")["digest"]
        st = A.load_unit_state(self.root, "demo", "alpha")
        st["counters"] = {"attempts": 1, "review_rounds": 2}
        A.save_unit_state(self.root, "demo", "alpha", st)

    def split(self, children, unit="alpha", reason="too big"):
        return A.split_unit(self.root, "demo", unit, {"children": children}, reason)

    def good_children(self):
        return [
            {"id": "alpha-a", "outputs": ["src/alpha.py"], "scope": ["src/alpha.py"]},
            {"id": "alpha-b", "outputs": [{"path": "src/alpha_data.txt", "min_bytes": 1}],
             "scope": ["src/alpha_data.txt", "tests/test_alpha.py"], "title": "Alpha data"},
        ]

    def test_subset_accepted_with_lineage_and_counters(self):
        rev = self.split(self.good_children())
        self.assertEqual(rev["seq"], 1)
        for cid in ("alpha-a", "alpha-b"):
            st = A.load_unit_state(self.root, "demo", cid)
            self.assertEqual(st["lineage"]["parent"], "alpha")
            self.assertEqual(st["lineage"]["reason"], "too big")
            self.assertTrue(st["lineage"]["time"].endswith("Z"))
            self.assertEqual(st["counters"], {"attempts": 1, "review_rounds": 2})
            self.assertEqual(st["authorization"]["plan_digest"], self.digest)
        self.assertEqual(A.load_unit_state(self.root, "demo", "alpha")["closure"]["state"], "split")
        revs = store.read_json(os.path.join(paths.plan_dir(self.root, "demo"), "revisions.json"))
        self.assertEqual(len(revs), 1)
        self.assertEqual(revs[0]["parent"], "alpha")
        self.assertEqual(set(revs[0]["drafts"]), {"alpha-a", "alpha-b"})
        self.assertIn("unit=alpha-a", revs[0]["drafts"]["alpha-a"]["body"])
        self.assertEqual(A.require_approval(self.root, "demo")["digest"], self.digest)

    def test_effective_plan_rewires_dependents(self):
        self.split(self.good_children())
        units = A.effective_plan(self.root, "demo")["units"]
        self.assertEqual([u["id"] for u in units], ["alpha-a", "alpha-b", "beta", "crunch"])
        beta = next(u for u in units if u["id"] == "beta")
        self.assertEqual(beta["needs"], ["alpha-a", "alpha-b"])
        child = units[1]
        self.assertEqual(child["prompt"], "Write alpha.")  # inherited
        self.assertEqual(child["outputs"], [{"path": "src/alpha_data.txt", "min_bytes": 1}])

    def test_child_inherits_parent_predicate_for_bare_path(self):
        self.split([{"id": "alpha-a", "outputs": ["src/alpha.py", "src/alpha_data.txt"]}])
        out = A.effective_plan(self.root, "demo")["units"][0]["outputs"]
        self.assertIn({"path": "src/alpha_data.txt", "min_bytes": 1}, out)

    def test_split_of_child_again(self):
        self.split(self.good_children())
        self.split([{"id": "alpha-b1", "outputs": ["src/alpha_data.txt"]}], unit="alpha-b")
        ids = [u["id"] for u in A.effective_plan(self.root, "demo")["units"]]
        self.assertEqual(ids, ["alpha-a", "alpha-b1", "beta", "crunch"])
        self.assertEqual(A.load_unit_state(self.root, "demo", "alpha-b1")["lineage"]["parent"], "alpha-b")

    def assertStop(self, children, needle):
        with self.assertRaises(A.OwnerStop) as cm:
            self.split(children)
        self.assertTrue(str(cm.exception).startswith("OWNER_STOP: "))
        self.assertIn(needle, str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(paths.plan_dir(self.root, "demo"), "revisions.json")))
        self.assertEqual(A.load_unit_state(self.root, "demo", "alpha")["closure"]["state"], "open")

    def test_scope_growth_refused(self):
        kids = self.good_children()
        kids[0]["scope"] = ["src/alpha.py", "src/other.py"]
        self.assertStop(kids, "adds scope 'src/other.py'")

    def test_output_growth_refused(self):
        kids = self.good_children()
        kids[0]["outputs"] = ["src/alpha.py", "src/new.py"]
        self.assertStop(kids, "adds output 'src/new.py'")

    def test_weaker_predicate_refused(self):
        kids = self.good_children()
        kids[1]["outputs"] = [{"path": "src/alpha_data.txt"}]
        self.assertStop(kids, "changes the predicate of output 'src/alpha_data.txt'")

    def test_uncovered_parent_output_refused(self):
        self.assertStop(self.good_children()[:1], "drops parent output(s) 'src/alpha_data.txt'")

    def test_unsafe_or_reused_id_refused(self):
        for bad in ("../evil", "Alpha", "a/b", "x" * 65):
            kids = self.good_children()
            kids[0]["id"] = bad
            with self.assertRaisesRegex(A.PlanStateError, "does not match"):
                self.split(kids)
        for dup in ("alpha", "beta"):
            kids = self.good_children()
            kids[0]["id"] = dup
            with self.assertRaisesRegex(A.PlanStateError, "already used"):
                self.split(kids)
        kids = self.good_children()
        kids[1]["id"] = "alpha-a"
        with self.assertRaisesRegex(A.PlanStateError, "already used"):
            self.split(kids)

    def test_unsafe_output_path_refused(self):
        kids = self.good_children()
        kids[0]["outputs"] = ["../x"]
        with self.assertRaises(A.PlanStateError):
            self.split(kids)

    def test_closed_unit_and_blank_reason_refused(self):
        st = A.load_unit_state(self.root, "demo", "alpha")
        st["closure"] = {"state": "merged"}
        A.save_unit_state(self.root, "demo", "alpha", st)
        with self.assertRaisesRegex(A.PlanStateError, "closed"):
            self.split(self.good_children())
        st["closure"] = {"state": "open"}
        A.save_unit_state(self.root, "demo", "alpha", st)
        with self.assertRaisesRegex(A.PlanStateError, "reason"):
            self.split(self.good_children(), reason="  ")

    def test_unknown_unit_and_unapproved_plan(self):
        with self.assertRaises(A.PlanStateError):
            self.split(self.good_children(), unit="ghost")
        plan = store.read_json(os.path.join(paths.plan_dir(self.root, "demo"), "plan.json"))
        plan["target_branch"] = "evil"
        store.write_json_atomic(os.path.join(paths.plan_dir(self.root, "demo"), "plan.json"), plan)
        with self.assertRaises(A.ApprovalError):
            self.split(self.good_children())

    def test_children_must_keep_needs_claims_test_command_kind(self):
        beta_kids = lambda **kw: [dict({"id": "beta-a", "outputs": ["src/beta.py"]}, **kw)]
        for kw, needle in (
            ({"needs": []}, "drops need(s) alpha"),
            ({"claims": ["something new"]}, "claim(s) not in parent beta"),
            ({"claims": ["beta works"]}, "drops parent claim(s)"),
            ({"test_command": "true"}, "changes test_command"),
            ({"kind": "job", "command": "x"}, "changes the unit kind"),
        ):
            with self.assertRaises(A.OwnerStop, msg=str(kw)) as cm:
                self.split(beta_kids(**kw), unit="beta")
            self.assertIn(needle, str(cm.exception))
        self.assertEqual(A.load_unit_state(self.root, "demo", "beta")["closure"]["state"], "open")

    def test_children_may_add_needs_and_split_claims(self):
        kids = [{"id": "beta-a", "outputs": ["src/beta.py"], "claims": ["beta works"]},
                {"id": "beta-b", "outputs": ["src/beta.py"], "claims": ["beta is fast"],
                 "needs": ["alpha", "beta-a"]}]
        self.split(kids, unit="beta")
        ids = [u["id"] for u in A.effective_plan(self.root, "demo")["units"]]
        self.assertEqual(ids, ["alpha", "beta-a", "beta-b", "crunch"])

    def test_malformed_child_fields_refused(self):
        for kw in ({"scope": "src/**"}, {"claims": "text"}, {"needs": "alpha"}):
            kids = self.good_children()
            kids[0].update(kw)
            with self.assertRaises(A.PlanStateError, msg=str(kw)):
                self.split(kids)
        kids = self.good_children()
        kids[0]["outputs"] = "src/alpha.py"
        with self.assertRaises(A.PlanStateError):
            self.split(kids)

    def test_reapprove_resets_split_closure_and_archives_children(self):
        st = A.load_unit_state(self.root, "demo", "alpha")
        st["closure"] = {"state": "blocked", "note": "x"}
        A.save_unit_state(self.root, "demo", "alpha", st)
        self.split(self.good_children())
        self.assertEqual(A.load_unit_state(self.root, "demo", "alpha")["closure"]["state"], "split")
        changed = plan_copy()
        changed["units"][1]["prompt"] = "different"
        res = A.approve_plan(self.root, changed, "tester")
        self.assertEqual(res["status"], "updated")
        self.assertEqual(A.load_unit_state(self.root, "demo", "alpha")["closure"], {"state": "open"})
        units = os.path.join(paths.plan_dir(self.root, "demo"), "units")
        self.assertEqual(sorted(os.listdir(units)), ["alpha", "beta", "crunch"])
        arch = os.path.join(paths.plan_dir(self.root, "demo"), "archive")
        stamps = os.listdir(arch)
        self.assertEqual(len(stamps), 1)
        self.assertEqual(sorted(os.listdir(os.path.join(arch, stamps[0]))), ["alpha", "alpha-a", "alpha-b", "beta"])
        self.assertFalse(os.path.exists(os.path.join(paths.plan_dir(self.root, "demo"), "revisions.json")))
        self.assertEqual([u["id"] for u in A.effective_plan(self.root, "demo")["units"]],
                         ["alpha", "beta", "crunch"])

    def test_job_children_keep_command(self):
        kids = [{"id": "crunch-a", "outputs": ["result.txt"], "command": "python evil.py"}]
        with self.assertRaisesRegex(A.OwnerStop, "changes command of crunch"):
            self.split(kids, unit="crunch")
        self.split([{"id": "crunch-a", "outputs": ["result.txt"]}], unit="crunch")

    def test_reapprove_archives_changed_units_only(self):
        for uid in ("alpha", "beta"):
            st = A.load_unit_state(self.root, "demo", uid)
            st["closure"] = {"state": "merged"}
            A.save_unit_state(self.root, "demo", uid, st)
        changed = plan_copy()
        changed["units"][1]["outputs"] = ["src/beta.py", "src/beta2.py"]
        A.approve_plan(self.root, changed, "tester")
        self.assertEqual(A.load_unit_state(self.root, "demo", "alpha")["closure"]["state"], "merged")
        self.assertEqual(A.load_unit_state(self.root, "demo", "beta")["closure"]["state"], "open")
        arch = os.path.join(paths.plan_dir(self.root, "demo"), "archive")
        got = os.path.join(arch, os.listdir(arch)[0], "beta", "unit.json")
        self.assertEqual(store.read_json(got)["closure"]["state"], "merged")

    def test_child_slurm_must_equal_parent(self):
        kid = {"id": "crunch-a", "outputs": ["result.txt"], "slurm": {"time": "10-00", "mem": "1T"}}
        with self.assertRaisesRegex(A.OwnerStop, "changes the slurm block of crunch"):
            self.split([kid], unit="crunch")
        p = plan_copy()
        p["units"][2]["slurm"] = {"time": "02:00:00", "mem": "8G"}
        A.approve_plan(self.root, p, "tester")
        self.split([{"id": "crunch-a", "outputs": ["result.txt"]}], unit="crunch")
        self.assertEqual(A.effective_plan(self.root, "demo")["units"][2]["slurm"], p["units"][2]["slurm"])

    def mark(self, uid, state="merged"):
        st = A.load_unit_state(self.root, "demo", uid)
        st["closure"] = {"state": state}
        A.save_unit_state(self.root, "demo", uid, st)

    def test_boundary_change_archives_every_unit(self):
        self.mark("alpha")
        self.mark("beta")
        p = plan_copy()
        p["target_branch"] = "release"
        A.approve_plan(self.root, p, "tester")
        for uid in ("alpha", "beta"):
            self.assertEqual(A.load_unit_state(self.root, "demo", uid)["closure"]["state"], "open")

    def test_new_unit_reusing_former_child_id_starts_fresh(self):
        self.split(self.good_children())
        self.mark("alpha-a")
        p = plan_copy()
        p["units"].append({"id": "alpha-a", "kind": "job", "title": "Reuse", "command": "true",
                           "outputs": ["z.txt"]})
        A.approve_plan(self.root, p, "tester")
        self.assertEqual(A.load_unit_state(self.root, "demo", "alpha-a")["closure"]["state"], "open")

    def test_interrupted_reapproval_converges(self):
        from unittest import mock
        self.mark("alpha")
        p = plan_copy()
        p["units"][0]["prompt"] = "changed"
        real = store.write_json_atomic
        def boom(path, data):
            if path.endswith("issues.json"):
                raise OSError("disk full")
            return real(path, data)
        with mock.patch.object(store, "write_json_atomic", side_effect=boom):
            with self.assertRaises(OSError):
                A.approve_plan(self.root, p, "tester")
        self.assertEqual(A.load_unit_state(self.root, "demo", "alpha")["closure"]["state"], "open")
        self.assertEqual(A.approve_plan(self.root, p, "tester")["status"], "updated")
        A.require_approval(self.root, "demo")
        self.assertEqual(len(store.read_json(os.path.join(paths.plan_dir(self.root, "demo"),
                                                          "approval_history.json"))), 1)

    def test_interrupted_split_is_healed_and_stale_marker_tolerated(self):
        from unittest import mock
        with mock.patch.object(A, "_apply_split_state", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.split(self.good_children())
        self.assertEqual(len(A.load_revisions(self.root, "demo")), 1)
        self.assertEqual(A.load_unit_state(self.root, "demo", "alpha")["closure"]["state"], "open")
        self.split([{"id": "beta-a", "outputs": ["src/beta.py"]}], unit="beta")
        self.assertEqual(A.load_unit_state(self.root, "demo", "alpha")["closure"]["state"], "split")
        self.assertEqual(A.load_unit_state(self.root, "demo", "alpha-a")["lineage"]["parent"], "alpha")
        self.mark("crunch", "split")  # marked split, no revision names it
        self.split([{"id": "crunch-a", "outputs": ["result.txt"]}], unit="crunch")

    def revisions_path(self):
        return os.path.join(paths.plan_dir(self.root, "demo"), "revisions.json")

    def test_revision_digest_and_tampering(self):
        self.split(self.good_children())
        revs = store.read_json(self.revisions_path())
        self.assertEqual(revs[0]["revision_digest"], A.revision_digest(self.digest, revs[0]))
        A.effective_plan(self.root, "demo")
        revs[0]["reason"] = "edited"
        store.write_json_atomic(self.revisions_path(), revs)
        with self.assertRaisesRegex(A.ApprovalError, "does not match its digest"):
            A.effective_plan(self.root, "demo")
        with self.assertRaisesRegex(A.ApprovalError, "does not match its digest"):
            A.require_approval(self.root, "demo")

    def test_forged_revision_with_valid_digest_fails_split_checks(self):
        self.split(self.good_children())
        revs = store.read_json(self.revisions_path())
        revs[0]["children"][0]["scope"] = ["src/alpha.py", "docs/*.md"]
        revs[0]["revision_digest"] = A.revision_digest(self.digest, revs[0])
        store.write_json_atomic(self.revisions_path(), revs)
        with self.assertRaisesRegex(A.ApprovalError, "adds scope"):
            A.effective_plan(self.root, "demo")
        with self.assertRaises(A.ApprovalError):
            A.require_approval(self.root, "demo")
        r = self.cli("plan", "show", "demo")
        self.assertIn("INVALID", r.stdout)

    def test_cli_split_and_show(self):
        spec = os.path.join(self.tmp, "spec.json")
        with open(spec, "w") as f:
            json.dump({"children": self.good_children()}, f)
        r = self.cli("plan", "split", "alpha", "--into", spec, "--reason", "too big")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(store.read_lock(self.root))
        r = self.cli("plan", "show", "demo")
        self.assertIn("unit alpha-a [code]", r.stdout)
        self.assertIn('"parent": "alpha"', r.stdout)
        self.assertIn("approval: valid", r.stdout)

    def test_cli_split_growth_prints_owner_stop(self):
        kids = self.good_children()
        kids[0]["scope"] = ["src/alpha.py", "docs/*.md"]
        spec = os.path.join(self.tmp, "spec.json")
        with open(spec, "w") as f:
            json.dump(kids, f)
        r = self.cli("plan", "split", "alpha", "--into", spec, "--reason", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("OWNER_STOP:", r.stderr)


if __name__ == "__main__":
    unittest.main()
