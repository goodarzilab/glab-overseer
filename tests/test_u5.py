import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import time
import unittest
from unittest import mock

from helpers import TmpCase, plan_copy

from overseer import approval, cli, execution as X, judge as J, models, paths, store, workspace as W

ID = ["-c", "user.name=t", "-c", "user.email=t@localhost"]
COMMIT = 'git -c user.name=w -c user.email=w@localhost commit -q -m work'


def sh(cwd, *args, check=True):
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True,
                       env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "LC_ALL": "C"})
    if check and r.returncode:
        raise AssertionError("git %s: %s" % (args, r.stderr))
    return r.stdout.strip()


def jload(path):
    with open(path) as f:
        return json.load(f)


class Case(TmpCase):
    UNIT = {"id": "alpha", "kind": "code", "title": "Add alpha", "prompt": "Write alpha.", "claims": ["alpha works"],
            "test_command": "test -f src/alpha.py", "outputs": ["src/alpha.py"],
            "scope": ["src/alpha*", "tests/test_alpha.py"], "needs": []}

    def setUp(self):
        super().setUp()
        sysbin = os.path.join(self.tmp, "sysbin")
        os.makedirs(sysbin)
        for t in ("sh", "bash", "sleep", "env", "cat", "echo", "git", "mkdir", "rm", "printf", "touch", "ln", "mv",
                  "head", "tr", "dd", "true", "false", "test"):
            src = shutil.which(t)
            if src:
                os.symlink(src, os.path.join(sysbin, t))
        penv = {"PATH": os.pathsep.join([self.shims, sysbin]), "HOME": self.tmp, "LC_ALL": "C.UTF-8",
                "SLURM_JOB_ID": "1"}
        p = mock.patch.dict(os.environ, penv, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.kill_all)
        self.origin = os.path.join(self.tmp, "origin.git")
        sh(self.tmp, "init", "-q", "--bare", self.origin)
        sh(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        sh(self.root, "init", "-q")
        sh(self.root, "symbolic-ref", "HEAD", "refs/heads/main")
        for rel, text in (("src/alpha_old.py", "old\n"), ("lib/x.py", "x\n"), ("README.md", "readme\n"),
                          (".coverage", "cov-fixture\n")):
            self.put(rel, text)
        sh(self.root, *(ID + ["add", "-A"]))
        sh(self.root, *(ID + ["commit", "-q", "-m", "init"]))
        sh(self.root, "remote", "add", "origin", self.origin)
        sh(self.root, "push", "-q", "origin", "main")
        self.write_models("claude")

    def kill_all(self):
        for i in list(X.iter_invocations(self.root)):
            ln = X._load(os.path.join(i["path"], "launch.json"))
            if isinstance(ln, dict) and isinstance(ln.get("pgid"), int) and ln["pgid"] != os.getpgrp():
                try:
                    os.killpg(ln["pgid"], signal.SIGKILL)
                except OSError:
                    pass
        for c in list(X._CHILDREN):
            try:
                c.kill()
                c.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        X._CHILDREN[:] = []

    def put(self, rel, text, base=None):
        p = os.path.join(base or self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def write_models(self, host, google=True):
        g = ({"available": True, "plan": "gemini-9-argon-high", "code": "gemini-9-argon-high",
              "review": "gemini-9-flash-high"} if google else {"available": False, "reason": "agy not found"})
        store.write_json_atomic(os.path.join(self.root, ".overseer", "models.json"), {
            "date": time.strftime("%Y-%m-%d", time.gmtime()), "host": host, "raw": "",
            "choices": {"claude": dict(models.CLAUDE_ROLES), "google": g}})

    def author(self, body, name="claude"):
        p = os.path.join(self.shims, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n%s\n" % body)
        os.chmod(p, 0o755)

    def approve(self, unit=None, job=None, pid="demo"):
        p = plan_copy()
        p["plan_id"] = pid
        p["scope"] = ["src/**", "tests/**"]
        p["units"] = [dict(self.UNIT, **(unit or {})),
                      {"id": "crunch", "kind": "job", "title": "Crunch", "command": "true", "outputs": ["result.txt"],
                       "needs": []}]
        if job:
            p["units"][1].update(job)
        approval.approve_plan(self.root, p, "me")
        return p

    def cli(self, *args):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = cli.main(["--root", self.root] + list(args))
        return rc, o.getvalue(), e.getvalue()

    def attempt_dir(self, uid="alpha", n=1):
        return paths.attempt_dir(self.root, "demo", uid, n)

    def wait_done(self, uid="alpha", timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            invs = list(X.iter_invocations(self.root, "demo", uid))
            if invs and X.invocation_status(invs[-1]["path"], True)["state"] not in X.ACTIVE:
                return
            time.sleep(0.05)
        self.fail("author did not finish")

    def run_unit(self, body, name="claude", expect=None, **plan):
        """Dispatch alpha with an author shim, wait for it, judge; returns the judgment."""
        self.approve(**plan)
        self.author(body, name)
        rc, out, err = self.cli("dispatch", "--unit", "alpha")
        self.assertEqual(rc, 0, err)
        self.wait_done()
        rc, out, err = self.cli("judge", "alpha")
        j = json.loads(out)
        if expect:
            self.assertEqual(j["status"], expect, j["reasons"])
        return j

    def wt(self):
        return os.path.join(self.attempt_dir(), "worktree")


class CodeJudgment(Case):
    def test_committing_author_is_done_and_dispatch_is_recorded(self):
        j = self.run_unit('mkdir -p src; echo hi > src/alpha.py; git add src; %s' % COMMIT, expect="DONE")
        ws = jload(os.path.join(self.attempt_dir(), "workspace.json"))
        origin_main = sh(self.origin, "rev-parse", "refs/heads/main")
        self.assertEqual((ws["base"], ws["base_ref"], ws["branch"]), (origin_main, "origin/main", "overseer/demo/alpha/a1"))
        self.assertEqual((j["base"], j["test_rc"], j["candidate"]), (origin_main, 0, sh(self.wt(), "rev-parse", "HEAD")))
        self.assertEqual(j["outputs"], [{"path": "src/alpha.py", "ok": True, "problems": []}])
        inv = X._load(os.path.join(self.attempt_dir(), "inv", "01-author", "intent.json"))
        self.assertIn("sonnet", inv["argv"])
        self.assertEqual(inv["cwd"], self.wt())
        self.assertTrue(os.path.exists(os.path.join(self.attempt_dir(), "inv", "01-author", "task.md")))
        self.assertEqual(sh(self.wt(), "status", "--porcelain", "--ignored"), "")  # nothing written into the worktree
        self.assertEqual(sh(self.root, "status", "--porcelain"), "")
        self.assertEqual(j["status"], "DONE")
        self.assertIsNotNone(J.valid_judgment(self.root, "demo", X._unit_of(approval.effective_plan(self.root, "demo"), "alpha")))

    def test_task_text_is_deterministic_and_built_from_the_unit(self):
        t = W.task_text(self.UNIT)
        self.assertEqual(t, W.task_text(dict(self.UNIT)))
        for needle in ("Write alpha.", "- alpha works", "- src/alpha*", "test -f src/alpha.py", "Never push"):
            self.assertIn(needle, t)

    def test_agy_like_uncommitted_author_is_committed_by_supervisor(self):
        self.write_models("google")
        j = self.run_unit("mkdir -p src; echo hi > src/alpha.py", name="agy", expect="DONE")
        self.assertEqual(len(j["commits"]), 1)
        self.assertEqual(sh(self.wt(), "log", "-1", "--format=%an"), "glab-overseer")
        inv = X._load(os.path.join(self.attempt_dir(), "inv", "01-author", "intent.json"))
        self.assertIn("gemini-9-argon-high", inv["argv"])

    def test_mixed_author_keeps_worker_commit_and_adds_one(self):
        j = self.run_unit('mkdir -p src; echo a > src/alpha.py; git add src; %s; echo b > src/alpha_b.py' % COMMIT,
                          expect="DONE")
        self.assertEqual(len(j["commits"]), 2)
        self.assertEqual(sh(self.wt(), "log", "--format=%an", "-2").split(), ["glab-overseer", "w"])

    def test_wrong_branch_and_rewritten_base_fail(self):
        j = self.run_unit('git checkout -q -b other; mkdir -p src; echo a > src/alpha.py; git add src; %s' % COMMIT)
        self.assertEqual((j["status"], j["reasons"]), ("FAILED", ["wrong_branch:refs/heads/other"]))

    def test_rewritten_base_fails(self):
        j = self.run_unit('c=$(git -c user.name=w -c user.email=w@localhost commit-tree -m rw "HEAD^{tree}"); '
                          'git reset -q --hard $c; mkdir -p src; echo a > src/alpha.py; git add src; %s' % COMMIT)
        self.assertEqual((j["status"], j["reasons"]), ("FAILED", ["base_not_ancestor"]))

    def test_prohibited_file_added_then_removed_fails(self):
        j = self.run_unit('mkdir -p src; echo a > src/alpha.py; echo k > src/alpha.pem; git add src; %s; '
                          'git rm -q src/alpha.pem; %s' % (COMMIT, COMMIT))
        self.assertEqual(j["status"], "FAILED")
        self.assertTrue(any(r.startswith("prohibited_path:src/alpha.pem@") for r in j["reasons"]), j["reasons"])

    def test_oversize_added_file_fails_unless_declared_output(self):
        body = 'mkdir -p src; echo a > src/alpha.py; dd if=/dev/zero of=src/alpha_big.bin bs=1024 count=5200 2>/dev/null; git add src; %s' % COMMIT
        j = self.run_unit(body)
        self.assertTrue(any(r.startswith("oversize:src/alpha_big.bin@") for r in j["reasons"]), j["reasons"])

    def test_scope_is_segment_aware_and_renames_check_both_paths(self):
        j = self.run_unit('mkdir -p src/alpha_dir; echo a > src/alpha_dir/x.py; echo b > src/alpha.py; git add src; %s' % COMMIT)
        self.assertTrue(any(r.startswith("out_of_scope:src/alpha_dir/x.py@") for r in j["reasons"]), j["reasons"])

    def test_rename_from_out_of_scope_path_fails(self):
        j = self.run_unit('mkdir -p src; git mv lib/x.py src/alpha_x.py; echo b > src/alpha.py; git add src; %s' % COMMIT)
        self.assertTrue(any(r.startswith("out_of_scope:lib/x.py@") for r in j["reasons"]), j["reasons"])

    def test_uncommitted_staged_rename_from_out_of_scope_path_fails(self):
        j = self.run_unit('mkdir -p src; echo b > src/alpha.py; git mv lib/x.py src/alpha_x.py')
        self.assertEqual((j["status"], j["out_of_scope_dirty"]), ("FAILED", ["lib/x.py"]))

    def test_deleted_file_out_of_scope_and_dirty_out_of_scope_are_named(self):
        j = self.run_unit('mkdir -p src; echo b > src/alpha.py; rm README.md; echo n > NOTES.md')
        self.assertEqual(j["status"], "FAILED")
        self.assertEqual(j["out_of_scope_dirty"], ["NOTES.md", "README.md"])
        self.assertEqual(sh(self.wt(), "rev-parse", "HEAD"), j["base"] if j["base"] else "")  # nothing committed

    def test_symlink_and_submodule_like_paths_fail(self):
        j = self.run_unit('mkdir -p src; echo a > src/alpha.py; ln -s /etc src/alpha_link')
        self.assertIn("symlink:src/alpha_link", j["reasons"])

    def test_cache_files_stay_untracked_and_tracked_fixture_untouched(self):
        j = self.run_unit('mkdir -p src/__pycache__ .pytest_cache; echo a > src/alpha.py; echo c > src/__pycache__/m.pyc; '
                          'echo d > .pytest_cache/f; echo e > src/alpha.pyc', expect="DONE")
        wt = self.wt()
        for rel in ("src/__pycache__/m.pyc", ".pytest_cache/f", "src/alpha.pyc"):
            self.assertTrue(os.path.exists(os.path.join(wt, rel)), rel)
        self.assertEqual(sh(wt, "ls-files", "src", ".pytest_cache"), "src/alpha.py\nsrc/alpha_old.py")
        with open(os.path.join(wt, ".coverage")) as f:
            self.assertEqual(f.read(), "cov-fixture\n")
        self.assertEqual(sh(wt, "diff", "--cached", "--name-only"), "")

    def test_test_that_edits_tracked_file_fails(self):
        j = self.run_unit('mkdir -p src; echo a > src/alpha.py', unit={"test_command": "echo x >> src/alpha.py"})
        self.assertEqual(j["status"], "FAILED")
        self.assertIn("test_modified_tracked:src/alpha.py", j["reasons"])

    def test_failing_and_timed_out_tests_fail(self):
        j = self.run_unit('mkdir -p src; echo a > src/alpha.py', unit={"test_command": "echo oops; exit 7"})
        self.assertEqual((j["status"], j["test_rc"], j["reasons"]), ("FAILED", 7, ["test_failed:rc=7"]))
        with open(os.path.join(self.attempt_dir(), "test.log")) as f:
            self.assertIn("oops", f.read())
        unit = X._unit_of(approval.effective_plan(self.root, "demo"), "alpha")
        unit = dict(unit, test_command="sleep 30")
        j = J.judge_unit(self.root, "demo", unit, 0.3)
        self.assertEqual((j["status"], j["test_rc"], j["reasons"]), ("FAILED", None, ["test_timeout:0.3s"]))

    def test_no_change_and_failed_author_fail(self):
        j = self.run_unit('true')
        self.assertEqual((j["status"], j["reasons"]), ("FAILED", ["no_change"]))

    def test_nonzero_author_exit_is_failure_evidence(self):
        j = self.run_unit('mkdir -p src; echo a > src/alpha.py; exit 3')
        self.assertEqual(j["status"], "FAILED")
        self.assertTrue(j["reasons"][0].startswith("author_failed:"), j["reasons"])

    def test_missing_declared_output_fails_and_judgment_invalidates_on_new_commit(self):
        j = self.run_unit('mkdir -p src; echo a > src/alpha_other.py', unit={"test_command": "true"})
        self.assertEqual(j["status"], "FAILED")
        self.assertEqual(j["reasons"], ["output_failed:src/alpha.py: missing at candidate"])

    def test_judgment_is_valid_only_while_head_equals_candidate(self):
        self.run_unit('mkdir -p src; echo a > src/alpha.py', expect="DONE")
        unit = X._unit_of(approval.effective_plan(self.root, "demo"), "alpha")
        self.assertEqual(J.valid_judgment(self.root, "demo", unit)["status"], "DONE")
        self.put("src/alpha_more.py", "m\n", base=self.wt())
        sh(self.wt(), *(ID + ["add", "-A"]))
        sh(self.wt(), *(ID + ["commit", "-q", "-m", "late"]))
        self.assertIsNone(J.valid_judgment(self.root, "demo", unit))

    def test_resolve_done_is_refused_for_code_and_for_non_uncertain(self):
        self.run_unit('mkdir -p src; echo a > src/alpha.py', expect="DONE")
        rc, _, err = self.cli("resolve", "alpha", "--as", "done", "--reason", "r", "--evidence", "e")
        self.assertEqual(rc, 1)
        self.assertIn("not UNCERTAIN", err)

    def test_uncertain_author_is_uncertain_and_leaves_the_worktree_alone(self):
        self.approve()
        self.author("sleep 30")
        self.assertEqual(self.cli("dispatch", "--unit", "alpha")[0], 0)
        inv = list(X.iter_invocations(self.root, "demo", "alpha"))[0]["path"]
        ln = X._load(os.path.join(inv, "launch.json"))
        os.killpg(ln["pgid"], signal.SIGKILL)
        time.sleep(0.3)
        rc, out, _ = self.cli("judge", "alpha")
        self.assertEqual((rc, json.loads(out)["status"]), (4, "UNCERTAIN"))


class Dispatch(Case):
    def test_dispatch_refuses_without_models_cache_or_for_unavailable_host(self):
        self.approve()
        os.unlink(os.path.join(self.root, ".overseer", "models.json"))
        rc, _, err = self.cli("dispatch", "--unit", "alpha")
        self.assertEqual((rc, "models.json" in err), (1, True))
        self.write_models("google", google=False)
        rc, _, err = self.cli("dispatch", "--unit", "alpha")
        self.assertEqual((rc, "unavailable" in err), (1, True))
        self.assertEqual(approval.load_unit_state(self.root, "demo", "alpha")["counters"]["attempts"], 0)

    def test_fetch_failure_refuses_before_any_attempt(self):
        self.approve()
        sh(self.root, "remote", "set-url", "origin", os.path.join(self.tmp, "missing.git"))
        rc, _, err = self.cli("dispatch", "--unit", "alpha")
        self.assertEqual(rc, 1)
        self.assertIn("git fetch origin main failed", err)
        self.assertEqual(approval.load_unit_state(self.root, "demo", "alpha")["counters"]["attempts"], 0)

    def test_base_is_the_fetched_origin_tip_not_the_local_branch(self):
        self.approve()
        other = os.path.join(self.tmp, "other")
        sh(self.tmp, "clone", "-q", self.origin, other)
        self.put("NEWS.md", "n\n", base=other)
        sh(other, *(ID + ["add", "-A"]))
        sh(other, *(ID + ["commit", "-q", "-m", "advance"]))
        sh(other, "push", "-q", "origin", "main")
        self.author("true")
        self.assertEqual(self.cli("dispatch", "--unit", "alpha")[0], 0)
        ws = jload(os.path.join(self.attempt_dir(), "workspace.json"))
        self.assertEqual(ws["base"], sh(other, "rev-parse", "HEAD"))
        self.assertNotEqual(ws["base"], sh(self.root, "rev-parse", "HEAD"))
        self.assertTrue(os.path.exists(os.path.join(self.wt(), "NEWS.md")))

    def test_unsafe_target_branch_is_refused(self):
        with self.assertRaises(W.WorkspaceError):
            W.check_branch_name("--upload-pack=x")
        with self.assertRaises(W.WorkspaceError):
            W.check_branch_name("a..b")

    def test_reuse_of_a_never_launched_attempt_keeps_its_base(self):
        self.approve()
        n, adir = X.acquire_attempt(self.root, "demo", "alpha")
        base = sh(self.origin, "rev-parse", "refs/heads/main")
        store.write_json_atomic(W.workspace_path(adir), {"base": base, "base_ref": "origin/main", "target": "main",
                                                          "branch": "overseer/demo/alpha/a1",
                                                          "worktree": os.path.join(adir, "worktree"), "time": "t"})
        self.author("true")
        self.assertEqual(self.cli("dispatch", "--unit", "alpha")[0], 0)
        self.assertEqual(approval.load_unit_state(self.root, "demo", "alpha")["counters"]["attempts"], 1)


class Predicates(Case):
    def repo(self):
        wt = os.path.join(self.tmp, "pred")
        sh(self.tmp, "init", "-q", wt)
        self.put("ok.txt", "hello\n", base=wt)
        self.put("empty.txt", "", base=wt)
        os.symlink("/etc/hostname", os.path.join(wt, "link.txt"))
        os.makedirs(os.path.join(wt, "d"))
        self.put("d/f.txt", "x\n", base=wt)
        sh(wt, *(ID + ["add", "-A"]))
        sh(wt, *(ID + ["commit", "-q", "-m", "c"]))
        return wt, sh(wt, "rev-parse", "HEAD")

    def test_code_outputs_existence_symlink_size_digest_and_validator(self):
        good = hashlib.sha256(b"hello\n").hexdigest()
        wt, head = self.repo()
        r = dict((o["path"], o) for o in J.code_outputs(wt, head, {"outputs": ["ok.txt", "nope.txt", "link.txt", "d", "empty.txt"]}))
        self.assertTrue(r["ok.txt"]["ok"])
        self.assertEqual(r["nope.txt"]["problems"], ["missing at candidate"])
        self.assertIn("not a regular file", r["link.txt"]["problems"][0])
        self.assertIn("not a regular file", r["d"]["problems"][0])
        self.assertTrue(r["empty.txt"]["ok"])  # code default min_bytes is 0
        res = J.code_outputs(wt, head, {"outputs": [{"path": "ok.txt", "min_bytes": 7}, {"path": "ok.txt", "sha256": good},
                                                    {"path": "ok.txt", "sha256": "0" * 64},
                                                    {"path": "ok.txt", "validate": 'test "$(cat "$OVERSEER_OUTPUT")" = hello'},
                                                    {"path": "ok.txt", "validate": "exit 5"}]})
        self.assertEqual([o["ok"] for o in res], [False, True, False, True, False])
        self.assertEqual(res[4]["problems"], ["validator exited 5"])
        self.assertIn("sha256 mismatch", res[2]["problems"])

    def test_job_outputs_missing_symlink_escape_size_digest_validator(self):
        adir = os.path.join(self.tmp, "att")
        out = os.path.join(adir, "outputs")
        os.makedirs(out)
        self.put("ok.txt", "hello\n", base=out)
        self.put("empty.txt", "", base=out)
        self.put("../../secret.txt", "s\n", base=out)
        os.symlink(os.path.join(adir, "..", "secret.txt"), os.path.join(out, "esc.txt"))
        os.symlink("ok.txt", os.path.join(out, "inside.txt"))
        outs = ["ok.txt", "nope.txt", "empty.txt", "esc.txt", "inside.txt", "../x", {"path": "ok.txt", "min_bytes": 9},
                {"path": "ok.txt", "sha256": hashlib.sha256(b"hello\n").hexdigest()},
                {"path": "ok.txt", "validate": "exit 1"}, {"path": "ok.txt", "validate": 'test -f "$OVERSEER_OUTPUT"'}]
        res = J.job_outputs(adir, {"outputs": outs})
        self.assertEqual([o["ok"] for o in res], [True, False, False, False, True, False, False, True, False, True])
        self.assertEqual(res[1]["problems"], ["missing"])
        self.assertIn("min_bytes", res[2]["problems"][0])  # job default min_bytes is 1
        self.assertIn("outside", res[3]["problems"][0])


class Jobs(Case):
    def job(self, command, **kw):
        self.approve(job={"command": command})
        self.assertEqual(self.cli("dispatch", "--unit", "crunch")[0], 0)
        self.wait_done("crunch")
        return json.loads(self.cli("judge", "crunch")[1])

    def test_scheduler_success_without_output_fails_and_with_output_is_done(self):
        j = self.job("true")
        self.assertEqual((j["status"], j["execution"]["rc"]), ("FAILED", 0))
        self.assertEqual(j["reasons"], ["output_failed:result.txt: missing"])

    def test_success_with_output_is_done(self):
        j = self.job('echo hi > "$OVERSEER_OUT/result.txt"')
        self.assertEqual((j["status"], j["outputs"][0]["ok"]), ("DONE", True))
        rc, out, _ = self.cli("judge", "crunch")
        self.assertEqual(rc, 0)

    def test_passing_output_with_failure_evidence_is_failed(self):
        j = self.job('echo hi > "$OVERSEER_OUT/result.txt"; exit 3')
        self.assertEqual(j["status"], "FAILED")
        self.assertTrue(j["reasons"][0].startswith("execution_failed:"), j["reasons"])
        self.assertTrue(j["outputs"][0]["ok"])

    def test_running_job_refuses_judgment(self):
        self.approve(job={"command": "sleep 30"})
        self.cli("dispatch", "--unit", "crunch")
        rc, _, err = self.cli("judge", "crunch")
        self.assertEqual((rc, "RUNNING" in err), (1, True))

    def uncertain_job(self):
        self.approve(job={"command": "sleep 30"})
        self.cli("dispatch", "--unit", "crunch")
        inv = list(X.iter_invocations(self.root, "demo", "crunch"))[0]["path"]
        ln = X._load(os.path.join(inv, "launch.json"))
        os.killpg(ln["pgid"], signal.SIGKILL)  # no exit.json: the outcome is unknowable
        time.sleep(0.3)
        self.assertEqual(X.invocation_status(inv, True)["state"], "UNCERTAIN")
        return inv

    def test_resolve_done_needs_passing_predicates_and_is_labeled(self):
        inv = self.uncertain_job()
        j = json.loads(self.cli("judge", "crunch")[1])
        self.assertEqual(j["status"], "UNCERTAIN")
        self.assertEqual(X.slots_in_use(self.root, True), 1)
        rc, _, err = self.cli("resolve", "crunch", "--as", "done", "--reason", "looked", "--evidence", "ls out")
        self.assertEqual(rc, 1)
        self.assertIn("output predicates fail", err)
        self.assertEqual(X.invocation_status(inv, True)["state"], "UNCERTAIN")
        self.assertFalse(os.path.exists(os.path.join(inv, "resolved.json")))
        self.put("result.txt", "data\n", base=os.path.join(self.attempt_dir("crunch"), "outputs"))
        rc, out, err = self.cli("resolve", "crunch", "--as", "done", "--reason", "looked", "--evidence", "ls out")
        self.assertEqual(rc, 0, err)
        rec = jload(os.path.join(self.attempt_dir("crunch"), "judgment.json"))
        self.assertEqual((rec["status"], rec["orchestrator_resolved"], rec["reason"], rec["evidence"]),
                         ("DONE", True, "looked", "ls out"))
        self.assertEqual((X.invocation_status(inv, True)["state"], X.slots_in_use(self.root, True)), ("RESOLVED", 0))
        self.assertTrue(any(e["kind"] == "judged" and e.get("orchestrator_resolved") for e in store.read_events(self.root)))
        rc, _, err = self.cli("resolve", "crunch", "--as", "failed", "--reason", "r", "--evidence", "e")
        self.assertEqual((rc, "not UNCERTAIN" in err), (1, True))
        rc, _, err = self.cli("judge", "crunch")
        self.assertEqual((rc, "resolved by the orchestrator" in err), (1, True))

    def test_resolve_failed_clears_unidentified_and_unconfirmed_cancellations(self):
        inv = self.uncertain_job()
        intent = X._load(os.path.join(inv, "intent.json"))
        store.write_json_atomic(os.path.join(inv, "cancel.json"), {"token": intent["token"], "requested_at": "t"})
        self.assertEqual(X.invocation_status(inv, True)["state"], "CANCELLING")
        store.write_json_atomic(os.path.join(inv, "cancel.json"),
                                {"token": intent["token"], "requested_at": "t", "unidentified": "no pid"})
        self.assertEqual(X.invocation_status(inv, True)["state"], "UNCERTAIN")
        rc, _, err = self.cli("resolve", "crunch", "--as", "failed", "--reason", "gave up", "--evidence", "none")
        self.assertEqual(rc, 0, err)
        cancel = jload(os.path.join(inv, "cancel.json"))
        self.assertEqual(cancel["orchestrator_resolved"]["as"], "failed")
        self.assertEqual((X.invocation_status(inv, True)["state"], X.slots_in_use(self.root, True)), ("RESOLVED", 0))
        self.assertEqual(jload(os.path.join(self.attempt_dir("crunch"), "judgment.json"))["status"], "FAILED")

    def test_resolve_requires_reason_and_evidence(self):
        self.uncertain_job()
        rc, _, err = self.cli("resolve", "crunch", "--as", "failed", "--reason", " ", "--evidence", "e")
        self.assertEqual((rc, "non-blank" in err), (1, True))


if __name__ == "__main__":
    unittest.main()
