from dataclasses import dataclass
from typing import Tuple

SAFE = ("owned", "not_open")
SESSION_SAFE = SAFE + ("session_idle", "session_cwd_idle", "no_nested_git")
WORKTREE_SAFE = ("owned", "not_open", "session_cwd_idle", "worktree_linked", "worktree_unlocked", "worktree_clean")


@dataclass(frozen=True)
class Rule:
    id: str
    group: str
    writer: str
    roots: Tuple[str, ...]
    names: Tuple[str, ...]
    ttl_days: float
    checks: Tuple[str, ...] = SAFE
    depth: int = 1
    kinds: Tuple[str, ...] = ("dir",)
    age: str = "deep"
    remover: str = "tree"
    exclude: Tuple[str, ...] = ()


GENERIC_RULES = (
    Rule("tmp-python", "tmp", "python tempfile and mktemp", ("{tmpdir}",),
         ("tmp*", "TemporaryDirectory.*"), 2, kinds=("dir", "file")),
    Rule("tmp-verify", "tmp", "verify and gate runs of every repo", ("{tmpdir}",),
         ("verify-*", "verify_*", "*-verify-*", "*-verify", "*-gate-*"), 1),
    Rule("tmp-chrome", "tmp", "chrome profile and url fetcher folders",
         ("{tmpdir}",), ("com.google.Chrome.*", ".com.google.Chrome.*"), 2, kinds=("dir", "file")),
    Rule("chrome-clone", "chrome", "chrome app clones, 1.4 GB each, made at every launch",
         ("{tmpdir}/../X/com.google.Chrome.code_sign_clone",), ("code_sign_clone.*",), 0.25),
    Rule("scratchpad", "scratch", "claude session scratchpads", ("{systmp}/claude-{uid}/*",),
         ("????????-????-????-????-????????????",), 3, SESSION_SAFE),
    Rule("t3-scratch", "scratch", "t3 thread folders", ("{home}/.t3/scratch",), ("*",), 14,
         SESSION_SAFE),
    Rule("worktree-agent", "worktrees", "claude agent worktrees inside a repo",
         ("{home}/dev/*/.claude/worktrees", "{repo}/.claude/worktrees"), ("*",), 7, WORKTREE_SAFE, age="worktree",
         remover="worktree"),
    Rule("worktree-session", "worktrees", "claude session worktrees", ("{home}/.claude-worktrees",),
         ("*",), 7, WORKTREE_SAFE, depth=2, age="worktree", remover="worktree"),
    Rule("worktree-t3", "worktrees", "t3 thread worktrees", ("{home}/.t3/worktrees",), ("*",), 7,
         WORKTREE_SAFE, depth=2, age="worktree", remover="worktree"),
    Rule("volume-scratch", "scratch", "claude-scratch on the data volume",
         ("{volumes}/Data/claude-scratch",), ("*",), 7, SESSION_SAFE),
)


try:
    from reaper_policy_projects import project_rules
except ImportError:
    def project_rules(_rule):
        return ()

RULES = GENERIC_RULES[:2] + tuple(project_rules(Rule)) + GENERIC_RULES[2:]
