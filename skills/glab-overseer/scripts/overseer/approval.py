"""Plan approval, unit state and splits.

Files under `.overseer/plans/<plan_id>/`:
  plan.json            normalized approved plan (never rewritten except by re-approval)
  issues.json          {"drafts": {unit: {title, body}}, "filed": {unit: number}}; only
                       "drafts" enters the digest
  approval.json        approver, time, digest, boundary
  approval_history.json  superseded approvals
  revisions.json       splits; the effective plan is plan.json plus these revisions
  units/<unit>/unit.json operational facets; never part of the digest

Functions that write state expect the caller to hold `store.ProjectLock`.
"""
import copy
import json
import os

from . import paths, plan as P, store

CLOSED_STATES = ("merged", "closed", "abandoned", "split")
FACETS = ("authorization", "execution", "evidence", "closure", "tracker")


class PlanInvalid(Exception):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


class ApprovalError(Exception):
    pass


class PlanStateError(Exception):
    pass


class OwnerStop(Exception):
    """A change the approval does not cover. The orchestrator stops and asks the owner."""

    def __init__(self, message):
        super().__init__("OWNER_STOP: " + message)


def _p(root, plan_id, name):
    return os.path.join(paths.plan_dir(root, plan_id), name)


# ---- unit state -----------------------------------------------------------

def default_unit_state(plan_id, unit_id, digest=None):
    return {
        "plan_id": plan_id, "unit_id": unit_id,
        "authorization": {"state": "approved", "plan_digest": digest},
        "execution": {"state": "pending"},
        "evidence": {},
        "closure": {"state": "open"},
        "tracker": {"issue": None},
        "lineage": None,
        "counters": {"attempts": 0, "review_rounds": 0},
    }


def unit_state_path(root, plan_id, unit_id):
    return os.path.join(paths.unit_dir(root, plan_id, unit_id), "unit.json")


def load_unit_state(root, plan_id, unit_id):
    st = store.read_json(unit_state_path(root, plan_id, unit_id), None)
    base = default_unit_state(plan_id, unit_id)
    if st is None:
        return base
    for k, v in base.items():
        st.setdefault(k, v)
    st["counters"] = dict(base["counters"], **st["counters"])
    return st


def save_unit_state(root, plan_id, unit_id, state):
    paths.ensure_state_dir(root)
    store.write_json_atomic(unit_state_path(root, plan_id, unit_id), state)


# ---- approval -------------------------------------------------------------

def _load_stored(root, plan_id):
    plan = store.read_json(_p(root, plan_id, "plan.json"), None)
    issues = store.read_json(_p(root, plan_id, "issues.json"), None)
    approval = store.read_json(_p(root, plan_id, "approval.json"), None)
    return plan, issues, approval


def approve_plan(root, plan, approver):
    """Validate, normalize and approve. Returns a result dict; raises PlanInvalid."""
    approver = approver.strip() if isinstance(approver, str) else ""
    if not approver:
        raise ApprovalError("approver must not be blank")
    errors, warnings = P.validate_plan(plan)
    if errors:
        raise PlanInvalid(errors)
    norm = P.normalize_plan(plan)
    drafts = P.render_drafts(norm)
    digest = P.approval_digest(norm, drafts)
    pid = norm["plan_id"]
    old_plan, old_issues, prev = _load_stored(root, pid)
    result = {"plan_id": pid, "digest": digest, "warnings": warnings,
              "titles": [drafts[u["id"]]["title"] for u in norm["units"]], "status": "approved"}

    if prev and prev.get("digest") == digest and old_plan == norm \
            and (old_issues or {}).get("drafts") == drafts:
        result["status"] = "unchanged"
        return result

    paths.ensure_state_dir(root)
    filed = {}
    if prev and prev.get("digest") != digest:
        result["status"] = "updated"
        hist = store.read_json(_p(root, pid, "approval_history.json"), [])
        hist.append({"approval": prev, "superseded_at": store.utc_now(), "superseded_by": digest,
                     "filed": (old_issues or {}).get("filed", {}),
                     "revisions": store.read_json(_p(root, pid, "revisions.json"), [])})
        store.write_json_atomic(_p(root, pid, "approval_history.json"), hist)
        try:
            os.unlink(_p(root, pid, "revisions.json"))
        except FileNotFoundError:
            pass
        ids = set(u["id"] for u in norm["units"])
        filed = dict((k, v) for k, v in (old_issues or {}).get("filed", {}).items() if k in ids)
    elif old_issues:
        filed = old_issues.get("filed", {})

    store.write_json_atomic(_p(root, pid, "plan.json"), norm)
    store.write_json_atomic(_p(root, pid, "issues.json"), {"drafts": drafts, "filed": filed})
    for u in norm["units"]:
        st = load_unit_state(root, pid, u["id"])
        st["authorization"] = {"state": "approved", "plan_digest": digest}
        save_unit_state(root, pid, u["id"], st)
    store.write_json_atomic(_p(root, pid, "approval.json"), {
        "plan_id": pid, "approver": approver, "approved_at": store.utc_now(),
        "digest": digest, "boundary": P.boundary(norm)})
    store.append_event(root, "plan_approved", plan_id=pid, approver=approver,
                       digest=digest, status=result["status"])
    return result


