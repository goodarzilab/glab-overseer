import hashlib
import json
import os
import shutil
import subprocess
import unittest

import test_u5 as U5
from test_u5 import COMMIT, jload, sh

from overseer import approval, execution as X, judge as J, review as R, workspace as W

AUTHOR = ('mkdir -p src tests; echo hi > src/alpha.py; echo t > tests/test_alpha.py; git add src tests; %s; '
          "echo '[{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"done\",\"session_id\":\"author-sess\"}]'"
          % COMMIT)
AGY_AUTHOR = ("mkdir -p src tests; echo hi > src/alpha.py; echo t > tests/test_alpha.py; "
              "echo '{\"status\":\"SUCCESS\",\"response\":\"done\",\"conversation_id\":\"author-sess\"}'")
PASS = {"verdict": "PASS", "findings": []}


def finding(fid="f1", sev="blocking", **kw):
    f = {"id": fid, "severity": sev, "claim": "alpha works", "file": "src/alpha.py", "line": 1,
         "description": "it breaks", "reproduction": "cat src/alpha.py"}
    f.update(kw)
    return f


def fail(*fs):
    return {"verdict": "FAIL", "findings": list(fs) or [finding()]}


def claude_out(review, sid="rev-sess", error=None):
    r = {"type": "result", "subtype": "success", "is_error": error is not None, "session_id": sid,
         "result": error if error is not None else json.dumps(review)}
    return [{"type": "system", "subtype": "init", "apiKeySource": "none", "session_id": sid}, r]


def agy_out(review, sid="rev-sess", error=None):
    return {"status": "ERROR" if error else "SUCCESS", "conversation_id": sid,
            "response": error if error is not None else json.dumps(review)}


