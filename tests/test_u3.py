import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from helpers import ENTRY, TmpCase, plan_copy

from overseer import approval, cli, controller as C, execution as X, slurm, store


def job(uid, command="true"):
    return {"id": uid, "kind": "job", "title": uid, "command": command, "outputs": ["r.txt"], "needs": []}


def make_plan(pid="demo", n=4, workers=3):
    p = plan_copy()
    p["plan_id"] = pid
    p["caps"] = {"workers": workers, "attempts": 2}
    p["units"] = [job("j%d" % i) for i in range(1, n + 1)]
    return p


def live_group_members(pgid):
    """Non-zombie processes in a process group. killpg(pgid, 0) also counts zombies, which
    linger in containers whose PID 1 does not reap orphans (seen on CI)."""
    if not os.path.isdir("/proc/self"):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return []
        return ["unknown"]
    live = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name) as f:
                rest = f.read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(rest[2]) == pgid and rest[0] != "Z":
            live.append(int(name))
    return live


class Crash(Exception):
    pass


def sysctl_usable():
    return sys.platform == "darwin" and isinstance(X._darwin_start(os.getpid()), tuple)


class Case(TmpCase):
    def setUp(self):
        super().setUp()
        sysbin = os.path.join(self.tmp, "sysbin")
        os.makedirs(sysbin)
        for t in ("sh", "sleep", "env", "cat", "echo", "git"):
            src = shutil.which(t)
            if src:
                os.symlink(src, os.path.join(sysbin, t))
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.penv = {"PATH": os.pathsep.join([self.shims, sysbin]), "HOME": self.tmp, "LC_ALL": "C.UTF-8",
                     "SLURM_JOB_ID": "1"}  # the sbatch shim makes this look like a Slurm host: be inside an allocation
        patcher = mock.patch.dict(os.environ, self.penv, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.kill_all)
        self.approve("demo")

    def approve(self, pid, n=4, workers=3):
        approval.approve_plan(self.root, make_plan(pid, n, workers), "me")

    def kill_all(self):
        for i in list(X.iter_invocations(self.root)):
            ln = X._load(os.path.join(i["path"], "launch.json"))
            if isinstance(ln, dict) and isinstance(ln.get("pgid"), int) and ln["pgid"] != os.getpgrp():
                try:
                    os.killpg(ln["pgid"], signal.SIGKILL)
                except OSError:
                    pass
        for c in list(X._CHILDREN):  # reap what this test started, so nothing outlives it
            try:
                c.kill()
                c.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        X._CHILDREN[:] = []

    def script(self, name, body):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, 0o755)
        return p

    def launch(self, uid="j1", argv=None, kind="author", plan="demo", n=None, **kw):
        if n is None:
            n = approval.load_unit_state(self.root, plan, uid)["counters"]["attempts"]
            if n == 0 or kw.pop("new", False):
                n, _ = X.allocate_attempt(self.root, plan, uid)
        return X.launch_invocation(self.root, plan, uid, n, kind, argv or [self.script("sl", "sleep 30")],
                                   self.root, **kw)

    def wait_for(self, inv, states=("EXITED",), timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            st = X.invocation_status(inv, True)
            if st["state"] in states:
                return st
            time.sleep(0.05)
        self.fail("timeout; last status %r" % (X.invocation_status(inv),))

    def wait_file(self, path, timeout=10):
        end = time.monotonic() + timeout
        while not os.path.exists(path):
            if time.monotonic() > end:
                self.fail("timeout waiting for %s" % path)
            time.sleep(0.02)

    def rec(self, inv, name):
        with open(os.path.join(inv, name)) as f:
            return json.load(f)


class AttemptTests(Case):
    def test_allocate_caps_and_exclusive(self):
        n, d = X.allocate_attempt(self.root, "demo", "j1")
        self.assertEqual((n, os.path.basename(d)), (1, "a1"))
        self.assertEqual(approval.load_unit_state(self.root, "demo", "j1")["counters"]["attempts"], 1)
        self.assertEqual(X.allocate_attempt(self.root, "demo", "j1")[0], 2)
        with self.assertRaisesRegex(X.ExecutionError, "attempts cap"):
            X.allocate_attempt(self.root, "demo", "j1")
        stray = os.path.join(self.root, ".overseer", "plans", "demo", "units", "j2", "attempts", "a1")
        os.makedirs(stray)  # an unmarked directory with content is never adopted
        with open(os.path.join(stray, "evidence.txt"), "w") as f:
            f.write("x")
        with self.assertRaisesRegex(X.ExecutionError, "already exists"):
            X.allocate_attempt(self.root, "demo", "j2")

    def test_allocation_crash_converges(self):
        with mock.patch.object(approval, "save_unit_state", side_effect=Crash):
            with self.assertRaises(Crash):
                X.allocate_attempt(self.root, "demo", "j1")
        self.assertEqual(approval.load_unit_state(self.root, "demo", "j1")["counters"]["attempts"], 0)
        self.assertEqual(X.allocate_attempt(self.root, "demo", "j1")[0], 1)
        self.assertEqual(approval.load_unit_state(self.root, "demo", "j1")["counters"]["attempts"], 1)

    def test_empty_unmarked_attempt_dir_is_adopted(self):
        adir = os.path.join(self.root, ".overseer", "plans", "demo", "units", "j1", "attempts", "a1")
        os.makedirs(adir)  # crash between mkdir and the marker
        self.assertEqual(X.allocate_attempt(self.root, "demo", "j1")[0], 1)
        self.assertEqual(self.rec(adir, "allocated.json")["attempt"], 1)

    def test_dispatch_reuse_still_runs_admission_checks(self):
        with mock.patch.object(X, "launch_invocation", side_effect=Crash):
            with self.assertRaises(Crash):
                cli.main(["--root", self.root, "dispatch", "--unit", "j1"])
        st = approval.load_unit_state(self.root, "demo", "j1")
        st["closure"] = {"state": "abandoned"}
        approval.save_unit_state(self.root, "demo", "j1", st)
        unit = {"id": "j1", "command": "true"}
        with self.assertRaisesRegex(X.ExecutionError, "abandoned"):
            X.dispatch_job(self.root, "demo", unit)
        st["closure"] = {"state": "open"}
        approval.save_unit_state(self.root, "demo", "j1", st)
        os.unlink(os.path.join(self.root, ".overseer", "plans", "demo", "approval.json"))
        with self.assertRaisesRegex(approval.ApprovalError, "approval"):
            X.dispatch_job(self.root, "demo", unit)

    def test_refused_while_active(self):
        self.launch("j1")
        with self.assertRaisesRegex(X.ExecutionError, "RUNNING"):
            X.allocate_attempt(self.root, "demo", "j1")

    def test_refused_while_uncertain(self):
        with self.assertRaises(Crash):
            self.launch("j1", spawn=mock.Mock(side_effect=Crash))  # intent, no process: UNCERTAIN
        with self.assertRaisesRegex(X.ExecutionError, "UNCERTAIN"):
            X.allocate_attempt(self.root, "demo", "j1")

    def test_quota_needs_flag(self):
        n, _ = X.allocate_attempt(self.root, "demo", "j1")
        X.record_classification(self.root, "demo", "j1", n, "QUOTA")
        with self.assertRaisesRegex(X.ExecutionError, "--after-quota"):
            X.allocate_attempt(self.root, "demo", "j1")
        self.assertEqual(X.allocate_attempt(self.root, "demo", "j1", after_quota=True)[0], 2)

    def test_unreadable_classification_blocks(self):
        n, d = X.allocate_attempt(self.root, "demo", "j1")
        with open(os.path.join(d, "classification.json"), "w") as f:
            f.write("[1]")
        with self.assertRaisesRegex(X.ExecutionError, "unreadable"):
            X.allocate_attempt(self.root, "demo", "j1")

    def test_counter_is_type_checked(self):
        st = approval.load_unit_state(self.root, "demo", "j1")
        st["counters"]["attempts"] = "1"
        approval.save_unit_state(self.root, "demo", "j1", st)
        with self.assertRaisesRegex(X.ExecutionError, "not a non-negative integer"):
            X.allocate_attempt(self.root, "demo", "j1")


class RecordTests(Case):
    def test_intent_launch_start_exit_and_rc(self):
        rec = self.launch("j1", [self.script("three", "echo out; echo err >&2; exit 3")], stdin_text="hi")
        inv = rec["dir"]
        self.assertEqual(os.path.basename(inv), "01-author")
        st = self.wait_for(inv)
        self.assertEqual((st["rc"], st["signal"]), (3, None))
        intent, launch = self.rec(inv, "intent.json"), self.rec(inv, "launch.json")
        start, ex = self.rec(inv, "start.json"), self.rec(inv, "exit.json")
        self.assertEqual(len(intent["token"]), 32)
        self.assertEqual(intent["argv_sha256"], X.argv_digest(intent["argv"]))
        self.assertTrue(os.path.isabs(intent["argv"][0]))
        self.assertEqual({launch["token"], start["token"], ex["token"]}, {intent["token"]})
        self.assertEqual(ex["argv_sha256"], intent["argv_sha256"])
        self.assertEqual((start["pid"], launch["pgid"]), (launch["pid"], launch["pid"]))
        self.assertEqual(launch["start"], X.proc_start(launch["pid"], True) or launch["start"])
        with open(os.path.join(inv, "stdout.log")) as f:
            self.assertEqual(f.read(), "out\n")
        with open(os.path.join(inv, "stderr.log")) as f:
            self.assertEqual(f.read(), "err\n")
        self.assertEqual(self.rec(inv, "intent.json")["kind"], "author")

    def test_exit_zero_after_sleep(self):
        rec = self.launch("j1", [self.script("ok", "sleep 0.3; exit 0")])
        self.assertEqual(X.invocation_status(rec["dir"])["state"], "RUNNING")
        self.assertEqual(self.wait_for(rec["dir"])["rc"], 0)

    def test_payload_sigkill_is_failure_not_success(self):
        rec = self.launch("j1", [self.script("k9", "kill -9 $$")])
        st = self.wait_for(rec["dir"])
        self.assertEqual((st["rc"], st["signal"]), (137, None))

    def test_exit_json_has_no_payload_signal_field(self):
        rec = self.launch("j1", [self.script("t", "kill -TERM $$")])
        self.wait_for(rec["dir"])
        self.assertEqual(self.rec(rec["dir"], "exit.json")["rc"], 143)
        self.assertNotIn("payload_signal", self.rec(rec["dir"], "exit.json"))

    def test_signalled_wrapper_never_records_rc_zero(self):
        # The payload ignores TERM and exits 0; only the wrapper is signalled, so its trap
        # sees $? == 0. A signalled invocation must still record a nonzero rc.
        ready = os.path.join(self.tmp, "ready-t")
        rec = self.launch("j1", [self.script("ign", "trap '' TERM; : > '%s'; sleep 1; exit 0" % ready)])
        end = time.time() + 10
        while not os.path.exists(ready) and time.time() < end:
            time.sleep(0.05)
        os.kill(rec["launch"]["pid"], signal.SIGTERM)
        self.wait_for(rec["dir"])
        ex = self.rec(rec["dir"], "exit.json")
        self.assertEqual((ex["rc"], ex["signal"]), (143, "TERM"))

    def test_wrapper_sigkill_is_uncertain(self):
        rec = self.launch("j1")
        os.killpg(rec["launch"]["pgid"], signal.SIGKILL)
        st = self.wait_for(rec["dir"], ("UNCERTAIN",))
        self.assertFalse(os.path.exists(os.path.join(rec["dir"], "exit.json")))
        self.assertIn("no matching exit.json", st["reason"])
        self.assertEqual(X.slots_in_use(self.root), 1)

    def test_pid_reuse_is_not_running(self):
        rec = self.launch("j1")
        ln = self.rec(rec["dir"], "launch.json")
        ln["start"] = "1"
        store.write_json_atomic(os.path.join(rec["dir"], "launch.json"), ln)
        self.assertEqual(X.invocation_status(rec["dir"])["state"], "UNCERTAIN")

    def test_foreign_start_json_pid_is_not_running(self):
        rec = self.launch("j1")
        self.wait_for_file(rec["dir"], "start.json")
        st = self.rec(rec["dir"], "start.json")
        st["pid"] += 1
        store.write_json_atomic(os.path.join(rec["dir"], "start.json"), st)
        self.assertEqual(X.invocation_status(rec["dir"])["state"], "UNCERTAIN")

    def wait_for_file(self, inv, name):
        end = time.time() + 10
        while not os.path.exists(os.path.join(inv, name)) and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(os.path.join(inv, name)))

    def test_stale_or_mismatched_exit_json_is_ignored(self):
        a = self.launch("j1", [self.script("one", "exit 0")])
        self.wait_for(a["dir"])
        n2, _ = X.allocate_attempt(self.root, "demo", "j1")
        with self.assertRaises(Crash):
            self.launch("j1", n=n2, spawn=mock.Mock(side_effect=Crash))
        b = {"dir": [i["path"] for i in X.iter_invocations(self.root) if i["attempt"] == 2][0]}
        shutil.copy(os.path.join(a["dir"], "exit.json"), os.path.join(b["dir"], "exit.json"))
        self.assertIn("does not match", X.invocation_status(b["dir"])["reason"])
        ex = self.rec(a["dir"], "exit.json")
        ex["argv_sha256"] = "0" * 64
        store.write_json_atomic(os.path.join(a["dir"], "exit.json"), ex)
        self.assertEqual(X.invocation_status(a["dir"])["state"], "UNCERTAIN")
        ex.update(argv_sha256=self.rec(a["dir"], "intent.json")["argv_sha256"], token="0" * 32)
        store.write_json_atomic(os.path.join(a["dir"], "exit.json"), ex)  # right digest, other invocation's token
        self.assertEqual(X.invocation_status(a["dir"])["state"], "UNCERTAIN")
        ex.update(token=self.rec(a["dir"], "intent.json")["token"], rc="0")
        store.write_json_atomic(os.path.join(a["dir"], "exit.json"), ex)  # rc must be an int
        self.assertEqual(X.invocation_status(a["dir"])["state"], "UNCERTAIN")

    def test_tampered_intent_digest_is_uncertain(self):
        rec = self.launch("j1")
        it = self.rec(rec["dir"], "intent.json")
        it["argv"] = it["argv"] + ["x"]
        store.write_json_atomic(os.path.join(rec["dir"], "intent.json"), it)
        self.assertEqual(X.invocation_status(rec["dir"])["state"], "UNCERTAIN")

    def test_env_canary_through_run_sh(self):
        canary = {"FAKE_API_KEY": "k", "GH_TOKEN": "t", "ANTHROPIC_BASE_URL": "u", "AWS_PROFILE": "p"}
        with mock.patch.dict(os.environ, canary):
            rec = self.launch("j1", [self.script("pe", "env")], extras={"OVERSEER_OUT": "/x"})
        self.wait_for(rec["dir"])
        with open(os.path.join(rec["dir"], "stdout.log")) as f:
            out = f.read()
        for k in canary:
            self.assertNotIn(k, out)
        self.assertIn("OVERSEER_OUT=/x", out)

    def test_command_must_resolve_absolutely(self):
        with self.assertRaisesRegex(X.ExecutionError, "not found"):
            self.launch("j1", ["no-such-tool"])

    def test_two_invocations_keep_separate_records(self):
        a = self.launch("j1", [self.script("a", "echo A")], kind="author")
        self.wait_for(a["dir"])
        b = self.launch("j1", [self.script("b", "echo B; exit 2")], kind="fixer", n=1)
        self.wait_for(b["dir"])
        self.assertEqual([os.path.basename(x["dir"]) for x in (a, b)], ["01-author", "02-fixer"])
        ta, tb = (self.rec(x["dir"], "intent.json")["token"] for x in (a, b))
        self.assertNotEqual(ta, tb)
        for x, t, rc, out in ((a, ta, 0, "A\n"), (b, tb, 2, "B\n")):
            self.assertEqual(self.rec(x["dir"], "exit.json")["token"], t)
            self.assertEqual(self.rec(x["dir"], "exit.json")["rc"], rc)
            self.assertEqual(self.rec(x["dir"], "start.json")["token"], t)
            with open(os.path.join(x["dir"], "stdout.log")) as f:
                self.assertEqual(f.read(), out)


