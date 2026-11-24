#!/usr/bin/env python3
USAGE = """Run a heavy test or build command with a memory cap, a time cap and a machine memory budget.

  python3 ~/.claude/scripts/capped_run.py [--mem-gb N] [--timeout S] [--wait S] [--serial] -- <command> [args...]

Why: on 2026-10-17 two tournament builders ran `make test` (swift test) at the same time on a
16 GB MacBook. Each left a swiftpm-testing-helper at about 19 GB and the laptop crashed. Five
things let it happen, and each has a counter here:

  two runs in parallel        a memory budget: each run reserves its --mem-gb cap before it
                              starts, and a run whose cap does not fit in what is left waits.
                              The sum of the caps of all running capped runs stays under the
                              budget, so small runs run together and big ones queue. --serial
                              (CAPPED_RUN_SERIAL=1) is the old rule: one run on the machine.
  no memory watch             the summed phys_footprint of the whole process tree, every poll.
                              ps RSS is not enough: macOS compresses idle pages, and a 19 GB
                              helper showed 67 MB RSS. phys_footprint counts compressed pages.
  `timeout N make` kills make  the command starts in its own session; on a cap, a signal or the
  but not the helper           end of the command, every process of the tree is stopped, also a
                              descendant that left the process group, and also leftovers that
                              outlive a command that exited normally
  a tool timeout only sends   the time cap belongs to this process, not to the tool call, so it
  the command to background   holds when the call goes to the background
  pkill failed without output each stop is checked: TERM, then KILL, then a STOP: line with the
                              pids that are still alive (exit 70)

Also stops the tree when the machine is low on memory (kern.memorystatus_level at or under
--min-free-pct), whatever uses it: a test run is the thing to give up first.

A SIGKILL of this process cannot be caught, so a guardian watches it: a second process in its own
session (a kill of the tool's process group does not reach it) that holds the read end of a pipe.
When this process dies in any way the pipe closes, and the guardian stops the tree it finds in the
state file (written at each poll) and logs `wrapper-killed`. If the guardian dies too, the next
capped run stops what is left before it starts, and refuses to start (exit 70) while any of it
cannot be stopped.

The reservations are in a ledger file (capped-run.ledger.json) under a short flock. A run frees its
share when it ends; a run that died without that (SIGKILL) is found by its pid and start time, its
tree is stopped, and its share is freed. A reservation is the --mem-gb cap, and a tree over its cap
is stopped, so the reserved sum is the most the runs can use. Admission is first come, first served:
a run registers in the ledger with its arrival time, and is admitted only when it fits and no live
run that arrived earlier is still waiting, so a big run is not overtaken by small ones. A --serial run
goes through the same queue with the whole budget as its share, so it waits only for the runs ahead
of it. Every admitted run then holds capped-run.lock (shared; --serial exclusive) for its life.

A capped run inside a capped run (a verify script that caps its own `swift test`, started under a
capped run) does not queue for the budget or the lock. It used to wait for the lock its own outer
run held until the outer time cap stopped both (the scripts/verify of an iOS app, 2026-10-30), and
a reservation of its own would count the same memory twice. The outer run gives its command
CAPPED_RUN_HOLDER=<its pid>. An inner run skips admission when that pid is one of its ancestors and
holds a share in the ledger (or is the pid in capped-run.lock.holder, written by an outer run of the
version before the ledger). It then keeps its own caps, but does not sweep, write a state file or
start a guardian: the outer run's reservation and watch cover the inner tree.

Default cap 6 GB: the 1,542 logged runs of 2026-10 peaked at p50 0.31, p99 3.16, max 5.93 GB, and 3
`xcodebuild test` runs were between 4 and 6 GB. Budget: 0.75 x hw.memsize (12 GB on a 16 GB Mac), so
two default caps fit. The other 4 GB are for the OS, and --min-free-pct stops a tree before the
machine is out of memory. The 2026-10-17 crash needed about 38 GB. A cap above the budget reserves
the whole budget and runs alone.

Exit: the command's own code; 124 time cap; 125 memory cap or low system memory; 75 the budget or
lock did not free up in --wait seconds; 70 some process could not be stopped; 128+N on signal N.
The last stderr line is always `CAPPED_RUN <reason> exit=<n> peak_gb=<x> secs=<s>`, and one JSON
line goes to ~/.claude/agent-metrics/history/capped-runs.jsonl when ~/.claude/agent-metrics exists.

Environment (defaults): CAPPED_RUN_MEM_GB (6), CAPPED_RUN_BUDGET_GB (0.75 x RAM),
CAPPED_RUN_SERIAL (0), CAPPED_RUN_TIMEOUT (900 s), CAPPED_RUN_WAIT (1800 s), CAPPED_RUN_MIN_FREE_PCT (8), CAPPED_RUN_POLL (0.5 s), CAPPED_RUN_STATE_DIR
(~/.claude/state), CAPPED_RUN_LOG (the jsonl path above), CAPPED_RUN_GRACE (5 s between TERM
and KILL). macOS reads phys_footprint through
libproc; elsewhere RSS from ps is the fallback.
"""
import argparse, contextlib, ctypes, ctypes.util, errno, fcntl, glob, json, os, signal, subprocess, sys, time