class Case(U5.Case):
    def setUp(self):
        super().setUp()
        for t in ("ls", "wc"):
            os.symlink(shutil.which(t, path="/usr/bin:/bin"), os.path.join(self.tmp, "sysbin", t))
        self.log = os.path.join(self.tmp, "reviewer.log")

    def reviewer(self, name, payload, rc=0):
        out = os.path.join(self.tmp, name + ".out")
        with open(out, "w") as f:
            f.write(payload if isinstance(payload, str) else json.dumps(payload))
        self.author('echo "%s|$PWD|$*|$(ls -A | wc -l)" >> %s; cat %s; exit %d' % (name, self.log, out, rc), name)

    def calls(self):
        try:
            with open(self.log) as f:
                return [l.rstrip("\n").split("|") for l in f]
        except FileNotFoundError:
            return []

    def done(self, host="claude", **kw):
        self.write_models(host)
        j = self.run_unit(AUTHOR if host == "claude" else AGY_AUTHOR, name="claude" if host == "claude" else "agy",
                          expect="DONE", **kw)
        self.unit = X._unit_of(approval.effective_plan(self.root, "demo"), "alpha")
        return j

    def review(self, *a):
        rc, out, err = self.cli("review", "alpha", *a)
        return rc, (json.loads(out) if out.strip() else None), err

    def state(self):
        return R.review_state(self.root, "demo", self.unit)

    def state_cand(self):
        return J.valid_judgment(self.root, "demo", self.unit)["candidate"]

    def rdir(self, r=1):
        return os.path.join(self.attempt_dir(), "reviews", "r%d" % r)

    def disp(self, data):
        p = os.path.join(self.tmp, "disp.json")
        with open(p, "w") as f:
            json.dump(data, f)
        return self.cli("dispositions", "alpha", p)

    def commit_more(self, rel):
        self.put(rel, "x\n", base=self.wt())
        sh(self.wt(), "add", "-A")
        sh(self.wt(), "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "x")

    def hash_wt(self):
        wt = self.wt()
        h = hashlib.sha256()
        h.update(sh(wt, "rev-parse", "HEAD").encode())
        for rel in sorted(sh(wt, "ls-files").split()) + [".git"]:
            p = os.path.join(wt, rel)
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    h.update(f.read())
        gd = sh(wt, "rev-parse", "--git-dir")
        with open(os.path.join(gd, "index"), "rb") as f:
            h.update(f.read())
        return h.hexdigest()


class Review(Case):
    def test_host_claude_pass_uses_agy_in_empty_scratch(self):
        self.done()
        self.reviewer("agy", agy_out(PASS, "rev-conv"))
        rc, res, err = self.review()
        self.assertEqual((rc, res["verdict"], res["state"], res["reviewer_family"], res["model"]),
                         (0, "PASS", "complete", "google", "gemini-9-flash-high"), err)
        (name, cwd, argv, count), = self.calls()
        self.assertEqual((name, count), ("agy", "0"))
        self.assertFalse(cwd.startswith(self.root) or cwd.startswith(self.tmp + "/project"))
        self.assertIn("gemini-9-flash-high", argv)
        rd = jload(os.path.join(self.rdir(), "round.json"))
        self.assertEqual((rd["state"], rd["candidate"], rd["same_family_fallback"]), ("complete", self.state_cand(), False))
        self.assertEqual(rd["calls"][0]["session_id"], "rev-conv")
        self.assertNotEqual(rd["calls"][0]["session_id"], "author-sess")
        for f in ("input.md", "review.json", "c1/stdout.txt", "c1/stderr.txt", "c1/argv.json", "c1/parsed.json", "c1/task.md"):
            self.assertTrue(os.path.exists(os.path.join(self.rdir(), f)), f)
        with open(os.path.join(self.rdir(), "input.md")) as f:
            text = f.read()
        for needle in ("- alpha works", R.COUNTER_CLAIM, "test -f src/alpha.py", "Exit code at the candidate: 0",
                       "+hi", "Earlier findings and dispositions"):
            self.assertIn(needle, text)
        self.assertEqual(self.state()["state"], "ELIGIBLE")
        self.assertEqual(approval.load_unit_state(self.root, "demo", "alpha")["counters"]["review_rounds"], 1)

    def test_host_google_uses_claude_haiku(self):
        self.done("google")
        self.reviewer("claude", claude_out(PASS))
        rc, res, err = self.review()
        self.assertEqual((rc, res["reviewer_family"], res["model"]), (0, "claude", "haiku"), err)
        self.assertIn("--model haiku", self.calls()[0][2])

    def test_schema_flag_only_when_survey_probe_passed(self):
        self.done()
        self.reviewer("agy", agy_out(PASS))
        self.review()
        self.assertNotIn("--json-schema", self.calls()[0][2])
        self.assertFalse(jload(os.path.join(self.rdir(), "c1", "argv.json"))["schema_flag"])

    def test_native_structured_output_is_used(self):
        self.done()
        o = agy_out(PASS)
        o["structured_output"] = fail()
        o["response"] = "ignored"
        self.reviewer("agy", o)
        rc, res, err = self.review()
        self.assertEqual((res["verdict"], res["findings"]), ("FAIL", 1), err)

    def test_review_requires_valid_done_judgment(self):
        self.approve()
        rc, res, err = self.review()
        self.assertEqual(rc, 1)
        self.assertIn("no attempt", err)
        self.done()
        self.commit_more("src/alpha_extra.py")
        rc, _, err = self.review()
        self.assertEqual(rc, 1)
        self.assertIn("DONE judgment", err)
        self.assertEqual(self.calls(), [])

    def test_malformed_output_records_failed_call_and_one_retry(self):
        self.done()
        self.reviewer("agy",
                      dict(agy_out(PASS), response='{"verdict": "MAYBE", "findings": []}'))
        rc, res, err = self.review()
        self.assertEqual((rc, res["state"], res["verdict"]), (1, "failed", None))
        rd = jload(os.path.join(self.rdir(), "round.json"))
        self.assertEqual((rd["calls"][0]["state"], rd["calls"][0]["classification"]), ("failed", "MALFORMED"))
        self.assertFalse(os.path.exists(os.path.join(self.rdir(), "review.json")))
        rc, _, err = self.review()
        self.assertIn("--retry", err)
        self.reviewer("agy", agy_out(PASS))
        rc, res, err = self.review("--retry")
        self.assertEqual((rc, res["round"], res["verdict"]), (0, 1, "PASS"), err)
        self.assertEqual([c["purpose"] for c in jload(os.path.join(self.rdir(), "round.json"))["calls"]], ["primary", "retry"])
        rc, _, err = self.review("--retry")
        self.assertEqual(rc, 1)
        self.assertIn("--retry needs", err)

    def test_failed_retry_consumes_the_round(self):
        self.done()
        self.reviewer("agy", agy_out(PASS, error="boom"), 1)
        self.assertEqual(self.review()[1]["state"], "failed")
        rc, res, err = self.review("--retry")
        self.assertEqual(res["state"], "failed", err)
        rc, res, err = self.review("--retry")
        self.assertEqual(rc, 1)
        rc, res, err = self.review()
        self.assertEqual((res["round"], res["state"]), (2, "failed"))
        self.assertEqual(len(self.calls()), 3)

    def test_quota_falls_back_once_to_host_family_and_is_labeled(self):
        self.done()
        self.reviewer("agy", agy_out(PASS, error="usage limit reached"), 1)
        self.reviewer("claude", claude_out(PASS, "fallback-sess"))
        rc, res, err = self.review()
        self.assertEqual((rc, res["verdict"], res["reviewer_family"], res["model"], res["same_family_fallback"]),
                         (0, "PASS", "claude", "haiku", True), err)
        rd = jload(os.path.join(self.rdir(), "round.json"))
        self.assertIn("QUOTA", rd["fallback_reason"])
        self.assertEqual([(c["purpose"], c["family"]) for c in rd["calls"]], [("primary", "google"), ("fallback", "claude")])
        self.assertEqual([c[0] for c in self.calls()], ["agy", "claude"])

    def test_auth_falls_back_but_second_failure_does_not_loop(self):
        self.done()
        self.reviewer("agy", agy_out(PASS, error="Please run /login"), 1)
        self.reviewer("claude", claude_out(PASS, error="Claude AI usage limit reached"), 1)
        rc, res, err = self.review()
        self.assertEqual((rc, res["state"], res["same_family_fallback"]), (1, "failed", True))
        self.assertEqual(len(self.calls()), 2)

    def test_unknown_and_transient_errors_do_not_fall_back(self):
        self.done()
        self.reviewer("agy", agy_out(PASS, error="something odd happened"), 1)
        self.reviewer("claude", claude_out(PASS))
        rc, res, err = self.review()
        self.assertEqual((rc, res["state"], res["same_family_fallback"]), (1, "failed", False))
        self.assertEqual([c[0] for c in self.calls()], ["agy"])
        self.reviewer("agy", agy_out(PASS, error="HTTP 529 overloaded"), 1)
        self.assertEqual(self.review("--retry")[1]["same_family_fallback"], False)
        self.assertEqual([c[0] for c in self.calls()], ["agy", "agy"])

    def test_reviewer_session_equal_to_author_is_refused(self):
        self.done()
        self.reviewer("agy", agy_out(PASS, "author-sess"))
        rc, res, err = self.review()
        self.assertEqual((rc, res["state"], res["reason"]), (1, "failed", "reviewer session id equals the author's"))

    def test_oversized_input_is_refused_not_truncated(self):
        self.done()
        self.reviewer("agy", agy_out(PASS))
        rc, _, err = self.review("--max-input-chars", "200")
        self.assertEqual(rc, 1)
        self.assertIn("not truncated", err)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.rdir()))

    def test_round_cap_holds_and_decision_only_at_cap(self):
        self.done()
        self.reviewer("agy", agy_out(fail(), "s"))
        rc, _, err = self.cli("dispositions", "alpha", "--orchestrator-decision", "ship it")[0:3]
        self.assertEqual(rc, 1)
        self.assertIn("only at the cap", err)
        for i in range(3):
            self.assertEqual(self.review()[1]["round"], i + 1)
        n = len(self.calls())
        rc, _, err = self.review()  # a fresh invocation, as after a restart
        self.assertEqual(rc, 1)
        self.assertIn("--orchestrator-decision", err)
        self.assertEqual(len(self.calls()), n)
        self.assertEqual(self.state()["state"], "NOT_ELIGIBLE")
        rc, out, err = self.cli("dispositions", "alpha", "--orchestrator-decision", "owner accepts the risk")
        self.assertEqual(rc, 0, err)
        st = json.loads(out)
        self.assertTrue(st["orchestrator_decision"])
        dec = jload(os.path.join(self.attempt_dir(), "reviews", "orchestrator_decision.json"))
        self.assertTrue(dec["orchestrator_decision"])
        self.commit_more("src/alpha_extra.py")
        self.assertEqual(self.state()["state"], "NOT_ELIGIBLE")  # bound to the old candidate


