import unittest

from helpers import TmpCase, plan_copy

from overseer import plan as P


def errs(plan):
    return P.validate_plan(plan)[0]


class ValidateTests(unittest.TestCase):
    def assertError(self, plan, needle):
        e = errs(plan)
        self.assertTrue(any(needle in line for line in e), "%r not in %r" % (needle, e))

    def unit(self, plan, uid):
        return next(u for u in plan["units"] if u["id"] == uid)

    def test_base_plan_valid(self):
        self.assertEqual(P.validate_plan(plan_copy()), ([], []))

    def test_bad_plan_id(self):
        for bad in ("Bad", "../x", "a b", "", "-a", "a" * 65):
            p = plan_copy()
            p["plan_id"] = bad
            self.assertError(p, "plan_id %r does not match" % bad)

    def test_bad_unit_id(self):
        for bad in ("A", "a/b", "..", "x" * 65):
            p = plan_copy()
            self.unit(p, "alpha")["id"] = bad
            self.assertError(p, "unit id %r does not match" % bad)

    def test_duplicate_unit_id(self):
        p = plan_copy()
        self.unit(p, "beta")["id"] = "alpha"
        self.assertError(p, "duplicate unit id 'alpha'")

    def test_unknown_needs(self):
        p = plan_copy()
        self.unit(p, "beta")["needs"] = ["ghost"]
        self.assertError(p, "unit beta: needs unknown unit 'ghost'")

    def test_cycle(self):
        p = plan_copy()
        self.unit(p, "alpha")["needs"] = ["crunch"]
        self.assertError(p, "dependency cycle among units: alpha, beta, crunch")

    def test_self_cycle(self):
        p = plan_copy()
        self.unit(p, "alpha")["needs"] = ["alpha"]
        self.assertError(p, "dependency cycle among units: alpha")

    def test_bad_kind(self):
        p = plan_copy()
        self.unit(p, "alpha")["kind"] = "pipeline"
        self.assertError(p, "unit alpha: kind 'pipeline' must be 'code' or 'job'")

    def test_code_missing_fields(self):
        for f in ("title", "prompt", "claims", "test_command", "outputs", "scope"):
            p = plan_copy()
            del self.unit(p, "alpha")[f]
            self.assertError(p, "unit alpha: missing or empty field '%s'" % f)

    def test_code_empty_claims(self):
        p = plan_copy()
        self.unit(p, "alpha")["claims"] = []
        self.assertError(p, "unit alpha: missing or empty field 'claims'")

    def test_job_missing_fields(self):
        for f in ("title", "command", "outputs"):
            p = plan_copy()
            del self.unit(p, "crunch")[f]
            self.assertError(p, "unit crunch: missing or empty field '%s'" % f)

    def test_model_provider_family_fields(self):
        for f in ("model", "provider", "family", "model_family"):
            p = plan_copy()
            self.unit(p, "alpha")[f] = "x"
            self.assertError(p, "unit alpha: field %r names a model, provider or family" % f)

    def test_unsafe_output_paths(self):
        for bad in ("/abs/x", "../x", "a/../../x"):
            p = plan_copy()
            self.unit(p, "alpha")["outputs"] = [bad]
            self.assertError(p, "unit alpha: output path %r must be repo-relative" % bad)
            p = plan_copy()
            self.unit(p, "crunch")["outputs"] = [{"path": bad}]
            self.assertError(p, "unit crunch: output path %r must be repo-relative" % bad)

    def test_unsafe_scope_globs(self):
        for bad in ("/abs/**", "../x/*"):
            p = plan_copy()
            self.unit(p, "alpha")["scope"] = [bad]
            self.assertError(p, "unit alpha: scope glob %r must be repo-relative" % bad)
            p = plan_copy()
            p["scope"] = [bad]
            self.assertError(p, "plan: scope glob %r must be repo-relative" % bad)

    def test_unit_scope_outside_plan_scope(self):
        p = plan_copy()
        self.unit(p, "alpha")["scope"] = ["other/x.md", "src/ok.py"]
        self.assertError(p, "unit alpha: scope glob 'other/x.md' is not covered by the plan scope")
        self.assertFalse(any("src/ok.py" in e for e in errs(p)))

    def test_scope_cover_rule(self):
        self.assertTrue(P.scope_covers(["src/**"], "src/a/b.py"))
        self.assertTrue(P.scope_covers(["src/**"], "src/**"))
        self.assertTrue(P.scope_covers(["docs/*.md"], "docs/a.md"))
        self.assertTrue(P.scope_covers(["x.txt"], "x.txt"))
        self.assertFalse(P.scope_covers(["src/**"], "srcx/a.py"))
        self.assertFalse(P.scope_covers(["src/**"], "tests/a.py"))

    def test_slurm_both_mem(self):
        p = plan_copy()
        self.unit(p, "crunch")["slurm"] = {"mem": "1G", "mem_per_cpu": "1G"}
        self.assertError(p, "unit crunch: slurm block has both mem and mem_per_cpu")

    def test_slurm_neither_mem(self):
        p = plan_copy()
        del p["slurm"]["mem"]
        self.assertError(p, "unit crunch: slurm block needs one of mem or mem_per_cpu")

    def test_slurm_finite_time(self):
        for bad in (None, "UNLIMITED", "infinite", "0", "soon", -5, True):
            p = plan_copy()
            if bad is None:
                del p["slurm"]["time"]
            else:
                p["slurm"]["time"] = bad
            self.assertError(p, "unit crunch: slurm block needs a finite time")
        for good in ("30", "01:30:00", "2-00", "1-12:30", 90):
            p = plan_copy()
            p["slurm"]["time"] = good
            self.assertEqual(errs(p), [], good)

    def test_slurm_unit_override_swaps_memory_key(self):
        p = plan_copy()
        self.unit(p, "crunch")["slurm"] = {"mem_per_cpu": "2G", "cpus": 4}
        self.assertEqual(errs(p), [])

    def test_job_without_any_slurm_block_is_ok(self):
        p = plan_copy()
        del p["slurm"]
        self.assertEqual(errs(p), [])

    def test_caps_above_limits(self):
        for k, v in (("workers", 4), ("attempts", 3), ("review_rounds", 4)):
            p = plan_copy()
            p["caps"] = {k: v}
            self.assertError(p, "caps.%s %d exceeds the maximum" % (k, v))

    def test_caps_lower_allowed(self):
        p = plan_copy()
        p["caps"] = {"workers": 1, "attempts": 1, "review_rounds": 1}
        self.assertEqual(errs(p), [])
        p["caps"] = {"workers": 0}
        self.assertError(p, "caps.workers must be an integer >= 1")

    def test_overlapping_output_warning(self):
        p = plan_copy()
        p["units"].append(dict(self.unit(p, "alpha"), id="gamma", needs=[], outputs=["src/alpha.py"],
                               scope=["src/alpha.py"]))
        e, w = P.validate_plan(p)
        self.assertEqual(e, [])
        self.assertEqual(len(w), 1)
        self.assertIn("units alpha and gamma have no dependency path but both declare output 'src/alpha.py'", w[0])

    def test_no_warning_with_dependency_path(self):
        p = plan_copy()
        self.unit(p, "beta")["outputs"] = ["src/alpha.py"]
        self.assertEqual(P.validate_plan(p), ([], []))

    def test_walltime(self):
        self.assertEqual(P.parse_walltime("1-00:00:00"), 1440)
        self.assertIsNone(P.parse_walltime("00:00:00"))