HOME = os.path.expanduser("~")
METRICS_DIR = os.path.join(HOME, ".claude", "agent-metrics")
STOP_GRACE = float(os.environ.get("CAPPED_RUN_GRACE", 5))


class RusageV2(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in (
        "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins", "wired_size",
        "resident_size", "phys_footprint", "proc_start_abstime", "proc_exit_abstime",
        "child_user_time", "child_system_time", "child_pkg_idle_wkups", "child_interrupt_wkups",
        "child_pageins", "child_elapsed_abstime", "diskio_bytesread", "diskio_byteswritten")]


try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    _libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(RusageV2)]
except (OSError, AttributeError):
    _libc = None


def usage(pid):
    if _libc is None:
        return None
    ri = RusageV2()
    if _libc.proc_pid_rusage(pid, 2, ctypes.byref(ri)) != 0:
        return None
    return ri.phys_footprint, ri.proc_start_abstime


def table():
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,rss="], capture_output=True, text=True).stdout
    rows = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) == 4 and all(x.isdigit() for x in f):
            rows[int(f[0])] = (int(f[1]), int(f[2]), int(f[3]) * 1024)
    return rows


def free_pct():
    if _libc is None:
        return None
    v, n = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
    if _libc.sysctlbyname(b"kern.memorystatus_level", ctypes.byref(v), ctypes.byref(n), None, 0) != 0:
        return None
    return v.value


def zombies(pids):
    if not pids:
        return set()
    out = subprocess.run(["ps", "-o", "pid=,stat=", "-p", ",".join(map(str, pids))],
                         capture_output=True, text=True).stdout
    return {int(f[0]) for f in (line.split() for line in out.splitlines()) if len(f) == 2 and f[1].startswith("Z")}


def same(pid, start):
    u = usage(pid)
    if u is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return start is None
    return start is None or u[1] == start


class Tree:

    def __init__(self, root):
        self.root, self.seen = root, {}

    def poll(self):
        rows = table()
        fresh = {self.root} | {p for p in rows if rows[p][1] == self.root}
        fresh |= {p for p, s in self.seen.items() if p in rows and same(p, s)}
        while True:
            more = {p for p, (pp, _, _) in rows.items() if pp in fresh and p not in fresh}
            if not more:
                break
            fresh |= more
        total = 0
        for p in fresh:
            u = usage(p)
            if p not in self.seen:
                self.seen[p] = u[1] if u else None
            elif u and self.seen[p] is not None and u[1] != self.seen[p]:
                continue
            total += u[0] if u else rows.get(p, (0, 0, 0))[2]
        return total

    def alive(self):
        return {p: s for p, s in self.seen.items() if same(p, s)}


def signal_all(procs, sig, pgid=None):
    if pgid:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    for p, s in procs.items():
        if same(p, s):
            try:
                os.kill(p, sig)
            except (ProcessLookupError, PermissionError):
                pass


def reap(child):
    if child is not None:
        try:
            child.wait(timeout=0)
        except subprocess.TimeoutExpired:
            pass


def stop(procs, pgid=None, child=None):
    signal_all(procs, signal.SIGTERM, pgid)
    for sig, wait in ((signal.SIGKILL, STOP_GRACE), (None, 2.0)):
        end = time.time() + wait
        while time.time() < end:
            reap(child)
            left = {p for p, s in procs.items() if same(p, s)}
            if not left or not left - zombies(left):
                return []
            time.sleep(0.05)
        if sig:
            signal_all(procs, sig, pgid)
    reap(child)
    left = {p for p, s in procs.items() if same(p, s)}
    return sorted(left - zombies(left))


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def reap_stale(state):
    try:
        with open(state) as f:
            old = json.load(f)
    except (OSError, ValueError):
        return [], []
    procs = {int(p): s for p, s in old.get("procs", {}).items()}
    left = {p: s for p, s in procs.items() if same(p, s)}
    survivors = []
    if left:
        print(f"capped_run: stopping {len(left)} process(es) left by an earlier run "
              f"({old.get('cmd', '?')}): {sorted(left)}", file=sys.stderr)
        survivors = stop(left)
    if not survivors:
        try:
            os.remove(state)
        except FileNotFoundError:
            pass
    return sorted(left), survivors


