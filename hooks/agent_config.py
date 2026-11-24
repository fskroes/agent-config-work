import subprocess

PREFIXES = ("agent", "claude")


def _git_get(name, repo, flags):
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
    k = key.lower()
    for p in PREFIXES:
        if p + "." + k in cfg:
            return cfg[p + "." + k]
    return None
