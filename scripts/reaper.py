#!/usr/bin/env python3
import argparse
import bisect
import fcntl
import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import reaper_policy as policy  # noqa: E402

HELP = """Delete disk the writers on this machine leave behind, by the table in reaper_policy.py.

Dry run by default: prints every path it would delete with size, rule and age. --apply deletes and
writes one JSON line per deletion to the log. A worktree with commits on no remote is pushed to
origin as refs/heads/wip/<branch> first; one with uncommitted tracked changes or untracked files that
are not ignored is kept. Symlinks are never followed. A path a live process has as cwd or open, or
that is named in a live process command line, is kept. Targets on /Volumes are opened only with
--volumes, and never when REAPER_NO_VOLUMES is set (the launchd job sets it: it has no stable Full
Disk Access grant, so an open there raises the Removable Volumes stall)."""

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
BOOTSTRAP_LINKS = ("node_modules", ".venv")
GIT_TIMEOUT = 120


class Env:
    def __init__(self, home=None, tmpdir=None, uid=None, now=None, volumes="/Volumes", systmp="/private/tmp",
                 allow_volumes=False, apply=False, sessions_dir=None, state_dir=None, run=None,
                 ttl_override=None, repo=None):
        self.home = home or os.path.expanduser("~")
        self.tmpdir = (tmpdir or os.environ.get("TMPDIR") or "/tmp").rstrip("/") or "/tmp"
        self.uid = os.getuid() if uid is None else uid
        self.now = time.time() if now is None else now
        self.volumes = volumes.rstrip("/")
        self.systmp = systmp.rstrip("/")
        self.volume_prefixes = (self.volumes + "/",)
        self.allow_volumes = allow_volumes
        self.apply = apply
        self.sessions_dir = sessions_dir or os.path.join(self.home, ".claude", "sessions")
        self.state_dir = state_dir or os.path.join(self.home, ".local", "state", "reaper")
        self.run = run or run_command
        self.ttl_override = ttl_override
        self.repo = repo
        self.repo_root = os.path.dirname(repo) if repo else None
        self.live = None
        self.session_ids = None
        self.session_cwds = None

    def log_path(self):
        return os.path.join(self.state_dir, "reaper.jsonl")


def run_command(cmd, cwd=None, timeout=GIT_TIMEOUT):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL, errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % timeout
    except OSError as e:
        return 127, str(e)
    return p.returncode, p.stdout if p.returncode == 0 else (p.stderr or p.stdout)


class LiveSet:
    def __init__(self, paths, cmdtext):
        self.paths = sorted(set(paths))
        self.cmdtext = cmdtext

    @classmethod
    def scan(cls, run):
        rc, out = run(["lsof", "-nP", "-w", "-F", "n"], timeout=300)
        paths = [line[1:] for line in out.splitlines() if line.startswith("n/")] if rc in (0, 1) else []
        rc, cmds = run(["ps", "-axo", "command="], timeout=60)
        return cls(paths, cmds if rc == 0 else "")

    def touches(self, *candidates):
        for c in candidates:
            i = bisect.bisect_left(self.paths, c)
            if i < len(self.paths) and self.paths[i] == c:
                return True
            j = bisect.bisect_left(self.paths, c + "/")
            if j < len(self.paths) and self.paths[j].startswith(c + "/"):
                return True
            if c in self.cmdtext:
                return True
        return False


class Cand:
    def __init__(self, rule, path, st, env):
        self.rule = rule
        self.path = path
        self.name = os.path.basename(path)
        self.st = st
        self.env = env
        self.canon = os.path.join(os.path.realpath(os.path.dirname(path)), self.name)
        self._measure = None
        self._gitdir = False

    def measure(self):
        if self._measure is None:
            self._measure = measure(self.path)
        return self._measure

    def gitdir(self):
        if self._gitdir is False:
            self._gitdir = read_gitdir(self.path)
        return self._gitdir


