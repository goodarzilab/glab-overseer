import hashlib
import json
import os
import shutil
import subprocess
import unittest

import test_u5 as U5
from test_u5 import COMMIT, jload, sh

from overseer import approval, paths, execution as X, judge as J, review as R, workspace as W

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
        self.author('echo "%s|$PWD|$*|$(($(ls -A | wc -l)))" >> %s; cat %s; exit %d' % (name, self.log, out, rc), name)

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
        self.assertFalse(cwd.startswith(self.wt()))
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


class Budget(Case):
    """The review cap is a budget per logical unit: earlier attempts and a split parent count against it."""

    def inherit(self, rounds):
        st = approval.load_unit_state(self.root, "demo", "alpha")
        st["lineage"] = {"parent": "alpha0", "reason": "test", "time": "2026-11-10T00:00:00Z", "review_rounds": rounds}
        approval.save_unit_state(self.root, "demo", "alpha", st)

    def test_rounds_used_before_this_attempt_leave_less_budget(self):
        self.done()
        self.inherit(2)
        self.reviewer("agy", agy_out(fail(), "s"))
        self.assertEqual(self.review()[1]["round"], 1)
        self.assertEqual(approval.load_unit_state(self.root, "demo", "alpha")["counters"]["review_rounds"], 3)
        n = len(self.calls())
        rc, _, err = self.review()
        self.assertEqual(rc, 1)
        self.assertIn("2 used before this attempt", err)
        self.assertEqual(len(self.calls()), n)
        self.commit_more("src/alpha_extra.py")  # a fixed candidate after the last permitted round
        self.assertEqual(self.cli("judge", "alpha")[0], 0)
        self.assertIn("stop alpha: review cap reached", self.cli("next")[1])
        rc, out, err = self.cli("dispositions", "alpha", "--orchestrator-decision", "three rounds across the unit")
        self.assertEqual(rc, 0, err)
        self.assertTrue(json.loads(out)["orchestrator_decision"])

    def test_a_new_attempt_after_a_spent_budget_gets_one_round(self):
        self.done()
        self.inherit(3)
        rc, out, err = self.cli("next")
        self.assertEqual(rc, 0, err)
        self.assertIn("review alpha", out)
        rc, _, err = self.cli("dispositions", "alpha", "--orchestrator-decision", "budget used by a1")
        self.assertEqual(rc, 1)
        self.assertIn("accepted only at the cap", err)
        self.reviewer("agy", agy_out(fail(), "s"))
        self.assertEqual(self.review()[1]["round"], 1)
        self.assertEqual(approval.load_unit_state(self.root, "demo", "alpha")["counters"]["review_rounds"], 4)
        n = len(self.calls())
        rc, _, err = self.review()
        self.assertEqual(rc, 1)
        self.assertIn("3 used before this attempt, which may use 1", err)
        self.assertEqual(len(self.calls()), n)
        self.commit_more("src/alpha_extra.py")
        self.assertEqual(self.cli("judge", "alpha")[0], 0)
        rc, out, err = self.cli("next")
        self.assertIn("stop alpha: review cap reached", out)
        self.assertNotIn("above the cap", out)
        rc, out, err = self.cli("dispositions", "alpha", "--orchestrator-decision", "one round on the new attempt")
        self.assertEqual((rc, json.loads(out)["state"]), (0, "ELIGIBLE"), err)

    def test_an_unretried_round_without_a_verdict_does_not_admit_a_decision(self):
        self.done()
        self.inherit(3)
        os.makedirs(self.rdir(1))
        with open(os.path.join(self.rdir(1), "round.json"), "w") as f:
            json.dump({"round": 1, "state": "aborted", "retried": False}, f)
        self.assertIn("review alpha: --retry", self.cli("next")[1])
        rc, _, err = self.cli("dispositions", "alpha", "--orchestrator-decision", "the review never ran")
        self.assertEqual(rc, 1)
        self.assertIn("has not been retried", err)
        with open(os.path.join(self.attempt_dir(), "reviews", "orchestrator_decision.json"), "w") as f:
            json.dump({"orchestrator_decision": True, "text": "recorded by an earlier version",
                       "candidate": self.state_cand(), "rounds": 4}, f)
        self.assertEqual(self.state()["state"], "NOT_ELIGIBLE")  # an earlier decision does not skip the retry

    def test_a_counter_above_the_unit_allowance_stops_next(self):
        self.done()
        self.inherit(3)
        st = approval.load_unit_state(self.root, "demo", "alpha")
        st["counters"]["review_rounds"] = 5  # 3 before this attempt plus its one round is 4
        approval.save_unit_state(self.root, "demo", "alpha", st)
        self.assertIn("review_rounds=5 above the cap 4", self.cli("next")[1])

    def test_used_counts_round_directories_of_earlier_attempts_and_lineage(self):
        self.done()
        for k, names in ((1, ["r1", "r2", "rx", "r0"]), (2, ["r1"]), (3, [])):
            for nm in names:
                os.makedirs(os.path.join(self.attempt_dir(n=k), "reviews", nm), exist_ok=True)
        used = approval.review_rounds_used
        self.assertEqual(used(self.root, "demo", "alpha", before_attempt=1), 0)
        self.assertEqual(used(self.root, "demo", "alpha", before_attempt=2), 2)
        self.assertEqual(used(self.root, "demo", "alpha", before_attempt=3), 3)
        self.assertEqual(used(self.root, "demo", "alpha"), 3)
        self.inherit(1)
        self.assertEqual(used(self.root, "demo", "alpha", before_attempt=2), 3)
        with open(os.path.join(self.attempt_dir(n=1), "reviews", "r9"), "w") as f:
            f.write("not a round")  # a file is not a round
        os.makedirs(os.path.join(self.attempt_dir(n=1), "reviews", "r8\n"))  # nor a name with a trailing newline
        self.assertEqual(used(self.root, "demo", "alpha", before_attempt=2), 3)

    def test_an_unfinished_round_over_a_legacy_budget_stops_next(self):
        self.done()
        self.inherit(3)
        for r in (1, 2):  # two rounds in one attempt over a spent budget: only legacy state can hold this
            os.makedirs(self.rdir(r))
            with open(os.path.join(self.rdir(r), "round.json"), "w") as f:
                json.dump({"round": r, "state": "aborted", "retried": False}, f)
        self.assertIn("stop alpha: the unit's review budget of 3 rounds is used", self.cli("next")[1])
        rc, out, err = self.cli("dispositions", "alpha", "--orchestrator-decision", "legacy rounds used the budget")
        self.assertEqual(rc, 0, err)

    def test_child_split_before_the_record_inherits_its_parents_rounds(self):
        self.done()
        for nm in ("r1", "r2"):
            os.makedirs(os.path.join(paths.attempt_dir(self.root, "demo", "alpha0", 1), "reviews", nm))
        st = approval.load_unit_state(self.root, "demo", "alpha")
        st["lineage"] = {"parent": "alpha0", "reason": "legacy", "time": "2026-11-01T00:00:00Z"}
        approval.save_unit_state(self.root, "demo", "alpha", st)
        self.assertEqual(approval.review_rounds_used(self.root, "demo", "alpha", before_attempt=1), 2)
        st["lineage"]["parent"] = "alpha"  # a self-referencing lineage cannot loop
        approval.save_unit_state(self.root, "demo", "alpha", st)
        self.assertEqual(approval.review_rounds_used(self.root, "demo", "alpha", before_attempt=1), 0)


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


