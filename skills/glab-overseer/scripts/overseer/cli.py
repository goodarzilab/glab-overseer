"""Command-line interface.

One registration point per subcommand: `register(name, help, configure, run)`.
A later unit replaces its stub by editing the matching `register` call (or
calling `register` again with the same name) and supplying `configure(parser)`
and `run(args) -> int`. Subcommands without `run` are stubs that exit 2.
"""
import argparse
import json
import sys

from . import approval, execution, judge, models, paths, plan as P, review, slurm, store, survey

from . import github as _github  # noqa: E402

COMMANDS = {}  # name -> dict(help, configure, run), in registration order


def register(name, help, configure=None, run=None):
    COMMANDS[name] = {"help": help, "configure": configure, "run": run}


def _err(msg):
    sys.stderr.write(msg.rstrip("\n") + "\n")


def _root(args):
    return paths.find_root(args.root)


# ---- plan -----------------------------------------------------------------

def _configure_plan(p):
    sub = p.add_subparsers(dest="plan_cmd", metavar="ACTION")
    sub.required = True
    v = sub.add_parser("validate", help="validate a plan file")
    v.add_argument("plan", metavar="PLAN")
    a = sub.add_parser("approve", help="validate, approve and store a plan")
    a.add_argument("plan", metavar="PLAN")
    a.add_argument("--approver", required=True)
    s = sub.add_parser("show", help="show an approved plan and its unit facets")
    s.add_argument("plan_id", metavar="PLAN_ID")
    sp = sub.add_parser("split", help="replace an unclosed unit with narrower children")
    sp.add_argument("unit", metavar="UNIT")
    sp.add_argument("--into", required=True, metavar="SPEC.json")
    sp.add_argument("--reason", required=True)
    sp.add_argument("--plan", default=None, metavar="PLAN_ID")


def _run_plan(args):
    root = _root(args)
    try:
        return _PLAN_ACTIONS[args.plan_cmd](root, args)
    except approval.PlanInvalid as e:
        for line in e.errors:
            _err("error: " + line)
        return 1


def _validate(root, args):
    errors, warnings = P.validate_plan(P.load_plan(args.plan))
    for w in warnings:
        _err("warning: " + w)
    for e in errors:
        _err("error: " + e)
    if errors:
        return 1
    print("plan is valid")
    return 0


def _approve(root, args):
    plan = P.load_plan(args.plan)
    with store.ProjectLock(root, "plan approve"):
        res = approval.approve_plan(root, plan, args.approver)
    for w in res["warnings"]:
        _err("warning: " + w)
    print("plan: %s" % res["plan_id"])
    for t in res["titles"]:
        print("issue: %s" % t)
    print("issues: %d" % len(res["titles"]))
    print("digest: %s" % res["digest"])
    print("status: %s" % res["status"])
    return 0


def _show(root, args):
    print("\n".join(approval.describe_plan(root, args.plan_id)))
    return 0


def _split(root, args):
    spec = store.read_json(args.into)
    with store.ProjectLock(root, "plan split"):
        pid = args.plan or approval.find_plan_for_unit(root, args.unit)
        rev = approval.split_unit(root, pid, args.unit, spec, args.reason)
    print("split %s into %s" % (args.unit, ", ".join(c["id"] for c in rev["children"])))
    return 0


_PLAN_ACTIONS = {"validate": _validate, "approve": _approve, "show": _show, "split": _split}


# ---- unlock ---------------------------------------------------------------

def _configure_unlock(p):
    p.add_argument("--force", action="store_true", help="required; removes the lock")
    p.add_argument("--reason", default="", help="why the lock is being removed")
    p.add_argument("--even-if-alive", action="store_true",
                   help="remove the lock even if its holder is alive on this host")


def _run_unlock(args):
    if not args.force:
        _err("error: unlock refuses to run without --force")
        return 1
    if not args.reason.strip():
        _err("error: unlock requires --reason TEXT")
        return 1
    holder = store.force_unlock(_root(args), args.reason, args.even_if_alive)
    if holder is None:
        print("no lock present")
    else:
        print("removed lock held by '%s' (pid %s)" % (holder.get("command"), holder.get("pid")))
    return 0


# ---- registration ---------------------------------------------------------

register("plan", "validate, approve, show or split a plan", _configure_plan, _run_plan)
register("unlock", "force-remove a stale project lock (records an event)", _configure_unlock, _run_unlock)
register("survey", "probe the host and repository", survey.configure_survey, survey.run_survey)
register("probe-transport", "probe stdin and --json-schema support of each CLI", None, survey.run_probe_transport)
register("models", "resolve model roles for a host", models.configure, models.run)
register("slurm-handshake", "verify that batch jobs see this project root", slurm.configure_handshake, slurm.run_handshake)
register("dispatch", "launch a unit attempt (code: worktree and author; job: command) on the chosen backend",
         execution.configure_dispatch, execution.run_dispatch)
register("status", "show unit facets, active invocations and slots",
         execution.configure_status, execution.run_status)
register("wait", "wait until an active invocation changes state (exit 3 on timeout)",
         execution.configure_wait, execution.run_wait)
register("abort", "abort a unit's active invocations", execution.configure_abort, execution.run_abort)
register("judge", "judge an attempt against its evidence", judge.configure_judge, judge.run_judge)
register("resolve", "resolve an UNCERTAIN attempt as done or failed (orchestrator decision)",
         judge.configure_resolve, judge.run_resolve)
register("review", "run a cross-family review round of a judged candidate", review.configure_review, review.run_review_cmd)
register("reproduce", "reproduce a review finding in a disposable worktree",
         review.configure_reproduce, review.run_reproduce)
register("dispositions", "record finding dispositions, an orchestrator decision, or show eligibility",
         review.configure_dispositions, review.run_dispositions)
register("fix", "launch a fixer in the unit's worktree", review.configure_fix, review.run_fix)
register("nonvacuous", "check that the tests fail without the non-test changes",
         review.configure_nonvacuous, review.run_nonvacuous)
for _name, _help in [
    ("merge", "merge an approved pull request (U8)"),
    ("next", "print the next action (U8)"),
    ("report", "write the evidence report (U9)"),
]:
    register(_name, _help)
_github.register_commands(register)


def _stub(name):
    _err("overseer %s: not implemented in this version" % name)
    return 2


def build_parser():
    ap = argparse.ArgumentParser(prog="overseer.py", description="glab-overseer supervisor")
    ap.add_argument("--root", default=None, help="project root (default: git toplevel, else cwd)")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND", title="commands")
    for name, c in COMMANDS.items():
        sp = sub.add_parser(name, help=c["help"], description=c["help"])
        if c["configure"]:
            c["configure"](sp)
    return ap


def main(argv=None):
    ap = build_parser()
    args, extra = ap.parse_known_args(argv)
    if not args.command:
        ap.print_help(sys.stderr)
        return 2
    cmd = COMMANDS[args.command]
    if cmd["run"] is None:
        return _stub(args.command)
    if extra:
        ap.error("unrecognized arguments: %s" % " ".join(extra))
    try:
        return cmd["run"](args)
    except (store.LockHeld, store.LockAlive, store.UnlockBusy) as e:
        _err("error: %s" % e)
    except (approval.ApprovalError, approval.PlanStateError, P.PlanError,
            paths.InvalidId, paths.UnsafePath, ValueError, OSError) as e:
        _err("error: %s" % e)
    except approval.OwnerStop as e:
        _err(str(e))
    return 1