class Decision:
    def __init__(self, rule, path, action, reason="", nbytes=0, age_days=0.0, wip=None, files=0):
        self.rule = rule
        self.path = path
        self.action = action
        self.reason = reason
        self.bytes = nbytes
        self.age_days = age_days
        self.wip = wip
        self.files = files

    def row(self):
        r = {"rule": self.rule, "path": self.path, "action": self.action, "bytes": self.bytes,
             "age_days": round(self.age_days, 2)}
        if self.reason:
            r["reason"] = self.reason
        if self.wip:
            r["wip_ref"] = self.wip
        return r


def measure(path):
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0.0, 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_blocks * 512, st.st_mtime, 1
    total, newest, files, seen = st.st_blocks * 512, st.st_mtime, 1, set()
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    s = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                files += 1
                if s.st_mtime > newest:
                    newest = s.st_mtime
                if s.st_nlink > 1:
                    key = (s.st_dev, s.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                total += s.st_blocks * 512
                if stat.S_ISDIR(s.st_mode):
                    stack.append(e.path)
    return total, newest, files


def on_volume(path, env):
    parts = [p for p in path.split("/") if p]
    cur = "/"
    for _ in range(40):
        for prefix in env.volume_prefixes:
            if (cur.rstrip("/") + "/").startswith(prefix):
                return True
        if not parts:
            return False
        nxt = os.path.join(cur, parts.pop(0))
        if any((nxt.rstrip("/") + "/").startswith(p) for p in env.volume_prefixes):
            return True
        try:
            st = os.lstat(nxt)
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(nxt)
            nxt = target if target.startswith("/") else os.path.normpath(os.path.join(cur, target))
            parts = [p for p in nxt.split("/") if p] + parts
            cur = "/"
            continue
        cur = nxt
    return False


def expand(template, env):
    if "{repo}" in template:
        if not env.repo_root:
            return None
        template = template.replace("{repo}", env.repo_root)
    s = (template.replace("{tmpdir}", env.tmpdir).replace("{home}", env.home)
         .replace("{uid}", str(env.uid))
         .replace("{volumes}", env.volumes).replace("{systmp}", env.systmp))
    return os.path.normpath(s)


def has_glob(s):
    return any(c in s for c in "*?[")


def list_dir(path):
    try:
        with os.scandir(path) as it:
            return [(e.name, e.path) for e in it]
    except OSError:
        return []


def expand_glob(pattern, env):
    bases = ["/"]
    for comp in [c for c in pattern.split("/") if c]:
        nxt = []
        for base in bases:
            names = [n for n, _ in list_dir(base) if fnmatch.fnmatchcase(n, comp)] if has_glob(comp) else [comp]
            for n in names:
                p = os.path.join(base, n)
                if on_volume(p, env) and not env.allow_volumes:
                    continue
                if os.path.lexists(p):
                    nxt.append(p)
        bases = nxt
    return sorted(bases)


def candidates(rule, env):
    for template in rule.roots:
        expanded = expand(template, env)
        if expanded is None:
            continue
        for root in expand_glob(expanded, env):
            level = [root]
            for depth in range(1, rule.depth + 1):
                nxt = []
                for d in level:
                    for name, p in list_dir(d):
                        if depth < rule.depth:
                            try:
                                if stat.S_ISDIR(os.lstat(p).st_mode):
                                    nxt.append(p)
                            except OSError:
                                pass
                        elif any(fnmatch.fnmatchcase(name, pat) for pat in rule.names) and not any(
                                fnmatch.fnmatchcase(name, x) for x in rule.exclude):
                            nxt.append(p)
                level = nxt
            for p in level:
                yield p


def read_gitdir(path):
    dotgit = os.path.join(path, ".git")
    try:
        st = os.lstat(dotgit)
        if not stat.S_ISREG(st.st_mode):
            return None
        with open(dotgit) as f:
            first = f.readline().strip()
    except OSError:
        return None
    if not first.startswith("gitdir:"):
        return None
    gd = first[7:].strip()
    return gd if os.path.isabs(gd) else os.path.normpath(os.path.join(path, gd))


def common_dir(gitdir):
    try:
        with open(os.path.join(gitdir, "commondir")) as f:
            rel = f.read().strip()
    except OSError:
        return None
    return os.path.normpath(os.path.join(gitdir, rel))


def worktree_activity(cand):
    newest = cand.st.st_mtime
    gd = cand.gitdir()
    if gd:
        for rel in ("HEAD", "logs/HEAD"):
            try:
                newest = max(newest, os.lstat(os.path.join(gd, rel)).st_mtime)
            except OSError:
                pass
    return newest


def live(env):
    if env.live is None:
        env.live = LiveSet.scan(env.run)
    return env.live


def live_sessions(env):
    if env.session_ids is None:
        ids, cwds = set(), []
        for name, p in list_dir(env.sessions_dir):
            if not name.endswith(".json"):
                continue
            try:
                with open(p) as f:
                    d = json.load(f)
                pid = int(d.get("pid", 0))
                if pid <= 0:
                    continue
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            except (OSError, ValueError, TypeError):
                continue
            if d.get("sessionId"):
                ids.add(d["sessionId"])
            if d.get("cwd"):
                cwds.append(d["cwd"])
        ids.update(UUID.findall(live(env).cmdtext))
        env.session_ids, env.session_cwds = ids, cwds
    return env.session_ids, env.session_cwds


def check_owned(c):
    return None if c.st.st_uid == c.env.uid else "owned by another user"


def check_not_open(c):
    if live(c.env).touches(c.canon, c.path):
        return "in use: a live process has it as cwd, open, or in its command line"
    return None


def check_session_idle(c):
    ids, _ = live_sessions(c.env)
    return "live claude session %s" % c.name if c.name in ids else None


def check_session_cwd_idle(c):
    _, cwds = live_sessions(c.env)
    for cwd in cwds:
        if cwd == c.canon or cwd == c.path or cwd.startswith(c.canon + "/") or cwd.startswith(c.path + "/"):
            return "live claude session works inside it"
    return None


def check_no_nested_git(c):
    stack = [(c.path, 0)]
    while stack:
        cur, depth = stack.pop()
        for name, p in list_dir(cur):
            if name == ".git":
                return "contains a git checkout (%s): kept, review by hand" % os.path.relpath(p, c.path)
            if depth < 3 and name != "node_modules":
                try:
                    if stat.S_ISDIR(os.lstat(p).st_mode):
                        stack.append((p, depth + 1))
                except OSError:
                    pass
    return None


def check_worktree_linked(c):
    return None if c.gitdir() and os.path.isdir(c.gitdir()) else "not a linked git worktree"


def check_worktree_unlocked(c):
    lock = os.path.join(c.gitdir(), "locked")
    if os.path.exists(lock):
        try:
            with open(lock) as f:
                why = f.read().strip()
        except OSError:
            why = ""
        return "locked%s" % (": " + why if why else "")
    return None


def check_worktree_clean(c):
    rc, out = c.env.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"], cwd=c.path)
    if rc != 0:
        return "git status failed: %s" % out.strip()[:80]
    left = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if not e:
            continue
        code, f = e[:2], e[3:]
        if code[0] in "RC":
            i += 1
        if code == "??" and f.rstrip("/") in BOOTSTRAP_LINKS and os.path.islink(os.path.join(c.path, f.rstrip("/"))):
            continue
        left.append(f)
    if left:
        return "uncommitted work: %d paths (%s)" % (len(left), ", ".join(left[:3]))
    return None


CHECKS = {
    "owned": check_owned,
    "not_open": check_not_open,
    "session_idle": check_session_idle,
    "session_cwd_idle": check_session_cwd_idle,
    "no_nested_git": check_no_nested_git,
    "worktree_linked": check_worktree_linked,
    "worktree_unlocked": check_worktree_unlocked,
    "worktree_clean": check_worktree_clean,
}


def wip_ref(c):
    rc, branch = c.env.run(["git", "symbolic-ref", "-q", "--short", "HEAD"], cwd=c.path)
    branch = branch.strip() if rc == 0 else ""
    if not branch:
        rc, sha = c.env.run(["git", "rev-parse", "--short", "HEAD"], cwd=c.path)
        branch = "detached-%s" % (sha.strip() if rc == 0 else "unknown")
    return "refs/heads/" + (branch if branch.startswith("wip/") else "wip/" + branch)


def prepare_worktree(c):
    rc, out = c.env.run(["git", "rev-list", "--count", "HEAD", "--not", "--remotes"], cwd=c.path)
    if rc != 0:
        return None, "git rev-list failed: %s" % out.strip()[:80]
    if int(out.strip() or 0) == 0:
        return None, None
    rc, _ = c.env.run(["git", "remote", "get-url", "origin"], cwd=c.path)
    if rc != 0:
        return None, "%s commits on no remote and no origin to push them to" % out.strip()
    ref = wip_ref(c)
    if not c.env.apply:
        return ref, None
    rc, out = c.env.run(["git", "push", "-q", "origin", "HEAD:" + ref], cwd=c.path)
    if rc != 0:
        return None, "push to %s failed: %s" % (ref, out.strip()[:100])
    return ref, None


def remove_tree(path):
    def onerror(func, p, exc):
        try:
            os.chmod(os.path.dirname(p), 0o700)
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            raise exc[1]
    st = os.lstat(path)
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path, onerror=onerror)
    else:
        os.unlink(path)