class Round1Fixes(Case):
    def test_k1_nonvacuous_restores_index_with_tree(self):
        self.done(unit={"test_command": 'test -n "$(git ls-files src/alpha.py)"'})
        self.assertEqual(json.loads(self.cli("nonvacuous", "alpha")[1])["status"], "PASS")

    def test_k2_earlier_findings_carry_all_fields_and_ruling(self):
        self.done()
        self.reviewer("agy", agy_out(fail(finding(description="D-ESC", reproduction="R-CMD", line=7))))
        self.review()
        ev = ["reviews/r1/review.json"]
        self.assertEqual(self.disp({"f1": {"ruling": "dismissed", "evidence": ev, "note": "N-OTE"}})[0], 0)
        self.review()
        with open(os.path.join(self.rdir(2), "input.md")) as f:
            text = f.read()
        for needle in ("D-ESC", "R-CMD", "line: 7", "file: src/alpha.py", "severity: blocking", "dismissed", "N-OTE"):
            self.assertIn(needle, text)

    def test_k3_diff_is_text_despite_diff_attribute(self):
        self.done()
        with open(os.path.join(self.root, sh(self.root, "rev-parse", "--git-path", "info/attributes")), "w") as f:
            f.write("src/alpha.py -diff\n")
        self.reviewer("agy", agy_out(PASS))
        self.review()
        with open(os.path.join(self.rdir(), "input.md")) as f:
            text = f.read()
        self.assertIn("+hi", text)
        self.assertNotIn("Binary files", text)

    def test_k4_second_review_refuses_while_locked_and_lock_is_released(self):
        self.done()
        self.reviewer("agy", agy_out(PASS))
        lock = os.path.join(self.attempt_dir(), "review.lock")
        with open(lock, "w") as f:
            f.write("{}")
        rc, _, err = self.review()
        self.assertEqual(rc, 1)
        self.assertIn("another review", err)
        self.assertEqual(self.calls(), [])
        os.unlink(lock)
        self.assertEqual(self.review()[0], 0)
        self.assertFalse(os.path.exists(lock))
        self.reviewer("agy", agy_out(PASS), 1)  # failure path releases too
        self.review()
        self.assertFalse(os.path.exists(lock))

    def test_k5_orphan_call_directory_is_recorded_and_skipped(self):
        self.done()
        self.reviewer("agy", dict(agy_out(PASS), response="not json"))
        self.review()
        os.mkdir(os.path.join(self.rdir(), "c2"))
        self.assertEqual(R._next_call_dir(self.rdir()), 3)  # scans the directory, not the recorded calls
        self.reviewer("agy", agy_out(PASS))
        rc, res, err = self.review("--retry")
        self.assertEqual((rc, res["state"]), (0, "complete"), err)
        calls = jload(os.path.join(self.rdir(), "round.json"))["calls"]
        self.assertEqual([(c["dir"], c["classification"]) for c in calls], [("c1", "MALFORMED"), ("c2", "ORPHANED"), ("c3", "OK")])

    def test_k6_scratch_is_outside_worktree_even_with_tmpdir_inside(self):
        self.done()
        inside = os.path.join(self.wt(), "tmpx")
        os.makedirs(inside)
        os.environ["TMPDIR"] = inside
        self.reviewer("agy", agy_out(PASS))
        self.assertEqual(self.review()[0], 0)
        cwd = self.calls()[0][1]
        self.assertTrue(cwd.startswith(os.path.join(self.attempt_dir(), "scratch")), cwd)
        self.assertFalse(cwd.startswith(self.wt()))
        self.assertEqual(os.listdir(os.path.join(self.attempt_dir(), "scratch")), [])

    def test_k7_author_session_appearing_during_the_review_is_caught(self):
        self.done()
        inv = os.path.join(self.attempt_dir(), "inv", "02-fixer")
        late = json.dumps([{"type": "result", "subtype": "success", "is_error": False, "result": "x", "session_id": "late-sess"}])
        out = os.path.join(self.tmp, "late.out")
        with open(out, "w") as f:
            json.dump(agy_out(PASS, "late-sess"), f)
        self.author("mkdir -p %s; echo '%s' > %s/stdout.log; cat %s" % (inv, late, inv, out), "agy")
        rc, res, err = self.review()
        self.assertEqual((rc, res["reason"]), (1, "reviewer session id equals the author's"))

    def test_k8_missing_caps_use_defaults(self):
        self.assertEqual(R._cap({}), 3)
        self.assertEqual(R._cap({"caps": {"workers": 1}}), 3)
        self.assertEqual(R._cap({"caps": {"review_rounds": 2}}), 2)
        self.assertEqual(W.caps_of({"caps": {"workers": 1}}), {"workers": 1, "attempts": 2, "review_rounds": 3})