class Dispositions(Case):
    def fail_round(self, *fs):
        self.done()
        self.reviewer("agy", agy_out(fail(*fs)))
        self.assertEqual(self.review()[1]["verdict"], "FAIL")

    def test_refusals(self):
        self.fail_round(finding("f1"), finding("f2", "nonblocking"))
        rc, _, err = self.disp({"f1": {"ruling": "inconclusive"}})
        self.assertIn("finding f2 has no ruling", err)
        rc, _, err = self.disp({"f1": {"ruling": "dismissed", "evidence": ["evidence/nope.json"]}, "f2": {"ruling": "dismissed"}})
        self.assertIn("evidence file not found", err)
        rc, _, err = self.disp({"f1": {"ruling": "dismissed"}, "f2": {"ruling": "dismissed"}})
        self.assertIn("needs at least one evidence", err)
        rc, _, err = self.disp({"f1": {"ruling": "nonblocking", "evidence": ["../../unit.json"]}, "f2": {"ruling": "blocked"}})
        self.assertEqual(rc, 1)
        rc, _, err = self.disp({"f1": {"ruling": "blocked"}, "f2": {"ruling": "blocked"}, "f9": {"ruling": "blocked"}})
        self.assertIn("unknown finding", err)
        self.assertFalse(os.path.exists(os.path.join(self.rdir(), "dispositions.json")))

    def test_reproduce_record_counts_as_evidence_and_unit_becomes_eligible(self):
        self.fail_round(finding("f1"), finding("f2", "nonblocking"))
        rc, out, err = self.cli("reproduce", "alpha", "--finding", "f1", "--command", "cat src/alpha.py; echo e >&2; exit 3")
        self.assertEqual(rc, 0, err)
        ev = json.loads(out)["evidence"]
        rec = jload(os.path.join(self.attempt_dir(), ev))
        self.assertEqual((rec["rc"], rec["stdout"], rec["stderr"], rec["candidate"]), (3, "hi\n", "e\n", self.state_cand()))
        rc, out, err = self.disp({"f1": {"ruling": "dismissed", "evidence": [ev], "note": "expected"}, "f2": {"ruling": "nonblocking"}})
        self.assertEqual((rc, json.loads(out)["state"]), (0, "ELIGIBLE"), err)

    def test_inconclusive_blocked_and_fixed_blocking_keep_unit_not_eligible(self):
        self.fail_round()
        for ruling, word in (("inconclusive", "inconclusive"), ("blocked", "blocked"), ("fixed", "reviewed in a later round")):
            ev = ["reviews/r1/review.json"]
            rc, out, err = self.disp({"f1": {"ruling": ruling, "evidence": ev}})
            self.assertEqual((rc, json.loads(out)["state"]), (1, "NOT_ELIGIBLE"), err)
            self.assertIn(word, json.loads(out)["reasons"][0])

    def test_nonblocking_finding_needs_only_a_ruling(self):
        self.fail_round(finding("n1", "nonblocking"))
        rc, out, err = self.disp({"n1": {"ruling": "inconclusive"}})
        self.assertEqual((rc, json.loads(out)["state"]), (0, "ELIGIBLE"), err)

    def test_head_change_after_disposition_is_refused_and_invalidates(self):
        self.fail_round()
        ev = ["reviews/r1/review.json"]
        self.assertEqual(self.disp({"f1": {"ruling": "dismissed", "evidence": ev}})[0], 0)
        self.assertEqual(self.state()["state"], "ELIGIBLE")
        self.commit_more("src/alpha_more.py")
        self.assertEqual(self.state()["state"], "NOT_ELIGIBLE")
        rc, _, err = self.disp({"f1": {"ruling": "dismissed", "evidence": ev}})
        self.assertEqual(rc, 1)
        self.assertIn("DONE judgment", err)
        self.assertEqual(self.cli("judge", "alpha")[0], 0)  # a new candidate
        rc, _, err = self.disp({"f1": {"ruling": "dismissed", "evidence": ev}})
        self.assertIn("review the new candidate", err)

    def test_pass_with_nonblocking_findings_is_eligible(self):
        self.done()
        self.reviewer("agy", agy_out({"verdict": "PASS", "findings": [finding("n", "nonblocking")]}))
        self.review()
        self.assertEqual(self.state()["state"], "ELIGIBLE")

    def test_pass_with_blocking_finding_needs_dispositions(self):
        self.done()
        self.reviewer("agy", agy_out({"verdict": "PASS", "findings": [finding()]}))
        self.review()
        self.assertEqual(self.state()["state"], "NOT_ELIGIBLE")


