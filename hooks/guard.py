#!/usr/bin/env python3
"""PreToolUse guard: the CLAUDE.md rules that a machine can check.

Reads the hook payload on stdin. Prints a deny or ask decision as JSON, or
nothing to allow. Any internal error allows the call: a broken guard must never
lock every session out of its tools. Each deny and ask also adds one line to
DECISION_LOG (rule id, decision, tool, agent, repo; never the command text).

`--format copilot` prints the decision the way GitHub Copilot CLI reads it: the
same fields at the top level, not inside Claude Code's `hookSpecificOutput`.
Copilot sends the Claude-style payload (`tool_name`, `tool_input`) when the hook
is registered under the PascalCase event `PreToolUse` (copilot/hooks/guard.json).
It sends no `transcript_path`: copilot_payload() adds the session's events.jsonl,
so the rules that read the transcript (triage, fresh-session) hold there too.
A Copilot sub-agent call carries `agent_id`, like Claude Code's.

Rules (each one names the CLAUDE.md section it enforces):
  shared-checkout   Edit/Write in the shared checkout of a team-mode repo   ("Branching")
  home-child        Edit/Write/MultiEdit/NotebookEdit in a child repo, by a session that runs in a Home Repo,
                    outside the worktrees that session made with worktree-child.sh. Files outside a git
                    work tree and a Copilot sub-agent call are not gated   ("Repos", root CLAUDE.md)
  changes-log       Edit/Write to agent-config's changes.md in a shared checkout, also through ~/.claude,
                    or Bash that names it other than to read it: log-change.sh does it ("Tests and evals")
  branch-in-shared  git switch/checkout in the shared checkout of a team repo ("Branching")
  no-verify         --no-verify on commit/push/merge/rebase, or git commit -n ("Safety")
  force-push        force push without both lease flags, or to main/master    ("Safety")
  push-main         plain push to main/master in a team-mode repo             ("Branching")
  merge-own         gh pr merge in a team repo -> ask; denied where agent.selfMerge sends it
                    through ~/.claude/scripts/self-merge.sh                    ("Branching")
  self-merge-config git config writes to the self-merge keys, or an edit of a git config
                    file: Fernando sets what may merge without a human merge ("Branching")
  destructive       git reset --hard, git clean -f, rm -rf on / or ~  -> ask  ("Safety")
  empty-var-path    rm/rsync/cp/mv on a path that starts with $VAR/ when VAR may be empty -> ask ("Safety")
  em-dash / phrase  em dash or a banned phrase in a commit, PR or issue text  ("How Fernando wants to be talked to")
  co-author         a Copilot co-author line in a commit message or PR
  unbounded-test    swift test/build, xcodebuild build/test or a make test target that does not go
                    through ~/.claude/scripts/capped_run.py                   ("Tests and evals")
  triage            the main session changes a file (Edit, Write, MultiEdit, NotebookEdit) before it ever
                    printed the triage block with ~/.claude/scripts/triage.py or as reply text; sub-agents
                    are never gated                                           ("Task sizing")
  subagent-wait     a sub-agent Bash wait (until/while loop, sleep, wait, tail -f) in the foreground with a
                    timeout over 270 s (on Copilot: not mode async, initial_wait over 270). Sub-agent requests
                    use the 5 minute prompt cache, so a longer wait makes the next request write the whole
                    context again                                             ("large-task", step 1)
  secret-read       Bash that names a claude-secret Keychain item (service or account) or runs `security
                    dump-keychain`: a secret reaches a command only through `secret.py run`, so its value never
                    enters the transcript                                                   ("Safety")
  secret-stage      git add that would stage a credential file (.secret, .env, secrets.env) at any depth:
                    the file is not in .gitignore. Judged by `git add --dry-run` with the same arguments; an
                    argument the shell still expands ($F, $(...)) is judged as -A over the whole repo  ("Safety")
  fresh-session     the main session runs worktree-next.sh while its context is over 150k tokens: the next
                    task starts in a new session                              ("Branching", second task)
  history-comment   a new code comment that quotes a chat instruction ("Fernando said never X", "as you asked",
                    "per the user's request") in an Edit, MultiEdit or Write of a code file. Comments that
                    record incidents (dates, metrics, "Fernando sets X") pass; .md and .txt are not read
                    ("Scope", "Tests and evals")
  new-doc           a Write that creates a .md file in a git work tree, outside the session scratchpad,
                    ~/.claude, repo skill folders (.claude/skills/<name>/) and the repos/ and docs/ folders of a
                    Home Repo (git config agent.home true), in a turn whose user prompt
                    names no Markdown, doc or README                          ("How to work")
"""
import glob, json, os, posixpath, re, shlex, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import agent_config   # agent.<key>, else the old name claude.<key>; ships next to this file

HOME = os.path.expanduser("~")
# Edits that go through these dirs are the designed live-edit path of agent-config
# (they are symlinks into the repo). Blocking them would block editing a skill.
# Under ~/.copilot only skills/ and hooks/ hold links into the repo (install.sh --profile work).
LIVE_EDIT_ROOTS = tuple(os.path.join(HOME, d) + os.sep for d in
                        (".claude", ".codex", os.path.join(".copilot", "skills"), os.path.join(".copilot", "hooks")))
BANNED_PHRASES = ("here's the kicker", "here's the thing", "plot twist", "let me break this down",
                  "the bottom line", "make no mistake")
MAIN = ("main", "master")
# The keys that decide what an agent may merge without Fernando (claude/scripts/self-merge.sh), and
# verify, which turns off the verify nudge (claude/hooks/verify_nudge.py) for a repo, and home, which
# makes a repo a Home Repo (rules new-doc and home-child). Denied under the name agent.<key> and under the
# old name claude.<key>, which the scripts still read (agent_config.py): a write of either is a way in.
SELF_MERGE_KEYS = re.compile(r"(agent|claude)(\.(selfmerge|selfmergeexclude|selfmergemethod|verifycmd|testcmd|verify|home))?",
                             re.I)
SELF_MERGE_CONFIG = ("self-merge-config: Fernando sets agent.selfMerge, verifyCmd, testCmd, selfMergeExclude, "
                     "selfMergeMethod, verify and home, never an agent. They decide what merges without Fernando, "
                     "which repos need a verify skill and which repo is the Home Repo. The old names claude.<key> count too. Give Fernando the git config command to run with the ! prefix.")
GH_VALUE_FLAGS = ("-R", "--repo", "--hostname")
# ~/.claude/agent-metrics/changes.md links into the shared checkout of agent-config. A row
# written there is never committed and blocks the next pull.
CHANGES_LOG = os.sep + os.path.join("claude", "agent-metrics", "changes.md")
CHANGES_LOG_MSG = ("changes-log: changes.md is in the shared checkout of agent-config. A row written here is never "
                   "committed and blocks the next pull. Log it with: bash ~/.claude/scripts/log-change.sh "
                   "'<item>' '<change>' '<metric you expect to move>'. Rows already appended by hand: "
                   "bash ~/.claude/scripts/log-change.sh --adopt")
# Bash that names that file passes only when it reads: a redirect into it, or any other command, is denied.
# Readers take no output operand (uniq and xxd -r do, so they are not here; gh is, unless -O/--output).
LOG_READERS = {"cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "wc", "diff", "cmp", "ls",
               "stat", "file", "readlink", "realpath", "shasum", "md5", "nl", "od", "cut", "column",
               "test", "[", "[[", "bat", "jq", "open", "echo", "printf", "basename", "dirname"}
LOG_GIT_READERS = {"diff", "show", "log", "blame", "status", "ls-files", "grep"}
LOG_COPIERS = {"cp", "rsync", "ditto", "install"}   # read when the file is a source, write when it is the target
LOG_INTERPRETERS = {"python", "python3", "perl", "ruby", "node", "osascript", "php"}
LOG_PROGRAMS = {"awk", "gawk", "sed"}   # a path inside their program text is a write: print > "f", w f
HEREDOC_KEEP_LINE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1([^\n]*)\n(.*?)\n\s*\2\s*(?=\n|$)", re.S)
# Test and build runs that can take many GB: two of them at once can crash a laptop. They go through capped_run.py.
# The rule holds only where the script is installed: guard.py updates on `git pull` (it is a link into the
# clone), capped_run.py only after install.sh links it (both profiles, also --profile work).
CAPPED_RUN = "capped_run.py"
CAPPED_RUN_PATH = os.environ.get("GUARD_CAPPED_RUN", os.path.join(HOME, ".claude", "scripts", CAPPED_RUN))
# Every deny and ask, one JSON line each (log_decision). Read by agent-metrics/pyramid_metrics.py.
# The default log is written only where install.sh made ~/.claude/agent-metrics: on a Copilot-only
# machine (install.sh --profile work) the guard must never create ~/.claude.
METRICS_DIR = os.path.join(HOME, ".claude", "agent-metrics")
DECISION_LOG = os.environ.get("GUARD_LOG") or os.path.join(METRICS_DIR, "history", "guard.jsonl")
XCODEBUILD_HEAVY = {"build", "test", "build-for-testing", "test-without-building", "archive", "analyze"}
# A make target is heavy when one of its words (split on - _ : . /) is in MAKE_HEAVY, or it is `check`:
# test-fast and e2e-ui are, typecheck, lint-check and checkout are not.
MAKE_HEAVY = {"test", "tests", "xctest", "bench", "benchmark", "benchmarks", "e2e"}
# Commands that run their arguments as a command. Their own options are skipped; the value options listed.
RUNNERS = {"timeout": {"-s", "-k", "--signal", "--kill-after"}, "gtimeout": {"-s", "-k", "--signal", "--kill-after"},
           "xcrun": {"--sdk", "--toolchain"}, "caffeinate": {"-t", "-w"}, "arch": set(), "nice": {"-n"},
           "nohup": set(), "time": set(), "env": {"-u", "-C", "-S"}, "command": set(), "exec": set()}
MAKE_VALUE_FLAGS = {"-C", "-f", "-j", "-l", "-o", "-W", "-I", "--file", "--directory", "--makefile"}
UNBOUNDED_MSG = ("unbounded-test: run heavy tests and builds through the memory and time cap, inside the machine memory budget: "
                 "python3 ~/.claude/scripts/capped_run.py -- {cmd}. Options: --mem-gb (6), --timeout (900 s), --serial. "
                 "Two parallel runs without the cap can use all the memory of the machine."
                 )
# secret.py ask saves what Fernando typed on a phone as login Keychain items; service and account both start "claude-secret".
# `security find-generic-password -w/-g` must name one of the two, and `dump-keychain -d` prints every item: that is how a
# value would reach the transcript. SECRET_KEYCHAIN points secret.py at another keychain (tests only).
SECRET_REF = re.compile(r"claude-secret|\bSECRET_KEYCHAIN\b|\bdump-keychain\b")
SECRET_MSG = ("secret-read: the claude-secret Keychain items hold values Fernando typed on a phone; they must never reach the "
              "transcript. Use: python3 ~/.claude/scripts/secret.py run NAME -- <command> (NAME becomes an env var, "
              "output is redacted), or secret.py list for the names. To get a new one: secret.py ask NAME --why '...'")
