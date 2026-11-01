"""Model roles per host family, recorded in `.overseer/models.json`."""
import datetime
import os
import re
import sys

from . import env as E, paths, store

CLAUDE_ROLES = {"plan": "opus", "code": "sonnet", "review": "haiku"}
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
    parts = [int(x) for x in version.split(".")]
    while parts and parts[-1] == 0:
        parts.pop()
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
    ids = [i for i, _ in entries if i.startswith("gemini-")]
    top = _best(ids, _PRO)
    review = _best(ids, _FLASH)
    if top is None:
        raise ModelError("no eligible plan/code model (gemini-<v>-(argon|carbon|pro)-high) in listing")
    if review is None:
        raise ModelError("no eligible review model (gemini-<v>-flash-high) in listing")
    return {"plan": top, "code": top, "review": review}


def _valid_cache(c):
    if not (isinstance(c, dict) and isinstance(c.get("date"), str) and isinstance(c.get("host_family"), str)
            and isinstance(c.get("choices"), dict) and isinstance(c.get("raw"), str)):
        return False
    try:
        datetime.date.fromisoformat(c["date"])
    except ValueError:
        return False
    return all(isinstance(c["choices"].get(r), str) for r in CLAUDE_ROLES)


def cache_age_days(cache, today=None):
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    return (today - datetime.date.fromisoformat(cache["date"])).days


def read_cache(root):
    try:
        return store.read_json(os.path.join(paths.state_dir(root), "models.json"), None)
    except ValueError:
        return "unparseable"


def resolve(root, host, refresh=False, base=None, today=None):
    """Return (record, warnings). Writes models.json only when it resolves anew."""
    if host not in ("claude", "google"):
        raise ModelError("host must be claude or google")
    warnings = []
    cache = read_cache(root)
    if cache is not None and not _valid_cache(cache):
        warnings.append("ignoring malformed models.json")
        cache = None
    if cache and cache["host_family"] == host and not refresh:
        age = cache_age_days(cache, today)
        if age > MAX_AGE_DAYS:
            warnings.append("models.json is %d days old; run `models --host %s --refresh`" % (age, host))
        return cache, warnings
    raw = ""
    if host == "claude":
        choices = dict(CLAUDE_ROLES)
    else:
        env = E.worker_env(base)
        r = E.run_tool(["agy", "models"], env, LIST_TIMEOUT)
        if r["timed_out"]:
            raise ModelError("`agy models` timed out")
        if r["rc"] != 0:
            raise ModelError("`agy models` failed (rc %s): %s" % (r["rc"], r["stderr"].strip()[:200]))
        raw = r["stdout"]
        choices = resolve_google(parse_listing(raw))
    day = (today or datetime.datetime.now(datetime.timezone.utc).date()).isoformat()
    record = {"date": day, "host_family": host, "raw": raw, "choices": choices}
    # Event first: the record is the cache, so a crash between the two writes must leave
    # the record missing (retry rewrites it) rather than the event missing (retry would hit the cache).
    store.append_event(root, "models-resolved", host_family=host, choices=choices)
    store.write_json_atomic(os.path.join(paths.state_dir(root), "models.json"), record)
    return record, warnings


def configure(p):
    p.add_argument("--host", required=True, choices=["claude", "google"])
    p.add_argument("--refresh", action="store_true", help="re-resolve even if models.json is current")


def run(args):
    root = paths.find_root(args.root)
    with store.ProjectLock(root, "models"):
        rec, warnings = resolve(root, args.host, args.refresh)
    for w in warnings:
        sys.stderr.write("warning: %s\n" % w)
    print("host: %s (resolved %s)" % (rec["host_family"], rec["date"]))
    for role in ("plan", "code", "review"):
        print("%s: %s" % (role, rec["choices"][role]))
    return 0