class Reproduce(Case):
    def test_judged_worktree_is_byte_identical_and_disposable_one_removed(self):
        self.done()
        self.reviewer("agy", agy_out(fail()))
        self.review()
        before = self.hash_wt()
        rc, out, err = self.cli("reproduce", "alpha", "--finding", "f1", "--command",
                                "echo x >> src/alpha.py; touch junk; git status --short; pwd")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.hash_wt(), before)
        self.assertEqual(sh(self.wt(), "status", "--porcelain"), "")
        rec = jload(os.path.join(self.attempt_dir(), json.loads(out)["evidence"]))
        scratch = rec["stdout"].strip().splitlines()[-1]
        self.assertFalse(os.path.exists(scratch))
        self.assertNotIn(scratch, sh(self.wt(), "worktree", "list"))
        self.assertEqual(len(sh(self.wt(), "worktree", "list").splitlines()), 2)
        self.assertEqual(self.cli("reproduce", "alpha", "--finding", "zz", "--command", "true")[0], 1)
        self.assertEqual(self.cli("reproduce", "alpha", "--finding", "f1", "--command", "  ")[0], 1)

    def test_timeout_still_removes_the_disposable_worktree(self):
        self.done()
        self.reviewer("agy", agy_out(fail()))
        self.review()
        rc, out, err = self.cli("reproduce", "alpha", "--finding", "f1", "--command", "sleep 30", "--timeout", "0.3")
        self.assertEqual(json.loads(out)["timed_out"], True)
        self.assertEqual(len(sh(self.wt(), "worktree", "list").splitlines()), 2)


