import contextlib
import io
import json
import os
import shutil
import subprocess
import time
from unittest import mock

from helpers import REPO

from overseer import approval, cli, execution as X, slurm as S, store
from test_u3 import Case, Crash, make_plan

FIX = os.path.join(REPO, "tests", "fixtures", "slurm")
SLURM = {"partition": "batch-cpu", "account": "lab-account", "time": "00:30:00", "mem": "2G", "cpus": 2}

COMMON = '''
name=; com=; prev=
for w in $(tail -n 1 "$F/sbatch.calls" 2>/dev/null); do
  case $prev in --job-name) name=$w;; --comment) com=$w;; esac
  prev=$w
done
'''
SHIMS = {
    "sbatch": '''. "$F/common.sh"
echo "$@" >> "$F/sbatch.calls"
mode=$(cat "$F/sbatch.mode" 2>/dev/null || echo ok)
case $mode in
  reject) echo "sbatch: error: invalid partition specified" >&2; exit 1;;
  hang) sleep 30; exit 0;;
  junk) echo "submitted somewhere"; exit 0;;
esac
for a; do s=$a; done
if [ -f "$F/sbatch.run" ]; then
  if [ -f "$F/sbatch.blind" ]; then sed "s#reply.json#reply.elsewhere#" "$s" > "$F/blind.sh"; sh "$F/blind.sh"; else sh "$s"; fi
fi
echo 4242''',
    "squeue": '''. "$F/common.sh"
echo squeue >> "$F/squeue.calls"
if [ -f "$F/squeue.state" ]; then echo "4242|$name|$com|$(cat "$F/squeue.state")|None"; exit 0; fi
cat "$F/squeue.out" 2>/dev/null
exit $(cat "$F/squeue.rc" 2>/dev/null || echo 0)''',
    "scontrol": '''. "$F/common.sh"
if [ ! -f "$F/scontrol.state" ]; then echo "slurm_load_jobs error: Invalid job id specified" >&2; exit 1; fi
read st ec < "$F/scontrol.state"
echo "JobId=4242 JobName=$name"
echo "   JobState=$st Reason=None Dependency=(null)"
echo "   Requeue=1 Restarts=0 BatchFlag=1 Reboot=0 ExitCode=$ec"
echo "   Comment=$com"
echo "   SubmitLine=sbatch --comment $com run.sh"''',
    "sacct": '''. "$F/common.sh"
[ -f "$F/sacct.sleep" ] && sleep $(cat "$F/sacct.sleep")
if [ -f "$F/sacct.state" ]; then
  read st ec < "$F/sacct.state"
  echo "4242|$st|$ec|$name"; echo "4242.batch|$st|$ec|batch"; exit 0
fi
echo "sacct: error: Problem talking to the database: Connection timed out" >&2; exit 1''',
    "scancel": '''echo "$@" >> "$F/scancel.calls"
[ -f "$F/scancel.hang" ] && sleep 30
exit 0''',
}