# A credential file that git would stage. The name, never the value, reaches the message.
SECRET_FILES = {".secret", ".env", "secrets.env"}
# git add flags that stage only tracked files, ask per hunk, or stage nothing: no dry run needed.
ADD_SKIP = re.compile(r"-[a-zA-Z]*[upien][a-zA-Z]*|--(update|patch|interactive|edit|dry-run|refresh)")
STAGE_MSG = ("secret-stage: this git add would stage {files}. A credential file never enters git: add its name to "
             ".gitignore in this change (check with git check-ignore -v <file>), then stage again. Rule: AGENTS.md 'Safety'.")
STAGE_DYNAMIC_MSG = ("secret-stage: this git add names a shell expansion ($VAR, $(...) or backticks) that the hook cannot "
                     "read, and git would stage {files} in this repo. Add those names to .gitignore in this change, or "
                     "name the paths literally. Rule: AGENTS.md 'Safety'.")
# An argument the shell still has to expand: the hook sees `$F`, git would see the file.
DYNAMIC_ARG = re.compile(r"[$`]")


def staged_secrets(d, rest, backtick=False):
    """(credential files, dynamic) for `git add <rest>` run in d. Asks git itself with --dry-run, so every pathspec
    form (., -A, :/, a folder, a glob, -f past .gitignore) is judged the way git judges it. When an argument is a
    shell expansion the path is unknown, so the dry run covers the whole repo (-A, and -f when given): dynamic is
    True and any credential file git could stage there counts. The parser lifts a backtick substitution out of
    the command, so `backtick` (the command holds one) counts as an expansion too.
    Fails open: no repo, a timeout or a git error gives []."""
    flags = [a for a in rest if a.startswith("-") and a != "--"]
    if any(ADD_SKIP.fullmatch(a) for a in flags):
        return [], False
    dynamic = backtick or any(DYNAMIC_ARG.search(a) for a in rest)
    if dynamic:
        force = any(a in ("-f", "--force") or re.fullmatch(r"-[a-zA-Z]*f[a-zA-Z]*", a) for a in flags)
        rest = ["-A"] + (["-f"] if force else [])
    try:
        r = subprocess.run(["git", "-C", d, "add", "--dry-run", "--ignore-missing", *rest],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return [], dynamic
    # One line per path, `add 'path'`, also when the exit code is 1 because an ignored path was named.
    paths = re.findall(r"^add '(.*)'$", r.stdout, re.M)
    return sorted(p for p in paths if posixpath.basename(p) in SECRET_FILES), dynamic


BACKTICK_MARK = "GUARDBACKTICKMARK"


def add_has_backtick(body):
    """Does a git add or git stage segment hold a backtick substitution in its own arguments? raw_segments turns a
    backtick into a separator, so the substitution leaves the segment; a marker word stays in it instead. A backtick
    in another segment (a commit message, an echo) does not count."""
    try:
        for toks in segments(body.replace("`", f" {BACKTICK_MARK} ")):
            sub, rest, _ = strip_git(toks)
            if sub in ("add", "stage") and any(BACKTICK_MARK in a for a in rest):
                return True
    except Exception:   # a parse failure must not make an ordinary git add look dynamic
        return False
    return False


GLUED_REDIRECT = re.compile(r"(?:&>>?|[0-9]*>>?\|?)([^>]+)$")   # printf x>>file: one token, no space


def git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


REPO_STATE = {}


def repo_state(path):
    """Return (in_repo, solo, shared_checkout) for the directory holding path."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    if not d:
        return False, False, False
    if d not in REPO_STATE:   # one hook process asks about one directory several times (check_edit, check_triage)
        if git(d, "rev-parse", "--is-inside-work-tree") != "true":
            REPO_STATE[d] = (False, False, False)
        else:
            gd = git(d, "rev-parse", "--path-format=absolute", "--git-dir")
            cd = git(d, "rev-parse", "--path-format=absolute", "--git-common-dir")
            REPO_STATE[d] = (True, agent_config.get("mode", d) == "solo", bool(gd) and gd == cd)
    return REPO_STATE[d]


def is_home_repo(path):
    """The repo that holds path is a Home Repo: `git config agent.home true`, which Fernando sets. A Home Repo holds
    the repo map (repos/) and the shared docs (docs/) of many repos; worktrees of it share the setting."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    return bool(d) and agent_config.get("home", d, "--bool") == "true"


def changes_log_in_shared(path):
    """path, links followed, is agent-config's changes.md in the shared checkout of a team-mode clone."""
    real = os.path.realpath(path)
    if not real.endswith(CHANGES_LOG):
        return False
    in_repo, solo, shared = repo_state(real)
    return in_repo and shared and not solo


def changes_log_bash(cmd, cwd, depth=0):
    """True when a Bash command writes agent-config's changes.md in a shared checkout, or names it in a
    command that is not a known reader. Follows cd and git -C, expands ~, $VARS and globs, unwraps bash -c
    and eval. Not seen: a path built while the command runs (f=changes.md; echo x >> $f, or a string join
    in code) and GNU cp -t. log-change.sh --adopt moves a row that gets past into a PR."""
    if ("changes" not in cmd and "agent-metrics" not in cmd) or depth > 3:
        return False

    def is_log(p, base):
        if not p or any(c.isspace() for c in p):   # text with spaces is a message, not a path
            return False
        full = os.path.join(base, os.path.expanduser(os.path.expandvars(p)))
        return any(changes_log_in_shared(c) for c in (glob.glob(full) if glob.has_magic(full) else [full]))

    body = HEREDOC_KEEP_LINE.sub(lambda m: " " + m.group(3) + "\n", cmd)   # drop the body, keep `>> file` after <<EOF
    body = re.sub(r"[0-9]*[<>]&(?:[0-9]+-?|-)", " ", body)   # 2>&1, >&-: fd copies, and & would split the segment
    body = re.sub(r"&>|>\||>&", ">", body)   # &>, &>>, >| and >& file all write the file like > and >>
    for toks in segments(body, depth):
        if toks[0] == "cd" and len(toks) > 1:
            cwd = os.path.normpath(os.path.join(cwd, os.path.expanduser(os.path.expandvars(toks[1]))))
            continue
        sub, rest, cdir = strip_git(toks) if os.path.basename(toks[0]) == "git" else (None, [], None)
        base = os.path.join(cwd, os.path.expanduser(cdir)) if cdir else cwd
        head, args, i = None, [], 0
        while i < len(toks):   # redirects can stand anywhere, the first (&> splits a segment before it)
            t = toks[i]
            r = REDIRECT.fullmatch(t)
            if r:
                target = r.group(1) or (toks[i + 1] if i + 1 < len(toks) else "")
                i += 1 if r.group(1) else 2
                if ">" in t and is_log(target, cwd):
                    return True
                continue
            g = GLUED_REDIRECT.search(t) if ">" in t and not any(c.isspace() for c in t) else None
            if g and is_log(g.group(1), cwd):
                return True
            if head is None:
                head = os.path.basename(t)
            else:
                args.append(t)
            i += 1
        if head is None:
            continue
        named = [n for n, a in enumerate(args) if is_log(a.split("=", 1)[-1], base)]
        in_text = [n for n, a in enumerate(args) if n not in named
                   and any(is_log(p, base) for p in re.findall(r"[^\s'\"(),;{}<>]+", a))]
        if in_text and (head in LOG_INTERPRETERS or head in LOG_PROGRAMS):   # code that names the file writes it
            return True
        if not named:
            continue
        if head in LOG_READERS or (head == "gh" and not any(a in ("-O", "--output") or a.startswith("--output=")
                                                                for a in args)):
            continue
        if head == "git" and sub in LOG_GIT_READERS:
            continue
        if head == "sed" and not any(re.match(r"-[a-zA-Z]*i", a) or a.startswith("--in-place") for a in args):
            continue
        if head in ("awk", "gawk") and "-i" not in args and "inplace" not in args:
            continue
        if head == "sort" and not any(a.startswith(("-o", "--output")) for a in args):
            continue
        last = max((n for n, a in enumerate(args) if not a.startswith("-")), default=-1)
        if head in LOG_COPIERS and last not in named:
            continue
        return True
    return False


def decide(kind, reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": kind,
                                   "permissionDecisionReason": f"[guard] {reason}"}}


PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)


def patch_paths(ti):
    """Files named in an apply_patch body (Copilot maps apply_patch to Edit). The
    field that holds the patch text is not documented, so every string that holds
    a patch body is read. Only called when the payload names no file at all, so an
    edit whose text quotes a patch header is never read as one."""
    texts = [v for v in ti.values() if isinstance(v, str) and "*** Begin Patch" in v]
    return [a or b for t in texts for a, b in PATCH_FILE.findall(t)]


def nearest_dir(path):
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    return d