class Nonvacuous(Case):
    def test_pass_fail_and_not_applicable(self):
        self.done(unit={"test_command": "test -f src/alpha.py"})
        before = self.hash_wt()
        rc, out, err = self.cli("nonvacuous", "alpha")
        rec = json.loads(out)
        self.assertEqual((rc, rec["status"], rec["candidate"]), (0, "PASS", self.state_cand()), err)
        self.assertEqual((rec["test_files"], rec["restored"]), (["tests/test_alpha.py"], ["src/alpha.py"]))
        self.assertEqual(self.hash_wt(), before)
        self.assertEqual(len(sh(self.wt(), "worktree", "list").splitlines()), 2)
        self.assertEqual(jload(os.path.join(self.attempt_dir(), "nonvacuous.json"))["status"], "PASS")

    def test_vacuous_test_is_fail(self):
        self.done(unit={"test_command": "true"})
        self.assertEqual(json.loads(self.cli("nonvacuous", "alpha")[1])["status"], "FAIL")

    def test_no_test_file_or_no_source_file_is_not_applicable(self):
        self.write_models("claude")
        self.run_unit('mkdir -p src; echo a > src/alpha.py', expect="DONE")
        self.unit = X._unit_of(approval.effective_plan(self.root, "demo"), "alpha")
        rec = json.loads(self.cli("nonvacuous", "alpha")[1])
        self.assertEqual((rec["status"], rec["reason"]), ("NOT_APPLICABLE", "no test file changed"))

    def test_only_test_files_changed_is_not_applicable(self):
        self.run_unit('mkdir -p tests; echo a > tests/test_alpha.py', expect="DONE",
                      unit={"outputs": ["tests/test_alpha.py"], "test_command": "true"})
        rec = json.loads(self.cli("nonvacuous", "alpha")[1])
        self.assertEqual((rec["status"], rec["reason"]), ("NOT_APPLICABLE", "no non-test file changed"))

    def test_test_globs_basename_and_default(self):
        d = R.DEFAULT_TEST_GLOBS
        self.assertTrue(all(R.is_test_file(p, d) for p in ("tests/a/b.py", "test_x.py", "src/test_x.py", "src/x_test.py")))
        self.assertFalse(any(R.is_test_file(p, d) for p in ("src/x.py", "src/tests_x.py", "mytests/a.py")))
        self.assertTrue(R.is_test_file("spec/a.py", ["spec/**"]))


