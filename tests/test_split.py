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