def require_approval(root, plan_id):
    """Return the approval record, or raise when plan or drafts differ from it."""
    paths.check_id(plan_id, "plan id")
    plan, issues, approval = _load_stored(root, plan_id)
    if approval is None or plan is None or issues is None:
        raise ApprovalError("plan %s has no complete approval" % plan_id)
    errors, _ = P.validate_plan(plan)
    if errors:
        raise ApprovalError("stored plan %s no longer validates: %s" % (plan_id, errors[0]))
    if P.approval_digest(plan, issues.get("drafts")) != approval.get("digest"):
        raise ApprovalError("stored plan or issue drafts for %s no longer match the approval digest; "
                            "re-run 'plan approve'" % plan_id)
    return approval


# ---- revisions and splits -------------------------------------------------

def load_revisions(root, plan_id):
    return store.read_json(_p(root, plan_id, "revisions.json"), [])


def apply_revision(plan, rev):
    child_ids = [c["id"] for c in rev["children"]]
    units = []
    for u in plan["units"]:
        if u["id"] == rev["parent"]:
            units.extend(copy.deepcopy(rev["children"]))
            continue
        u = copy.deepcopy(u)
        if rev["parent"] in u["needs"]:
            u["needs"] = sorted(set([n for n in u["needs"] if n != rev["parent"]] + child_ids))
        units.append(u)
    out = dict(plan)
    out["units"] = units
    return out


def effective_plan(root, plan_id):
    """The approved plan with all recorded splits applied."""
    plan = store.read_json(_p(root, plan_id, "plan.json"))
    for rev in load_revisions(root, plan_id):
        plan = apply_revision(plan, rev)
    return plan


def find_plan_for_unit(root, unit_id):
    pdir = paths.plans_dir(root)
    found = []
    for pid in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        if paths.valid_id(pid) and os.path.exists(_p(root, pid, "plan.json")):
            if any(u["id"] == unit_id for u in effective_plan(root, pid)["units"]):
                found.append(pid)
    if len(found) != 1:
        raise PlanStateError("unit %r is in %d plans; pass --plan PLAN_ID" % (unit_id, len(found)))
    return found[0]


_INHERIT = ("kind", "prompt", "claims", "test_command", "command", "slurm", "needs", "scope")


def _build_children(parent, spec):
    items = spec.get("children") if isinstance(spec, dict) else spec
    if not isinstance(items, list) or not items:
        raise PlanStateError("split spec needs a non-empty list of children")
    kids = []
    for item in items:
        if not isinstance(item, dict):
            raise PlanStateError("each child must be an object")
        cid = item.get("id")
        if not paths.valid_id(cid):
            raise PlanStateError("child unit id %r does not match %s" % (cid, paths.ID_PATTERN))
        if item.get("kind", parent["kind"]) != parent["kind"]:
            raise PlanStateError("child %s changes the unit kind" % cid)
        if not P._present(item.get("outputs")):
            raise PlanStateError("child %s must declare outputs" % cid)
        ppred = dict((o["path"], o) for o in parent["outputs"])
        raw = dict((k, copy.deepcopy(parent[k])) for k in _INHERIT if k in parent)
        raw.update(copy.deepcopy(item))
        raw["title"] = item.get("title") or "%s (%s)" % (parent["title"], cid)
        outs = []
        for o in item["outputs"]:
            p = P.output_path(o)
            outs.append(dict(ppred[p]) if isinstance(o, str) and p in ppred else o)
        raw["outputs"] = outs
        errors = []
        P._check_outputs("child %s" % cid, outs, errors)
        if errors:
            raise PlanStateError(errors[0])
        kids.append(P.normalize_unit(raw))
    return kids


