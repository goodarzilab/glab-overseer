"""Host survey (`survey`) and transport probes (`probe-transport`).

Each external query runs under a timeout. A timeout is recorded as
"unknown", never as absent. Without --probe-auth no model is called.
"""
import os
import platform
import sys
import tempfile

from . import adapters, env as E, models, paths, store

QUERY_TIMEOUT = 20
PROBE_TIMEOUT = 180
TOOLS = ("claude", "agy", "gh", "git", "sbatch", "squeue", "scontrol", "sacct", "sinfo")
SLURM_TOOLS = ("sbatch", "squeue", "scontrol", "sacct", "sinfo")
UNKNOWN = "unknown"
KEY_WARNING = ("LLM API-key variables are present; `claude` bills a present key instead of the "
               "subscription. Worker environments exclude them.")


def _query(argv, env, timeout):
    """Run a query; return (status, result) with status ok, failed or unknown (timeout)."""
    r = E.run_tool(argv, env, timeout)
    if r["timed_out"]:
        return UNKNOWN, r
    return ("ok" if r["rc"] == 0 else "failed"), r


def _version(path, env, timeout):
    st, r = _query([path, "--version"], env, timeout)
    if st == UNKNOWN:
        return UNKNOWN
    lines = r["stdout"].strip().splitlines() or r["stderr"].strip().splitlines()
    return lines[0][:200] if st == "ok" and lines else None


def parse_partitions(text):
    out = []
    for line in text.splitlines():
        f = line.strip().split("|")
        if len(f) == 3 and f[0]:
            out.append({"name": f[0].rstrip("*"), "time_limit": f[1], "availability": f[2]})
    return out


def _slurm(found, env, base, timeout):
    info = {"in_allocation": bool(base.get("SLURM_JOB_ID")), "partitions": None}
    if base.get("OVERSEER_DISABLE_SLURM") == "1":
        info.update(usable=False, reason="OVERSEER_DISABLE_SLURM=1")
        return info
    missing = [t for t in SLURM_TOOLS if t not in found]
    if missing:
        info.update(usable=False, reason="missing tools: " + ", ".join(missing))
        return info
    st, r = _query([found["sinfo"], "-h", "-o", "%P|%l|%a"], env, timeout)
    if st == UNKNOWN:
        info.update(usable=UNKNOWN, partitions=UNKNOWN, reason="sinfo timed out")
    elif st == "failed":
        info.update(usable=False, reason="sinfo failed (rc %s)" % r["rc"])
    else:
        info.update(usable=True, reason=None, partitions=parse_partitions(r["stdout"]))
    return info


def _auth_probe(family, found, base, timeout, record):
    if adapters.BINARY[family] not in found:
        return {"status": "skipped"}
    roles = models.roles_for(record, family) if record else (
        models.CLAUDE_ROLES if family == "claude" else None)
    if roles is None:
        return {"status": "skipped", "reason": "no review model for %s (run models first)" % family}
    with tempfile.TemporaryDirectory(prefix="ovr-probe-") as cwd, \
            tempfile.TemporaryDirectory(prefix="ovr-task-") as task_dir:
        model = roles["review"]
        inv = adapters.prepare(family, "author", model, "Reply with the single word ok.", cwd,
                               task_dir=task_dir, transport="file")
        r = E.run_tool(inv["argv"], E.worker_env(base), timeout, cwd=cwd)
    if r["timed_out"]:
        return {"status": UNKNOWN}
    res = adapters.parse_result(family, r["rc"], r["stdout"], r["stderr"])
    out = {"status": "ok" if res["ok"] else "failed", "classification": res["classification"]}
    if family == "claude":
        out["api_key_source"] = res["api_key_source"]
    return out