class SlurmCase(Case):
    def setUp(self):
        super().setUp()
        self.F = os.path.join(self.tmp, "fake")
        os.makedirs(self.F)
        fakebin = self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(fakebin)
        with open(os.path.join(self.F, "common.sh"), "w") as f:
            f.write('F=%s\n%s' % (self.F, COMMON))
        for name, body in SHIMS.items():
            p = os.path.join(fakebin, name)
            with open(p, "w") as f:
                f.write("#!/bin/sh\nF=%s\n%s\n" % (self.F, body))
            os.chmod(p, 0o755)
        sysbin = os.path.join(self.tmp, "sysbin")
        for t in ("tail", "mv", "uname", "hostname", "sed"):
            src = shutil.which(t, path="/usr/bin:/bin:/usr/local/bin")
            if src and not os.path.exists(os.path.join(sysbin, t)):
                os.symlink(src, os.path.join(sysbin, t))
        os.environ["PATH"] = os.pathsep.join([fakebin, sysbin])
        for name, val in (("TMP_ROOTS", ()), ("SBATCH_TIMEOUT", 3), ("SACCT_TIMEOUT", 1), ("QUERY_TIMEOUT", 5)):
            p = mock.patch.object(S, name, val)
            p.start()
            self.addCleanup(p.stop)
        plan = make_plan("sl", 3)
        plan["slurm"] = dict(SLURM)
        approval.approve_plan(self.root, plan, "me")
        bare = make_plan("bare", 1)
        del bare["slurm"]
        approval.approve_plan(self.root, bare, "me")
        self.survey(True)
        self.handshake_record()

    def survey(self, usable):
        store.write_json_atomic(os.path.join(self.root, ".overseer", "survey.json"), {"slurm": {"usable": usable}})

    def handshake_record(self, when=None, root=None, token="a1b2c3", reply=True):
        d = os.path.join(self.root, ".overseer", "handshake", "a1b2c3")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "nonce"), "w") as f:
            f.write("nonce-1")
        if reply:
            store.write_json_atomic(os.path.join(d, "reply.json"), {"nonce": "nonce-1", "host": "node001"})
        store.write_json_atomic(os.path.join(self.root, ".overseer", "handshake.json"), {
            "root": root or self.root, "time": when or store.utc_now(), "token": token, "host": "node001"})

    def shim(self, name, body):
        p = os.path.join(self.fakebin, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")

    def fake(self, name, text=""):
        with open(os.path.join(self.F, name), "w") as f:
            f.write(text)

    def unfake(self, name):
        try:
            os.unlink(os.path.join(self.F, name))
        except OSError:
            pass

    def calls(self, name="sbatch"):
        try:
            with open(os.path.join(self.F, name + ".calls")) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def unit(self, plan, uid):
        return X._unit_of(approval.effective_plan(self.root, plan), uid)

    def dispatch(self, uid="j1", plan="sl"):
        return X.dispatch_job(self.root, plan, self.unit(plan, uid))

    def status(self, rec):
        return X.invocation_status(rec["dir"], True)

    def exit_rec(self, rec, rc, sig=None):
        store.write_json_atomic(os.path.join(rec["dir"], "exit.json"), {
            "token": rec["token"], "argv_sha256": rec["argv_sha256"], "rc": rc, "signal": sig, "end_time": "x"})

    def evidence(self, rec):
        return self.rec(rec["dir"], "evidence.json")


class ChoiceAndRefusals(SlurmCase):
    def test_backend_choice(self):
        self.assertEqual(S.choose_backend(self.root), "slurm")
        self.assertEqual(S.choose_backend(self.root, {"OVERSEER_DISABLE_SLURM": "1"}), "local")
        for usable in (False, "unknown"):
            self.survey(usable)
            self.assertEqual(S.choose_backend(self.root), "local")
        os.unlink(os.path.join(self.root, ".overseer", "survey.json"))
        self.assertEqual(S.choose_backend(self.root), "local")

    def test_disable_flag_values(self):
        for v in ("1", "true", "TRUE", "Yes", " yes "):
            self.assertTrue(S.disabled({"OVERSEER_DISABLE_SLURM": v}), v)
        for v in ("", "0", "false", "no", "2", "on", "enabled"):
            self.assertFalse(S.disabled({"OVERSEER_DISABLE_SLURM": v}), v)
            self.assertEqual(S.choose_backend(self.root, {"OVERSEER_DISABLE_SLURM": v}), "slurm", v)
        self.assertFalse(S.disabled({}))

    def test_disable_routes_local_and_runs_no_slurm_shim(self):
        with mock.patch.dict(os.environ, {"OVERSEER_DISABLE_SLURM": "1"}):
            rec = self.dispatch()
        self.assertEqual(rec["backend"], "local")
        self.wait_for(rec["dir"])
        for n in ("sbatch", "squeue", "scontrol", "sacct", "scancel"):
            self.assertEqual(self.calls(n), [])

    def test_no_resources_refused_before_sbatch_and_before_attempt(self):
        for plan, needs in (("bare", "partition, time, mem or mem_per_cpu, cpus"), ("demo", "cpus")):
            with self.assertRaises(S.SlurmError) as cm:
                self.dispatch("j1", plan)
            self.assertIn("missing: " + needs, str(cm.exception))
            self.assertIn("approved plan must declare", str(cm.exception))
            self.assertEqual(approval.load_unit_state(self.root, plan, "j1")["counters"]["attempts"], 0)
        self.assertEqual(self.calls(), [])

    def test_resource_merge_and_validation(self):
        plan = {"slurm": dict(SLURM)}
        r = S.resources(plan, {"slurm": {"mem_per_cpu": "1G", "gres": "gpu:1", "qos": "normal", "cpus": 4}})
        self.assertEqual((r["mem_per_cpu"], r["cpus"], r["gres"], r["qos"]), ("1G", 4, "gpu:1", "normal"))
        self.assertNotIn("mem", r)
        for bad in ({"time": "UNLIMITED"}, {"partition": "--wrap=x"}, {"cpus": True}, {"mem": "lots"},
                    {"cpus": "2"}, {"mem": "1G", "mem_per_cpu": "1G"}):
            with self.assertRaises(S.SlurmError, msg=bad):
                S.resources(plan, {"slurm": bad})

    def test_handshake_required_fresh_and_for_this_root(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 8 * 86400))
        for kw in ({"when": old}, {"root": "/elsewhere"}, {"when": "garbage"}):
            self.handshake_record(**kw)
            with self.assertRaisesRegex(S.SlurmError, "handshake"):
                self.dispatch()
        os.unlink(os.path.join(self.root, ".overseer", "handshake.json"))
        with self.assertRaisesRegex(S.SlurmError, "handshake"):
            self.dispatch()
        self.assertEqual(self.calls(), [])
        week = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 6 * 86400))
        self.handshake_record(when=week)
        self.assertEqual(self.dispatch()["launch"]["job_id"], "4242")

    def test_handshake_record_needs_its_token_nonce_and_reply(self):
        d = os.path.join(self.root, ".overseer", "handshake", "a1b2c3")
        S.check_handshake(self.root)
        os.makedirs(os.path.join(self.root, ".overseer", "handshake", "evil"))
        for token in (None, "", "../a1b2c3", "nope", 7):
            self.handshake_record(token=token)
            with self.assertRaisesRegex(S.SlurmError, "handshake", msg=token):
                S.check_handshake(self.root)
        self.handshake_record(reply=False)
        os.unlink(os.path.join(d, "reply.json"))
        with self.assertRaisesRegex(S.SlurmError, "handshake"):
            S.check_handshake(self.root)
        for reply in ({"nonce": "other", "host": "node001"}, {"nonce": "nonce-1", "host": "elsewhere"},
                      {"nonce": "nonce-1"}, []):
            store.write_json_atomic(os.path.join(d, "reply.json"), reply)
            with self.assertRaisesRegex(S.SlurmError, "handshake"):
                S.check_handshake(self.root)
        self.handshake_record()
        S.check_handshake(self.root)
        os.unlink(os.path.join(d, "nonce"))
        with self.assertRaisesRegex(S.SlurmError, "handshake"):
            self.dispatch()
        self.assertEqual(self.calls(), [])

    def test_tmp_root_refused_before_any_submission(self):
        with mock.patch.object(S, "TMP_ROOTS", ("/tmp", "/var/tmp")):
            self.assertIn("private", S.root_problem(os.path.join(self.tmp, "x"), {}))
            with self.assertRaisesRegex(S.SlurmError, "private to each Slurm job"):
                self.dispatch()
            with self.assertRaisesRegex(S.SlurmError, "private"):
                S.handshake(self.root, "sl")
        self.assertIn("scratch", S.root_problem("/scratch/p/x", {"TMPDIR": "/scratch/p"}))
        self.assertIsNone(S.root_problem("/data/p", {"TMPDIR": "/scratch/p"}))
        self.assertEqual(self.calls(), [])

    def test_local_agent_refused_on_slurm_host_outside_allocation(self):
        argv = [self.script("ag", "true")]
        with self.assertRaisesRegex(X.ExecutionError, "login node"):
            self.launch("j1", argv, kind="author", plan="sl")
        inv = os.path.join(self.root, ".overseer", "plans", "sl", "units", "j1", "attempts", "a1", "inv")
        self.assertFalse(os.path.exists(inv))
        self.launch("j1", argv, kind="job", plan="sl")  # a job unit may run locally
        with mock.patch.dict(os.environ, {"SLURM_JOB_ID": "7"}):
            self.launch("j2", argv, kind="author", plan="sl")
        self.survey(False)
        self.launch("j3", argv, kind="fixer", plan="sl")

    def test_login_node_guard_ignores_the_disable_flag(self):
        store.write_json_atomic(os.path.join(self.root, ".overseer", "survey.json"), {
            "slurm": {"usable": False, "reason": "disabled"}, "tools": {"sbatch": {"found": True}}})
        argv = [self.script("ag", "true")]
        for flag in ("1", "", "0"):
            with mock.patch.dict(os.environ, {"OVERSEER_DISABLE_SLURM": flag}):
                with self.assertRaisesRegex(X.ExecutionError, "login node"):
                    self.launch("j1", argv, kind="author", plan="sl")
        with mock.patch.dict(os.environ, {"OVERSEER_DISABLE_SLURM": "1", "SLURM_JOB_ID": "7"}):
            self.launch("j1", argv, kind="author", plan="sl")
        self.assertEqual(self.calls(), [])