class CrashTests(Case):
    def test_crash_before_intent_leaves_nothing_active(self):
        with mock.patch.object(X, "_write_scripts", side_effect=Crash):
            with self.assertRaises(Crash):
                self.launch("j1")
        self.assertEqual(X.slots_in_use(self.root), 0)
        rec = self.launch("j1")  # converges: the next sequence number, no duplicate effect
        self.assertEqual(os.path.basename(rec["dir"]), "02-author")

    def test_intent_without_launch_is_uncertain_and_never_relaunched(self):
        marker = os.path.join(self.tmp, "ran")
        spawn = mock.Mock(side_effect=Crash)
        with self.assertRaises(Crash):
            self.launch("j1", [self.script("m", "echo x >> %s" % marker)], spawn=spawn)
        inv = next(X.iter_invocations(self.root))["path"]
        self.assertFalse(os.path.exists(os.path.join(inv, "launch.json")))
        for _ in range(2):
            self.assertEqual(X.invocation_status(inv, True)["state"], "UNCERTAIN")
        self.assertEqual(X.slots_in_use(self.root, True), 1)
        time.sleep(0.2)
        self.assertEqual(spawn.call_count, 1)
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(os.path.exists(os.path.join(inv, "start.json")))
        self.assertFalse(os.path.exists(os.path.join(inv, "launch.json")))

    def test_decoy_mentioning_run_sh_is_not_adopted(self):
        with self.assertRaises(Crash):
            self.launch("j1", spawn=mock.Mock(side_effect=Crash))
        inv = next(X.iter_invocations(self.root))["path"]
        run_sh = self.rec(inv, "intent.json")["run_sh"]
        decoy = subprocess.Popen([shutil.which("cat"), run_sh, "-"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        self.addCleanup(decoy.wait)
        self.addCleanup(decoy.stdin.close)
        time.sleep(0.1)
        self.assertEqual(decoy.poll(), None)
        self.assertEqual(X.invocation_status(inv, True)["state"], "UNCERTAIN")
        self.assertFalse(os.path.exists(os.path.join(inv, "launch.json")))

    def test_crash_after_spawn_is_reconciled_from_process_table(self):
        def spawn_then_crash(inv, run_sh, envd, cwd):
            X._spawn_local(inv, run_sh, envd, cwd)
            raise Crash
        with self.assertRaises(Crash):
            self.launch("j1", spawn=spawn_then_crash)
        inv = next(X.iter_invocations(self.root))["path"]
        self.assertFalse(os.path.exists(os.path.join(inv, "launch.json")))
        st = X.invocation_status(inv, True)
        self.assertEqual(st["state"], "RUNNING")
        ln = self.rec(inv, "launch.json")
        self.assertTrue(ln["reconciled"])
        self.assertEqual(ln["token"], self.rec(inv, "intent.json")["token"])
        self.assertEqual(X.slots_in_use(self.root), 1)

    def test_dispatch_crash_before_launch_reuses_attempt(self):
        with mock.patch.object(X, "launch_invocation", side_effect=Crash):
            with self.assertRaises(Crash):
                self.run_cli("dispatch", "--unit", "j1")
        self.assertEqual(approval.load_unit_state(self.root, "demo", "j1")["counters"]["attempts"], 1)
        rc, out, err = self.run_cli("dispatch", "--unit", "j1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["attempt"], 1)

    def run_cli(self, *args):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = cli.main(["--root", self.root] + list(args))
        return rc, o.getvalue(), e.getvalue()


class SlotTests(Case):
    def test_cap_counts_across_plans_and_kinds(self):
        self.approve("other", 1)
        self.launch("j1", kind="author")
        self.launch("j2", kind="job")
        self.launch("j1", plan="other", kind="fixer")
        self.assertEqual(X.slots_in_use(self.root), 3)
        with self.assertRaises(X.SlotsFull):
            self.launch("j3")
        self.assertEqual(len([i for i in X.iter_invocations(self.root)]), 3)

    def test_plan_cap_and_project_limit_are_separate(self):
        self.approve("solo", 2, workers=1)
        self.launch("j1", plan="demo")
        self.launch("j1", plan="solo")  # demo's work does not block the idle plan
        with self.assertRaisesRegex(X.SlotsFull, "plan solo has 1 of its 1"):
            self.launch("j2", plan="solo")
        self.launch("j2", plan="demo")
        with self.assertRaisesRegex(X.SlotsFull, "project-wide limit of 3"):
            self.launch("j3", plan="demo")
        self.assertEqual(X.slots_in_use(self.root, plan_id="demo"), 2)

    def wide(self, n=6, workers=1, job_workers=5):
        p = make_plan("wide", n, workers)
        p["caps"]["job_workers"] = job_workers
        approval.approve_plan(self.root, p, "me")
        probe = mock.patch.dict(X.PROBES, {"slurm": lambda *a: "QUEUED"})  # no squeue: the jobs stay queued
        probe.start()
        self.addCleanup(probe.stop)

    def sbatch(self, uid, plan="wide", kind="job"):
        return self.launch(uid, plan=plan, kind=kind, backend="slurm", spawn=lambda inv, run_sh, env, cwd: {"job_id": "9%s" % uid[1:]})

    def test_slurm_jobs_use_their_own_cap(self):
        self.wide()
        for u in ("j1", "j2", "j3", "j4", "j5"):
            self.sbatch(u)
        self.assertEqual(X.slots_in_use(self.root, pool="job_workers"), 5)
        self.assertEqual(X.slots_in_use(self.root), 0)
        with self.assertRaisesRegex(X.SlotsFull, "plan wide has 5 of its 5 Slurm job slots"):
            self.sbatch("j6")
        self.launch("j6", plan="wide")  # an author still has its worker slot
        with self.assertRaisesRegex(X.SlotsFull, "plan wide has 1 of its 1 worker slots"):
            self.launch("j6", plan="wide", kind="job", n=1)  # a job on this host counts as a worker

    def test_slurm_job_slots_have_a_project_wide_limit(self):
        self.wide()
        with mock.patch.dict(X.POOL_LIMIT, {"job_workers": 2}):
            self.sbatch("j1")
            self.sbatch("j1", plan="demo")
            with self.assertRaisesRegex(X.SlotsFull, "project-wide limit of 2 Slurm job slots"):
                self.sbatch("j2")

    def test_next_dispatches_up_to_the_job_cap_on_slurm_only(self):
        self.wide(n=6, workers=3, job_workers=5)
        acts = lambda: [a["unit"] for a in C.compute_actions(self.root, "wide") if a["action"] == "dispatch"]
        with mock.patch.object(slurm, "choose_backend", return_value="local"):
            self.assertEqual(acts(), ["j1", "j2", "j3"])
        with mock.patch.object(slurm, "choose_backend", return_value="slurm"):
            self.assertEqual(acts(), ["j1", "j2", "j3", "j4", "j5"])

    def test_finished_invocation_frees_slot(self):
        for u in ("j1", "j2", "j3"):
            self.launch(u, [self.script("q", "exit 0")])
        for i in list(X.iter_invocations(self.root)):
            self.wait_for(i["path"])
        self.assertEqual(X.slots_in_use(self.root), 0)
        self.launch("j4")


class CommandTests(Case):
    def run_cli(self, *args, **kw):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = cli.main(["--root", self.root] + list(args))
        return rc, o.getvalue(), e.getvalue()

    def test_dispatch_job_sets_overseer_out_and_launcher_exit_keeps_payload(self):
        p = make_plan()
        p["units"][0]["command"] = 'sleep 1; echo "$OVERSEER_OUT" > "$OVERSEER_OUT/o.txt"; exit 3'
        approval.approve_plan(self.root, p, "me")
        r = subprocess.run([sys.executable, ENTRY, "--root", self.root, "dispatch", "--unit", "j1"],
                           env=self.penv, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        inv = next(X.iter_invocations(self.root))["path"]
        self.assertEqual(X.invocation_status(inv)["state"], "RUNNING")  # the launcher is gone
        st = self.wait_for(inv)
        self.assertEqual(st["rc"], 3)
        out = os.path.join(os.path.dirname(os.path.dirname(inv)), "outputs")
        with open(os.path.join(out, "o.txt")) as f:
            self.assertEqual(f.read().strip(), out)

    def test_dispatch_refuses_second_while_running_and_code_without_models(self):
        p = make_plan()
        p["units"][0]["command"] = "sleep 30"
        approval.approve_plan(self.root, p, "me")
        self.assertEqual(self.run_cli("dispatch", "--unit", "j1")[0], 0)
        rc, _, err = self.run_cli("dispatch", "--unit", "j1")
        self.assertEqual(rc, 1)
        self.assertIn("RUNNING", err)
        code = plan_copy()
        code["plan_id"] = "codeplan"
        approval.approve_plan(self.root, code, "me")
        rc, _, err = self.run_cli("dispatch", "--unit", "alpha")
        self.assertEqual(rc, 1)
        self.assertIn("models.json", err)  # code units dispatch through U5 and need the model cache

    def test_status_json_reports_slots_and_active(self):
        rec = self.launch("j1")
        rc, out, _ = self.run_cli("status", "--json")
        data = json.loads(out)
        self.assertEqual((rc, data["slots_in_use"], data["plans"]["demo"]["workers_cap"]), (0, 1, 3))
        self.assertEqual((data["project_limit"], data["plans"]["demo"]["slots_in_use"]), (3, 1))
        u = data["plans"]["demo"]["units"]["j1"]
        self.assertEqual(u["active"][0]["state"], "RUNNING")
        self.assertEqual((u["attempts"], u["authorization"]["state"]), (1, "approved"))
        self.assertEqual(data["plans"]["demo"]["units"]["j2"]["active"], [])
        rc, out, _ = self.run_cli("status")
        self.assertIn("slots 1/3 (project 1/3)", out)
        self.assertIn(rec["seq"], out)

    def test_wait_exit_codes(self):
        rec = self.launch("j1", [self.script("short", "sleep 0.6")])
        self.assertEqual(self.run_cli("wait", "--timeout", "0.3", "--poll", "0.1")[0], 3)
        rc, out, _ = self.run_cli("wait", "--timeout", "10", "--poll", "0.1")
        self.assertEqual(rc, 0)
        line = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((line["from"], line["to"], line["rc"], line["unit"]), ("RUNNING", "EXITED", 0, "j1"))
        self.assertEqual(self.run_cli("wait", "--timeout", "0.2")[0], 0)  # nothing active

    def test_wait_reports_a_new_invocation(self):
        self.launch("j1")
        n, _ = X.allocate_attempt(self.root, "demo", "j2")
        t = threading.Timer(0.4, lambda: self.launch("j2", n=n))
        t.start()
        self.addCleanup(t.join)
        lines = []
        self.assertEqual(X.wait_for_change(self.root, timeout=5, poll=0.1, emit=lines.append), 0)
        got = json.loads(lines[0])
        self.assertEqual((got["unit"], got["from"]), ("j2", None))

    def test_abort_cooperative(self):
        ready = os.path.join(self.tmp, "ready")
        rec = self.launch("j1", [self.script("rd", "echo > %s; sleep 30" % ready)])
        self.wait_file(ready)
        rc, out, _ = self.run_cli("abort", "j1", "--grace", "5")
        self.assertEqual(rc, 0, out)
        st = X.invocation_status(rec["dir"])
        self.assertEqual((st["state"], st["signal"], st["rc"]), ("EXITED", "TERM", 143))  # rc read before sig is set
        self.assertEqual(X.slots_in_use(self.root), 0)
        self.assertEqual(self.run_cli("abort", "j1")[1].strip(), "nothing to abort for j1")

    def wait_for_start(self, inv):
        end = time.time() + 10
        while not os.path.exists(os.path.join(inv, "start.json")) and time.time() < end:
            time.sleep(0.05)

    def test_abort_escalates_and_frees_slot_only_after_confirmation(self):
        ready = os.path.join(self.tmp, "ready")
        rec = self.launch("j1", [self.script("stubborn", "trap '' TERM; echo > %s; sleep 30" % ready)])
        self.wait_file(ready)  # TERM must arrive after the payload ignores it
        seen = []

        def spy(_):
            seen.append((X.slots_in_use(self.root), self.rec(rec["dir"], "cancel.json").get("confirmed_at")))
            time.sleep(0.1)

        res = X.abort_invocation(self.root, rec["dir"], grace=0.5, sleep=spy)
        self.assertEqual(res, "CANCELLED")
        self.assertTrue(seen and all(s == (1, None) for s in seen))  # requested, still holding the slot
        self.assertEqual(X.slots_in_use(self.root), 0)
        cancel = self.rec(rec["dir"], "cancel.json")
        self.assertLessEqual(cancel["requested_at"], cancel["confirmed_at"])
        kinds = [e["kind"] for e in store.read_events(self.root) if e["kind"].startswith("cancel_")]
        self.assertEqual(kinds, ["cancel_requested", "cancel_confirmed"])
        self.assertFalse(os.path.exists(os.path.join(rec["dir"], "exit.json")))  # SIGKILL skips the trap

    @unittest.skipUnless(os.path.isdir("/proc/self"), "finding a payload after its wrapper dies needs /proc")
    def test_abort_waits_for_whole_process_group(self):
        ready = os.path.join(self.tmp, "ready")
        rec = self.launch("j1", [self.script("kid", "trap '' TERM; echo > %s; sleep 30" % ready)])
        self.wait_file(ready)
        os.kill(rec["launch"]["pid"], signal.SIGKILL)  # the wrapper dies, its payload group lives on
        self.assertEqual(self.wait_for(rec["dir"], ("UNCERTAIN",))["state"], "UNCERTAIN")
        seen = []
        res = X.abort_invocation(self.root, rec["dir"], grace=0.4,
                                 sleep=lambda _: (seen.append(X.slots_in_use(self.root)), time.sleep(0.1)))
        self.assertEqual(res, "CANCELLED")
        self.assertTrue(seen and set(seen) == {1})
        self.assertEqual(live_group_members(rec["launch"]["pgid"]), [])
        self.assertEqual(X.slots_in_use(self.root), 0)

    def test_abort_without_identifiable_launch_is_not_confirmed(self):
        with self.assertRaises(Crash):
            self.launch("j1", spawn=mock.Mock(side_effect=Crash))
        inv = next(X.iter_invocations(self.root))["path"]
        self.assertEqual(X.abort_invocation(self.root, inv, grace=0.1), "UNIDENTIFIED")
        self.assertNotIn("confirmed_at", self.rec(inv, "cancel.json"))
        self.assertEqual(X.invocation_status(inv)["state"], "UNCERTAIN")
        self.assertEqual(X.slots_in_use(self.root), 1)
        kinds = [e["kind"] for e in store.read_events(self.root) if e["kind"].startswith("cancel_")]
        self.assertEqual(kinds, ["cancel_unidentified"])
        self.assertEqual(self.run_cli("abort", "j1")[0], 1)

    def start_decoy(self):
        d = subprocess.Popen([shutil.which("sleep"), "30"], start_new_session=True)
        self.addCleanup(d.wait)
        self.addCleanup(d.kill)
        return d

    def test_abort_never_signals_a_reused_pgid(self):
        rec = self.launch("j1", [self.script("pl", "sleep 30")])
        self.wait_for_start(rec["dir"])
        time.sleep(0.2)
        os.kill(rec["launch"]["pid"], signal.SIGKILL)  # wrapper gone, payload lives
        decoy = self.start_decoy()  # an unrelated session now owns the recorded pid and pgid
        ln = self.rec(rec["dir"], "launch.json")
        ln.update(pid=decoy.pid, pgid=decoy.pid)
        store.write_json_atomic(os.path.join(rec["dir"], "launch.json"), ln)
        have_proc = os.path.isdir("/proc/self")
        # Without /proc the orphaned payload cannot be found, so the honest answer is UNIDENTIFIED.
        want = "CANCELLED" if have_proc else "UNIDENTIFIED"
        self.assertEqual(X.abort_invocation(self.root, rec["dir"], grace=1), want)
        self.assertIsNone(decoy.poll())
        if have_proc:
            self.assertEqual(X._token_pids(self.rec(rec["dir"], "intent.json")["token"]), [])

    def test_start_identity_separates_processes_started_in_the_same_second(self):
        if sys.platform == "darwin" and not sysctl_usable():
            self.skipTest("the ps lstart fallback has one-second resolution")
        while time.time() % 1 > 0.5:  # start both well inside one wall-clock second
            time.sleep(0.01)
        a = self.start_decoy()
        time.sleep(0.05)
        b = self.start_decoy()
        sa, sb = X.proc_start(a.pid), X.proc_start(b.pid)
        self.assertTrue(sa and sb, (sa, sb))
        self.assertNotEqual(sa, sb)
        self.assertFalse(X.identified_alive(b.pid, sa))
        a.kill()
        a.wait()
        self.assertIsNone(X.proc_start(a.pid))

    @unittest.skipUnless(sys.platform == "darwin", "macOS sysctl path")
    def test_darwin_start_skips_zombies_unless_allowed(self):
        p = subprocess.Popen([shutil.which("sleep"), "30"])
        self.addCleanup(p.wait)
        start = X.proc_start(p.pid)
        p.kill()
        end = time.time() + 5
        while X.proc_start(p.pid) is not None and time.time() < end:  # killed, not yet reaped
            time.sleep(0.02)
        self.assertIsNone(X.proc_start(p.pid))
        self.assertEqual(X.proc_start(p.pid, allow_zombie=True), start)

    def test_unusable_sysctl_falls_back_to_ps(self):
        p = self.start_decoy()
        with mock.patch.object(X, "_darwin_start", return_value=X._UNUSABLE):
            start = X.proc_start(p.pid)
            self.assertTrue(start)
            self.assertTrue(X.identified_alive(p.pid, start))
        if sysctl_usable():
            self.assertRegex(X.proc_start(p.pid), X.SYSCTL_START_RE)
            self.assertTrue(X.identified_alive(p.pid, start))  # a ps-recorded identity still matches

    def test_recorded_sysctl_identity_is_not_checked_with_ps(self):
        p = self.start_decoy()
        if not sysctl_usable():
            self.skipTest("needs the macOS sysctl identity")
        start = X.proc_start(p.pid)
        with mock.patch.object(X, "_ps_exe", side_effect=AssertionError("ps used")):
            self.assertTrue(X.identified_alive(p.pid, start))

    def test_abort_without_proc_and_dead_wrapper_is_unidentified(self):
        rec = self.launch("j1", [self.script("pl", "sleep 30")])
        self.wait_for_start(rec["dir"])
        os.kill(rec["launch"]["pid"], signal.SIGKILL)
        self.wait_for(rec["dir"], ("UNCERTAIN",))
        with mock.patch.object(X, "_token_pids", return_value=None):
            self.assertEqual(X.abort_invocation(self.root, rec["dir"], grace=0.1), "UNIDENTIFIED")
        self.assertNotIn("confirmed_at", self.rec(rec["dir"], "cancel.json"))
        self.assertEqual(X.slots_in_use(self.root), 1)

    @unittest.skipUnless(os.path.isdir("/proc/self"), "finding a payload after its wrapper dies needs /proc")
    def test_outstanding_abort_holds_slot_after_exit_json(self):
        rec = self.launch("j1", [self.script("bg", "sleep 30 &")])  # wrapper exits, a child survives
        self.wait_for(rec["dir"])
        token = self.rec(rec["dir"], "intent.json")["token"]
        end = time.monotonic() + 5  # exit.json lands while the wrapper may still be exiting
        while len(X._token_pids(token)) != 1 and time.monotonic() < end:
            time.sleep(0.05)
        self.assertEqual(len(X._token_pids(token)), 1)
        store.write_json_atomic(os.path.join(rec["dir"], "cancel.json"),
                                {"token": token, "requested_at": store.utc_now()})
        self.assertEqual(X.invocation_status(rec["dir"])["state"], "CANCELLING")
        self.assertEqual(X.slots_in_use(self.root), 1)
        self.assertEqual(X.abort_invocation(self.root, rec["dir"], grace=1), "EXITED")  # resumed and confirmed
        self.assertEqual(X._token_pids(token), [])
        self.assertEqual(X.slots_in_use(self.root), 0)

    def test_launch_without_start_identity_is_terminated(self):
        pids = []

        def spawn(inv, run_sh, envd, cwd):
            f = X._spawn_local(inv, run_sh, envd, cwd)
            pids.append(f["pid"])
            return f
        with mock.patch.object(X, "proc_start", return_value=None):
            with self.assertRaisesRegex(X.ExecutionError, "no start identity"):
                self.launch("j1", spawn=spawn)
        inv = next(X.iter_invocations(self.root))["path"]
        self.assertEqual(X.invocation_status(inv)["state"], "LAUNCH_FAILED")
        end = time.time() + 5
        while time.time() < end:
            X._reap(0)
            try:
                os.killpg(pids[0], 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pids[0], 0)
        self.assertEqual(X.slots_in_use(self.root), 0)

    def test_ps_is_found_without_path(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertIn(X._ps_exe(), ("/bin/ps", "/usr/bin/ps"))

    def test_whitespace_run_sh_is_not_adopted_without_proc(self):
        real = os.path.isdir
        out = {"rc": 0, "stdout": "123 /bin/sh /a b/run.sh\n456 /bin/sh /ab/run.sh\n", "stderr": "", "timed_out": False}
        with mock.patch.object(X.os.path, "isdir", lambda p: False if p == "/proc/self" else real(p)), \
                mock.patch.object(X, "_ps_exe", return_value="/bin/ps"), \
                mock.patch.object(X.E, "run_tool", return_value=out):
            self.assertEqual(X.find_by_script("/a b/run.sh"), [])
            self.assertEqual(X.find_by_script("/ab/run.sh"), [456])

    @unittest.skipIf(os.getuid() == 0, "root can read every environ")
    @unittest.skipUnless(sys.platform.startswith("linux"), "prctl(PR_SET_DUMPABLE) and /proc are Linux-only")
    def test_abort_not_confirmed_while_an_undumpable_payload_may_run(self):
        ready = os.path.join(self.tmp, "ready")
        code = "import ctypes,time;ctypes.CDLL(None).prctl(4,0);open(%r,'w').close();time.sleep(30)" % ready
        rec = self.launch("j1", [self.script("ud", "exec '%s' -c \"%s\"" % (sys.executable, code))])
        self.wait_file(ready)
        os.kill(rec["launch"]["pid"], signal.SIGKILL)  # wrapper gone; the payload hides its environ
        self.wait_for(rec["dir"], ("UNCERTAIN",))
        token = self.rec(rec["dir"], "intent.json")["token"]
        self.assertTrue(X._token_pids(token, (rec["launch"]["pid"], rec["launch"]["pgid"])).unknown)
        self.assertEqual(X.abort_invocation(self.root, rec["dir"], grace=0.2, confirm_wait=0.2), "UNCONFIRMED")
        cancel = self.rec(rec["dir"], "cancel.json")
        self.assertNotIn("confirmed_at", cancel)
        self.assertIn("unreadable", cancel["cancel_unconfirmed_reason"])
        self.assertEqual(X.invocation_status(rec["dir"])["state"], "CANCELLING")

    def test_without_proc_surviving_group_member_blocks_confirmation(self):
        ready = os.path.join(self.tmp, "ready")
        rec = self.launch("j1", [self.script("kid", "(trap '' TERM; echo > %s; exec sleep 30) & wait" % ready)])
        self.wait_file(ready)
        with mock.patch.object(X, "_token_pids", return_value=None):
            res = X.abort_invocation(self.root, rec["dir"], grace=0.3, confirm_wait=0.3)
        self.assertEqual(res, "UNCONFIRMED")  # the wrapper exits on TERM; its child lives in the group
        self.assertNotIn("confirmed_at", self.rec(rec["dir"], "cancel.json"))
        self.assertEqual(X.slots_in_use(self.root), 1)

    def test_unidentified_cancel_is_not_cleared_by_a_later_exit_json(self):
        with self.assertRaises(Crash):
            self.launch("j1", spawn=mock.Mock(side_effect=Crash))
        inv = next(X.iter_invocations(self.root))["path"]
        self.assertEqual(X.abort_invocation(self.root, inv, grace=0.1), "UNIDENTIFIED")
        it = self.rec(inv, "intent.json")
        store.write_json_atomic(os.path.join(inv, "exit.json"), {
            "token": it["token"], "argv_sha256": it["argv_sha256"], "rc": 0, "signal": None})
        self.assertEqual(X.invocation_status(inv)["state"], "UNCERTAIN")
        self.assertEqual(X.slots_in_use(self.root), 1)

    def test_several_process_matches_are_not_adopted(self):
        with self.assertRaises(Crash):
            self.launch("j1", spawn=mock.Mock(side_effect=Crash))
        inv = next(X.iter_invocations(self.root))["path"]
        with mock.patch.object(X, "find_by_script", return_value=[os.getpid(), os.getppid()]):
            self.assertEqual(X.invocation_status(inv, True)["state"], "UNCERTAIN")
        self.assertFalse(os.path.exists(os.path.join(inv, "launch.json")))

    def test_unconfirmed_abort_keeps_slot(self):
        rec = self.launch("j1")
        with mock.patch.object(X, "identified_alive", return_value=True), \
                mock.patch.object(X.os, "killpg"), mock.patch.object(X.os, "kill"):
            self.assertEqual(X.abort_invocation(self.root, rec["dir"], grace=0.1, sleep=lambda s: None, confirm_wait=0.2), "UNCONFIRMED")
        self.assertNotIn("confirmed_at", self.rec(rec["dir"], "cancel.json"))
        self.assertEqual(X.slots_in_use(self.root), 1)


if __name__ == "__main__":
    unittest.main()