def remove_worktree(c):
    gd = c.gitdir()
    cd = common_dir(gd) if gd else None
    if not cd:
        raise OSError("no common dir for %s" % c.path)
    rc, out = c.env.run(["git", "--git-dir", cd, "worktree", "remove", "--force", c.path])
    if rc != 0:
        raise OSError("git worktree remove failed: %s" % out.strip()[:120])
    if os.path.lexists(c.path):
        remove_tree(c.path)
        c.env.run(["git", "--git-dir", cd, "worktree", "prune"])


def append_log(env, row):
    os.makedirs(env.state_dir, exist_ok=True)
    row = dict(row, ts=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(env.now)))
    with open(env.log_path(), "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def evaluate(rule, path, env):
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return Decision(rule.id, path, "keep", "symlink, never followed")
    kind = "dir" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "other"
    if kind not in rule.kinds:
        return None
    c = Cand(rule, path, st, env)
    ttl = env.ttl_override if env.ttl_override is not None else rule.ttl_days
    cutoff = env.now - ttl * 86400
    newest = worktree_activity(c) if rule.age == "worktree" else st.st_mtime
    if newest > cutoff:
        return Decision(rule.id, path, "keep", "young", age_days=(env.now - newest) / 86400)
    for name in rule.checks:
        why = CHECKS[name](c)
        if why:
            return Decision(rule.id, path, "keep", why, age_days=(env.now - newest) / 86400)
    wip = None
    if rule.remover == "worktree":
        wip, why = prepare_worktree(c)
        if why:
            return Decision(rule.id, path, "keep", why, age_days=(env.now - newest) / 86400)
    nbytes, deep_newest, files = c.measure()
    if rule.age == "deep":
        newest = max(newest, deep_newest)
        if newest > cutoff:
            return Decision(rule.id, path, "keep", "young", age_days=(env.now - newest) / 86400)
    d = Decision(rule.id, path, "would-delete", nbytes=nbytes, age_days=(env.now - newest) / 86400,
                 wip=wip, files=files)
    if not env.apply:
        return d
    try:
        if rule.remover == "worktree":
            remove_worktree(c)
        else:
            remove_tree(path)
    except OSError as e:
        d.action, d.reason = "error", str(e)[:200]
        append_log(env, d.row())
        return d
    d.action = "deleted"
    append_log(env, d.row())
    return d


def common_of(path):
    gd = read_gitdir(path)
    cd = common_dir(gd) if gd else None
    return os.path.realpath(cd) if cd else None


def run_rules(env, rules, only=None, on_decision=None):
    decisions, claimed = [], set()
    for rule in rules:
        if only and rule.id not in only and rule.group not in only:
            continue
        roots = [x for x in (expand(r, env) for r in rule.roots) if x]
        if roots and any(on_volume(r, env) for r in roots) and not env.allow_volumes:
            d = Decision(rule.id, roots[0], "skip", "volume target, not opened (needs --volumes, never from launchd)")
            decisions.append(d)
            if on_decision:
                on_decision(d)
            continue
        for path in candidates(rule, env):
            if path in claimed:
                continue
            claimed.add(path)
            if env.repo and rule.remover == "worktree" and common_of(path) != env.repo:
                continue
            d = evaluate(rule, path, env)
            if d:
                decisions.append(d)
                if on_decision:
                    on_decision(d)
    return decisions


def human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return ("%d%s" % (n, unit)) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0


def line(d):
    if d.action in ("would-delete", "deleted", "error"):
        extra = " wip=%s" % d.wip if d.wip else ""
        extra += " ERROR %s" % d.reason if d.action == "error" else ""
        return "%-12s %8s  %-16s age=%5.1fd  %s%s" % (d.action, human(d.bytes), d.rule, d.age_days, d.path, extra)
    return "%-12s %8s  %-16s age=%5.1fd  %s  (%s)" % (d.action, "-", d.rule, d.age_days, d.path, d.reason)


def summarize(decisions, rules):
    rows = []
    for rule in rules:
        mine = [d for d in decisions if d.rule == rule.id]
        if not mine:
            continue
        gone = [d for d in mine if d.action in ("would-delete", "deleted")]
        rows.append({"rule": rule.id, "ttl_days": rule.ttl_days, "delete": len(gone),
                     "bytes": sum(d.bytes for d in gone),
                     "young": sum(1 for d in mine if d.action == "keep" and d.reason == "young"),
                     "kept": sum(1 for d in mine if d.action == "keep" and d.reason != "young"),
                     "skipped": sum(1 for d in mine if d.action == "skip"),
                     "errors": sum(1 for d in mine if d.action == "error")})
    return rows


def print_summary(rows, apply):
    verb = "deleted" if apply else "would delete"
    print("%-16s %6s %-13s %8s %8s %7s" % ("rule", "ttl_d", verb, "bytes", "young", "kept"))
    for r in rows:
        print("%-16s %6g %-13d %8s %8d %7d%s%s" % (
            r["rule"], r["ttl_days"], r["delete"], human(r["bytes"]), r["young"], r["kept"],
            "  skipped (volume)" if r["skipped"] else "", "  ERRORS %d" % r["errors"] if r["errors"] else ""))
    print("%-16s %6s %-13d %8s" % ("TOTAL", "", sum(r["delete"] for r in rows),
                                   human(sum(r["bytes"] for r in rows))))


def validate(rules):
    problems, seen = [], set()
    for r in rules:
        if r.id in seen:
            problems.append("duplicate id %s" % r.id)
        seen.add(r.id)
        if not r.ttl_days > 0:
            problems.append("%s: ttl_days must be above 0" % r.id)
        if not r.roots or not r.names:
            problems.append("%s: roots and names are required" % r.id)
        for n in r.checks:
            if n not in CHECKS:
                problems.append("%s: unknown check %s" % (r.id, n))
        for n in r.names:
            if "/" in n:
                problems.append("%s: name pattern %r has a slash" % (r.id, n))
        for root in r.roots:
            if not (root.startswith("/") or root.startswith("{")):
                problems.append("%s: root %r is not absolute" % (r.id, root))
        if r.remover == "worktree" and "worktree_clean" not in r.checks:
            problems.append("%s: a worktree row needs worktree_clean" % r.id)
        if r.remover == "worktree" and r.age != "worktree":
            problems.append("%s: a worktree row needs age=worktree" % r.id)
        if r.remover not in ("tree", "worktree"):
            problems.append("%s: unknown remover %s" % (r.id, r.remover))
        if r.age not in ("deep", "mtime", "worktree"):
            problems.append("%s: unknown age basis %s" % (r.id, r.age))
        if r.depth < 1:
            problems.append("%s: depth must be 1 or more" % r.id)
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    ap.add_argument("--volumes", action="store_true", help="also open targets under /Volumes")
    ap.add_argument("--only", action="append", default=[], metavar="RULE_OR_GROUP",
                    help="rule id or group (tmp, chrome, scratch, worktrees); repeatable")
    ap.add_argument("--repo", metavar="PATH", help="worktree rows: only worktrees of this repo")
    ap.add_argument("--ttl-days", type=float, help="override every selected rule's ttl")
    ap.add_argument("--list-rules", action="store_true", help="print the policy table and exit")
    ap.add_argument("--json", action="store_true", help="print one JSON object per decision")
    ap.add_argument("--quiet", action="store_true", help="print only the summary")
    ap.add_argument("--state-dir", default=os.environ.get("REAPER_STATE_DIR"))
    a = ap.parse_args(argv)

    rules = policy.RULES
    problems = validate(rules)
    if problems:
        print("reaper: bad policy table:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    if a.list_rules:
        for r in rules:
            print("%-16s %-9s ttl=%-5g depth=%d names=%s roots=%s checks=%s" % (
                r.id, r.group, r.ttl_days, r.depth, ",".join(r.names), ",".join(r.roots), ",".join(r.checks)))
        return 0
    known = {r.id for r in rules} | {r.group for r in rules}
    for o in a.only:
        if o not in known:
            print("reaper: unknown rule or group %r" % o, file=sys.stderr)
            return 2

    repo = None
    if a.repo:
        rc, out = run_command(["git", "-C", a.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"])
        if rc != 0:
            print("reaper: %s is not a git repo" % a.repo, file=sys.stderr)
            return 2
        repo = os.path.realpath(out.strip())
    env = Env(volumes=os.environ.get("REAPER_VOLUMES", "/Volumes"), systmp=os.environ.get("REAPER_SYSTMP", "/private/tmp"),
              allow_volumes=a.volumes and not os.environ.get("REAPER_NO_VOLUMES"), apply=a.apply,
              state_dir=a.state_dir, ttl_override=a.ttl_days, repo=repo)

    lock = None
    if a.apply:
        os.makedirs(env.state_dir, exist_ok=True)
        lock = open(os.path.join(env.state_dir, "lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            print("reaper: another run holds the lock", file=sys.stderr)
            return 3
    try:
        return sweep(a, env, rules)
    finally:
        if lock:
            lock.close()


def sweep(a, env, rules):
    def show(d):
        if a.json:
            print(json.dumps(d.row(), sort_keys=True), flush=True)
        elif not a.quiet and not (d.action == "keep" and d.reason == "young"):
            print(line(d), flush=True)

    decisions = run_rules(env, rules, set(a.only) or None, show)
    rows = summarize(decisions, rules)
    if a.json:
        print(json.dumps({"summary": rows, "apply": a.apply}, sort_keys=True))
    else:
        print_summary(rows, a.apply)
        if not a.apply:
            print("dry run: nothing deleted. --apply deletes.")
    if a.apply:
        append_log(env, {"action": "run", "deleted": sum(r["delete"] for r in rows),
                         "bytes": sum(r["bytes"] for r in rows), "errors": sum(r["errors"] for r in rows)})
    return 1 if any(r["errors"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
