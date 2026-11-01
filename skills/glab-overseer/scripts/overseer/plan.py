"""Plan format: load, validate, normalize, issue drafts and the approval digest.

Plans are JSON. State operations (approve, split, unit state) live in
`approval.py`.
"""
import hashlib
import json
import re

from .paths import ID_PATTERN, glob_match, has_glob_meta, relpath_problem, valid_id

CAP_MAX = {"workers": 3, "attempts": 2, "review_rounds": 3}
KINDS = ("code", "job")
CODE_REQUIRED = ("title", "prompt", "claims", "test_command", "outputs", "scope")
JOB_REQUIRED = ("title", "command", "outputs")
FORBIDDEN_KEYS = ("model", "models", "provider", "family", "thinking", "effort")
FORBIDDEN_SUFFIXES = ("_model", "_provider", "_family")
OUTPUT_KEYS = ("path", "min_bytes", "sha256", "validate")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
TIME_RE = re.compile(r"^(?:(\d+)-)?(\d+)(?::(\d+))?(?::(\d+))?$")


class PlanError(ValueError):
    pass


def load_plan(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise PlanError("cannot read plan %s: %s" % (path, e.strerror))
    except ValueError as e:
        raise PlanError("plan is not valid JSON: %s" % e)
    if not isinstance(data, dict):
        raise PlanError("plan must be a JSON object")
    return data


def scope_covers(plan_globs, glob):
    """True when `glob` is covered by the plan scope. Segment-aware and conservative.

    Covered only when: `glob` equals a plan glob; or `glob` has no glob
    metacharacters and `paths.glob_match(plan_glob, glob)` holds; or a plan
    glob is `**` or ends in `/**` and `glob` lies under that prefix, compared
    segment by segment (`src/**` covers `src/a/*.py`, not `srcx/a.py`).
    Narrower patterns such as `src/a[bc].py` under `src/a?.py` are not covered
    (false negatives are accepted; list literal paths or widen to `dir/**`).
    """
    gsegs = glob.split("/")
    for p in plan_globs:
        if glob == p:
            return True
        if not has_glob_meta(glob) and glob_match(p, glob):
            return True
        if p == "**" or p.endswith("/**"):
            prefix = [] if p == "**" else p[:-3].split("/")
            if len(gsegs) > len(prefix) and gsegs[:len(prefix)] == prefix:
                return True
    return False


def parse_walltime(value):
    """Minutes for a finite Slurm walltime, else None.

    Accepts MM, MM:SS, HH:MM:SS, D-HH, D-HH:MM, D-HH:MM:SS or a positive int of
    minutes. UNLIMITED, INFINITE, zero and anything else return None.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    m = TIME_RE.match(value.strip())
    if not m:
        return None
    days, a, b, c = m.groups()
    if days is not None:
        total = int(days) * 1440 + int(a) * 60 + int(b or 0) + int(c or 0) / 60.0
    elif c is not None:
        total = int(a) * 60 + int(b) + int(c) / 60.0
    elif b is not None:
        total = int(a) + int(b) / 60.0
    else:
        total = int(a)
    return total if total > 0 else None


def merge_slurm(plan_slurm, unit_slurm):
    """Unit values override plan defaults; setting one memory key drops the other default."""
    merged = dict(plan_slurm or {})
    u = dict(unit_slurm or {})
    if "mem" in u:
        merged.pop("mem_per_cpu", None)
    if "mem_per_cpu" in u:
        merged.pop("mem", None)
    merged.update(u)
    return merged


def output_path(o):
    return o.get("path") if isinstance(o, dict) else o


def _present(v):
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return v is not None


def _check_globs(label, what, globs, errors):
    if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
        errors.append("%s: %s must be a list of strings" % (label, what))
        return False
    ok = True
    for g in globs:
        problem = relpath_problem(g)
        if problem:
            errors.append("%s: %s %r must be repo-relative; it %s" % (label, what, g, problem))
            ok = False
    return ok


def _check_outputs(label, outs, errors):
    if not isinstance(outs, list):
        if outs is not None:
            errors.append("%s: outputs must be a list, got %s" % (label, type(outs).__name__))
        return
    for o in outs:
        if isinstance(o, dict):
            extra = sorted(set(o) - set(OUTPUT_KEYS))
            if extra:
                errors.append("%s: output %r has unknown keys %s" % (label, o.get("path"), extra))
            p = o.get("path")
            if not isinstance(p, str):
                errors.append("%s: output object needs a string 'path'" % label)
                continue
            mb = o.get("min_bytes")
            if mb is not None and (isinstance(mb, bool) or not isinstance(mb, int) or mb < 0):
                errors.append("%s: output %r min_bytes must be a non-negative integer" % (label, p))
            sh = o.get("sha256")
            if sh is not None and not (isinstance(sh, str) and SHA_RE.match(sh)):
                errors.append("%s: output %r sha256 must be 64 lowercase hex digits" % (label, p))
            v = o.get("validate")
            if v is not None and not isinstance(v, str):
                errors.append("%s: output %r validate must be a string" % (label, p))
        elif isinstance(o, str):
            p = o
        else:
            errors.append("%s: output entries must be strings or objects" % label)
            continue
        problem = relpath_problem(p)
        if problem:
            errors.append("%s: output path %r must be repo-relative; it %s" % (label, p, problem))


def _slurm_errors(label, block, errors):
    if "mem" in block and "mem_per_cpu" in block:
        errors.append("%s: slurm block has both mem and mem_per_cpu" % label)


def validate_plan(plan):
    """Return (errors, warnings): lists of one-line strings."""
    errors, warnings = [], []
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"], warnings

    pid = plan.get("plan_id")
    if not valid_id(pid):
        errors.append("plan_id %r does not match %s" % (pid, ID_PATTERN))
    if not (isinstance(plan.get("repo"), str) and REPO_RE.match(plan["repo"])):
        errors.append("repo %r must be GitHub owner/name" % (plan.get("repo"),))
    if not (isinstance(plan.get("target_branch"), str) and plan["target_branch"].strip()):
        errors.append("missing or empty field 'target_branch'")

    scope = plan.get("scope")
    plan_scope = []
    if not _present(scope):
        errors.append("missing or empty field 'scope'")
    elif _check_globs("plan", "scope glob", scope, errors):
        plan_scope = scope
    if "test_globs" in plan:
        _check_globs("plan", "test_globs entry", plan["test_globs"], errors)

    caps = plan.get("caps", {})
    if not isinstance(caps, dict):
        errors.append("caps must be an object")
    else:
        for k in sorted(caps):
            v = caps[k]
            if k not in CAP_MAX:
                errors.append("caps.%s is not a known cap (workers, attempts, review_rounds)" % k)
            elif isinstance(v, bool) or not isinstance(v, int) or v < 1:
                errors.append("caps.%s must be an integer >= 1" % k)
            elif v > CAP_MAX[k]:
                errors.append("caps.%s %d exceeds the maximum %d" % (k, v, CAP_MAX[k]))

    plan_slurm = plan.get("slurm")
    if plan_slurm is not None:
        if not isinstance(plan_slurm, dict):
            errors.append("plan: slurm must be an object")
            plan_slurm = None
        else:
            _slurm_errors("plan", plan_slurm, errors)

    units = plan.get("units")
    if not isinstance(units, list) or not units:
        errors.append("plan has no units")
        return errors, warnings

    seen, ids = set(), []
    for i, u in enumerate(units):
        if not isinstance(u, dict):
            errors.append("unit[%d]: must be an object" % i)
            continue
        uid = u.get("id")
        label = "unit %s" % uid if valid_id(uid) else "unit[%d]" % i
        if not valid_id(uid):
            errors.append("%s: unit id %r does not match %s" % (label, uid, ID_PATTERN))
        elif uid in seen:
            errors.append("duplicate unit id %r" % uid)
        else:
            seen.add(uid)
            ids.append(uid)
        _validate_unit(u, label, plan_scope, plan_slurm, errors)

    known = set(ids)
    graph = {}
    for u in units:
        if not (isinstance(u, dict) and valid_id(u.get("id"))):
            continue
        needs = u.get("needs", [])
        if not isinstance(needs, list) or not all(isinstance(n, str) for n in needs):
            needs = []
        for n in needs:
            if n not in known:
                errors.append("unit %s: needs unknown unit %r" % (u["id"], n))
        graph.setdefault(u["id"], sorted(set(n for n in needs if n in known)))

    cyc = _cycle_members(graph)
    if cyc:
        errors.append("dependency cycle among units: %s" % ", ".join(cyc))

    _overlap_warnings(units, graph, warnings)
    return errors, warnings


def _forbidden_key(k):
    k = str(k).lower()
    return k in FORBIDDEN_KEYS or k.endswith(FORBIDDEN_SUFFIXES)


def _validate_unit(u, label, plan_scope, plan_slurm, errors):
    for k in sorted(u):
        if _forbidden_key(k):
            errors.append("%s: field %r names a model, provider or family; "
                          "model roles are fixed by the host, not by the plan" % (label, k))
    kind = u.get("kind")
    if kind not in KINDS:
        errors.append("%s: kind %r must be 'code' or 'job'" % (label, kind))
        return
    for f in (CODE_REQUIRED if kind == "code" else JOB_REQUIRED):
        if not _present(u.get(f)):
            errors.append("%s: missing or empty field '%s'" % (label, f))
    for f in ("title", "prompt", "test_command", "command"):
        if f in u and u[f] is not None and not isinstance(u[f], str):
            errors.append("%s: field '%s' must be a string" % (label, f))
    needs = u.get("needs", [])
    if not isinstance(needs, list) or not all(isinstance(n, str) for n in needs):
        errors.append("%s: needs must be a list of unit ids" % label)
    if kind == "code":
        claims = u.get("claims")
        if _present(claims) and not (isinstance(claims, list) and all(isinstance(c, str) and c.strip() for c in claims)):
            errors.append("%s: claims must be a list of non-empty strings" % label)
        scope = u.get("scope")
        if _present(scope) and _check_globs(label, "scope glob", scope, errors):
            for g in scope:
                if plan_scope and not scope_covers(plan_scope, g):
                    errors.append("%s: scope glob %r is not covered by the plan scope; list the literal "
                                  "paths, or widen the plan scope to a dir/** glob" % (label, g))
    _check_outputs(label, u.get("outputs"), errors)
    block = u.get("slurm")
    if block is not None and not isinstance(block, dict):
        errors.append("%s: slurm must be an object" % label)
        block = None
    if block is not None:
        _slurm_errors(label, block, errors)
    if block is not None or plan_slurm is not None:
        merged = merge_slurm(plan_slurm, block)
        if "mem" not in merged and "mem_per_cpu" not in merged:
            errors.append("%s: slurm block needs one of mem or mem_per_cpu" % label)
        if parse_walltime(merged.get("time")) is None:
            errors.append("%s: slurm block needs a finite time (got %r)" % (label, merged.get("time")))


def _cycle_members(graph):
    indeg = dict((n, 0) for n in graph)
    for n, deps in graph.items():
        indeg[n] = len(deps)
    ready = [n for n, d in indeg.items() if d == 0]
    done = set()
    while ready:
        n = ready.pop()
        done.add(n)
        for m, deps in graph.items():
            if n in deps and m not in done:
                indeg[m] -= 1
                if indeg[m] == 0:
                    ready.append(m)
    return sorted(n for n in graph if n not in done)


def _reach(graph, start):
    out, stack = set(), list(graph.get(start, []))
    while stack:
        n = stack.pop()
        if n not in out:
            out.add(n)
            stack.extend(graph.get(n, []))
    return out


def _overlap_warnings(units, graph, warnings):
    by_path = {}
    for u in units:
        if not (isinstance(u, dict) and valid_id(u.get("id")) and isinstance(u.get("outputs"), list)):
            continue
        for o in u["outputs"]:
            p = output_path(o)
            if isinstance(p, str) and u["id"] not in by_path.setdefault(p, []):
                by_path[p].append(u["id"])
    reach = {}
    for p in sorted(by_path):
        owners = by_path[p]
        for i, a in enumerate(owners):
            for b in owners[i + 1:]:
                ra = reach.setdefault(a, _reach(graph, a))
                rb = reach.setdefault(b, _reach(graph, b))
                if b not in ra and a not in rb:
                    warnings.append("units %s and %s have no dependency path but both declare output %r" % (a, b, p))


def normalize_output(o):
    if isinstance(o, str):
        return {"path": o}
    return dict((k, o[k]) for k in OUTPUT_KEYS if k in o and o[k] is not None)


def normalize_unit(u):
    out = {
        "id": u["id"], "kind": u["kind"], "title": u["title"],
        "needs": sorted(set(u.get("needs") or [])),
        "outputs": sorted((normalize_output(o) for o in u["outputs"]), key=lambda o: o["path"]),
    }
    if u["kind"] == "code":
        out["prompt"] = u["prompt"]
        out["claims"] = list(u["claims"])
        out["test_command"] = u["test_command"]
        out["scope"] = sorted(set(u["scope"]))
    else:
        out["command"] = u["command"]
        if u.get("slurm"):
            out["slurm"] = dict(u["slurm"])
    return out


def normalize_plan(plan):
    """Canonical semantic form of a valid plan; the digest is computed over this."""
    out = {
        "plan_id": plan["plan_id"],
        "repo": plan["repo"],
        "target_branch": plan["target_branch"],
        "scope": sorted(set(plan["scope"])),
        "caps": dict(CAP_MAX, **(plan.get("caps") or {})),
        "test_globs": sorted(set(plan.get("test_globs") or [])),
        "units": [normalize_unit(u) for u in plan["units"]],
    }
    if plan.get("slurm"):
        out["slurm"] = dict(plan["slurm"])
    return out


def issue_marker(plan_id, unit_id):
    return "<!-- overseer plan=%s unit=%s -->" % (plan_id, unit_id)


def render_draft(plan_id, unit):
    lines = [issue_marker(plan_id, unit["id"]), "", "Kind: %s" % unit["kind"], ""]
    if unit.get("claims"):
        lines += ["## Claims"] + ["- %s" % c for c in unit["claims"]] + [""]
    lines += ["## Outputs"] + ["- `%s`" % o["path"] for o in unit["outputs"]] + [""]
    lines += ["## Needs"] + (["- %s" % n for n in unit["needs"]] or ["- none"]) + [""]
    return {"title": unit["title"], "body": "\n".join(lines)}


def render_drafts(plan):
    return dict((u["id"], render_draft(plan["plan_id"], u)) for u in plan["units"])


def boundary(plan):
    return {
        "repo": plan["repo"], "target_branch": plan["target_branch"],
        "scope": plan["scope"], "caps": plan["caps"], "slurm": plan.get("slurm"),
    }


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def approval_digest(plan, drafts):
    """SHA-256 over the normalized plan, the issue drafts and the boundary."""
    body = {"plan": plan, "drafts": drafts, "boundary": boundary(plan)}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