class Round2Fixes(Case):
    def test_m1_nonvacuous_uses_the_review_diff_base(self):
        self.done(unit={"test_command": "test -f src/alpha.py"})
        with open(os.path.join(self.attempt_dir(), "integration.json"), "w") as f:
            json.dump({"integration_base": self.state_cand()}, f)
        rec = json.loads(self.cli("nonvacuous", "alpha")[1])
        self.assertEqual((rec["status"], rec["base"]), ("NOT_APPLICABLE", self.state_cand()))

    def test_m2_baseline_must_pass_and_rc_127_after_revert_is_a_real_failure(self):
        flag = os.path.join(self.tmp, "flag")
        self.write_models("claude")
        self.run_unit('mkdir -p src tests; echo true > src/alpha.py; echo t > tests/test_alpha.py', expect="DONE",
                      unit={"test_command": "bash src/alpha.py; rc=$?; test -e %s && exit 9; exit $rc" % flag})
        self.unit = X._unit_of(approval.effective_plan(self.root, "demo"), "alpha")
        rec = json.loads(self.cli("nonvacuous", "alpha")[1])
        self.assertEqual((rec["status"], rec["test_rc"]), ("PASS", 127))
        os.unlink(os.path.join(self.attempt_dir(), "nonvacuous.json"))
        with open(flag, "w") as f:
            f.write("x")
        rc, _, err = self.cli("nonvacuous", "alpha")
        self.assertEqual(rc, 1)
        self.assertIn("baseline does not pass", err)
        self.assertFalse(os.path.exists(os.path.join(self.attempt_dir(), "nonvacuous.json")))

    def test_m3_reproduce_cannot_move_judged_refs(self):
        self.done()
        self.reviewer("agy", agy_out(fail()))
        self.review()
        ref = "refs/heads/overseer/demo/alpha/a1"
        before = sh(self.wt(), "rev-parse", ref)
        rc, out, err = self.cli("reproduce", "alpha", "--finding", "f1", "--command",
                                "git update-ref %s HEAD^; git branch -f other HEAD^; git tag t1 HEAD^" % ref)
        self.assertEqual(rc, 0, err)
        self.assertEqual(sh(self.wt(), "rev-parse", ref), before)
        self.assertEqual(sh(self.wt(), "branch", "--list", "other"), "")
        # a command that reaches the judged repository by absolute path is detected and the record is invalid
        rc, out, err = self.cli("reproduce", "alpha", "--finding", "f1", "--command",
                                "git -C %s update-ref %s HEAD^" % (self.wt(), ref))
        self.assertEqual(rc, 1)
        self.assertIn("invalid", err)
        self.assertFalse(R._evidence_ok(self.attempt_dir(), "evidence/rep-2.json"))
        self.assertTrue(R._evidence_ok(self.attempt_dir(), "evidence/rep-1.json"))

    def test_m4_round_directory_without_round_json_is_aborted(self):
        self.done()
        self.reviewer("agy", agy_out(PASS))
        os.makedirs(self.rdir(1))
        rc, res, err = self.review()
        self.assertEqual((rc, res["round"]), (0, 2), err)
        self.assertEqual(jload(os.path.join(self.rdir(1), "round.json"))["state"], "aborted")

    def test_m4_retry_reuses_the_aborted_round(self):
        self.done()
        self.reviewer("agy", agy_out(PASS))
        os.makedirs(self.rdir(1))
        rc, res, err = self.review("--retry")
        self.assertEqual((rc, res["round"], res["verdict"]), (0, 1, "PASS"), err)
        self.assertTrue(os.path.exists(os.path.join(self.rdir(1), "input.md")))

    def test_m5_review_state_rechecks_session_collision(self):
        self.done()
        self.reviewer("agy", agy_out(PASS, "rev-conv"))
        self.review()
        self.assertEqual(self.state()["state"], "ELIGIBLE")
        inv = os.path.join(self.attempt_dir(), "inv", "02-fixer")
        os.makedirs(inv)
        with open(os.path.join(inv, "stdout.log"), "w") as f:
            json.dump([{"type": "result", "subtype": "success", "is_error": False, "result": "x", "session_id": "rev-conv"}], f)
        st = self.state()
        self.assertEqual(st["state"], "NOT_ELIGIBLE")
        self.assertIn("session id", st["reasons"][0])