def _check_no_growth(parent, kids):
    pout = dict((o["path"], o) for o in parent["outputs"])
    covered = set()
    for k in kids:
        for o in k["outputs"]:
            if o["path"] not in pout:
                raise OwnerStop("child %s adds output %r beyond parent %s" % (k["id"], o["path"], parent["id"]))
            if o != pout[o["path"]]:
                raise OwnerStop("child %s changes the predicate of output %r (parent %s)"
                                % (k["id"], o["path"], parent["id"]))
            covered.add(o["path"])
        for g in k.get("scope", []):
            if not P.scope_covers(parent.get("scope", []), g):
                raise OwnerStop("child %s adds scope %r beyond parent %s" % (k["id"], g, parent["id"]))
    dropped = sorted(set(pout) - covered)
    if dropped:
        raise OwnerStop("split of %s drops parent output(s) %s" % (parent["id"], ", ".join(map(repr, dropped))))


def split_unit(root, plan_id, unit_id, spec, reason):
    """Replace an unclosed unit with children. Returns the revision record."""
    if not isinstance(reason, str) or not reason.strip():
        raise PlanStateError("split requires a non-blank reason")
    require_approval(root, plan_id)
    eff = effective_plan(root, plan_id)
    parent = next((u for u in eff["units"] if u["id"] == unit_id), None)
    if parent is None:
        raise PlanStateError("unit %r is not in plan %s (already split?)" % (unit_id, plan_id))
    pstate = load_unit_state(root, plan_id, unit_id)
    if pstate["closure"].get("state") in CLOSED_STATES:
        raise PlanStateError("unit %s is closed (%s); only unclosed units can be split"
                             % (unit_id, pstate["closure"].get("state")))
    kids = _build_children(parent, spec)
    ever = set(u["id"] for u in store.read_json(_p(root, plan_id, "plan.json"))["units"])
    for r in load_revisions(root, plan_id):
        ever.update(c["id"] for c in r["children"])
    seen = set()
    for k in kids:
        if k["id"] in ever or k["id"] in seen:
            raise PlanStateError("child unit id %r is already used in plan %s" % (k["id"], plan_id))
        seen.add(k["id"])
    _check_no_growth(parent, kids)

    now = store.utc_now()
    rev = {"seq": len(load_revisions(root, plan_id)) + 1, "time": now, "parent": unit_id,
           "reason": reason.strip(), "children": kids,
           "drafts": dict((k["id"], P.render_draft(plan_id, k)) for k in kids)}
    errors, _ = P.validate_plan(apply_revision(eff, rev))
    if errors:
        raise PlanStateError("split produces an invalid plan: " + "; ".join(errors))

    digest = require_approval(root, plan_id)["digest"]
    for k in kids:
        st = default_unit_state(plan_id, k["id"], digest)
        st["lineage"] = {"parent": unit_id, "reason": rev["reason"], "time": now}
        st["counters"] = dict(pstate["counters"])
        st["authorization"]["via"] = "split"
        save_unit_state(root, plan_id, k["id"], st)
    pstate["closure"] = {"state": "split", "children": [k["id"] for k in kids], "time": now}
    save_unit_state(root, plan_id, unit_id, pstate)
    store.write_json_atomic(_p(root, plan_id, "revisions.json"), load_revisions(root, plan_id) + [rev])
    store.append_event(root, "unit_split", plan_id=plan_id, parent=unit_id,
                       children=[k["id"] for k in kids], reason=rev["reason"])
    return rev


# ---- show -----------------------------------------------------------------

def describe_plan(root, plan_id):
    paths.check_id(plan_id, "plan id")
    plan, issues, approval = _load_stored(root, plan_id)
    if plan is None or approval is None:
        raise PlanStateError("plan %s is not approved" % plan_id)
    try:
        require_approval(root, plan_id)
        verdict = "valid"
    except ApprovalError as e:
        verdict = "INVALID: %s" % e
    lines = ["plan: %s" % plan_id, "digest: %s" % approval["digest"],
             "approver: %s at %s" % (approval["approver"], approval["approved_at"]),
             "approval: %s" % verdict]
    units = effective_plan(root, plan_id)["units"]
    lines.append("units: %d" % len(units))
    for u in units:
        st = load_unit_state(root, plan_id, u["id"])
        lines.append("unit %s [%s] %s" % (u["id"], u["kind"], u["title"]))
        for f in FACETS + ("lineage", "counters"):
            lines.append("  %s: %s" % (f, json.dumps(st[f], sort_keys=True)))
    return lines
