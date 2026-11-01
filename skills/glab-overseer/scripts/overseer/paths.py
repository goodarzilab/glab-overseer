"""Project root discovery and `.overseer/` path helpers.

Every id that becomes a path component goes through `check_id`; every
repository-relative path goes through `safe_relpath` (or `relpath_problem`
when no filesystem is involved).
"""
import fnmatch
import os
import re
import subprocess

ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
ID_RE = re.compile(ID_PATTERN)
STATE_DIRNAME = ".overseer"


class UnsafePath(ValueError):
    pass


class InvalidId(ValueError):
    pass


def valid_id(value):
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


def check_id(value, what="id"):
    if not valid_id(value):
        raise InvalidId("%s %r does not match %s" % (what, value, ID_PATTERN))
    return value


def find_root(start=None):
    """Git toplevel of `start` (default cwd), else `start` itself."""
    cwd = os.path.realpath(start or os.getcwd())
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=15,
        )
        top = out.stdout.strip()
        if out.returncode == 0 and top:
            return os.path.realpath(top)
    except (OSError, subprocess.SubprocessError):
        pass
    return cwd


def relpath_problem(rel):
    """Return why `rel` is not an acceptable repo-relative path, or None."""
    if not isinstance(rel, str) or not rel.strip():
        return "is empty or not a string"
    if "\0" in rel:
        return "contains a NUL byte"
    if os.path.isabs(rel) or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        return "is absolute"
    if ".." in rel.replace("\\", "/").split("/"):
        return "contains '..'"
    return None


def safe_relpath(root, rel):
    """Resolve `rel` under `root`; refuse absolute paths, `..` and escaping symlinks."""
    problem = relpath_problem(rel)
    if problem:
        raise UnsafePath("path %r %s" % (rel, problem))
    root_real = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root_real, rel))
    if os.path.commonpath([root_real, full]) != root_real:
        raise UnsafePath("path %r resolves outside the project root" % (rel,))
    return full


def state_dir(root):
    return os.path.join(root, STATE_DIRNAME)


def ensure_state_dir(root):
    """Create `.overseer/` and its self-ignoring `.gitignore` on first state write."""
    d = state_dir(root)
    os.makedirs(d, exist_ok=True)
    gi = os.path.join(d, ".gitignore")
    if not os.path.exists(gi):
        try:
            with open(gi, "x", encoding="utf-8") as f:
                f.write("*\n")
        except FileExistsError:
            pass
    return d


def lock_path(root):
    return os.path.join(state_dir(root), "lock")


def events_path(root):
    return os.path.join(state_dir(root), "events.jsonl")


def plans_dir(root):
    return os.path.join(state_dir(root), "plans")


def plan_dir(root, plan_id):
    return os.path.join(plans_dir(root), check_id(plan_id, "plan id"))


def unit_dir(root, plan_id, unit_id):
    return os.path.join(plan_dir(root, plan_id), "units", check_id(unit_id, "unit id"))


def attempt_dir(root, plan_id, unit_id, n):
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError("attempt number must be a positive integer")
    return os.path.join(unit_dir(root, plan_id, unit_id), "attempts", "a%d" % n)


GLOB_META = "*?["


def has_glob_meta(s):
    return any(c in s for c in GLOB_META)


def _seg_match(pats, segs):
    if not pats:
        return not segs
    if pats[0] == "**":
        return any(_seg_match(pats[1:], segs[i:]) for i in range(len(segs) + 1))
    if not segs:
        return False
    return fnmatch.fnmatchcase(segs[0], pats[0]) and _seg_match(pats[1:], segs[1:])


def glob_match(pattern, path):
    """Segment-aware glob match for real repo-relative paths.

    `*`, `?` and `[...]` never cross `/`. A segment that is exactly `**`
    matches zero or more whole segments.
    """
    return _seg_match(pattern.split("/"), path.split("/"))