class Fix(Case):
    def test_fixer_runs_in_same_worktree_and_new_candidate_needs_new_round(self):
        self.done()
        self.reviewer("agy", agy_out(PASS))
        self.review()
        cand1 = self.state_cand()
        self.assertEqual(self.state()["state"], "ELIGIBLE")
        p = os.path.join(self.tmp, "fix.md")
        with open(p, "w") as f:
            f.write("Fix the thing the reviewer found.\n")
        self.author('mkdir -p src; echo more > src/alpha_more.py; git add src; %s; '
                    "echo '[{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"d\",\"session_id\":\"fixer-sess\"}]'"
                    % COMMIT)
        rc, out, err = self.cli("fix", "alpha", "--prompt-file", p)
        self.assertEqual((rc, json.loads(out)["invocation"]), (0, "02-fixer"), err)
        self.wait_done()
        inv = X._load(os.path.join(self.attempt_dir(), "inv", "02-fixer", "intent.json"))
        self.assertEqual((inv["kind"], inv["cwd"]), ("fixer", self.wt()))
        self.assertIn("sonnet", inv["argv"])
        with open(os.path.join(self.attempt_dir(), "inv", "02-fixer", "task.md")) as f:
            self.assertIn("Fix the thing the reviewer found.", f.read())
        self.assertEqual(self.state()["state"], "NOT_ELIGIBLE")  # HEAD moved
        self.assertEqual(self.cli("judge", "alpha")[0], 0)
        cand2 = self.state_cand()
        self.assertNotEqual(cand1, cand2)
        self.assertEqual(self.state()["state"], "NOT_ELIGIBLE")
        self.assertEqual(self.review()[1]["round"], 2)
        self.assertEqual(self.state()["state"], "ELIGIBLE")
        self.assertEqual(R.author_sessions(self.root, "demo", "alpha", 1, "claude"), {"author-sess", "fixer-sess"})

    def test_fix_refuses_bad_prompt_and_busy_unit(self):
        self.done()
        p = os.path.join(self.tmp, "fix.md")
        with open(p, "w") as f:
            f.write("  \n")
        self.assertEqual(self.cli("fix", "alpha", "--prompt-file", p)[0], 1)
        self.assertEqual(self.cli("fix", "alpha", "--prompt-file", p + ".missing")[0], 1)
        with open(p, "w") as f:
            f.write("go\n")
        self.author("sleep 5")
        self.assertEqual(self.cli("fix", "alpha", "--prompt-file", p)[0], 0)
        rc, _, err = self.cli("fix", "alpha", "--prompt-file", p)
        self.assertEqual(rc, 1)
        self.assertIn("RUNNING", err)


class Units(Case):
    def test_validate_review_is_strict(self):
        ok = fail()
        self.assertEqual(R.validate_review(ok)[1], [])
        bad = [None, [], {"verdict": "PASS"}, dict(PASS, extra=1), {"verdict": "FAIL", "findings": []},
               {"verdict": "PASS", "findings": [finding(line=True)]}, {"verdict": "PASS", "findings": [finding(line="3")]},
               {"verdict": "PASS", "findings": [finding(severity="high")]},
               {"verdict": "PASS", "findings": [finding(), finding()]},
               {"verdict": "PASS", "findings": [dict(finding(), extra=1)]},
               {"verdict": "PASS", "findings": [finding(id="")]}, {"verdict": "PASS", "findings": [finding(file=None)]}]
        for b in bad:
            self.assertIsNone(R.validate_review(b)[0], b)
        self.assertIsNotNone(R.validate_review({"verdict": "PASS", "findings": [finding(line=None)]})[0])


    def test_patch_id_is_stable_across_bases_and_differs_per_change(self):
        r = self.root
        base = sh(r, "rev-parse", "HEAD")

        def commit(cwd, rel, text):
            self.put(rel, text, base=cwd)
            sh(cwd, "add", rel)
            sh(cwd, *(U5.ID + ["commit", "-q", "-m", rel]))
            return sh(cwd, "rev-parse", "HEAD")
        a = commit(r, "lib/new.py", "n\n")
        sh(r, "branch", "b2", base)
        wt2 = os.path.join(self.tmp, "wt2")
        sh(r, "worktree", "add", "-q", wt2, "b2")
        mid = commit(wt2, "other.txt", "o\n")
        same = commit(wt2, "lib/new.py", "n\n")
        other = commit(wt2, "lib/new2.py", "n\n")
        pid = R.patch_id(r, base, a)
        self.assertRegex(pid, "^[0-9a-f]{40}$")
        self.assertEqual(pid, R.patch_id(r, mid, same))
        self.assertNotEqual(pid, R.patch_id(r, same, other))
        self.assertIsNone(R.patch_id(r, a, a))
        want = subprocess.run("git diff %s %s | git patch-id --stable" % (base, a), shell=True, cwd=r,
                              capture_output=True, text=True).stdout.split()[0]
        self.assertEqual(pid, want)


if __name__ == "__main__":
    unittest.main()