class Submission(SlurmCase):
    def test_sbatch_argv_and_records(self):
        rec = self.dispatch()
        adir = os.path.dirname(os.path.dirname(rec["dir"]))
        tok = rec["token"]
        want = ("--parsable --job-name overseer-j1-a1-%s --comment %s --chdir %s --output %s/slurm-%%j.out "
                "--error %s/slurm-%%j.err --partition batch-cpu --account lab-account --time 00:30:00 --mem 2G "
                "--cpus-per-task 2 %s" % (tok[:8], tok, adir, adir, adir, rec["run_sh"]))
        self.assertEqual(self.calls(), [want])
        self.assertEqual(rec["slurm"]["partition"], "batch-cpu")
        self.assertEqual(self.rec(rec["dir"], "launch.json")["job_id"], "4242")
        self.assertEqual(self.rec(rec["dir"], "sbatch.json")["outcome"], "submitted")
        with open(os.path.join(rec["dir"], "token")) as f:
            self.assertEqual(f.read().strip(), tok)
        self.assertEqual(self.rec(rec["dir"], "intent.json")["env_extras"]["OVERSEER_OUT"],
                         os.path.join(adir, "outputs"))

    def test_unit_block_overrides_and_optional_flags(self):
        plan = make_plan("ov", 1)
        plan["slurm"] = dict(SLURM)
        plan["units"][0]["slurm"] = {"mem_per_cpu": "1G", "gres": "gpu:1", "qos": "normal", "cpus": 3}
        approval.approve_plan(self.root, plan, "me")
        rec = self.dispatch("j1", "ov")
        line = self.calls()[0]
        self.assertIn("--mem-per-cpu 1G --cpus-per-task 3 --gres gpu:1 --qos normal " + rec["run_sh"], line)
        self.assertNotIn(" --mem ", line)

    def test_intent_precedes_sbatch_and_crash_converges_without_resubmission(self):
        with mock.patch.object(S, "submit", side_effect=Crash):
            with self.assertRaises(Crash):
                self.dispatch()
        inv = next(X.iter_invocations(self.root, "sl", "j1"))["path"]
        self.assertTrue(os.path.exists(os.path.join(inv, "intent.json")))
        self.assertFalse(os.path.exists(os.path.join(inv, "launch.json")))
        self.assertEqual(X.invocation_status(inv, True)["state"], "UNCERTAIN")  # sbatch may never have run
        self.assertEqual(X.slots_in_use(self.root), 1)
        self.fake("squeue.state", "RUNNING")  # the job did exist: reconcile by token
        self.fake("sbatch.calls", "x --comment %s --job-name n\n" % self.rec(inv, "intent.json")["token"])
        st = X.invocation_status(inv, True)
        self.assertEqual((st["state"], st["job_id"]), ("RUNNING", "4242"))
        self.assertEqual(self.rec(inv, "launch.json")["reconciled"], "squeue")
        with self.assertRaisesRegex(X.ExecutionError, "RUNNING"):
            self.dispatch()

    def test_rejection_is_recorded_with_stderr_and_not_retried(self):
        self.fake("sbatch.mode", "reject")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):  # the CLI reports it and exits 1, no traceback
            rc = cli.main(["--root", self.root, "dispatch", "--unit", "j1", "--plan", "sl"])
        self.assertEqual(rc, 1)
        self.assertIn("invalid partition specified", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertTrue(issubclass(S.SbatchRejected, X.ExecutionError))
        inv = next(X.iter_invocations(self.root, "sl", "j1"))["path"]
        self.assertIn("invalid partition", self.rec(inv, "launch_error.json")["error"])
        self.assertEqual(self.rec(inv, "sbatch.json")["outcome"], "rejected")
        self.assertEqual(X.invocation_status(inv, True)["state"], "LAUNCH_FAILED")
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(X.slots_in_use(self.root), 0)

    def test_recovery_requires_the_full_token_in_the_comment(self):
        cases = (("j1", 'echo "4242|$name|someone-elses-token|RUNNING|None"'),  # same name, other comment
                 ("j2", 'echo "4242|$name|(null)|RUNNING|None"'))  # site shows no comments: no name fallback
        for uid, squeue in cases:
            self.fake("sbatch.mode", "hang")
            self.unfake("sbatch.calls")
            self.shim("squeue", '. "$F/common.sh"\n' + squeue)
            with mock.patch.object(S, "SBATCH_TIMEOUT", 1):
                with self.assertRaises(S.SbatchAmbiguous):
                    self.dispatch(uid)
            inv = next(X.iter_invocations(self.root, "sl", uid))["path"]
            self.assertEqual(X.invocation_status(inv, True)["state"], "UNCERTAIN")
            self.assertFalse(os.path.exists(os.path.join(inv, "launch.json")))
            self.assertEqual(X.abort_invocation(self.root, inv), "UNIDENTIFIED")  # nothing is scancelled
            self.assertEqual(self.calls("scancel"), [])
            self.assertEqual(len(self.calls()), 1)

    def test_ambiguous_sbatch_found_in_squeue_records_job_with_one_call(self):
        for mode in ("hang", "junk"):
            self.fake("sbatch.mode", mode)
            self.unfake("sbatch.calls")
            self.fake("squeue.state", "PENDING")
            with mock.patch.object(S, "SBATCH_TIMEOUT", 1):
                rec = self.dispatch("j1" if mode == "hang" else "j2")
            self.assertEqual(rec["launch"]["job_id"], "4242")
            self.assertEqual(len(self.calls()), 1)
            self.assertEqual(self.rec(rec["dir"], "sbatch.json")["outcome"], "recovered_by_squeue")
            self.assertEqual(self.status(rec)["state"], "QUEUED")
        self.assertEqual(X.slots_in_use(self.root), 2)

    def test_ambiguous_sbatch_not_in_squeue_stays_uncertain_with_one_call(self):
        self.fake("sbatch.mode", "hang")
        with mock.patch.object(S, "SBATCH_TIMEOUT", 1):
            with self.assertRaises(S.SbatchAmbiguous):
                self.dispatch()
        inv = next(X.iter_invocations(self.root, "sl", "j1"))["path"]
        self.assertEqual(self.rec(inv, "sbatch.json")["outcome"], "ambiguous")
        self.assertEqual(X.invocation_status(inv, True)["state"], "UNCERTAIN")
        with self.assertRaisesRegex(X.ExecutionError, "UNCERTAIN"):
            self.dispatch()
        self.assertEqual(len(self.calls()), 1)  # never a second sbatch for one intent
        self.assertEqual(X.slots_in_use(self.root), 1)

    def test_dispatch_cli_reports_job_id(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--root", self.root, "dispatch", "--unit", "j1", "--plan", "sl"])
        self.assertEqual((rc, json.loads(out.getvalue())["job_id"]), (0, "4242"))


class RunSh(SlurmCase):
    def run_script(self, rec):
        return subprocess.run(["/bin/sh", rec["run_sh"]], capture_output=True, text=True, timeout=30)

    def test_token_mismatch_exits_97_without_running_payload(self):
        marker = os.path.join(self.tmp, "payload-ran")
        plan = make_plan("pl", 1)
        plan["slurm"] = dict(SLURM)
        plan["units"][0]["command"] = "echo x > '%s'" % marker
        approval.approve_plan(self.root, plan, "me")
        rec = self.dispatch("j1", "pl")
        with open(os.path.join(rec["dir"], "token"), "w") as f:
            f.write("not-this-invocation\n")
        self.assertEqual(self.run_script(rec).returncode, 97)
        ex = self.rec(rec["dir"], "exit.json")
        self.assertEqual((ex["rc"], ex["error"], ex["token"]), (97, "identity_mismatch", rec["token"]))
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(os.path.exists(os.path.join(rec["dir"], "start.json")))
        os.unlink(os.path.join(rec["dir"], "token"))  # a missing token file is a mismatch as well
        os.unlink(os.path.join(rec["dir"], "exit.json"))
        self.assertEqual(self.run_script(rec).returncode, 97)
        self.assertFalse(os.path.exists(marker))
        with open(os.path.join(rec["dir"], "token"), "w") as f:  # control: the right token runs the payload
            f.write(rec["token"] + "\n")
        self.assertEqual(self.run_script(rec).returncode, 0)
        self.assertTrue(os.path.exists(marker))

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_matching_token_runs_payload_and_exports(self):
        rec = self.dispatch()
        self.assertEqual(self.run_script(rec).returncode, 0)
        self.assertEqual(self.rec(rec["dir"], "exit.json")["rc"], 0)
        self.assertIn("OVERSEER_OUT", self.read(rec["run_sh"]))

    def test_local_run_sh_has_no_identity_check(self):
        rec = self.launch("j1", [self.script("p", "true")], kind="job", plan="demo")
        self.assertNotIn("identity_mismatch", self.read(rec["run_sh"]))
        self.wait_for(rec["dir"])

    def test_extras_named_like_wrapper_internals_cannot_break_it(self):
        extras = {"INV": "/nonexistent", "TOKEN": "x", "rc": "9", "PY": "/bin/false", "REC": "r", "SHA": "s",
                  "sig": "KILL", "note": "n", "fin": "f", "t": "t"}
        payload = self.script("chk", 'test "$INV" = /nonexistent && test "$rc" = 9 && test "$TOKEN" = x')
        plan = make_plan("ex", 1)
        plan["slurm"] = dict(SLURM)
        approval.approve_plan(self.root, plan, "me")
        n, _ = X.allocate_attempt(self.root, "ex", "j1")
        self.fake("sbatch.run")  # the shim runs run.sh inside sbatch, as a batch job would
        rec = S.launch(self.root, "ex", "j1", n, "job", [payload], self.root, extras=extras)
        ex = self.rec(rec["dir"], "exit.json")
        self.assertEqual((ex["rc"], ex["token"]), (0, rec["token"]))
        self.assertEqual(ex.get("error"), None)
        rec2 = self.launch("j1", [payload], kind="job", plan="demo", extras=extras)  # local
        self.assertEqual(self.wait_for(rec2["dir"])["rc"], 0)

    def test_reserved_and_invalid_extras_are_refused(self):
        n, _ = X.allocate_attempt(self.root, "sl", "j1")
        for name in ("_OVR_x", "_OVR_PY", X.TOKEN_VAR, "has-dash", "1abc", "a b", "", "A=B"):
            for fn in (lambda e: self.launch("j1", [self.script("p", "true")], kind="job", plan="demo", extras=e),
                       lambda e: S.launch(self.root, "sl", "j1", n, "job", [self.script("p", "true")],
                                          self.root, extras=e)):
                with self.assertRaisesRegex(X.ExecutionError, "invalid environment variable", msg=name):
                    fn({name: "v"})
        with self.assertRaises(X.ExecutionError):
            self.launch("j1", [self.script("p", "true")], kind="job", plan="demo", extras={"OK": 1})
        self.assertEqual(self.calls(), [])


class Evidence(SlurmCase):
    def test_judge_table(self):
        def src(outcome, state=None, exit="0:0"):
            return {"outcome": outcome, "state": state, "exit": exit}
        ok0 = {"rc": 0, "signal": None}
        none = None
        cases = [
            (src("listed", "PENDING"), none, none, none, [], "QUEUED"),
            (src("listed", "RUNNING"), none, none, ok0, ["time_limit@x"], "RUNNING"),
            (src("absent"), src("known", "RUNNING"), none, none, [], "RUNNING"),
            (src("absent"), src("known", "COMPLETED"), none, none, [], "COMPLETED"),
            (src("absent"), src("known", "COMPLETED"), none, ok0, [], "COMPLETED"),
            (src("absent"), src("known", "COMPLETED"), none, {"rc": 1, "signal": None}, [], "FAILED"),
            (src("absent"), src("known", "COMPLETED", "1:0"), none, ok0, [], "FAILED"),
            (src("absent"), src("known", "TIMEOUT"), none, ok0, [], "FAILED"),
            (src("absent"), src("known", "OUT_OF_MEMORY", "0:125"), none, none, [], "FAILED"),
            (src("absent"), src("known", "COMPLETED"), none, ok0, ["time_limit@x"], "FAILED"),
            (src("absent"), src("not_known"), src("known", "COMPLETED"), none, [], "COMPLETED"),
            (src("absent"), src("known", "COMPLETED"), src("known", "FAILED", "1:0"), none, [], "FAILED"),
            (src("absent"), src("not_known"), src("timed_out"), ok0, [], "COMPLETED"),
            (src("absent"), src("not_known"), src("timed_out"), {"rc": 143, "signal": "TERM"}, [], "FAILED"),
            (src("absent"), src("not_known"), src("timed_out"), none, [], "UNCERTAIN"),
            (src("absent"), src("not_known"), src("timed_out"), none, ["cancelled@x"], "FAILED"),
            (src("timed_out"), src("timed_out"), src("error"), none, [], "UNCERTAIN"),
            (src("timed_out"), src("timed_out"), none, ok0, [], "COMPLETED"),
            (src("absent"), src("known", "WEIRD"), none, ok0, [], "UNCERTAIN"),
            (src("absent"), src("mismatch"), src("unparseable"), none, [], "UNCERTAIN"),
            (src("listed", "FAILED"), src("known", "COMPLETED"), none, ok0, [], "FAILED"),
            (src("listed", "COMPLETED"), none, none, none, [], "UNCERTAIN"),
        ]
        for sq, sc, sa, ex, logs, want in cases:
            verdict, ev = S.judge(sq, sc, sa, ex, logs)
            self.assertEqual(verdict, want, (sq, sc, sa, ex, logs, ev))
            self.assertTrue(ev)

    def test_parsers_on_captured_output(self):
        def fixture(name):
            return "cat '%s'" % os.path.join(FIX, name + ".txt")
        self.shim("squeue", fixture("squeue"))
        sq = S.query_squeue()
        self.assertEqual(len(sq["rows"]), 8)
        self.assertEqual(S.squeue_view(sq, "3507140", "tok-abcd1234"), {"outcome": "listed", "state": "RUNNING"})
        self.assertEqual(S.squeue_view(sq, "3507140", "other")["outcome"], "mismatch")
        self.assertEqual(S.squeue_view(sq, "1", "x")["outcome"], "absent")
        self.assertEqual(S.find_by_token("tok-abcd1234"), ("found", "3507140"))
        self.assertEqual(S.find_by_token("x"), ("none",))
        self.shim("scontrol", fixture("scontrol_done"))
        sc = S.query_scontrol("3507140", "tok-abcd1234")
        self.assertEqual((sc["outcome"], sc["state"], sc["exit"]), ("known", "FAILED", "3:0"))
        self.assertEqual(S.query_scontrol("3507140", "wrong")["outcome"], "mismatch")
        self.assertEqual(S.query_scontrol("1", "tok-abcd1234")["outcome"], "unparseable")
        self.shim("scontrol", fixture("scontrol_running"))
        self.assertEqual(S.query_scontrol("3507140", "tok-abcd1234")["state"], "RUNNING")
        self.shim("sacct", fixture("sacct") + "; exit 1")
        self.assertEqual(S.query_sacct("3507140", "n")["outcome"], "error")
        self.shim("squeue", "echo 'garbage line'")
        self.assertEqual(S.query_squeue()["outcome"], "unparseable")
        self.shim("squeue", "exit 1")
        self.assertEqual(S.squeue_view(S.query_squeue(), "1", "t")["outcome"], "error")

    def test_queued_and_running_from_squeue_count_as_slots(self):
        rec = self.dispatch()
        self.fake("squeue.state", "PENDING")
        self.assertEqual(self.status(rec)["state"], "QUEUED")
        self.assertEqual(X.slots_in_use(self.root), 1)
        self.fake("squeue.state", "RUNNING")
        self.assertEqual(self.status(rec)["state"], "RUNNING")
        self.assertFalse(os.path.exists(os.path.join(rec["dir"], "evidence.json")))

    def test_success_through_scontrol_with_real_wrapper(self):
        self.fake("sbatch.run")
        rec = self.dispatch()
        self.fake("scontrol.state", "COMPLETED 0:0")
        st = self.status(rec)
        self.assertEqual((st["state"], st["rc"], st["verdict"]), ("EXITED", 0, "COMPLETED"))
        ev = self.evidence(rec)
        self.assertEqual(ev["sources"]["scontrol"]["outcome"], "known")
        self.assertIsNone(ev["sources"]["sacct"])
        self.assertEqual(ev["sources"]["exit"]["outcome"], "matched")
        self.assertEqual(ev["token"], rec["token"])
        self.assertFalse(ev["sacct_ever_answered"])
        self.assertEqual(self.calls("scancel"), [])

    def test_hung_sacct_is_killed_and_wrapper_decides(self):
        rec = self.dispatch()
        self.exit_rec(rec, 0)
        self.fake("sacct.sleep", "30")
        self.fake("sacct.state", "FAILED 1:0")  # would be failure evidence if sacct ever answered
        t0 = time.time()
        st = self.status(rec)
        self.assertLess(time.time() - t0, 4)  # SACCT_TIMEOUT is 1 s here, QUERY_TIMEOUT 5 s
        self.assertEqual((st["state"], st["rc"], st["verdict"]), ("EXITED", 0, "COMPLETED"))
        ev = self.evidence(rec)
        self.assertEqual(ev["sources"]["sacct"]["outcome"], "timed_out")
        self.assertFalse(ev["sacct_ever_answered"])

    def test_sacct_answers_for_forgotten_job_and_is_recorded(self):
        rec = self.dispatch()
        self.fake("sacct.state", "COMPLETED 0:0")
        st = self.status(rec)
        self.assertEqual((st["state"], st["rc"]), ("EXITED", 0))
        self.assertTrue(self.evidence(rec)["sacct_ever_answered"])
        self.unfake("sacct.state")  # a later timeout does not erase that sacct once answered
        self.status(rec)
        self.assertTrue(self.evidence(rec)["sacct_ever_answered"])

    def test_completed_with_wrapper_rc_1_is_failed(self):
        rec = self.dispatch()
        self.fake("scontrol.state", "COMPLETED 0:0")
        self.exit_rec(rec, 1)
        st = self.status(rec)
        self.assertEqual((st["state"], st["rc"], st["verdict"]), ("EXITED", 1, "FAILED"))

    def test_no_evidence_is_uncertain_and_holds_slot(self):
        rec = self.dispatch()
        st = self.status(rec)
        self.assertEqual(st["state"], "UNCERTAIN")
        self.assertIn("no terminal evidence", st["evidence"])
        self.assertEqual(self.evidence(rec)["verdict"], "UNCERTAIN")
        self.assertEqual(X.slots_in_use(self.root), 1)
        with open(os.path.join(os.path.dirname(os.path.dirname(rec["dir"])), "slurm-4242.out"), "w") as f:
            f.write("harmless text\n")  # logs alone never produce success
        self.assertEqual(self.status(rec)["state"], "UNCERTAIN")

    def test_timeout_state_and_time_limit_log_are_failed(self):
        rec = self.dispatch()
        self.fake("scontrol.state", "TIMEOUT 0:0")
        st = self.status(rec)
        self.assertEqual((st["state"], st["verdict"]), ("EXITED", "FAILED"))
        self.assertNotEqual(st["rc"], 0)
        self.unfake("scontrol.state")
        rec2 = self.dispatch("j2")
        self.exit_rec(rec2, 0)
        log = os.path.join(os.path.dirname(os.path.dirname(rec2["dir"])), "slurm-4242.err")
        with open(log, "w") as f:
            f.write("slurmstepd: error: *** JOB 4242 ON node001 CANCELLED AT 2026-01-01T00:00:00 DUE TO TIME LIMIT ***\n")
        st = self.status(rec2)
        self.assertEqual((st["state"], st["verdict"]), ("EXITED", "FAILED"))
        self.assertNotEqual(st["rc"], 0)
        self.assertIn("failure:log_time_limit@slurm-4242.err", st["evidence"])

    def test_log_scan_streams_whole_files_and_flags_partial_scans(self):
        adir = os.path.join(self.tmp, "logs")
        os.makedirs(adir)
        out = os.path.join(adir, "slurm-9.out")
        with open(out, "w") as f:
            f.write("slurmstepd: error: Detected 1 oom_kill event in StepId=9.batch\n")
            f.write("filler line of ordinary output\n" * 20000)  # the match is far from the end
        self.assertEqual(S.scan_logs(adir, "9"), (["out_of_memory@slurm-9.out"], {"slurm-9.out": "read", "slurm-9.err": "absent"}))
        with open(out, "w") as f:
            f.write("x\n" * 300 + "slurmstepd: error: oom_kill event\n" + "y\n" * 300)  # only in the middle
        with mock.patch.object(S, "LOG_FULL_MAX", 400), mock.patch.object(S, "LOG_EDGE", 50):
            hits, seen = S.scan_logs(adir, "9")
        self.assertEqual((hits, seen["slurm-9.out"]), ([], "partial"))
        with open(out, "w") as f:
            f.write("x\n" * 300 + "y\n" * 20 + "DUE TO TIME LIMIT\n")  # inside the last edge
        with mock.patch.object(S, "LOG_FULL_MAX", 400), mock.patch.object(S, "LOG_EDGE", 50):
            self.assertEqual(S.scan_logs(adir, "9")[0], ["time_limit@slurm-9.out"])
        ok0 = {"rc": 0, "signal": None}
        sq = {"outcome": "absent"}
        done = {"outcome": "known", "state": "COMPLETED", "exit": "0:0"}
        self.assertEqual(S.judge(sq, None, None, ok0, [], True)[0], "UNCERTAIN")  # wrapper alone is not enough
        self.assertEqual(S.judge(sq, done, None, ok0, [], True)[0], "COMPLETED")
        self.assertEqual(S.judge(sq, None, None, ok0, ["oom@x"], True)[0], "FAILED")
        self.assertEqual(S.judge(sq, None, None, ok0, [], False)[0], "COMPLETED")

    def test_partial_log_scan_is_recorded_and_blocks_wrapper_only_success(self):
        rec = self.dispatch()
        self.exit_rec(rec, 0)
        log = os.path.join(os.path.dirname(os.path.dirname(rec["dir"])), "slurm-4242.out")
        with open(log, "w") as f:
            f.write("a\n" * 500)
        with mock.patch.object(S, "LOG_FULL_MAX", 100), mock.patch.object(S, "LOG_EDGE", 20):
            self.assertEqual(self.status(rec)["state"], "UNCERTAIN")
        self.assertEqual(self.evidence(rec)["sources"]["logs"]["files"]["slurm-4242.out"], "partial")

    def test_scheduler_terminal_state_survives_scontrol_forgetting(self):
        rec = self.dispatch()
        self.fake("scontrol.state", "FAILED 3:0")
        self.assertEqual(self.status(rec)["rc"], 3)
        self.unfake("scontrol.state")
        self.exit_rec(rec, 0)
        st = self.status(rec)
        self.assertEqual((st["rc"], st["verdict"]), (3, "FAILED"))
        self.assertTrue(self.evidence(rec)["sources"]["scontrol"]["cached"])

    def test_foreign_or_unbound_records_give_no_success(self):
        rec = self.dispatch()
        store.write_json_atomic(os.path.join(rec["dir"], "exit.json"), {
            "token": "other", "argv_sha256": rec["argv_sha256"], "rc": 0, "signal": None})
        self.fake("sacct.state", "COMPLETED 0:0")
        with mock.patch.object(S, "job_name", return_value="someone-else"):
            st = self.status(rec)
        self.assertEqual(st["state"], "UNCERTAIN")
        self.assertEqual(self.evidence(rec)["sources"]["exit"]["outcome"], "mismatch")


class Abort(SlurmCase):
    def test_abort_requests_then_confirms_only_with_terminal_evidence(self):
        rec = self.dispatch()
        self.fake("squeue.state", "RUNNING")
        res = X.abort_invocation(self.root, rec["dir"], grace=0, sleep=lambda s: None)
        self.assertEqual(res, "UNCONFIRMED")
        self.assertEqual(self.calls("scancel"), ["4242"])
        c = self.rec(rec["dir"], "cancel.json")
        self.assertIn("requested_at", c)
        self.assertNotIn("confirmed_at", c)
        self.assertEqual(X.invocation_status(rec["dir"], True)["state"], "CANCELLING")
        self.assertEqual(X.slots_in_use(self.root), 1)
        self.unfake("squeue.state")  # left the queue, but nothing says why: still unconfirmed
        self.assertEqual(X.invocation_status(rec["dir"], True)["state"], "CANCELLING")
        self.fake("scontrol.state", "CANCELLED 0:15")
        self.assertEqual(X.invocation_status(rec["dir"], True)["state"], "CANCELLED")
        self.assertIn("confirmed_at", self.rec(rec["dir"], "cancel.json"))
        kinds = [e["kind"] for e in store.read_events(self.root) if e["kind"].startswith("cancel_")]
        self.assertEqual(kinds, ["cancel_requested", "cancel_confirmed"])
        self.assertEqual(self.calls("scancel"), ["4242"])
        self.assertEqual(X.slots_in_use(self.root), 0)

    def test_abort_confirms_on_wrapper_exit_and_does_not_confirm_while_listed(self):
        rec = self.dispatch()
        self.exit_rec(rec, 143, "TERM")
        self.fake("squeue.state", "COMPLETING")
        self.assertEqual(X.abort_invocation(self.root, rec["dir"], grace=0, sleep=lambda s: None), "UNCONFIRMED")
        self.unfake("squeue.state")
        res = X.abort_invocation(self.root, rec["dir"], grace=0, sleep=lambda s: None)
        self.assertEqual(res, "CANCELLED")
        self.assertEqual(self.calls("scancel"), ["4242"])  # the pending request is not repeated

    def test_confirmed_cancellation_is_cancelled_even_when_wrapper_rc_is_0(self):
        rec = self.dispatch()
        self.fake("squeue.state", "RUNNING")
        self.assertEqual(X.abort_invocation(self.root, rec["dir"], grace=0, sleep=lambda s: None), "UNCONFIRMED")
        self.unfake("squeue.state")
        self.exit_rec(rec, 0)  # the payload finished as the cancel arrived
        self.fake("scontrol.state", "CANCELLED 0:15")
        st = X.invocation_status(rec["dir"], True)
        self.assertEqual((st["state"], st["verdict"]), ("CANCELLED", "FAILED"))
        self.assertNotIn("rc", st)

    def test_timed_out_scancel_is_not_repeated(self):
        rec = self.dispatch()
        self.fake("scancel.hang")
        self.fake("squeue.state", "RUNNING")
        with mock.patch.object(S, "QUERY_TIMEOUT", 1):
            X.abort_invocation(self.root, rec["dir"], grace=0, sleep=lambda s: None)
            X.abort_invocation(self.root, rec["dir"], grace=0, sleep=lambda s: None)
        self.assertEqual(len(self.calls("scancel")), 1)
        self.assertTrue(self.rec(rec["dir"], "cancel.json")["scancel"]["timed_out"])

    def test_abort_without_job_id_is_unidentified(self):
        with mock.patch.object(S, "submit", side_effect=Crash):
            with self.assertRaises(Crash):
                self.dispatch()
        inv = next(X.iter_invocations(self.root, "sl", "j1"))["path"]
        self.assertEqual(X.abort_invocation(self.root, inv), "UNIDENTIFIED")
        self.assertEqual(self.calls("scancel"), [])


class Handshake(SlurmCase):
    def hs(self, **kw):
        return S.handshake(self.root, "sl", timeout=kw.pop("timeout", 5), poll=0.05, **kw)

    def test_success_records_root_host_and_time(self):
        self.fake("sbatch.run")
        os.unlink(os.path.join(self.root, ".overseer", "handshake.json"))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["--root", self.root, "slurm-handshake", "--plan", "sl", "--timeout", "5", "--poll", "0.05"])
        self.assertEqual(rc, 0)
        rec = self.rec(self.root + "/.overseer", "handshake.json")
        self.assertEqual(rec["root"], self.root)
        self.assertTrue(rec["host"])
        self.assertRegex(rec["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        d = os.path.join(self.root, ".overseer", "handshake", rec["token"])
        self.assertEqual(self.rec(d, "reply.json")["host"], rec["host"])
        self.assertIn("--partition batch-cpu --account lab-account --time 00:30:00 --mem 2G --cpus-per-task 2",
                      self.calls()[0])
        S.check_handshake(self.root)
        self.assertIn("slurm_handshake", [e["kind"] for e in store.read_events(self.root)])

    def test_reply_the_controller_cannot_see_fails(self):
        os.unlink(os.path.join(self.root, ".overseer", "handshake.json"))
        self.fake("sbatch.run")
        self.fake("sbatch.blind")
        with self.assertRaisesRegex(S.SlurmError, "does not see"):
            self.hs(timeout=1)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".overseer", "handshake.json")))
        self.assertEqual(self.calls("scancel"), ["4242"])
        with self.assertRaises(S.SlurmError):
            self.dispatch()

    def test_wrong_nonce_fails_and_missing_resources_refuse_before_sbatch(self):
        os.unlink(os.path.join(self.root, ".overseer", "handshake.json"))
        self.fake("sbatch.run")
        with mock.patch.object(S.secrets, "token_hex", return_value="n" * 32), \
                mock.patch.object(S, "_JOB", S._JOB.replace('"$n"', '"forged"')):
            with self.assertRaisesRegex(S.SlurmError, "nonce"):
                self.hs()
        self.unfake("sbatch.calls")
        with self.assertRaisesRegex(S.SlurmError, "missing: cpus"):
            S.handshake(self.root, "demo")
        self.assertEqual(self.calls(), [])

    def test_slurm_unusable_or_disabled_refuses(self):
        self.survey(False)
        with self.assertRaisesRegex(S.SlurmError, "not usable"):
            self.hs()
        self.survey(True)
        with mock.patch.dict(os.environ, {"OVERSEER_DISABLE_SLURM": "1"}):
            with self.assertRaisesRegex(S.SlurmError, "not usable"):
                self.hs()
        self.assertEqual(self.calls(), [])