def collect(root, base=None, probe_auth=False, timeout=QUERY_TIMEOUT):
    base = dict(os.environ if base is None else base)
    senv = E.supervisor_env(base)
    slurm_off = base.get("OVERSEER_DISABLE_SLURM") == "1"
    found = {}
    for t in TOOLS:
        p = E.resolve_exe(t, base.get("PATH", ""))
        if p:
            found[t] = p
    tools = {}
    for t in TOOLS:
        if t not in found:
            tools[t] = {"found": False, "path": None, "version": None}
        elif slurm_off and t in SLURM_TOOLS:
            tools[t] = {"found": True, "path": found[t], "version": None}
        else:
            tools[t] = {"found": True, "path": found[t], "version": _version(found[t], senv, timeout)}
    git = {"toplevel": None, "origin": None}
    if "git" in found:
        st, r = _query([found["git"], "-C", root, "rev-parse", "--show-toplevel"], senv, timeout)
        git["toplevel"] = r["stdout"].strip() if st == "ok" else (UNKNOWN if st == UNKNOWN else None)
        if st == "ok":
            st, r = _query([found["git"], "-C", root, "remote", "get-url", "origin"], senv, timeout)
            git["origin"] = r["stdout"].strip() if st == "ok" else (UNKNOWN if st == UNKNOWN else None)
        else:
            git["origin"] = git["toplevel"]
    if "gh" in found:
        st, _ = _query([found["gh"], "auth", "status"], senv, timeout)
        gh_auth = {"ok": "authenticated", "failed": "not authenticated", UNKNOWN: UNKNOWN}[st]
    else:
        gh_auth = "gh not found"
    keys = E.llm_key_names(base)
    survey = {
        "time": store.utc_now(),
        "os": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "python": platform.python_version(),
        "git": git, "gh_auth": gh_auth, "tools": tools,
        "slurm": _slurm(found, senv, base, timeout),
        "llm_key_vars": keys,
        "warnings": [KEY_WARNING] if keys else [],
    }
    if probe_auth:
        rm = models.load_valid(root)
        survey["auth_probe"] = {f: _auth_probe(f, found, base, PROBE_TIMEOUT, rm) for f in adapters.FAMILIES}
    return survey


def _survey_path(root):
    return os.path.join(paths.state_dir(root), "survey.json")


def _write(root, survey, kind):
    """Write survey.json, then log the event: a crash between them converges on rerun."""
    paths.ensure_state_dir(root)
    store.write_json_atomic(_survey_path(root), survey)
    store.append_event(root, kind)


def _existing(root):
    try:
        cur = store.read_json(_survey_path(root), {})
    except ValueError:
        return {}
    return cur if isinstance(cur, dict) else {}


def configure_survey(p):
    p.add_argument("--probe-auth", action="store_true",
                   help="ask each CLI a one-line question to record how it authenticates")


def run_survey(args):
    root = paths.find_root(args.root)
    with store.ProjectLock(root, "survey"):
        s = collect(root, probe_auth=args.probe_auth)
        old = _existing(root)
        for k in ("transport", "auth_probe"):  # keep earlier probe results a plain survey does not redo
            if k in old and k not in s:
                s[k] = old[k]
        _write(root, s, "survey")
    for w in s["warnings"]:
        sys.stderr.write("warning: %s\n" % w)
    print("slurm usable: %s" % s["slurm"]["usable"])
    print("tools found: %s" % ", ".join(t for t in TOOLS if s["tools"][t]["found"]))
    print("wrote %s" % _survey_path(root))
    return 0


def run_probe_transport(args):
    root = paths.find_root(args.root)
    base = dict(os.environ)
    with store.ProjectLock(root, "probe-transport"):
        survey = _existing(root)
        rm = models.load_valid(root)
        if rm is None:
            raise ValueError("no usable models.json: run `overseer.py models --host claude|google` first")
        transport = {}
        tools = survey["tools"] if isinstance(survey.get("tools"), dict) else {}
        for family in adapters.FAMILIES:
            roles = models.roles_for(rm, family)
            name = adapters.BINARY[family]
            path = E.resolve_exe(name, base.get("PATH", ""))
            if not path:
                transport[family] = {"stdin": None, "schema": None, "note": "cli not found"}
                continue
            # Bind the result to the CLI that was probed; `prepare` ignores it if the CLI changes.
            ver = _version(path, E.supervisor_env(base), QUERY_TIMEOUT)
            tools[name] = {"found": True, "path": path, "version": ver}
            if roles is None:
                res = {"stdin": "unavailable", "schema": "unavailable"}
            else:
                with tempfile.TemporaryDirectory(prefix="ovr-probe-") as cwd:
                    res = adapters.probe_transport(family, cwd, E.worker_env(base),
                                                   roles["review"], PROBE_TIMEOUT)
            transport[family] = dict(res, path=path, version=ver)
        survey["tools"] = tools
        survey["transport"] = transport
        _write(root, survey, "probe-transport")
    for family, t in transport.items():
        print("%s: stdin=%s schema=%s" % (family, t["stdin"], t["schema"]))
    return 0