def guardian(r, w, state, log, ledger, wpid):
    os.setsid()
    os.close(w)
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    try:
        got = os.read(r, 1)
    except OSError:
        got = b""
    if not got:
        left, survivors = reap_stale(state)
        if not survivors:
            release(ledger, wpid)
        log_line(log, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "reason": "wrapper-killed", "stale": left,
                       "survivors": survivors})
    os._exit(0)


def acquire(lockfile, end, exclusive):
    fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o644)
    op = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    said = False
    while True:
        try:
            fcntl.flock(fd, op)
            return fd
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise
        if not said:
            try:
                holder = json.load(open(lockfile + ".holder"))
            except (OSError, ValueError):
                holder = {}
            print(f"capped_run: waiting up to {max(end - time.time(), 0):g}s for the run of pid "
                  f"{holder.get('pid', '?')} in {holder.get('cwd', '?')}: {holder.get('cmd', '?')}", file=sys.stderr)
            said = True
        if time.time() >= end:
            os.close(fd)
            return None
        time.sleep(0.2)


@contextlib.contextmanager
def ledger_lock(ledger):
    fd = os.open(ledger + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_ledger(ledger):
    try:
        with open(ledger) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def release(ledger, pid):
    with ledger_lock(ledger):
        held = read_ledger(ledger)
        if held.pop(str(pid), None) is not None:
            write_json(ledger, held)


def sweep(state_dir, ledger):
    stale, stuck = [], []
    for path in sorted(glob.glob(os.path.join(state_dir, "capped-run.tree*.json"))):
        try:
            with open(path) as f:
                owner = json.load(f)
        except (OSError, ValueError):
            continue
        if owner.get("pid") and same(owner["pid"], owner.get("wstart")):
            continue
        guard = None
        if not owner.get("pid"):
            guard = os.open(os.path.join(state_dir, "capped-run.lock"), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(guard, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                os.close(guard)
                continue
        try:
            left, survivors = reap_stale(path)
        finally:
            if guard is not None:
                os.close(guard)
        stale += left
        stuck += survivors
    held = read_ledger(ledger)
    gone = [k for k, e in held.items() if not same(int(k), e.get("start"))
            and not os.path.exists(os.path.join(state_dir, f"capped-run.tree.{k}.json"))]
    for k in gone:
        del held[k]
    if gone:
        write_json(ledger, held)
    return stale, stuck


def admit(state_dir, ledger, gb, budget, end, entry):
    gb, said, stale, me = min(gb, budget), False, [], str(os.getpid())
    with ledger_lock(ledger):
        held = read_ledger(ledger)
        held[me] = dict(entry, gb=gb, waiting=True, arrived=time.time())
        write_json(ledger, held)
    while True:
        with ledger_lock(ledger):
            left, stuck = sweep(state_dir, ledger)
            stale += left
            held = read_ledger(ledger)
            mine = held.get(me)
            if mine is None:
                mine = held[me] = dict(entry, gb=gb, waiting=True, arrived=time.time())
            used = sum(e["gb"] for e in held.values() if not e.get("waiting"))
            ahead = [k for k, e in held.items() if e.get("waiting") and k != me
                     and (e["arrived"], int(k)) < (mine["arrived"], int(me))]
            if stuck or (not ahead and used + gb <= budget + 1e-9):
                if stuck:
                    del held[me]
                else:
                    held[me] = dict(mine, waiting=False, since=time.time())
                write_json(ledger, held)
                return ("stuck" if stuck else "ok"), stale, stuck, gb
            if time.time() >= end:
                del held[me]
                write_json(ledger, held)
                return "timeout", stale, [], gb
        if not said:
            who = "; ".join(f"pid {k} {e['gb']:g} GB in {e.get('cwd', '?')}: {e.get('cmd', '?')}"
                            for k, e in held.items() if not e.get("waiting")) or "none"
            print(f"capped_run: waiting up to {max(end - time.time(), 0):g}s for {gb:g} GB of the {budget:g} GB "
                  f"budget ({len(ahead)} run(s) queued ahead; {used:g} GB reserved by {who})", file=sys.stderr)
            said = True
        time.sleep(0.2)


def ram_gb():
    if _libc is not None:
        v, n = ctypes.c_uint64(0), ctypes.c_size_t(8)
        if _libc.sysctlbyname(b"hw.memsize", ctypes.byref(v), ctypes.byref(n), None, 0) == 0 and v.value:
            return v.value / 1024 ** 3
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024 ** 3
    except (ValueError, OSError, AttributeError):
        return None


def outer_run(lockfile, ledger):
    try:
        pid = int(os.environ.get("CAPPED_RUN_HOLDER", ""))
    except ValueError:
        return None
    share = read_ledger(ledger).get(str(pid))
    if not share or share.get("waiting") or not same(pid, share.get("start")):
        try:
            with open(lockfile + ".holder") as f:
                if json.load(f).get("pid") != pid:
                    return None
        except (OSError, ValueError, AttributeError):
            return None
    rows, p, seen = table(), os.getppid(), set()
    while p > 1 and p not in seen:
        if p == pid:
            return pid
        seen.add(p)
        p = rows.get(p, (0,))[0]
    return None


def log_line(path, rec):
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        print(f"capped_run: could not write the log {path}: {e}", file=sys.stderr)


def env_float(name, default):
    return float(os.environ.get(name, default))


def main(argv=None):
    ap = argparse.ArgumentParser(description=USAGE.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mem-gb", type=float, default=env_float("CAPPED_RUN_MEM_GB", 6),
                    help="stop the tree when its summed phys_footprint passes this; also the share of "
                         "the budget this run reserves (default 6)")
    ap.add_argument("--budget-gb", type=float, default=env_float("CAPPED_RUN_BUDGET_GB", (ram_gb() or 16) * 0.75),
                    help="the most the caps of all running capped runs may add up to (default 0.75 x RAM)")
    ap.add_argument("--serial", action="store_true", default=os.environ.get("CAPPED_RUN_SERIAL", "0") not in ("", "0"),
                    help="one capped run on the machine at a time, the old rule (or CAPPED_RUN_SERIAL=1)")
    ap.add_argument("--timeout", type=float, default=env_float("CAPPED_RUN_TIMEOUT", 900),
                    help="stop the tree after this many seconds (default 900)")
    ap.add_argument("--wait", type=float, default=env_float("CAPPED_RUN_WAIT", 1800),
                    help="seconds to wait for budget or the lock (default 1800)")
    ap.add_argument("--min-free-pct", type=float, default=env_float("CAPPED_RUN_MIN_FREE_PCT", 8),
                    help="stop the tree when kern.memorystatus_level is at or under this (default 8)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("no command. Example: capped_run.py -- make test")
    poll = env_float("CAPPED_RUN_POLL", 0.5)
    state_dir = os.environ.get("CAPPED_RUN_STATE_DIR", os.path.join(HOME, ".claude", "state"))
    log = os.environ.get("CAPPED_RUN_LOG")
    if not log and os.path.isdir(METRICS_DIR):
        log = os.path.join(METRICS_DIR, "history", "capped-runs.jsonl")
    os.makedirs(state_dir, exist_ok=True)
    lockfile, ledger = os.path.join(state_dir, "capped-run.lock"), os.path.join(state_dir, "capped-run.ledger.json")
    state = os.path.join(state_dir, f"capped-run.tree.{os.getpid()}.json")
    shown = " ".join(cmd)
    me = usage(os.getpid())
    wstart = me[1] if me else None

    def giveup(reason, code, waited):
        print(f"CAPPED_RUN {reason} exit={code} peak_gb=0 secs={waited:.0f}", file=sys.stderr)
        log_line(log, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "cwd": os.getcwd(), "cmd": shown, "reason": reason,
                       "exit": code, "waited": round(waited, 1), "budget_gb": round(a.budget_gb, 2)})

    started = time.time()
    end = started + a.wait
    entry = {"start": wstart, "cwd": os.getcwd(), "cmd": shown}
    outer = outer_run(lockfile, ledger)
    if outer:
        print(f"capped_run: inside the capped run of pid {outer}, which holds the budget share and watches this "
              "tree. No queue; this run's caps still hold.", file=sys.stderr)
        status, stale, stuck, reserved = "inside", [], [], 0
    else:
        status, stale, stuck, reserved = admit(state_dir, ledger, a.budget_gb if a.serial else a.mem_gb,
                                               a.budget_gb, end, entry)
    if status == "timeout":
        giveup("lock-wait", 75, time.time() - started)
        return 75
    fd = None
    if status == "ok":
        fd = acquire(lockfile, end, a.serial)
        if fd is None:
            release(ledger, os.getpid())
            giveup("lock-wait", 75, time.time() - started)
            return 75
        if a.serial:
            write_json(lockfile + ".holder", {"pid": os.getpid(), "cwd": os.getcwd(), "cmd": shown,
                                              "since": time.time()})
    if stuck:
        print(f"STOP: could not stop pids {stuck} left by an earlier run, so this run does not start. Look with: "
              f"ps -o pid,ppid,stat,command -p {','.join(map(str, stuck))}", file=sys.stderr)
        print(f"CAPPED_RUN stale-survivors exit=70 peak_gb=0 secs={time.time() - started:.0f}", file=sys.stderr)
        log_line(log, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "cwd": os.getcwd(), "cmd": shown,
                       "reason": "stale-survivors", "exit": 70, "stale": stale, "survivors": stuck})
        return 70
    sys.stdout.flush(), sys.stderr.flush()
    w = None
    if not outer:
        r, w = os.pipe()
        if os.fork() == 0:
            guardian(r, w, state, log, ledger, os.getpid())
        os.close(r)

    caught = []
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, lambda n, _f: caught.append(n))

    t0 = time.time()
    child = subprocess.Popen(cmd, start_new_session=True,
                             env=dict(os.environ, CAPPED_RUN_HOLDER=str(outer or os.getpid())))
    tree, peak, reason, code = Tree(child.pid), 0, None, None
    cap = a.mem_gb * 1024 ** 3
    while True:
        fp = tree.poll()
        peak = max(peak, fp)
        if not outer:
            write_json(state, {"pid": os.getpid(), "wstart": wstart, "cmd": shown,
                               "procs": {str(p): s for p, s in tree.seen.items()}})
        rc = child.poll()
        if rc is not None:
            reason, code = "done", rc
            break
        free = free_pct()
        if caught:
            reason, code = "signal", 128 + caught[0]
        elif fp > cap:
            reason, code = "memory", 125
        elif free is not None and free <= a.min_free_pct:
            reason, code = "low-system-memory", 125
        elif time.time() - t0 > a.timeout:
            reason, code = "timeout", 124
        if reason:
            break
        time.sleep(poll)

    procs = tree.alive()
    leftovers = sorted(p for p in procs if p != child.pid)
    if reason == "memory":
        print(f"capped_run: the tree used {fp / 1024 ** 3:.1f} GB, over the cap of {a.mem_gb:g} GB. Stopping it.",
              file=sys.stderr)
    elif reason == "low-system-memory":
        print(f"capped_run: the machine has {free}% memory free (limit {a.min_free_pct:g}%). Stopping the tree.",
              file=sys.stderr)
    elif reason == "timeout":
        print(f"capped_run: no end after {a.timeout:g}s. Stopping the tree.", file=sys.stderr)
    elif reason == "done" and leftovers:
        print(f"capped_run: the command ended but left {len(leftovers)} process(es) running: {leftovers}. "
              "Stopping them.", file=sys.stderr)
    survivors = stop(procs, child.pid, child) if procs else []
    if survivors:
        print(f"STOP: could not stop pids {survivors}. Look with: ps -o pid,ppid,stat,command -p "
              f"{','.join(map(str, survivors))}", file=sys.stderr)
        code = 70
    elif not outer:
        try:
            os.remove(state)
        except OSError:
            pass
    secs = time.time() - t0
    print(f"CAPPED_RUN {reason} exit={code} peak_gb={peak / 1024 ** 3:.2f} secs={secs:.0f}", file=sys.stderr)
    log_line(log, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "cwd": os.getcwd(), "cmd": shown, "reason": reason,
                   "exit": code, "peak_gb": round(peak / 1024 ** 3, 3), "secs": round(secs, 1),
                   "cap_gb": a.mem_gb, "leftovers": len(leftovers), "survivors": survivors, "stale": stale,
                   "waited": round(t0 - started, 1), "reserved_gb": round(reserved, 2),
                   "budget_gb": round(a.budget_gb, 2), "serial": a.serial, "inside": outer})
    if outer:
        return code
    if not survivors:
        release(ledger, os.getpid())
    os.write(w, b"d")
    os.close(w)
    os.close(fd)
    return code


if __name__ == "__main__":
    sys.exit(main())