class Round3Fixes(Case):
    def test_q1_baseline_artifacts_do_not_reach_the_reverted_run(self):
        self.done(unit={"test_command": "[ -e .artifact ] && exit 0; touch .artifact; test -f src/alpha.py"})
        self.assertEqual(json.loads(self.cli("nonvacuous", "alpha")[1])["status"], "PASS")

    def test_q2_content_edits_of_the_judged_worktree_invalidate_the_record(self):
        self.done()
        self.reviewer("agy", agy_out(fail()))
        self.review()
        for cmd in ("echo x >> %s/src/alpha.py", "rm %s/src/alpha.py", "touch %s/stray.txt"):
            n = len([x for x in os.listdir(os.path.join(self.attempt_dir(), "evidence"))] if os.path.isdir(
                os.path.join(self.attempt_dir(), "evidence")) else [])
            rc, out, err = self.cli("reproduce", "alpha", "--finding", "f1", "--command", cmd % self.wt())
            self.assertEqual(rc, 1, cmd)
            self.assertIn("invalid", err)
            rec = jload(os.path.join(self.attempt_dir(), "evidence", "rep-%d.json" % (n + 1)))
            self.assertFalse(rec["valid"])
            self.assertIn("judged worktree changed", rec["problem"])

    def test_q3_reproduction_records_must_parse_and_be_valid(self):
        self.done()
        ev = os.path.join(self.attempt_dir(), "evidence")
        os.makedirs(ev)
        for name, text, want in (("rep-1.json", "{", False), ("rep-2.json", '{"kind": "reproduce"}', False),
                                 ("rep-3.json", '{"kind": "reproduce", "valid": true}', True),
                                 ("rep-4.json", '{"valid": true}', True), ("other.json", "{", True),
                                 ("note.txt", "free text", True)):
            with open(os.path.join(ev, name), "w") as f:
                f.write(text)
            self.assertEqual(R._evidence_ok(self.attempt_dir(), "evidence/" + name), want, name)
        self.assertFalse(R._evidence_ok(self.attempt_dir(), "evidence"))
        self.assertFalse(R._evidence_ok(self.attempt_dir(), "evidence/missing.json"))


if __name__ == "__main__":
    unittest.main()
