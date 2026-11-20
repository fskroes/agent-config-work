"""Read the git config keys of the agent setup (mode, branchPrefix, selfMerge, home, ...).

Claude Code, GitHub Copilot CLI and Codex all read these keys, so their name is agent.<key>.
The old name claude.<key> still works: it is read when agent.<key> is not set. Write only
agent.<key>. The bash twin is claude/scripts/agent-config.sh; both follow the same rules.

    get(key, repo=None, *flags)   agent.<key>, else claude.<key>, else None
    both(key, repo=None, *flags)  (agent value, claude value), each None when not set
    pick(cfg, key)                the same choice as get, over a dict that git config
                                  --get-regexp filled (keys in lower case)

flags go to `git config` as they are, for example "--local", "--no-includes" or "--bool".
get falls back to claude.<key> only when agent.<key> is not set (git exit 1), so an
agent.<key> that git refuses (a bad --bool value) never lets the old name through.

This file lives next to guard.py because the work export ships hooks/ (install.sh,
export_work): guard.py on the work laptop imports it from its own folder.
"""
import subprocess

PREFIXES = ("agent", "claude")   # the new name first: it wins when both are set


def _git_get(name, repo, flags):
    """(exit code, value) of one `git config --get`; a git that does not start is code 1."""
    argv = ["git"] + (["-C", repo] if repo else []) + ["config"] + list(flags) + ["--get", name]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return 1, None
    return r.returncode, r.stdout.rstrip("\n") if r.returncode == 0 else None


def get(key, repo=None, *flags):
    rc, v = _git_get("agent." + key, repo, flags)
    if rc == 1:
        rc, v = _git_get("claude." + key, repo, flags)
    return v if rc == 0 else None


def both(key, repo=None, *flags):
    return tuple(_git_get(p + "." + key, repo, flags)[1] for p in PREFIXES)


def pick(cfg, key):
    """agent.<key>, else claude.<key>, from a dict with lower-case keys; None when neither."""
    k = key.lower()
    for p in PREFIXES:
        if p + "." + k in cfg:
            return cfg[p + "." + k]
    return None