class ValidateCliTests(TmpCase):
    def test_exit_codes_and_lines(self):
        r = self.cli("plan", "validate", self.write_plan())
        self.assertEqual(r.returncode, 0, r.stderr)
        p = plan_copy()
        p["units"][0]["kind"] = "x"
        p["units"][1]["needs"] = ["ghost"]
        r = self.cli("plan", "validate", self.write_plan(p))
        self.assertEqual(r.returncode, 1)
        lines = [x for x in r.stderr.splitlines() if x]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(x.startswith("error: ") for x in lines))

    def test_warning_exits_zero_on_stderr(self):
        p = plan_copy()
        p["units"].append(dict(p["units"][0], id="gamma", needs=[], outputs=["src/alpha.py"],
                               scope=["src/alpha.py"]))
        r = self.cli("plan", "validate", self.write_plan(p))
        self.assertEqual(r.returncode, 0)
        self.assertIn("warning: units alpha and gamma have no dependency path", r.stderr)

    def test_unreadable_and_bad_json(self):
        r = self.cli("plan", "validate", "/nonexistent/plan.json")
        self.assertEqual(r.returncode, 1)
        bad = self.write_plan({"x": 1})
        with open(bad, "w") as f:
            f.write("{nope")
        self.assertEqual(self.cli("plan", "validate", bad).returncode, 1)


if __name__ == "__main__":
    unittest.main()