def check_home_child(path, p, copilot=False):
    """A session that runs in a Home Repo writes in another repo only in a worktree that worktree-child.sh made
    for it. The record is <home git common dir>/claude-child-worktrees/<session id>, one real path per line.
    No session id, a Copilot sub-agent call (it sends its own id, which has no record) and any error allow."""
    try:
        cwd, sid = p.get("cwd"), p.get("session_id")
        if not path or not cwd or not isinstance(sid, str) or not sid or (copilot and p.get("agent_id")):
            return None
        absp = os.path.abspath(os.path.join(cwd, os.path.expanduser(path)))
        if absp.startswith(LIVE_EDIT_ROOTS):
            return None
        home_dir = nearest_dir(cwd)
        if not home_dir or not is_home_repo(home_dir):
            return None
        d = nearest_dir(absp)
        top = d and git(d, "rev-parse", "--show-toplevel")
        if not top:
            return None
        home_common = git(home_dir, "rev-parse", "--path-format=absolute", "--git-common-dir")
        there = git(d, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if not home_common or not there or os.path.realpath(home_common) == os.path.realpath(there):
            return None
        home_common = os.path.realpath(home_common)
        rec = os.path.join(home_common, "claude-child-worktrees", re.sub(rb"[^A-Za-z0-9_-]", b"_", str(sid).encode()).decode())
        try:
            with open(rec) as f:
                recorded = {os.path.realpath(x.strip()) for x in f if x.strip()}
        except OSError:
            recorded = set()
        if os.path.realpath(top) in recorded:
            return None
        return decide("deny", f"home-child: {absp} is in another repo ({os.path.realpath(top)}) and this Home session "
                              "has no worktree there. Run: bash ~/.claude/scripts/worktree-child.sh <repo> <slug>, "
                              "then edit through the path it prints.")
    except Exception:
        return None


def check_edit(path, cwd=None):
    if not path:
        return None
    path = os.path.expanduser(path)
    if cwd and not os.path.isabs(path):   # relative to the session, not to this process
        path = os.path.join(cwd, path)
    absp = os.path.abspath(path)
    gitdir = os.path.dirname(absp)   # <repo>/.git/config, or <repo>.git/config in a bare repo
    if (os.path.basename(absp) == "config" and os.path.isfile(os.path.join(gitdir, "HEAD"))
            and os.path.isdir(os.path.join(gitdir, "objects"))):
        return decide("deny", SELF_MERGE_CONFIG)
    if changes_log_in_shared(absp):   # before the live-edit pass: ~/.claude/agent-metrics/changes.md is a link
        return decide("deny", CHANGES_LOG_MSG)
    if SECRET_REF.search(absp):
        return decide("deny", SECRET_MSG)
    if absp.startswith(LIVE_EDIT_ROOTS):
        return None
    in_repo, solo, shared = repo_state(absp)
    if in_repo and shared and not solo:
        return decide("deny", f"shared-checkout: {absp} is in the shared checkout of a team-mode repo. "
                              "Run the setup block in CLAUDE.md 'Branching' and edit in your worktree. "
                              "Already changed files here? git stash -u, set up, git stash pop in the worktree.")
    return None


HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n(.*?)\n\s*\2\s*(?=\n|$)", re.S)
WRAPPERS = {"env", "command", "builtin", "exec", "sudo", "time", "nohup", "nice", "xargs",
            "if", "then", "else", "elif", "do", "while", "until", "{", "}", "!"}
SHELLS = {"bash", "sh", "zsh"}
PATH_CMDS = {"rm", "rsync", "cp", "mv"}
NEVER_EMPTY = {"HOME": "\x04", "PWD": "\x04", "OLDPWD": "\x04", "TMPDIR": "\x04", "USER": "\x04"}   # OPAQUE
NAME = r"[A-Za-z_][A-Za-z0-9_]*"
NAME_VALUE = re.compile(rf"({NAME})\+?=(.*)", re.S)
VAR = re.compile(rf"\$(?:({NAME}|[0-9]+|[@*])|\{{({NAME}|[0-9]+|[@*])([^}}]*)\}})")
CLEARS = ("unset", "read", "local", "typeset", "declare", "readonly", "export", "mapfile", "readarray")
OPENERS, CLOSERS = {"if", "while", "until", "for", "case", "select"}, {"fi", "done", "esac"}
KEYWORDS = ("then", "do", "else", "elif", "{", "}", "!")
RUNS_TEXT = re.compile(r"(?:\b(?:bash|sh|zsh)\b(?:[ \t]+\S+)*?[ \t]+-[a-zA-Z]*c[a-zA-Z]*[ \t]+|\beval\b[^;&|\n]*)\Z")
SHELL_HEREDOC = re.compile(r"\b(?:bash|sh|zsh)\b[^;&|\n]*<<|<<[^\n]*\|\s*(?:sudo\s+)?(?:bash|sh|zsh)\b")   # bash <<SH, cat <<SH | bash
REDIRECT = re.compile(r"[0-9]*(?:[<>]{1,2}|&>>?|>&)(\S*)")
MORE_WRAPPERS = {"timeout", "setsid", "stdbuf", "ionice", "caffeinate"}
VALUE_OPTS = {   # wrapper -> its options that take a value (sudo -n is a flag: it must not swallow the command)
    "sudo": ("-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"), "env": ("-u", "-C", "-S", "-P"),
    "xargs": ("-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"), "nice": ("-n",), "exec": ("-a",),
    "timeout": ("-s", "-k"), "stdbuf": ("-i", "-o", "-e"), "ionice": ("-c", "-n", "-p"), "caffeinate": ("-t", "-w")}
CHILD = "\x03"   # a $ in the single-quoted text of bash -c '...' or eval '...': it expands later, in that shell
SUBSTITUTION = "${__SUBSTITUTION_%d__}"   # a variable that is never set; the number finds the command inside
EMPTY = "${__EMPTY__}"   # expand(): a variable that may be empty
OPAQUE = "\x04"   # expand(): a variable that is set to something not empty, with no path at / in it (HOME, ${X:?})
TEMP_DIRS = ("tmp", "private/tmp", "var/tmp", "var/folders", "private/var/folders")


HASH, SLASH, DASH, SEMI, MARKUP = "#", "//", "--", ";", "<!--"
COMMENT_MARKS = {   # file extension -> its line comment marker; c-style files also have /* */ and * lines
    **dict.fromkeys(("py", "sh", "bash", "zsh", "toml", "yaml", "yml", "rb", "pl", "conf", "cfg", "ini", "r", "nix"), HASH),
    **dict.fromkeys(("js", "mjs", "cjs", "jsx", "ts", "tsx", "swift", "c", "h", "cc", "cpp", "hpp", "m", "mm", "java",
                     "kt", "go", "rs", "cs", "php", "css", "scss", "less", "jsonc", "json5"), SLASH),
    **dict.fromkeys(("sql", "lua", "hs"), DASH), **dict.fromkeys(("html", "htm", "xml", "svg", "vue"), MARKUP),
    "el": SEMI, "lisp": SEMI, "asm": SEMI}
COMMENT_MARKS_BY_NAME = {"Makefile": HASH, "Dockerfile": HASH, "Brewfile": HASH, "Gemfile": HASH}
_NAMED = r"(?:fernando|the reviewer)"   # a person who steers this repo: any verb of asking counts
_TOLD = r"(?:said|asked|requested|told|instructed|insisted|wants?|wanted|prefers?|preferred)"
_ADVERB = r"(?:'s| has| had| have| just| also| explicitly| specifically| originally| previously)*"
DIRECTIVE = re.compile(r"\b(?:never|always|must|not|don't|do not|no longer|stop|only)\b")
HISTORY_PATTERNS = (   # lowercase text. "the user" and "you" alone are an app's own user or a reader: they need a chat shape
    re.compile(rf"\b{_NAMED}{_ADVERB}\s+{_TOLD}\b"),
    re.compile(rf"\b{_NAMED}\s+(?:does not|doesn't|did not|didn't|do not|don't)\s+want\b"),
    re.compile(rf"\bper {_NAMED}\b"),
    re.compile(rf"\brequested by {_NAMED}\b"),
    re.compile(rf"\b{_NAMED}:\s"),   # only with a directive word, see history_hit
    re.compile(rf"\b{_NAMED}'s\s+(?:request|instruction|instructions)\b"),
    re.compile(rf"\b(?:per|at) {_NAMED}'s\s+(?:request|instruction|instructions|ask|feedback|review|comment|direction)\b"),
    re.compile(r"\bas (?:you|fernando|the reviewer)\s+(?:requested|asked|instructed|said|wanted)\b"),
    re.compile(r"\bas (?:requested|asked|instructed)(?:\s+by\b|\s*$|\s*[,.;:)\]]|\s+(?:in|on|during|earlier|before|above)\b)"),
    re.compile(r"\b(?:per|at) (?:your|the user's|user's|the human's)\s+"
               r"(?:request|instruction|instructions|ask|feedback|review|comment|direction)\b"),
    re.compile(r"\byou (?:told|asked|said to) (?:me|us)\b"),
    re.compile(r"\bthe user (?:asked|requested|told) (?:me|us)\s+to\b"),
    re.compile(r"\bthe user (?:said|asked) (?:never|always)\b"),
)
HISTORY_MSG = ("history-comment: line {n} of {name} quotes a chat instruction ('{line}'). A comment states the rule or "
               "the reason as a fact about the code (what breaks, what it protects), not who asked for it. Rewrite it "
               "that way, or drop it, and put the history in the commit message. Incidents and metrics "
               "stay fine.")


def comment_lines(text, mark):
    """(line number, comment text) of every comment in text: whole-line comments, and a trailing `  # x` or ` // x`
    after code. Block comments (/* */, <!-- -->) are followed across lines. Strings are not parsed, so a marker
    inside a string literal after code can read as a trailing comment: a known cost of a cheap scan."""
    out, block = [], None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if block:
            end = line.find(block)
            out.append((n, line[:end] if end >= 0 else line))
            block = None if end >= 0 else block
            continue
        if mark == MARKUP:
            if line.startswith("<!--"):
                end = line.find("-->")
                out.append((n, line[4:end] if end >= 0 else line[4:]))
                block = None if end >= 0 else "-->"
            continue
        if mark == SLASH:
            if line.startswith("/*"):
                end = line.find("*/", 2)
                out.append((n, line[2:end] if end >= 0 else line[2:]))
                block = None if end >= 0 else "*/"
                continue
            if line.startswith("*") and not line.startswith("*/"):   # block comment continuation
                out.append((n, line.lstrip("* ")))
                continue
        if line.startswith(mark):
            if not line.startswith("#!"):
                out.append((n, line[len(mark):]))
            continue
        t = re.search(rf"\s{re.escape(mark)}\s(.*)", raw)   # trailing comment: the marker set off by spaces
        before = raw[:t.start()] if t else ""
        if t and mark != SEMI and "://" not in raw[:t.start() + 3] and before.count('"') % 2 == 0 and before.count("'") % 2 == 0:
            out.append((n, t.group(1)))
    return out


def history_hit(comment):
    """The matching text when the comment attributes an instruction to a person in the conversation."""
    low = comment.lower().replace("’", "'")
    for pat in HISTORY_PATTERNS:
        m = pat.search(low)
        if not m:
            continue
        if re.search(r":\s$", m.group(0)):
            if not DIRECTIVE.search(low):
                continue   # a name and a colon alone is a label; with a directive word it is an instruction
        return m.group(0)
    return None


def in_work_tree(path):
    """The file's directory or a parent holds .git: a scratch file outside any repo is nobody's code."""
    d = os.path.dirname(os.path.abspath(os.path.expanduser(path)))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return True
        if d == os.path.dirname(d):
            return False
        d = os.path.dirname(d)


def check_history_comment(tool, ti, existing=None):
    """Deny a NEW code comment that quotes a chat instruction. Edit and MultiEdit read the lines of new_string that
    old_string lacks; Write reads the lines that `existing` (the file before the write) lacks. Markdown, text and
    unknown file types, and files outside a git work tree, are not read: a doc may say who decided what."""
    path = ti.get("file_path") or ""
    base = os.path.basename(path)
    mark = COMMENT_MARKS.get(base.rsplit(".", 1)[-1].lower() if "." in base else "") or COMMENT_MARKS_BY_NAME.get(base)
    if not mark or not in_work_tree(path):
        return None
    if tool == "Write":
        pairs = [(ti.get("content") or "", existing or "")]
    elif tool == "Edit":
        pairs = [(ti.get("new_string") or "", ti.get("old_string") or "")]
    elif tool == "MultiEdit":
        pairs = [(e.get("new_string") or "", e.get("old_string") or "") for e in ti.get("edits") or []]
    else:
        return None
    for new, old in pairs:
        seen = {l.strip() for l in old.splitlines()}
        lines = new.splitlines()
        for n, c in comment_lines(new, mark):
            if lines[n - 1].strip() in seen:
                continue
            if history_hit(c):
                return decide("deny", HISTORY_MSG.format(n=n, name=base, line=c.strip()[:80]))
    return None


def existing_text(path, cwd=None):
    """The text of the file a Write is about to replace, for history-comment; empty when there is none."""
    try:
        p = os.path.expanduser(path or "")
        p = os.path.join(cwd, p) if cwd else p
        return open(p, errors="replace").read() if os.path.isfile(p) else ""
    except OSError:
        return ""


# new-doc: agents wrote Markdown because the rules asked for docs.
# A new .md in a git work tree passes only in the session
# scratchpad, under ~/.claude (skills, plans, memory notes), in a skill folder, or when the user prompt of this turn
# asks for one. A skill folder (`.claude/skills/<name>/` of a repo, `claude/skills/<name>/` of agent-config) holds
# agent instructions, not docs: the verify skill builder must write `.claude/skills/verify-<app>/SKILL.md` into every repo.
NEW_DOC_SUFFIX = re.compile(r"\.md$", re.I)
NEW_DOC_ROOT = os.path.join(HOME, ".claude") + os.sep   # with the separator: ~/.claude-worktrees is not under it
# Anchored: a repo cloned under ~/dev/skills/ or a docs/skills/ folder is not exempt.
NEW_DOC_SCRATCH = re.compile(r"^(?:/private)?/tmp/claude-\d+/[^/]+/[^/]+/scratchpad/")   # path of the file
NEW_DOC_SKILL = re.compile(r"^(?:\.claude|claude|codex)/skills/[^/]+/")   # path relative to the repo root
# The repo map and the shared docs of a Home Repo are docs by design: a pointer per repo, a glossary and decisions per area.
NEW_DOC_HOME = re.compile(r"^(?:repos|docs)/[^/]")   # path relative to the root of a Home Repo
ASKS_FOR_DOC = re.compile(r"\bmarkdown\b|\.md\b|\breadmes?\b|\bdoc(?:s|uments?|umentation)?\b", re.I)
NEW_DOC_MSG = ("new-doc: {name} would be a new Markdown file in a git repo, and this turn's prompt asks for no "
               "Markdown, doc or README. Documentation is the code, its tests, its comments and the commit message; "
               "a new doc is a decision for Fernando. Put the reason in a comment or the commit message, keep notes "
               "in the session scratchpad, or ask Fernando for the file. Rule: AGENTS.md 'How to work'.")
PROMPT_TAIL = 64 << 20   # bytes read back from the end for the prompt: a turn is far shorter than this
# User lines the harness writes, not the person: an interrupt marker, a notification, a local command's output
NOT_TYPED = ("[Request interrupted", "<task-notification>", "<local-command-stdout>", "<local-command-stderr>",
             "<local-command-caveat>")


def prompt_text(line):
    """The prompt the user typed, when this transcript line is one, else None. Claude Code: a main-thread `user`
    line with text and no tool result (isMeta lines are skill text and caveats, not typed). Copilot CLI: a
    user.message event of the main agent."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    if d.get("type") == "user.message":
        data = d.get("data")
        return data.get("content") if isinstance(data, dict) and not d.get("agentId") and isinstance(
            data.get("content"), str) else None
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
        return None
    origin = d.get("origin")   # a task notification is a user line too; the prompt before it still holds
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        c = "\n".join(b.get("text") or "" for b in c if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str) or not c.strip() or c.startswith(NOT_TYPED):
        return None
    return c


def last_prompt(path):
    """The text of the newest typed prompt in the transcript, or None. Reads lines back from the end."""
    import mmap
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            end, floor = len(m), max(0, len(m) - PROMPT_TAIL)
            while end > floor:
                start = max(floor, m.rfind(b"\n", floor, end - 1) + 1)   # the line ends at end, its newline kept
                line = m[start:end]
                # cheap skips first: tool results and sub-agent lines are most of a turn
                if (b'"type":"user' in line or b'"type": "user' in line) and b'"tool_result"' not in line:
                    t = prompt_text(line)
                    if t is not None:
                        return t
                end = start
    except (OSError, ValueError):
        return None
    return None


def repo_relative(absp):
    """absp relative to the top of its work tree, or "" when git cannot say. The file may not exist yet."""
    d = os.path.dirname(absp)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    top = git(d, "rev-parse", "--show-toplevel") if d else None
    if not top:
        return ""
    real = os.path.realpath(absp)   # macOS /var -> /private/var: compare the same spelling as git's
    rel = os.path.relpath(real, os.path.realpath(top))
    return "" if rel.startswith("..") else rel


def check_new_doc(tool, path, p, cwd=None):
    """Deny a Write that creates a .md file inside a git work tree, outside the scratchpad, ~/.claude and skill
    folders, in a turn whose prompt names no Markdown, doc or README. Overwriting a file that exists is not creating one."""
    if tool != "Write" or not path or not NEW_DOC_SUFFIX.search(path):
        return None
    absp = os.path.abspath(os.path.join(cwd or "", os.path.expanduser(path)))
    if absp.startswith(NEW_DOC_ROOT) or NEW_DOC_SCRATCH.search(absp) or os.path.exists(absp):
        return None
    if not repo_state(absp)[0]:
        return None
    rel = repo_relative(absp)
    if NEW_DOC_SKILL.match(rel) or (NEW_DOC_HOME.match(rel) and is_home_repo(absp)):
        return None
    # A Copilot sub-agent sends no transcript, so its turn's prompt cannot be read; like triage, it is not gated
    if p.get("agent_id") and not p.get("transcript_path"):
        return None
    prompt = last_prompt(p["transcript_path"]) if p.get("transcript_path") else None
    if prompt and ASKS_FOR_DOC.search(prompt):
        return None
    return decide("deny", NEW_DOC_MSG.format(name=os.path.basename(absp)))


def mask_single_quotes(text):
    """$ and ` inside '...' do not expand, so they are masked; in the command text of bash -c '...' and eval '...' a
    $ becomes CHILD, for the shell that runs it. A small scanner with a stack, not a regex: an apostrophe inside "..."
    or after a backslash is no quote, $(...) and `...` inside "..." start a fresh context, a # comment is dropped."""
    out, i, n, stack = [], 0, len(text), []
    while i < n:
        ch, top = text[i], stack[-1] if stack else None
        if ch == "\\":
            child = top == '"c' and text.startswith("$", i + 1)   # bash -c "rm $X \$Y": $Y is for the child
            out.append(CHILD if child else text[i:i + 2])
            i += 2
            continue
        if text.startswith("$(", i):
            stack.append("(")
            out.append("$(")
            i += 2
            continue
        if top in ('"', '"c'):
            if ch == '"':
                stack.pop()
            elif ch == "`":
                stack.append("`")
        elif ch == ")" and top == "(":
            stack.pop()
        elif ch == "(" and top == "(":
            stack.append("(")
        elif ch == "`":
            stack.pop() if top == "`" else stack.append("`")
        elif ch == '"':
            stack.append('"c' if RUNS_TEXT.search("".join(out[-200:])) else '"')
        elif ch == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            quoted = text[i:j + 1]
            if RUNS_TEXT.search("".join(out[-200:])):
                quoted = quoted.replace("$", CHILD)
            else:
                quoted = quoted.replace("$", "\x01").replace("`", "\x02")
            out.append(quoted)
            i = j + 1
            continue
        elif ch == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def blank_substitutions(text):
    """(text, inner commands): every $(...) (nested too) and `...` becomes a variable that is never set, because its
    output may be empty; the commands inside are returned so that they are checked too."""
    out, inner, i = [], [], 0
    while i < len(text):
        if text.startswith("$((", i):   # $((1)) is a number
            i = substitution_end(text, i + 2)
            out.append("0")
        elif text.startswith("$(", i) or text[i] == "`":
            start = i
            if text[i] == "`":
                j = text.find("`", i + 1)
                i = len(text) if j < 0 else j + 1
                inner.append(text[start + 1:i - 1])
            else:
                i = substitution_end(text, i + 2)
                inner.append(text[start + 2:i - 1])
            out.append(SUBSTITUTION % (len(inner) - 1))
        else:
            out.append(text[i])
            i += 1
    return "".join(out), inner


def substitution_end(text, i):
    """The index after the ) that closes the $( before i. A ( or ) inside quotes does not count, so
    $(cut -d')' -f1) and $(echo ":)") end at the right place; a $( inside "..." nests."""
    stack, n = ["("], len(text)
    while i < n and stack:
        ch, top = text[i], stack[-1]
        if ch == "\\":
            i += 2
            continue
        if text.startswith("$(", i):
            stack.append("(")
            i += 2
            continue
        if top == '"':
            if ch == '"':
                stack.pop()
        elif ch == "'":
            j = text.find("'", i + 1)
            i = n if j < 0 else j
        elif ch == '"':
            stack.append('"')
        elif ch == "(":
            stack.append("(")
        elif ch == ")":
            stack.pop()
        i += 1
    return i


def root_path(arg, safe):
    """True when an empty variable makes arg / or a path at /. The variables are first replaced by what is known of
    them (expand); then two checks run. In front: variables that may be empty, then a slash, and the rest is nothing,
    `*`, `.` or a path that exists at / (`rsync -a $2/ d` copies /, `rm -rf $X/usr` removes /usr). In the middle: a
    folder at / and nothing but variables that may be empty after it (`rm -rf /opt/$X/` removes /opt). `$ROOT/.env`
    is no root path (/.env does not exist), so the bootstrap `cp "$ROOT/.env" .` does not ask."""
    word = expand(arg, safe)
    if EMPTY not in word:
        return False
    lead = len(word) - len(word.lstrip("/"))   # /$D/* and //$D are / or /* as well when $D is empty
    pos = leading_vars(word[lead:])
    rest = word[lead + pos:]
    if pos and (lead or rest.startswith("/")) and root_rest("/" + rest):   # /$D, $D/x and /${D}usr start at /
        return True
    return middle_root(word)


def middle_root(word):
    """/opt/$X, /opt/$X/$Y/, /usr$X, /opt/$X/* and /opt/$X/..: a folder at / and after it only variables that may be
    empty, so that what is left is that folder, its *, or / itself."""
    body = word.lstrip("/")
    if len(body) == len(word) or EMPTY not in body:
        return False
    folder = re.match(r"[^/$]*", body).group(0)
    tail = body[len(folder):]
    if not folder or EMPTY not in tail:
        return False
    left = posixpath.normpath("/" + folder + tail.replace(EMPTY, ""))
    if left in ("/", "/*"):   # /opt/$X/.. and /opt/$X/../*
        return True
    return left in ("/" + folder, "/" + folder + "/*") and any(
        re.search(r"[*?\[]", n) or os.path.exists("/" + n) for n in brace_words(folder))


def expand(word, safe):
    """word with each variable replaced by what is known of it: its value from safe (which may hold EMPTY itself), or
    OPAQUE when it is guarded, or EMPTY when it may be empty (unknown, or with a modifier such as ${S%/*})."""
    def known(m):
        name, mod = m.group(1) or m.group(2), m.group(3) or ""
        if mod.startswith((":?", "?")) or (mod[:2] in (":-", ":=") and len(mod) > 2 and "$" not in mod):
            return OPAQUE
        return safe[name] if mod == "" and name in safe else EMPTY
    return VAR.sub(known, word)


def value_of(value, safe):
    """What NAME=value stores in safe: the expanded value, or None when it may be empty (D=, D=$X, D=$(cmd)).
    D=/$X stores /${__EMPTY__}, so a later `rm -rf $D` is judged as `rm -rf /$X`."""
    if value in ("", '""', "''"):
        return None
    value = expand(value, safe)
    if len(value) > 4096:   # D=$D$D doubles each time: a cap so that the hook cannot hang; may be empty then
        return None
    return value if value.replace(EMPTY, "") else None


def leading_vars(text):
    """The end of the EMPTY variables (and the slashes between them) at the start of an expanded text."""
    pos = 0
    while text.startswith(EMPTY, pos):
        pos += len(EMPTY)
    return pos


def root_rest(rest):
    """rest follows the empty variables and starts at /: is it /, /*, /. or a path that exists at /? More empty
    variables ($D1/$D2/usr) are skipped."""
    i, n = 0, len(rest)
    while True:   # an index loop, not recursion or slices: $X/ fifty thousand times stays fast and does not fail open
        while i < n and rest[i] == "/":
            i += 1
        j = i
        while rest.startswith(EMPTY, j):
            j += len(EMPTY)
        if j == i or (j < n and rest[j] != "/"):
            break
        i = j
    rest = rest[i:]
    for t in TEMP_DIRS:   # cp f "$JOB/tmp/f.bak": a named file in a temp folder, not the folder itself
        if rest.startswith(t + "/"):
            name = rest[len(t) + 1:].split("/")[0]
            if re.fullmatch(r"[\w.@+-]+", name) and ".." not in rest.split("/") and name != ".":   # not tmp/a/../..
                return False
    first = rest.split("/")[0].replace(EMPTY, "")   # $X/usr$Y and $X/$Y* lose their empty variables too
    if re.search(r"[*?\[]", first):   # a glob at / can match anything there
        return True
    return any(n in ("", ".", "..") or os.path.exists("/" + n) for n in brace_words(first))


def brace_words(word, cap=256):
    """$P/{bin,lib} and {b,u}{in,sr}: the words bash makes of the braces (innermost group first, so nested ones too),
    at most cap of them, so that $D/{a,b}{a,b}... cannot hang the hook."""
    todo, done = [word], []
    while todo and len(done) < cap:
        w = todo.pop()
        m = re.search(r"\{([^{}]*,[^{}]*)\}", w)
        if not m:
            done.append(w)
            continue
        todo.extend(w[:m.start()] + alt + w[m.end():] for alt in m.group(1).split(","))
        if len(todo) > cap * 4:
            break
    return done


def commands(toks, depth=0, given=None):
    """(kind, payload, given) for each command in one segment: ("run", tokens), ("shell", text) or ("eval", text);
    given are the NAME=literal prefixes, which a child shell sees. Sees through those prefixes, redirects, the
    WRAPPERS, timeout and the like, and find -exec."""
    given = {} if given is None else given
    while toks:
        head = os.path.basename(toks[0])
        r = REDIRECT.fullmatch(toks[0])
        if r:   # >f rm ... or > f rm ...
            toks = toks[1:] if r.group(1) else toks[2:]
            continue
        m = NAME_VALUE.fullmatch(toks[0])
        if m and m.group(2) not in ("", '""') and "$" not in m.group(2):
            given[m.group(1)] = m.group(2)
        if m or head in WRAPPERS or head in MORE_WRAPPERS:
            takes = VALUE_OPTS.get(head, ())
            toks = toks[1:]
            while toks and toks[0].startswith("-") and len(toks) > 1:
                toks = toks[2:] if toks[0] in takes else toks[1:]
            if head == "timeout" and toks:
                toks = toks[1:]   # the duration
            continue
        break
    if not toks:
        return []
    head = os.path.basename(toks[0])
    if head in SHELLS:
        for i, t in enumerate(toks[1:], 1):
            if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", t) and i + 1 < len(toks):
                return [("shell", toks[i + 1], given)]
        return []
    if head == "eval":
        return [("eval", " ".join(toks[1:]), given)]
    if head == "find" or toks[0] in ("-exec", "-execdir", "-ok", "-okdir"):   # a second -exec starts a segment after \;
        starts = [i for i, t in enumerate(toks) if t in ("-exec", "-execdir", "-ok", "-okdir")]
        return [c for i in starts for c in commands(toks[i + 1:], depth, dict(given))]
    return [("run", toks, given)]


def empty_var_path(cmd, safe=None, exported=None, depth=0):
    """The first rm/rsync/cp/mv path that an empty variable turns into / or a path at / (root_path), else None.
    In zsh (the Bash tool's shell) `set -- $p` does not split $p, so $2 can stay empty.
    NAME=value that runs for sure before the use (at the start, after ; or a newline or &&; not before & or |,
    not inside ( )) stores the value with expand, so that a later $NAME is judged as if the value were typed there.
    Set inside if/for/while it holds until the block ends or reaches else; set after && until the next ; or ||.
    unset, read, local, mapfile, printf -v, getopts, select and a later NAME= take it back. A for variable over literal
    words and a standalone : "${NAME:?}" count as set. bash -c sees exported variables and its NAME=x prefixes only.
    This is a check against accidents, not a proof: a static rule cannot follow every shell form."""
    safe = dict(NEVER_EMPTY) if safe is None else safe   # name -> expanded value (expand), for the set variables
    exported = {} if exported is None else exported
    body = HEREDOC.sub(lambda m: m.group(0).split("\n", 1)[0] + "\n", cmd)   # cat <<EOF >f; rm ...: keep that rest
    body = mask_single_quotes(body)
    body = re.sub(r"[0-9]*[<>]&[0-9]*-?|(?<!&)&>>?", " ", body)   # 2>&1, >&2, &>f: not a separator
    body, inner = blank_substitutions(body)
    unchecked = set(range(len(inner))) if depth < 3 else set()

    def check_inner(numbers):   # the commands inside $(...) and `...`, with the variables known at that point
        for k in sorted(numbers & unchecked):
            unchecked.discard(k)
            bad = empty_var_path(inner[k], dict(safe), dict(exported), depth + 1)
            if bad:
                return bad
        return None
    scopes, chain = [], set()   # names set inside each open block; names set after && (they hold until ; or ||)

    def drop(names):
        for n in names:
            safe.pop(n, None)
        names.clear()
    for before, toks, after in raw_segments(body, seps=True):
        bad = check_inner({int(k) for t in toks for k in re.findall(r"__SUBSTITUTION_(\d+)__", t)})
        if bad:
            return bad
        if before != "&&":
            drop(chain)
        while toks and toks[0] in KEYWORDS + ("function",):
            if toks[0] in ("else", "elif") and scopes:
                drop(scopes[-1])   # if c; then S=x; else rm -rf $S/; fi
            toks = toks[2:] if toks[0] == "function" else toks[1:]   # function NAME { ...
        if not toks:
            continue
        if toks[0] in CLOSERS:
            if scopes:
                drop(scopes.pop())
            continue
        if toks[0] in OPENERS:
            scopes.append(set())
            if toks[0] == "select" and len(toks) > 1:
                safe.pop(toks[1], None)
            if toks[0] == "for" and len(toks) > 1:
                words = toks[3:] if len(toks) > 2 and toks[2] == "in" else []
                if words and all(w and "$" not in w for w in words):
                    safe[toks[1]] = OPAQUE
                else:
                    safe.pop(toks[1], None)
            if toks[0] not in ("if", "while", "until") or len(toks) < 2:
                continue
            toks = toks[1:]   # `if rm -rf $D/usr; then`: the condition runs
        bare = toks
        while len(bare) > 1 and NAME_VALUE.fullmatch(bare[0]):
            bare = bare[1:]   # IFS= read -r D
        if bare[0] == "printf" and "-v" in bare[:-1]:   # printf -v D ...
            safe.pop(bare[bare.index("-v") + 1], None)
            exported.pop(bare[bare.index("-v") + 1], None)
        if bare[0] == "getopts" and len(bare) > 2:   # getopts ab D
            safe.pop(bare[2], None)
            exported.pop(bare[2], None)
        if bare[0] == ":" and before in (None, ";", "&&") and after in (None, ";", "&&", "||"):
            for t in bare[1:]:   # : "${D:?}" stops the shell when D is empty, so D is set from here on
                g = re.fullmatch(rf"\$\{{({NAME}):?\?[^}}]*\}}", t)
                if g and g.group(1) not in safe:
                    for scope in ([chain] if before == "&&" else []) + scopes[-1:]:
                        scope.add(g.group(1))
                    safe[g.group(1)] = OPAQUE
        if bare[0] in CLEARS and not any(NAME_VALUE.fullmatch(t) for t in bare[1:]):
            for t in bare[1:]:
                if re.fullmatch(NAME, t):
                    if bare[0] == "export" and t in safe:
                        exported[t] = safe[t]
                    elif bare[0] != "export":
                        safe.pop(t, None)
                        exported.pop(t, None)
            if bare is toks:
                continue
        lead = toks[1:] if toks[0] in ("export", "local", "declare", "typeset", "readonly") else toks
        exports = toks[0] == "export" or (toks[0] in ("declare", "typeset") and any(
            t.startswith("-") and "x" in t for t in toks[1:]))
        if lead is not toks:
            lead = [t for t in lead if not t.startswith("-")]   # declare -x D=x
        pairs = [NAME_VALUE.fullmatch(t) for t in lead]
        if lead and all(pairs):   # NAME=x on its own; NAME=x before a command sets it for that command only
            sure = before in (None, ";", "&&") and after in (None, ";", "&&", "||")
            for m in pairs:
                name = m.group(1)
                if m.group(0).startswith(name + "+=") and name in safe:
                    continue   # D+=$X keeps a set D set
                value = value_of(m.group(2), safe) if sure else None
                if value is not None and name not in safe:   # it holds until its block or && chain ends
                    for scope in ([chain] if before == "&&" else []) + scopes[-1:]:
                        scope.add(name)
                for d in (safe, exported) if exports else (safe,):
                    if value is None:
                        d.pop(name, None)
                    else:
                        d[name] = value
            continue
        for kind, payload, given in commands(toks):
            if kind == "run":
                if os.path.basename(payload[0]) in PATH_CMDS:
                    for a in payload[1:]:
                        if not a.startswith("-") and root_path(a, safe):
                            return a
            elif depth < 3 and kind == "eval":   # eval runs in this shell
                bad = empty_var_path(payload.replace(CHILD, "$"), safe, exported, depth + 1)
                if bad:
                    return bad
            elif depth < 3:   # bash -c: a $ outside single quotes expands here first; the rest in a new shell that
                # sees exported variables and the NAME=literal prefixes of this command only
                bad = (empty_var_path(payload.replace(CHILD, "\x01"), dict(safe), dict(exported), depth + 1)
                       or empty_var_path(payload.replace("$", "\x01").replace(CHILD, "$"),
                                         {**NEVER_EMPTY, **exported, **given}, {**exported, **given}, depth + 1))
                if bad:
                    return bad
    bad = check_inner(set(unchecked))   # nested ones, and any not in a segment
    if bad:
        return bad
    for m in HEREDOC.finditer(cmd) if depth < 3 else ():   # bash <<SH or cat <<SH | bash
        line = cmd[cmd.rfind("\n", 0, m.start()) + 1:m.start() + len(m.group(0).split("\n", 1)[0])]
        if not SHELL_HEREDOC.search(line):
            continue
        if m.group(1):   # <<'SH': the new shell expands the body; it sees exported variables and D=x bash prefixes
            given = dict(re.findall(rf"(?:^|\s)({NAME})=([^\s$;&|'\"]+)(?=\s+(?:{NAME}=\S+\s+)*(?:sudo\s+)?(?:bash|sh|zsh)\b)",
                                    line))
            bad = empty_var_path(m.group(3), {**NEVER_EMPTY, **exported, **given}, {**exported, **given}, depth + 1)
        else:   # <<SH: this shell expands the body first, with its own variables (a D=x bash prefix is not one of them)
            bad = empty_var_path(m.group(3), dict(safe), dict(exported), depth + 1)
        if bad:
            return bad
    return None


def split_heredocs(cmd):
    """Return (command with heredoc bodies removed, the bodies). A body is message
    text, not commands: a body line that starts with `git reset --hard` is not one."""
    bodies = []

    def cut(m):
        bodies.append(m.group(3))
        return "<<" + m.group(2) + "\n"
    return HEREDOC.sub(cut, cmd), "\n".join(bodies)


def segments(cmd, depth=0):
    """Quote-aware split on ; & | && || ( ) and newlines. Unwraps VAR=x, env,
    sudo, command and `bash -c '...'`, so a wrapper cannot hide a git call."""
    return [u for cur in raw_segments(cmd) for u in unwrap(cur, depth)]


def raw_segments(cmd, seps=False):
    """The token lists of segments() before unwrap(), in order. With seps: (separator before, tokens, separator after),
    None at the start and the end."""
    # line continuations join, backticks are a command substitution like $(...)
    cmd = cmd.replace("\\\n", " ").replace("`", " ; ")
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        toks = list(lex)
    except ValueError:  # unbalanced quotes: fall back to a plain split (with seps, split ; & | ( ) as well)
        toks = re.findall(r"[;&|()]+|[^\s;&|()]+" if seps else r"\S+", cmd.replace("\n", " ; "))
    out, cur, last = [], [], None
    for t in toks + [None]:
        if t is None or (t and set(t) <= set(";&|()")):
            if cur:
                out.append((last, cur, t))
            cur, last = [], t
        else:
            cur.append(t)
    return out if seps else [toks for _, toks, _ in out]


def unwrap(toks, depth):
    while toks and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", toks[0]) or os.path.basename(toks[0]) in WRAPPERS):
        toks = toks[1:]
        while toks and toks[0].startswith("-") and len(toks) > 1:  # env -i, sudo -u x, nice -n 5, xargs -I {}
            toks = toks[2:] if toks[0] in ("-u", "-n", "-g", "-I", "-L", "-P") else toks[1:]
    if toks and toks[0] == "eval" and depth < 3:
        return segments(" ".join(toks[1:]), depth + 1)
    if toks and os.path.basename(toks[0]) in SHELLS and depth < 3:
        for i, t in enumerate(toks[1:], 1):
            if re.fullmatch(r"-[a-z]*c[a-z]*", t) and i + 1 < len(toks):
                return segments(toks[i + 1], depth + 1)
    return [toks] if toks else []


def strip_git(toks):
    """['git','-C','x','push',...] -> ('push', [...], 'x') ; (None, [], None) if not git."""
    if not toks or os.path.basename(toks[0]) != "git":
        return None, [], None
    i, cdir = 1, None
    while i < len(toks) and toks[i].startswith("-"):
        if toks[i] == "-C" and i + 1 < len(toks):
            cdir = toks[i + 1]
        i += 2 if toks[i] in ("-C", "-c") else 1
    return (toks[i], toks[i + 1:], cdir) if i < len(toks) else (None, [], cdir)


def gh_words(toks):
    """gh's positional words: `gh -R o/r pr merge 3` -> ['pr', 'merge', '3']."""
    out, skip = [], False
    for a in toks[1:]:
        if skip:
            skip = False
        elif a in GH_VALUE_FLAGS:
            skip = True
        elif not a.startswith("-"):
            out.append(a)
    return out


def config_write_key(rest):
    """The key a `git config` call writes, removes or renames; None for a read."""
    pos, flags, skip = [], set(), False
    for a in rest:
        if skip:
            skip = False
        elif a in ("-f", "--file", "--blob", "--type", "--default", "--comment", "--value"):
            skip = True
        elif a.startswith("-"):
            flags.add(a.split("=")[0])
        else:
            pos.append(a)
    if not pos or pos[0] in ("get", "list"):
        return None
    verb = pos[0] in ("set", "unset", "rename-section", "remove-section")
    if verb:
        pos = pos[1:]
    writes = verb or len(pos) >= 2 or flags & {"--unset", "--unset-all", "--add", "--replace-all",
                                               "--rename-section", "--remove-section"}
    return pos[0] if writes and pos else None


def check_text(text, where):
    """Scan the raw text (quotes, heredoc bodies and all) of a commit or PR command.
    The whole command is scanned, not one segment, because the message often sits in
    a heredoc or $(cat ...) outside the git tokens. Known cost: `echo "x" && git
    commit` is judged on the echo text too. `git commit -F file` is not read."""
    if re.search(r"co-authored-by:\s*(github )?copilot", text, re.I):
        return decide("deny", f"co-author: the {where} credits Copilot. Use only the attribution lines "
                              "Claude Code gives you.")
    if "—" in text:
        return decide("deny", f"em-dash: the {where} contains an em dash. Use a comma, a period or parentheses.")
    low = text.lower().replace("’", "'")
    for p in BANNED_PHRASES:
        if p in low:
            return decide("deny", f"words: the {where} contains the banned phrase '{p}'.")
    return None


def rm_flags(args):
    flags = set()
    for a in args:
        if a == "--":
            break
        if a.startswith("--"):
            flags.add({"--recursive": "r", "--force": "f"}.get(a, ""))
        elif a.startswith("-"):
            flags.update(a[1:].lower())
    return flags


def is_commit_not_path(d, name):
    """A sha, tag or origin/main that is not also a file: checking it out detaches HEAD."""
    return (not os.path.exists(os.path.join(d, name))
            and git(d, "rev-parse", "--verify", "--quiet", name + "^{commit}") is not None)


def is_branch(d, name):
    return any(git(d, "show-ref", "--verify", "--quiet", ref) is not None
               for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"))


def capped(toks):
    """True when the segment is `capped_run.py ...` or `python3 [-I] capped_run.py ...`."""
    i = 0
    if toks and re.fullmatch(r"python[0-9.]*", os.path.basename(toks[0])):
        i = 1
        while i < len(toks) and toks[i].startswith("-"):
            i += 1
    return i < len(toks) and os.path.basename(toks[i]) == CAPPED_RUN


def heavy_run(toks):
    """The heavy test or build command in a segment, as text, or None. timeout/gtimeout/xcrun in front
    are looked through: `timeout N make test` stops make but not the test helper it starts."""
    while toks and (os.path.basename(toks[0]) in RUNNERS or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", toks[0])):
        runner = os.path.basename(toks[0])
        toks = toks[1:]
        while toks and toks[0].startswith("-") and runner in RUNNERS:
            toks = toks[2:] if toks[0] in RUNNERS[runner] else toks[1:]
        if runner in ("timeout", "gtimeout") and toks:
            toks = toks[1:]   # the duration
        while runner == "env" and toks and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", toks[0]):
            toks = toks[1:]
    if not toks:
        return None
    head, args = os.path.basename(toks[0]), toks[1:]
    if head == "swift" and args[:1] and args[0] in ("test", "build"):
        return " ".join(toks)
    if head == "xcodebuild" and XCODEBUILD_HEAVY & set(args):
        return " ".join(toks)
    if head in ("make", "gmake"):
        targets, skip = [], False
        for a in args:
            if skip:
                skip = False
            elif a in MAKE_VALUE_FLAGS:
                skip = True
            elif not a.startswith("-") and "=" not in a:
                targets.append(a)
        if any(t == "check" or MAKE_HEAVY & set(re.split(r"[-_:./]", t.lower())) for t in targets):
            return " ".join(toks)
    return None


def secret_reach(body):
    """Does a command that names a secret item run something other than git, gh or cd? Commit, PR and issue
    text may name it. A $(...) or backtick can run cat inside that text, and a heredoc can feed the path to
    python3, so those count as a reach; so does a command that cannot be parsed."""
    if "$(" in body or "`" in body:
        return True
    try:
        return any(t[0] not in ("git", "gh", "cd") for t in segments(body))
    except Exception:
        return True


def check_bash(cmd, cwd):
    cwd = cwd or os.getcwd()
    states = {}

    def state(d):  # lazy: most Bash calls are not git and pay for no git subprocess
        d = os.path.normpath(os.path.join(cwd, os.path.expanduser(d))) if d else cwd
        if d not in states:
            states[d] = repo_state(d)
        return d, states[d]

    try:
        hit = changes_log_bash(cmd, cwd)
    except Exception:   # a parse failure here must not take the other rules down with it (main fails open)
        hit = False
    if hit:
        return decide("deny", CHANGES_LOG_MSG)
    body, heredocs = split_heredocs(cmd)
    if SECRET_REF.search(cmd) and secret_reach(body):
        return decide("deny", SECRET_MSG)
    for toks in segments(body):
        if toks[0] == "cd" and len(toks) > 1:  # `cd <repo> && git switch x` is judged in <repo>
            cwd = os.path.normpath(os.path.join(cwd, os.path.expanduser(toks[1])))
            continue
        if not capped(toks) and os.path.isfile(CAPPED_RUN_PATH):
            heavy = heavy_run(toks)
            if heavy:
                return decide("deny", UNBOUNDED_MSG.format(cmd=heavy))
        sub, rest, cdir = strip_git(toks)
        if sub in ("commit", "push", "merge", "rebase", "am") and "--no-verify" in rest:
            return decide("deny", "no-verify: never skip hooks. Fix what the hook reports.")
        if sub == "commit" and any(re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", a) for a in rest):
            return decide("deny", "no-verify: git commit -n skips hooks. Fix what the hook reports.")
        if sub == "commit":
            r = check_text(cmd, "commit message")
            if r:
                return r
        if sub in ("add", "stage"):   # git stage is git's synonym of git add
            found, dynamic = staged_secrets(state(cdir)[0], rest, "`" in body and add_has_backtick(body))
            if found:
                return decide("deny", (STAGE_DYNAMIC_MSG if dynamic else STAGE_MSG).format(files=", ".join(found)))
        if sub == "push":
            force = any(a in ("-f", "--force") or a.startswith("--force-with-lease") or a.startswith("+")
                        for a in rest)
            targets = [a.split(":")[-1].lstrip("+").removeprefix("refs/heads/") for a in rest if not a.startswith("-")][1:]
            to_main = any(t in MAIN for t in targets)
            if force and to_main:
                return decide("deny", "force-push: never force push to main or master.")
            if force and not ("--force-if-includes" in rest and any(a.startswith("--force-with-lease") for a in rest)):
                return decide("deny", "force-push: use git push --force-with-lease --force-if-includes on your own "
                                      "session branch. Plain --force or --force-with-lease alone can destroy a teammate's commit.")
            if to_main and not state(cdir)[1][1]:
                return decide("deny", "push-main: this repo is team mode. Push your branch and open a PR.")
        if sub in ("switch", "checkout"):
            d, (_, solo, shared) = state(cdir)
            if shared and not solo:
                pos = [a for a in rest if not a.startswith("-")]
                changes_branch = (sub == "switch" or any(a in ("-b", "-B", "--orphan", "--detach") for a in rest)
                                  or ("--" not in rest and len(pos) == 1 and (pos[0] == "-" or is_branch(d, pos[0])
                                                                              or is_commit_not_path(d, pos[0]))))
                if changes_branch:
                    return decide("deny", "branch-in-shared: never switch branches in the shared checkout. "
                                          "Other sessions work here. Use your worktree.")
        if sub == "reset" and "--hard" in rest:
            return decide("ask", "destructive: git reset --hard drops uncommitted work. Confirm with Fernando.")
        if sub == "clean" and any(re.match(r"^-[a-zA-Z]*f", a) or a == "--force" for a in rest):
            return decide("ask", "destructive: git clean -f deletes untracked files. Confirm with Fernando.")
        if toks and os.path.basename(toks[0]) == "rm" and {"r", "f"} <= rm_flags(toks[1:]):
            paths = [a for a in toks[1:] if not a.startswith("-")]
            if any(p.rstrip("/*") in ("", "~", "$HOME", "${HOME}", HOME, ".", "..") for p in paths):
                return decide("ask", "destructive: rm -rf on a root, home or whole-directory path. Confirm with Fernando.")
        if toks and toks[0] == "gh" and len(toks) > 2 and toks[1] in ("pr", "issue") and toks[2] in ("create", "edit", "comment"):
            r = check_text(cmd, "PR text" if toks[1] == "pr" else "issue text")
            if r:
                return r
        if sub == "config":
            key = config_write_key(rest)
            if key and SELF_MERGE_KEYS.fullmatch(key):
                return decide("deny", SELF_MERGE_CONFIG)
        gw = gh_words(toks) if toks and os.path.basename(toks[0]) == "gh" else []
        if gw[:2] == ["pr", "merge"] or (gw[:1] == ["api"] and any(
                re.search(r"pulls/[^/\s]+/merge\b|/merges\b|mergePullRequest|enqueuePullRequest", a) for a in toks)):
            d, (in_repo, solo, _) = state(None)
            # The flag wins over solo, so `git config agent.mode solo` cannot reopen gh pr merge.
            # Read like self-merge.sh reads it: repo config only, no includes. "verify" under either
            # name is enough here, the stricter reading: self-merge.sh lets agent.selfMerge win and
            # STOPs when it is not verify, so a PR in a repo with two values waits for Fernando.
            if in_repo and "verify" in agent_config.both("selfMerge", d, "--local", "--no-includes"):
                return decide("deny", "self-merge: this repo merges its own PRs only through "
                                      "bash ~/.claude/scripts/self-merge.sh, which runs the tests and verify on the "
                                      "rebased head and merges that PR head only.")
            if not solo:
                return decide("ask", "merge-own: in a team repo a human merges the PR. Confirm with Fernando.")
    try:   # last, so that a deny above is never hidden behind this ask; a bug in it allows
        bad = empty_var_path(cmd)
    except Exception:
        bad = None
    if bad:
        return decide("ask", f"empty-var-path: {bad} becomes / or a folder at / when the variable is empty. Guard it "
                             "with ${VAR:?} or set it to a path earlier in this command. Confirm with Fernando.")
    return None


# The triage block ("Task sizing") must be printed before the first file change. Only a Size line plus three more
# of the other four lines count as a block. Seen in the transcripts: `Size: small, why` and `**Size:** small`.
# The template line of AGENTS.md, `Size: small | medium | large`, is not a size; `Size: small | one file` is.
TRIAGE_SIZE = re.compile(r"^\W*Size:(?![^\n]*small\s*\|\s*medium\s*\|\s*large)\W*(small|medium|large)\b", re.I | re.M)
TRIAGE_OTHER = re.compile(r"^\W*(Tests|Agents|Branch|End)\W*:", re.I | re.M)
TRIAGE_DENIES = 2             # denies per session, then the gate gives up: a stuck agent must still be able to work
# Claude Code writes the text of the current reply ~100 ms after PreToolUse starts.
# GUARD_TRIAGE_WAIT scales the waits; the tests set it to 0.
TRIAGE_POLLS = tuple(w * float(os.environ.get("GUARD_TRIAGE_WAIT") or 1) for w in (0.15, 0.15, 0.1))
# A block written as reply text often goes out in the thinking channel instead, and the transcript keeps only a
# one-line summary of thinking.
# So the message names the
# command, whose output is a tool result: always shown, always on disk, and it writes the `.printed` marker.
TRIAGE_CMD = "python3 ~/.claude/scripts/triage.py"
TRIAGE_MSG = ("triage: no triage block was printed in this session before the first file change. Print it now, "
              f"then retry this same call: {TRIAGE_CMD} 'Size: small|medium|large, why' 'Tests: which tests run, "
              "and why' 'Agents: solo or fan-out, and why' 'Branch: <branch> in <worktree path>' "
              "'End: <where the task stops>'. Rule: AGENTS.md 'Task sizing'.")
# Copilot CLI keeps the reply text in events.jsonl, and its shell has no CLAUDE_CODE_SESSION_ID for triage.py.
TRIAGE_MSG_COPILOT = ("triage: no triage block was printed in this session before the first file change. Print it now "
                      "as plain text in your reply, five lines, then retry this same call: Size: small|medium|large, "
                      "why / Tests: which tests run, and why / Agents: solo or fan-out, and why / Branch: <branch> in "
                      "<worktree path> / End: <where the task stops>. Rule: AGENTS.md 'Task sizing'.")


# Not task work: the session scratchpad (temporary scripts and notes) and the auto-memory notes.
TRIAGE_EXEMPT = re.compile(r"/claude-\d+/[^\n]*/scratchpad/|/\.claude/projects/[^/]+/memory/")


def is_triage(text):
    return bool(TRIAGE_SIZE.search(text)) and len({m.lower() for m in TRIAGE_OTHER.findall(text)}) >= 3


TRIAGE_HEAD = 8 << 20   # bytes searched forward first: the block is usually among the first replies
# Copilot CLI's events.jsonl: the reply text is data.content of assistant.message, the compaction summary is
# data.summaryContent of session.compaction_complete. Lines of a sub-agent carry a top-level agentId.
COPILOT_TEXT_EVENTS = (b'{"type":"assistant.message"', b'{"type":"session.compaction_complete"')


def copilot_line_has_triage(line):
    try:
        d = json.loads(line)
    except ValueError:
        return False
    data = d.get("data") if isinstance(d, dict) else None
    if not isinstance(data, dict) or d.get("agentId"):
        return False
    text = data.get("content") if d.get("type") == "assistant.message" else data.get("summaryContent")
    return isinstance(text, str) and is_triage(text)


def transcript_has_triage(path, lo=0):
    """(found, size): is there a triage block in a main-thread assistant text, or in a compaction summary, at or
    after offset lo? Searches the first TRIAGE_HEAD bytes forward, then the rest from the end, with mmap: the
    block is printed early or recently, and a 180 MB transcript costs the same either way. Most hits are skipped
    on a look at the first 700 bytes of their line."""
    import mmap   # only when a session has no marker yet
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        size = len(m)
        split = min(size, lo + TRIAGE_HEAD)

        def hit(i):   # does the line around the "Size:" at offset i hold a block? returns (line_start, line_stop, found)
            start = m.rfind(b"\n", 0, i) + 1
            stop = m.find(b"\n", i)
            stop = size if stop < 0 else stop
            head = m[start:min(stop, start + 700)]   # role and isSidechain come first in a line
            if head.startswith(COPILOT_TEXT_EVENTS):
                return start, stop, copilot_line_has_triage(m[start:stop])
            if head.startswith(b'{"type":"api-') or b'"isSidechain":true' in head or (
                    b'"role":"user"' in head and b"This session is being continued" not in head):
                return start, stop, False
            line = m[start:stop]
            # The reply of the model, or the summary that replaced an older part of this session: it keeps the block
            summary = b'"isCompactSummary":true' in line
            if b'"isSidechain":true' in line or not (summary or b'"role":"assistant"' in line):
                return start, stop, False
            try:
                blocks = json.loads(line)["message"]["content"]
            except (ValueError, KeyError, TypeError):
                return start, stop, False
            if isinstance(blocks, str):
                blocks = [{"type": "text", "text": blocks}] if summary else []
            return start, stop, isinstance(blocks, list) and any(
                isinstance(b, dict) and b.get("type") == "text" and is_triage(b.get("text") or "") for b in blocks)

        pos = lo   # `**Size:**` has "Size:" too; `**Size**:` does not and is not seen
        while (i := m.find(b"Size:", pos, split)) >= 0:
            _, pos, found = hit(i)
            if found:
                return True, size
        end = size
        while (i := m.rfind(b"Size:", split, end)) >= 0:
            end, _, found = hit(i)
            if found:
                return True, size
        return False, size


def triage_marker(sid):
    """The marker path of one session, without its suffix. triage.py writes `<it>.printed` here."""
    return os.path.join(os.environ.get("GUARD_TRIAGE_DIR") or os.environ.get("TMPDIR") or "/tmp",
                        "claude-triage-" + re.sub(r"[^\w.-]", "_", str(sid))[:80])


def check_triage(p, paths=None, copilot=False):
    """Deny the first file change of the main session when it never printed the triage block. One marker file
    per session in $TMPDIR: `.printed` once triage.py printed the block, `.found` once a block is seen in the
    transcript (or the gate gave up), `.deny-1`, `.deny-2` the denies.
    paths: the absolute files the call changes; by default the file_path or notebook_path of tool_input."""
    sid, tp = p.get("session_id"), p.get("transcript_path")
    # Only sub-agent calls carry agent_id. agent_type is also set for a main session started with `claude --agent X`.
    if not sid or not tp or p.get("agent_id"):
        return None
    if paths is None:
        ti = p.get("tool_input") or {}
        paths = [str(ti.get("file_path") or ti.get("notebook_path") or "")]
    paths = [q for q in paths if not TRIAGE_EXEMPT.search(q)]
    if not paths:
        return None
    base = triage_marker(sid)
    if os.path.exists(base + ".printed") or os.path.exists(base + ".found") or not os.path.isfile(tp):
        return None
    # A file outside any git work tree (a plan file, a note in /tmp) is not task work
    if not any(repo_state(q)[0] for q in paths):
        return None
    found, size = transcript_has_triage(tp)
    for wait in TRIAGE_POLLS:   # the block may be in the same reply as this call, and not on disk yet
        if found:
            break
        time.sleep(wait)
        found, size = transcript_has_triage(tp, size)
    if found:
        open(base + ".found", "w").close()
        return None
    for n in range(1, TRIAGE_DENIES + 1):   # claim deny slot n; O_EXCL makes the cap hold for parallel calls
        try:
            os.close(os.open(f"{base}.deny-{n}", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            continue
        return decide("deny", TRIAGE_MSG_COPILOT if copilot else TRIAGE_MSG)
    open(base + ".found", "w").close()   # every slot is taken: the gate gives up
    return None


SUBAGENT_WAIT_MS = 270000   # under the 5 minute cache TTL, with room for the request itself
# Only a command that waits is held to the cap: 83% of sub-agent calls with a long timeout end in under
# 270 s, and denying those would only cost a retry. A poll loop or a sleep is what ran into the TTL.
# `--wait` (an option) and `await` are not a wait: the keyword must start a command.
WAIT_SHAPED = re.compile(r"(^|[;&|(!\n]\s*)(until|while|wait)\b|\bsleep\s+[\d$\"'.]|"
                         r"\btail\b[^;&|\n]*\s(-[a-zA-Z]*[fF]\b|--follow\b)", re.M)
SUBAGENT_WAIT_MSG = ("subagent-wait: a sub-agent's prompt cache lives 5 minutes. A foreground wait of more "
                     "than 270 s lets it expire, and the next request writes your whole context again (12x the "
                     "price of reading it). Start the long command with run_in_background: true, then wait in "
                     "polls of at most 270 s each (timeout: 270000), e.g. "
                     "for i in $(seq 26); do grep -qs DONE build.log && break; sleep 10; done")


SUBAGENT_WAIT_MSG_COPILOT = ("subagent-wait: a sub-agent's prompt cache lives 5 minutes. A foreground wait of more "
                             "than 270 s lets it expire, and the next request writes your whole context again. Start "
                             "the long command with mode: \"async\", then wait with read_bash delays of at most 270 s, "
                             "or poll with initial_wait of at most 270, e.g. "
                             "for i in $(seq 26); do grep -qs DONE build.log && break; sleep 10; done")


def check_subagent_wait(p, copilot=False):
    """Deny a sub-agent's foreground wait (poll loop, sleep) that may block past the 5 minute cache TTL.
    Only sub-agent calls carry agent_id (see check_triage); the main session's cache lives 1 hour in Claude Code.
    Copilot's main session also gets a 5 minute cache, but the rule stays sub-agent only on both, for parity.
    Copilot's bash tool waits initial_wait seconds (default 30) and then moves the command to the background;
    mode "async" starts it there. Copilot's read_bash runs no hook, so its delay cannot be checked here."""
    ti = p.get("tool_input") or {}
    background = ti.get("mode") == "async" if copilot else ti.get("run_in_background")
    if not p.get("agent_id") or background or not WAIT_SHAPED.search(str(ti.get("command", ""))):
        return None
    try:
        timeout = float(ti.get("initial_wait") or 0) * 1000 if copilot else int(ti.get("timeout") or 0)
    except (TypeError, ValueError):
        return None
    if timeout <= SUBAGENT_WAIT_MS:
        return None
    return decide("deny", SUBAGENT_WAIT_MSG_COPILOT if copilot else SUBAGENT_WAIT_MSG)


try:
    FRESH_SESSION_TOKENS = int(os.environ.get("GUARD_FRESH_SESSION_TOKENS") or 150000)
except ValueError:   # a bad value must not crash the guard at import, outside main()'s fail-open
    FRESH_SESSION_TOKENS = 150000
# Run as a command (`bash ~/.claude/scripts/worktree-next.sh x`, `FOO=1 env bash -x worktree-next.sh x`,
# `./worktree-next.sh x`), not read or grepped
WORKTREE_NEXT = re.compile(r"(^|[;&|(\n])\s*(\w+=\S*\s+)*(env\s+)?((ba|z)?sh\s+(-\w+\s+)*)?(\S*/)?worktree-next\.sh\b",
                           re.M)
FRESH_SESSION_MSG = ("fresh-session: this session holds {k}k tokens of context, and every request re-reads all of "
                     "it. Do not start the next task here. Finish this task at its End line, then tell Fernando "
                     "that the next task starts in a new session, where worktree-setup.sh runs again. To keep it in "
                     "this session anyway, Fernando runs the command with the ! prefix.")


def check_fresh_session(p):
    """Deny worktree-next.sh in a main session whose last request held more than FRESH_SESSION_TOKENS of context.
    The reader lives in fresh_session.py, next to this file; a missing or broken reader allows the call."""
    if p.get("agent_id") or not p.get("transcript_path"):
        return None
    if not WORKTREE_NEXT.search(str((p.get("tool_input") or {}).get("command", ""))):
        return None
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    from fresh_session import context_tokens
    ctx = context_tokens(p["transcript_path"])
    return decide("deny", FRESH_SESSION_MSG.format(k=ctx // 1000)) if ctx and ctx > FRESH_SESSION_TOKENS else None


def copilot_payload(p):
    """Copilot CLI sends no transcript_path. Its main session writes ~/.copilot/session-state/<session_id>/
    events.jsonl ($COPILOT_HOME replaces ~/.copilot). A sub-agent call sends its own id as session_id, and no
    rule reads a sub-agent's transcript, so none is added there."""
    sid = p.get("session_id")
    if p.get("transcript_path") or p.get("agent_id") or not isinstance(sid, str) or not re.fullmatch(r"[\w-]+", sid):
        return p
    home = os.environ.get("COPILOT_HOME") or os.path.join(HOME, ".copilot")
    return dict(p, transcript_path=os.path.join(home, "session-state", sid, "events.jsonl"))


def for_copilot(r):
    """Copilot CLI reads permissionDecision and its reason at the top level.
    Its machine has no CLAUDE.md and no Claude Code, so two reasons name what it has."""
    out = dict(r["hookSpecificOutput"])
    out.pop("hookEventName", None)
    reason = out["permissionDecisionReason"].replace("CLAUDE.md 'Branching'", "the 'Branching' section of your instructions")
    out["permissionDecisionReason"] = reason.replace("Use only the attribution lines Claude Code gives you.",
                                                     "Add no Co-authored-by line for Copilot.")
    return out


def log_decision(r, tool, cwd, agent):
    """One JSON line per deny or ask in DECISION_LOG: the rule id, never the command text.
    agent-metrics/pyramid_metrics.py counts them per week, to see what the environment
    catches before a person has to. A failed write never changes the decision."""
    try:
        if not os.environ.get("GUARD_LOG") and not os.path.isdir(METRICS_DIR):
            return
        out = r["hookSpecificOutput"]
        rule = out["permissionDecisionReason"].replace("[guard] ", "", 1).split(":", 1)[0]
        common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir") if cwd else None
        repo = os.path.basename(os.path.dirname(common)) if common else ""
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rule": rule,
                "decision": out["permissionDecision"], "tool": tool, "agent": agent, "repo": repo}
        os.makedirs(os.path.dirname(DECISION_LOG), exist_ok=True)
        with open(DECISION_LOG, "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    copilot = argv[:2] == ["--format", "copilot"] or argv[:1] == ["--format=copilot"]
    try:
        p = json.load(sys.stdin)
        if copilot:
            p = copilot_payload(p)
        tool, ti = p.get("tool_name", ""), p.get("tool_input") or {}
        where = p.get("cwd") or os.getcwd()   # the repo the decision is logged under
        if tool == "Bash":
            r = check_bash(ti.get("command", ""), where)
            if not r:
                r = check_subagent_wait(p, copilot) or check_fresh_session(p)
        elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            # Copilot's edit and create tools name the file `path`; its apply_patch names none
            path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
            named = any(k in ti for k in ("file_path", "notebook_path", "path"))
            paths = [path] if path else ([] if named else patch_paths(ti))
            # Claude Code sends absolute paths; only Copilot's are joined to the session cwd
            cwd = p.get("cwd") if copilot else None
            hit = next(((q, d) for q, d in ((q, check_edit(q, cwd) or check_home_child(q, p, copilot)) for q in paths)
                        if d), None)
            r = hit and hit[1]
            if not r and path:
                r = check_history_comment(tool, dict(ti, file_path=path), existing_text(path, cwd))
            if not r:
                r = check_new_doc(tool, path, p, cwd)
            if not r:
                r = check_triage(p, [os.path.join(cwd or "", os.path.expanduser(q)) for q in paths] if copilot else None,
                                 copilot)
            if hit:
                where = os.path.dirname(os.path.join(cwd or "", hit[0]))
                while where and not os.path.isdir(where):
                    where = os.path.dirname(where)
        else:
            r = None
    except Exception:  # fail open, see docstring
        return 0
    if r:
        log_decision(r, tool, where, "copilot" if copilot else "claude")
        print(json.dumps(for_copilot(r) if copilot else r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
