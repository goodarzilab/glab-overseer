"""Model roles for both families, resolved from one host, in `.overseer/models.json`."""
import datetime
import os
import re
import sys

from . import env as E, paths, store

CLAUDE_ROLES = {"plan": "opus", "code": "sonnet", "review": "haiku"}
FAMILIES = ("claude", "google")
MAX_AGE_DAYS = 30
LIST_TIMEOUT = 60
_PRO = re.compile(r"^gemini-(\d+(?:\.\d+)*)-(argon|carbon|pro)-high$")
_FLASH = re.compile(r"^gemini-(\d+(?:\.\d+)*)-(flash)-high$")
_RANK = {"argon": 3, "carbon": 2, "pro": 1, "flash": 0}


class ModelError(ValueError):
    pass


def parse_listing(text):
    """Parse `<id><TAB><label>` lines; lines without a tab (progress text) are skipped."""
    entries = []
    for line in (text or "").splitlines():
        if "\t" in line:
            mid, label = line.split("\t", 1)
            if mid.strip():
                entries.append((mid.strip(), label.strip()))
    if not entries:
        raise ModelError("agy model listing is empty or unparseable")
    return entries


def _vkey(version):
    parts = [int(x) for x in version.split(".")]  # exact tuples: (3,) sorts below (3, 0)
    return tuple(parts)


def _best(ids, rx):
    best = None
    for mid in ids:
        m = rx.match(mid)
        if m:
            key = (_vkey(m.group(1)), _RANK[m.group(2)])
            if best is None or key > best[0]:
                best = (key, mid)
    return best[1] if best else None


def resolve_google(entries):
    """The newest pro-tier model plans and codes; without one (some accounts list only flash), the newest
    flash model does. The newest flash model always reviews."""
    ids = [i for i, _ in entries if i.startswith("gemini-")]
    review = _best(ids, _FLASH)
    if review is None:
        raise ModelError("no eligible review model (gemini-<v>-flash-high) in listing")
    top = _best(ids, _PRO) or review
    return {"plan": top, "code": top, "review": review}


def _roles_ok(d):
    return isinstance(d, dict) and all(isinstance(d.get(r), str) and d[r] for r in CLAUDE_ROLES)


def _valid_cache(c):
    """Shape: {date, host, raw, choices: {claude: roles, google: roles+available or unavailable}}."""
    if not (isinstance(c, dict) and isinstance(c.get("date"), str) and c.get("host") in FAMILIES
            and isinstance(c.get("choices"), dict) and isinstance(c.get("raw"), str)):
        return False
    try:
        datetime.date.fromisoformat(c["date"])
    except ValueError:
        return False
    g = c["choices"].get("google")
    google_ok = isinstance(g, dict) and ((g.get("available") is True and _roles_ok(g))
                                         or (g.get("available") is False and isinstance(g.get("reason"), str)))
    return _roles_ok(c["choices"].get("claude")) and google_ok


def cache_age_days(cache, today=None):
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    return (today - datetime.date.fromisoformat(cache["date"])).days


def read_cache(root):
    try:
        return store.read_json(os.path.join(paths.state_dir(root), "models.json"), None)
    except ValueError:
        return "unparseable"


def load_valid(root):
    """The cached record when it is well formed, else None."""
    c = read_cache(root)
    return c if _valid_cache(c) else None


def roles_for(record, family):
    """Role map for a family, or None when the family is unavailable on this host."""
    c = record["choices"][family]
    return None if c.get("available") is False else c


LIST_ATTEMPTS = 2  # measured 2026-11-01: agy 1.4.12 intermittently prints
# "Error: timed out waiting for available models" and lists fine seconds later.


def _list_once(env):
    r = E.run_tool(["agy", "models"], env, LIST_TIMEOUT)
    if r["timed_out"]:
        raise ModelError("`agy models` timed out")
    if r["rc"] != 0:
        raise ModelError("`agy models` failed (rc %s): %s" % (r["rc"], r["stderr"].strip()[:200]))
    return r["stdout"]


def _resolve_google(base, strict):
    """Return (roles-or-unavailable dict, raw). Errors raise only when `strict`.

    The raw listing is kept even when it yields no eligible role."""
    env = E.worker_env(base)
    raw, err = "", None
    for _ in range(LIST_ATTEMPTS):
        try:
            raw = _list_once(env)
            return dict(resolve_google(parse_listing(raw)), available=True), raw
        except ModelError as e:
            err = e
    if strict:
        raise err
    return {"available": False, "reason": str(err)}, raw


def resolve(root, host, refresh=False, base=None, today=None):
    """Return (record, warnings). Resolves both families; writes models.json only when it resolves anew."""
    if host not in FAMILIES:
        raise ModelError("host must be claude or google")
    warnings = []
    cache = read_cache(root)
    if cache is not None and not _valid_cache(cache):
        warnings.append("ignoring malformed models.json")
        cache = None
    if cache and cache["host"] == host and not refresh:
        age = cache_age_days(cache, today)
        if age > MAX_AGE_DAYS:
            warnings.append("models.json is %d days old; run `models --host %s --refresh`" % (age, host))
        return cache, warnings
    google, raw = _resolve_google(base, strict=(host == "google"))
    choices = {"claude": dict(CLAUDE_ROLES), "google": google}
    day = (today or datetime.datetime.now(datetime.timezone.utc).date()).isoformat()
    record = {"date": day, "host": host, "raw": raw, "choices": choices}
    # Event first: the record is the cache, so a crash between the two writes must leave
    # the record missing (retry rewrites it) rather than the event missing (retry would hit the cache).
    store.append_event(root, "models-resolved", host=host, choices=choices)
    store.write_json_atomic(os.path.join(paths.state_dir(root), "models.json"), record)
    return record, warnings


def configure(p):
    p.add_argument("--host", required=True, choices=list(FAMILIES))
    p.add_argument("--refresh", action="store_true", help="re-resolve even if models.json is current")


def run(args):
    root = paths.find_root(args.root)
    with store.ProjectLock(root, "models"):
        rec, warnings = resolve(root, args.host, args.refresh)
    for w in warnings:
        sys.stderr.write("warning: %s\n" % w)
    print("host: %s (resolved %s)" % (rec["host"], rec["date"]))
    for fam in FAMILIES:
        roles = roles_for(rec, fam)
        if roles is None:
            print("%s: unavailable (%s)" % (fam, rec["choices"][fam]["reason"]))
        else:
            print("%s: %s" % (fam, ", ".join("%s=%s" % (r, roles[r]) for r in CLAUDE_ROLES)))
    return 0
