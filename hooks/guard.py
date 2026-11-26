#!/usr/bin/env python3
RULES = """PreToolUse guard: the CLAUDE.md rules that a machine can check.

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
                    outside the worktrees that session made with worktree-child.sh (a repo nested in one
                    of them, such as atlas studio/, counts as inside it). Files outside a git
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
  new-comment       an Edit, MultiEdit or Write of a code file in a git work tree (on Copilot: edit, create
                    and apply_patch) that adds a comment or a Python docstring. The file after the edit is read
                    whole, so a fragment inside a string or a block comment reads right.
                    Kept and moved comments, shebang and encoding lines, tool directives (noqa, type: ignore,
                    pragma, eslint-disable, ...), license headers, the work-export markers and Swift MARK: pass.
                    The git hook of repo_check.py (check comment) holds the same rule for Codex and a person.
                    A repo opts out with git config agent.comments allow, which only Fernando sets: the reason
                    goes in the commit message and a rule becomes a check   ("Scope", "Tests and evals")
  sudo              Bash that runs sudo, also behind env, command, xargs, bash -c and eval -> ask: Fernando
                    runs it                                                   ("After every task")
  base64            Bash that prints base64 text: base64 or openssl base64 as the last stage of a pipeline,
                    or piped only into head, tail, cat, tr, fold, cut or a grep that prints lines, also in a
                    heredoc a shell runs; with no decode flag, no output redirect and no -o/-out/--output
                                                                              ("Context window")
  rewrite           a Write over an existing file of 200 lines or more that keeps 80% or more of its non-blank
                    lines: Edit changes the lines that differ                 ("Scope")
  pr-body           gh pr create with no body or with --fill, or a body that does not say how it was tested
                    and what outcome it moves                                 ("After every task")
"""
import collections, glob, importlib.util, json, os, posixpath, re, shlex, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import agent_config

def __getattr__(name):
    if name in ("ALLOWED_COMMENT", "HEADER_COMMENT", "PROSE_WORDS", "allowed_comment"):
        import comment_scan
        return getattr(comment_scan, name)
    raise AttributeError(name)


HOME = os.path.expanduser("~")
LIVE_EDIT_ROOTS = tuple(os.path.join(HOME, d) + os.sep for d in
                        (".claude", ".codex", os.path.join(".copilot", "skills"), os.path.join(".copilot", "hooks")))
BANNED_PHRASES = ("here's the kicker", "here's the thing", "plot twist", "let me break this down",
                  "the bottom line", "make no mistake")
MAIN = ("main", "master")
SELF_MERGE_KEYS = re.compile(r"(agent|claude)(\.(selfmerge|selfmergeexclude|selfmergemethod|verifycmd|testcmd|verify|home|cleanup|cleanuphome|comments))?",
                             re.I)
SELF_MERGE_CONFIG = ("self-merge-config: Fernando sets agent.selfMerge, verifyCmd, testCmd, selfMergeExclude, "
                     "selfMergeMethod, verify, home, cleanup, cleanupHome and comments, never an agent. They decide what merges without Fernando, "
                     "which repos need a verify skill, which repo is the Home Repo, where cleanup issues go and which repo may hold code comments. The old names claude.<key> count too. Give Fernando the git config command to run with the ! prefix.")
GH_VALUE_FLAGS = ("-R", "--repo", "--hostname")
CHANGES_LOG = os.sep + os.path.join("claude", "agent-metrics", "changes.md")
CHANGES_LOG_MSG = ("changes-log: changes.md is in the shared checkout of agent-config. A row written here is never "
                   "committed and blocks the next pull. Log it with: bash ~/.claude/scripts/log-change.sh "
                   "'<item>' '<change>' '<metric you expect to move>'. Rows already appended by hand: "
                   "bash ~/.claude/scripts/log-change.sh --adopt")
LOG_READERS = {"cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "wc", "diff", "cmp", "ls",
               "stat", "file", "readlink", "realpath", "shasum", "md5", "nl", "od", "cut", "column",
               "test", "[", "[[", "bat", "jq", "open", "echo", "printf", "basename", "dirname"}
LOG_GIT_READERS = {"diff", "show", "log", "blame", "status", "ls-files", "grep"}
LOG_COPIERS = {"cp", "rsync", "ditto", "install"}
LOG_INTERPRETERS = {"python", "python3", "perl", "ruby", "node", "osascript", "php"}
LOG_PROGRAMS = {"awk", "gawk", "sed"}
HEREDOC_KEEP_LINE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1([^\n]*)\n(.*?)\n\s*\2\s*(?=\n|$)", re.S)
CAPPED_RUN = "capped_run.py"
CAPPED_RUN_PATH = os.environ.get("GUARD_CAPPED_RUN", os.path.join(HOME, ".claude", "scripts", CAPPED_RUN))
METRICS_DIR = os.path.join(HOME, ".claude", "agent-metrics")
DECISION_LOG = os.environ.get("GUARD_LOG") or os.path.join(METRICS_DIR, "history", "guard.jsonl")
XCODEBUILD_HEAVY = {"build", "test", "build-for-testing", "test-without-building", "archive", "analyze"}
MAKE_HEAVY = {"test", "tests", "xctest", "bench", "benchmark", "benchmarks", "e2e"}
RUNNERS = {"timeout": {"-s", "-k", "--signal", "--kill-after"}, "gtimeout": {"-s", "-k", "--signal", "--kill-after"},
           "xcrun": {"--sdk", "--toolchain"}, "caffeinate": {"-t", "-w"}, "arch": set(), "nice": {"-n"},
           "nohup": set(), "time": set(), "env": {"-u", "-C", "-S"}, "command": set(), "exec": set()}
MAKE_VALUE_FLAGS = {"-C", "-f", "-j", "-l", "-o", "-W", "-I", "--file", "--directory", "--makefile"}
UNBOUNDED_MSG = ("unbounded-test: run heavy tests and builds through the memory and time cap, inside the machine memory budget: "
                 "python3 ~/.claude/scripts/capped_run.py -- {cmd}. Options: --mem-gb (6), --timeout (900 s), --serial. "
                 "Two parallel runs without the cap can use all the memory of the machine."
                 )
SECRET_REF = re.compile(r"claude-secret|\bSECRET_KEYCHAIN\b|\bdump-keychain\b")
SECRET_MSG = ("secret-read: the claude-secret Keychain items hold values Fernando typed on a phone; they must never reach the "
              "transcript. Use: python3 ~/.claude/scripts/secret.py run NAME -- <command> (NAME becomes an env var, "
              "output is redacted), or secret.py list for the names. To get a new one: secret.py ask NAME --why '...'")
SECRET_FILES = {".secret", ".env", "secrets.env"}
ADD_SKIP = re.compile(r"-[a-zA-Z]*[upien][a-zA-Z]*|--(update|patch|interactive|edit|dry-run|refresh)")
STAGE_MSG = ("secret-stage: this git add would stage {files}. A credential file never enters git: add its name to "
             ".gitignore in this change (check with git check-ignore -v <file>), then stage again. Rule: AGENTS.md 'Safety'.")
STAGE_DYNAMIC_MSG = ("secret-stage: this git add names a shell expansion ($VAR, $(...) or backticks) that the hook cannot "
                     "read, and git would stage {files} in this repo. Add those names to .gitignore in this change, or "
                     "name the paths literally. Rule: AGENTS.md 'Safety'.")
DYNAMIC_ARG = re.compile(r"[$`]")


def staged_secrets(d, rest, backtick=False):
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
    paths = re.findall(r"^add '(.*)'$", r.stdout, re.M)
    return sorted(p for p in paths if posixpath.basename(p) in SECRET_FILES), dynamic


BACKTICK_MARK = "GUARDBACKTICKMARK"


def add_has_backtick(body):
    try:
        for toks in segments(body.replace("`", f" {BACKTICK_MARK} ")):
            sub, rest, _ = strip_git(toks)
            if sub in ("add", "stage") and any(BACKTICK_MARK in a for a in rest):
                return True
    except Exception:
        return False
    return False


GLUED_REDIRECT = re.compile(r"(?:&>>?|[0-9]*>>?\|?)([^>]+)$")


def git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


REPO_STATE = {}


def repo_state(path):
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    if not d:
        return False, False, False
    if d not in REPO_STATE:
        if git(d, "rev-parse", "--is-inside-work-tree") != "true":
            REPO_STATE[d] = (False, False, False)
        else:
            gd = git(d, "rev-parse", "--path-format=absolute", "--git-dir")
            cd = git(d, "rev-parse", "--path-format=absolute", "--git-common-dir")
            REPO_STATE[d] = (True, agent_config.get("mode", d) == "solo", bool(gd) and gd == cd)
    return REPO_STATE[d]


def is_home_repo(path):
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    return bool(d) and agent_config.get("home", d, "--bool") == "true"


def changes_log_in_shared(path):
    real = os.path.realpath(path)
    if not real.endswith(CHANGES_LOG):
        return False
    in_repo, solo, shared = repo_state(real)
    return in_repo and shared and not solo


def changes_log_bash(cmd, cwd, depth=0):
    if ("changes" not in cmd and "agent-metrics" not in cmd) or depth > 3:
        return False

    def is_log(p, base):
        if not p or any(c.isspace() for c in p):
            return False
        full = os.path.join(base, os.path.expanduser(os.path.expandvars(p)))
        return any(changes_log_in_shared(c) for c in (glob.glob(full) if glob.has_magic(full) else [full]))

    body = HEREDOC_KEEP_LINE.sub(lambda m: " " + m.group(3) + "\n", cmd)
    body = re.sub(r"[0-9]*[<>]&(?:[0-9]+-?|-)", " ", body)
    body = re.sub(r"&>|>\||>&", ">", body)
    for toks in segments(body, depth):
        if toks[0] == "cd" and len(toks) > 1:
            cwd = os.path.normpath(os.path.join(cwd, os.path.expanduser(os.path.expandvars(toks[1]))))
            continue
        sub, rest, cdir = strip_git(toks) if os.path.basename(toks[0]) == "git" else (None, [], None)
        base = os.path.join(cwd, os.path.expanduser(cdir)) if cdir else cwd
        head, args, i = None, [], 0
        while i < len(toks):
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
        if in_text and (head in LOG_INTERPRETERS or head in LOG_PROGRAMS):
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
    texts = [v for v in ti.values() if isinstance(v, str) and "*** Begin Patch" in v]
    return [a or b for t in texts for a, b in PATCH_FILE.findall(t)]


def nearest_dir(path):
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    return d


def check_home_child(path, p, copilot=False):
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
        top = os.path.realpath(top)
        if any(top == r or top.startswith(r.rstrip(os.sep) + os.sep) for r in recorded):
            return None
        return decide("deny", f"home-child: {absp} is in another repo ({top}) and this Home session "
                              "has no worktree there. Run: bash ~/.claude/scripts/worktree-child.sh <repo> <slug>, "
                              "then edit through the path it prints.")
    except Exception:
        return None


def check_edit(path, cwd=None):
    if not path:
        return None
    path = os.path.expanduser(path)
    if cwd and not os.path.isabs(path):
        path = os.path.join(cwd, path)
    absp = os.path.abspath(path)
    gitdir = os.path.dirname(absp)
    if (os.path.basename(absp) == "config" and os.path.isfile(os.path.join(gitdir, "HEAD"))
            and os.path.isdir(os.path.join(gitdir, "objects"))):
        return decide("deny", SELF_MERGE_CONFIG)
    if changes_log_in_shared(absp):
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
NEVER_EMPTY = {"HOME": "\x04", "PWD": "\x04", "OLDPWD": "\x04", "TMPDIR": "\x04", "USER": "\x04"}
NAME = r"[A-Za-z_][A-Za-z0-9_]*"
NAME_VALUE = re.compile(rf"({NAME})\+?=(.*)", re.S)
VAR = re.compile(rf"\$(?:({NAME}|[0-9]+|[@*])|\{{({NAME}|[0-9]+|[@*])([^}}]*)\}})")
CLEARS = ("unset", "read", "local", "typeset", "declare", "readonly", "export", "mapfile", "readarray")
OPENERS, CLOSERS = {"if", "while", "until", "for", "case", "select"}, {"fi", "done", "esac"}
KEYWORDS = ("then", "do", "else", "elif", "{", "}", "!")
RUNS_TEXT = re.compile(r"(?:\b(?:bash|sh|zsh)\b(?:[ \t]+\S+)*?[ \t]+-[a-zA-Z]*c[a-zA-Z]*[ \t]+|\beval\b[^;&|\n]*)\Z")
SHELL_HEREDOC = re.compile(r"\b(?:bash|sh|zsh)\b[^;&|\n]*<<|<<[^\n]*\|\s*(?:sudo\s+)?(?:bash|sh|zsh)\b")
REDIRECT = re.compile(r"[0-9]*(?:[<>]{1,2}|&>>?|>&)(\S*)")
MORE_WRAPPERS = {"timeout", "setsid", "stdbuf", "ionice", "caffeinate"}
VALUE_OPTS = {
    "sudo": ("-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"), "env": ("-u", "-C", "-S", "-P"),
    "xargs": ("-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"), "nice": ("-n",), "exec": ("-a",),
    "timeout": ("-s", "-k"), "stdbuf": ("-i", "-o", "-e"), "ionice": ("-c", "-n", "-p"), "caffeinate": ("-t", "-w")}
CHILD = "\x03"
SUBSTITUTION = "${__SUBSTITUTION_%d__}"
EMPTY = "${__EMPTY__}"
OPAQUE = "\x04"
TEMP_DIRS = ("tmp", "private/tmp", "var/tmp", "var/folders", "private/var/folders")


_NAMED = r"(?:fernando|the reviewer)"
_TOLD = r"(?:said|asked|requested|told|instructed|insisted|wants?|wanted|prefers?|preferred)"
_ADVERB = r"(?:'s| has| had| have| just| also| explicitly| specifically| originally| previously)*"
DIRECTIVE = re.compile(r"\b(?:never|always|must|not|don't|do not|no longer|stop|only)\b")
HISTORY_PATTERNS = (
    re.compile(rf"\b{_NAMED}{_ADVERB}\s+{_TOLD}\b"),
    re.compile(rf"\b{_NAMED}\s+(?:does not|doesn't|did not|didn't|do not|don't)\s+want\b"),
    re.compile(rf"\bper {_NAMED}\b"),
    re.compile(rf"\brequested by {_NAMED}\b"),
    re.compile(rf"\b{_NAMED}:\s"),
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
NEW_COMMENT_MSG = ("new-comment: line {n} of {name} adds a {kind} ('{line}'){more}. Add no code comments and no "
                   "docstrings: nothing checks them, and they can hide a workaround. Fix the root cause. Rename or "
                   "restructure the code so that it needs no comment. Put the reason in the commit message. Turn a "
                   "rule into a check: a test, a lint rule or a guard. Kept and moved comments, shebang lines, tool "
                   "directives (noqa, type: ignore, eslint-disable) and license headers pass. Fernando sets git config "
                   "agent.comments allow for a repo that needs comments.")
COPILOT_TOOLS = {"bash": "Bash", "edit": "Edit", "create": "Write", "write": "Write", "multiedit": "MultiEdit", "apply_patch": "Edit",
                 "notebookedit": "NotebookEdit"}
COPILOT_FIELDS = (("old_str", "old_string"), ("new_str", "new_string"), ("file_text", "content"))
PATCH_HEAD = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$")


def history_hit(comment):
    low = comment.lower().replace("’", "'")
    for pat in HISTORY_PATTERNS:
        m = pat.search(low)
        if not m:
            continue
        if re.search(r":\s$", m.group(0)):
            if not DIRECTIVE.search(low):
                continue
        return m.group(0)
    return None


def in_work_tree(path):
    d = os.path.dirname(os.path.abspath(os.path.expanduser(path)))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return True
        if d == os.path.dirname(d):
            return False
        d = os.path.dirname(d)


UNSCANNED_TYPES = frozenset(("md", "markdown", "txt", "json", "csv", "tsv", "log", "lock", "rst", "png"))


def scanner():
    import comment_scan
    return comment_scan


def resolve(path, cwd):
    p = os.path.expanduser(path or "")
    return os.path.join(cwd, p) if cwd else p


def splice(text, old, new, every=False):
    if not old or old not in text:
        return None
    return text.replace(old, new) if every else text.replace(old, new, 1)


def splice_lines(text, old, new):
    got = splice(text, old + "\n", new + "\n" if new else "")
    return got if got is not None else splice(text, old, new)


def edit_pairs(tool, ti, existing):
    if tool == "Write":
        return [(existing or "", ti.get("content") or "")], False
    if tool == "Edit":
        edits = [ti]
    elif tool == "MultiEdit":
        edits = ti.get("edits") or []
    else:
        return [], False
    fragments = [(e.get("old_string") or "", e.get("new_string") or "") for e in edits]
    text = existing or ""
    for e, (old, new) in zip(edits, fragments):
        text = splice(text, old, new, bool(e.get("replace_all")))
        if text is None:
            return fragments, any(old for old, _ in fragments)
    return [(existing or "", text)], False


def patch_changes(ti):
    out = []
    for text in (v for v in ti.values() if isinstance(v, str) and "*** Begin Patch" in v):
        cur = None
        for line in text.splitlines():
            head = PATCH_HEAD.match(line)
            if head:
                cur = {"path": head.group(2), "kind": head.group(1), "hunks": []}
                out.append(cur)
                if cur["kind"] != "Delete":
                    cur["hunks"].append(([], []))
            elif cur is None or cur["kind"] == "Delete" or line.startswith("*** "):
                continue
            elif line.startswith("@@"):
                cur["hunks"].append(([], []))
            else:
                old, new = cur["hunks"][-1]
                if not line.startswith("+"):
                    old.append(line[1:])
                if not line.startswith("-"):
                    new.append(line[1:])
    return out


def patch_pairs(change, before):
    hunks = [("\n".join(o), "\n".join(n)) for o, n in change["hunks"] if o or n]
    text = before
    for old, new in hunks:
        text = splice_lines(text, old, new)
        if text is None:
            return hunks, any(old for old, _ in hunks)
    return [(before, text)], False


def patch_items(ti, cwd):
    items = []
    for change in patch_changes(ti):
        path = resolve(change["path"], cwd)
        items.append((path, comment_hits(path, *patch_pairs(change, existing_text(change["path"], cwd)))))
    return items


def comment_hits(path, pairs, partial=False):
    if not path or path.rsplit(".", 1)[-1].lower() in UNSCANNED_TYPES:
        return []
    scan = scanner()
    if not scan.language(path) or not in_work_tree(path):
        return []
    hits = [h for before, after in pairs for h in scan.added(after, before, path)]
    return [h for h in hits if not (partial and h.kind == "docstring" and h.line == 1)]


def comment_items(tool, ti, existing=None):
    path = ti.get("file_path") or ""
    return [(path, comment_hits(path, *edit_pairs(tool, ti, existing)))]


def first_line(text):
    return (text.strip().splitlines() or [""])[0]


def history_decision(items):
    for path, hits in items:
        for h in hits:
            if history_hit(h.text):
                return decide("deny", HISTORY_MSG.format(n=h.line, name=os.path.basename(path),
                                                         line=first_line(h.text)[:80]))
    return None


def comments_allowed(path):
    value = agent_config.get("comments", nearest_dir(os.path.dirname(os.path.abspath(os.path.expanduser(path)))))
    return (value or "").strip().lower() == "allow"


def new_comment_decision(items):
    for path, hits in items:
        bad = [h for h in hits if not h.allowed]
        if bad and not comments_allowed(path):
            more = f" and {len(bad) - 1} more" if len(bad) > 1 else ""
            return decide("deny", NEW_COMMENT_MSG.format(n=bad[0].line, name=os.path.basename(path), kind=bad[0].kind,
                                                         line=first_line(bad[0].text)[:60], more=more))
    return None


def check_comment_rules(items):
    return history_decision(items) or new_comment_decision(items)


def check_history_comment(tool, ti, existing=None):
    return history_decision(comment_items(tool, ti, existing))


def check_new_comment(tool, ti, existing=None):
    return new_comment_decision(comment_items(tool, ti, existing))


REWRITE_MIN_LINES = 200
REWRITE_KEEP_SHARE = 0.8
REWRITE_MSG = ("rewrite: this Write replaces {name} ({total} lines) and keeps {kept}% of its non-blank lines unchanged. "
               "A whole-file rewrite hides the real change from the reviewer and costs more output. Use Edit for the "
               "lines that change.")


def check_rewrite(tool, ti, existing=None):
    lines = (existing or "").splitlines()
    if tool != "Write" or len(lines) < REWRITE_MIN_LINES:
        return None
    old = collections.Counter(l.rstrip() for l in lines if l.strip())
    new = collections.Counter(l.rstrip() for l in (ti.get("content") or "").splitlines() if l.strip())
    total = sum(old.values())
    kept = sum(min(n, new[l]) for l, n in old.items())
    if not total or kept / total < REWRITE_KEEP_SHARE:
        return None
    name = os.path.basename(ti.get("file_path") or "")
    return decide("deny", REWRITE_MSG.format(name=name, total=len(lines), kept=round(100 * kept / total)))


def existing_text(path, cwd=None):
    try:
        p = os.path.expanduser(path or "")
        p = os.path.join(cwd, p) if cwd else p
        return open(p, errors="replace").read() if os.path.isfile(p) else ""
    except OSError:
        return ""


NEW_DOC_SUFFIX = re.compile(r"\.md$", re.I)
NEW_DOC_ROOT = os.path.join(HOME, ".claude") + os.sep
NEW_DOC_SCRATCH = re.compile(r"^(?:/private)?/tmp/claude-\d+/[^/]+/[^/]+/scratchpad/")
NEW_DOC_SKILL = re.compile(r"^(?:\.claude|claude|codex)/skills/[^/]+/")
NEW_DOC_HOME = re.compile(r"^(?:repos|docs)/[^/]")
ASKS_FOR_DOC = re.compile(r"\bmarkdown\b|\.md\b|\breadmes?\b|\bdoc(?:s|uments?|umentation)?\b", re.I)
NEW_DOC_MSG = ("new-doc: {name} would be a new Markdown file in a git repo, and this turn's prompt asks for no "
               "Markdown, doc or README. Documentation is the code, its tests and the commit message; "
               "a new doc is a decision for Fernando. Put the reason in the commit message, keep notes "
               "in the session scratchpad, or ask Fernando for the file. Rule: AGENTS.md 'How to work'.")
PROMPT_TAIL = 64 << 20
NOT_TYPED = ("[Request interrupted", "<task-notification>", "<local-command-stdout>", "<local-command-stderr>",
             "<local-command-caveat>")


def prompt_text(line):
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
    origin = d.get("origin")
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
    import mmap
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            end, floor = len(m), max(0, len(m) - PROMPT_TAIL)
            while end > floor:
                start = max(floor, m.rfind(b"\n", floor, end - 1) + 1)
                line = m[start:end]
                if (b'"type":"user' in line or b'"type": "user' in line) and b'"tool_result"' not in line:
                    t = prompt_text(line)
                    if t is not None:
                        return t
                end = start
    except (OSError, ValueError):
        return None
    return None


def repo_relative(absp):
    d = os.path.dirname(absp)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    top = git(d, "rev-parse", "--show-toplevel") if d else None
    if not top:
        return ""
    real = os.path.realpath(absp)
    rel = os.path.relpath(real, os.path.realpath(top))
    return "" if rel.startswith("..") else rel


def check_new_doc(tool, path, p, cwd=None):
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
    if p.get("agent_id") and not p.get("transcript_path"):
        return None
    prompt = last_prompt(p["transcript_path"]) if p.get("transcript_path") else None
    if prompt and ASKS_FOR_DOC.search(prompt):
        return None
    return decide("deny", NEW_DOC_MSG.format(name=os.path.basename(absp)))


def mask_single_quotes(text):
    out, i, n, stack = [], 0, len(text), []
    while i < n:
        ch, top = text[i], stack[-1] if stack else None
        if ch == "\\":
            child = top == '"c' and text.startswith("$", i + 1)
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
    out, inner, i = [], [], 0
    while i < len(text):
        if text.startswith("$((", i):
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
    word = expand(arg, safe)
    if EMPTY not in word:
        return False
    lead = len(word) - len(word.lstrip("/"))
    pos = leading_vars(word[lead:])
    rest = word[lead + pos:]
    if pos and (lead or rest.startswith("/")) and root_rest("/" + rest):
        return True
    return middle_root(word)


def middle_root(word):
    body = word.lstrip("/")
    if len(body) == len(word) or EMPTY not in body:
        return False
    folder = re.match(r"[^/$]*", body).group(0)
    tail = body[len(folder):]
    if not folder or EMPTY not in tail:
        return False
    left = posixpath.normpath("/" + folder + tail.replace(EMPTY, ""))
    if left in ("/", "/*"):
        return True
    return left in ("/" + folder, "/" + folder + "/*") and any(
        re.search(r"[*?\[]", n) or os.path.exists("/" + n) for n in brace_words(folder))


def expand(word, safe):
    def known(m):
        name, mod = m.group(1) or m.group(2), m.group(3) or ""
        if mod.startswith((":?", "?")) or (mod[:2] in (":-", ":=") and len(mod) > 2 and "$" not in mod):
            return OPAQUE
        return safe[name] if mod == "" and name in safe else EMPTY
    return VAR.sub(known, word)


def value_of(value, safe):
    if value in ("", '""', "''"):
        return None
    value = expand(value, safe)
    if len(value) > 4096:
        return None
    return value if value.replace(EMPTY, "") else None


def leading_vars(text):
    pos = 0
    while text.startswith(EMPTY, pos):
        pos += len(EMPTY)
    return pos


def root_rest(rest):
    i, n = 0, len(rest)
    while True:
        while i < n and rest[i] == "/":
            i += 1
        j = i
        while rest.startswith(EMPTY, j):
            j += len(EMPTY)
        if j == i or (j < n and rest[j] != "/"):
            break
        i = j
    rest = rest[i:]
    for t in TEMP_DIRS:
        if rest.startswith(t + "/"):
            name = rest[len(t) + 1:].split("/")[0]
            if re.fullmatch(r"[\w.@+-]+", name) and ".." not in rest.split("/") and name != ".":
                return False
    first = rest.split("/")[0].replace(EMPTY, "")
    if re.search(r"[*?\[]", first):
        return True
    return any(n in ("", ".", "..") or os.path.exists("/" + n) for n in brace_words(first))


def brace_words(word, cap=256):
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
    given = {} if given is None else given
    while toks:
        head = os.path.basename(toks[0])
        r = REDIRECT.fullmatch(toks[0])
        if r:
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
                toks = toks[1:]
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
    if head == "find" or toks[0] in ("-exec", "-execdir", "-ok", "-okdir"):
        starts = [i for i, t in enumerate(toks) if t in ("-exec", "-execdir", "-ok", "-okdir")]
        return [c for i in starts for c in commands(toks[i + 1:], depth, dict(given))]
    return [("run", toks, given)]


def empty_var_path(cmd, safe=None, exported=None, depth=0):
    safe = dict(NEVER_EMPTY) if safe is None else safe
    exported = {} if exported is None else exported
    body = HEREDOC.sub(lambda m: m.group(0).split("\n", 1)[0] + "\n", cmd)
    body = mask_single_quotes(body)
    body = re.sub(r"[0-9]*[<>]&[0-9]*-?|(?<!&)&>>?", " ", body)
    body, inner = blank_substitutions(body)
    unchecked = set(range(len(inner))) if depth < 3 else set()

    def check_inner(numbers):
        for k in sorted(numbers & unchecked):
            unchecked.discard(k)
            bad = empty_var_path(inner[k], dict(safe), dict(exported), depth + 1)
            if bad:
                return bad
        return None
    scopes, chain = [], set()

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
                drop(scopes[-1])
            toks = toks[2:] if toks[0] == "function" else toks[1:]
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
            toks = toks[1:]
        bare = toks
        while len(bare) > 1 and NAME_VALUE.fullmatch(bare[0]):
            bare = bare[1:]
        if bare[0] == "printf" and "-v" in bare[:-1]:
            safe.pop(bare[bare.index("-v") + 1], None)
            exported.pop(bare[bare.index("-v") + 1], None)
        if bare[0] == "getopts" and len(bare) > 2:
            safe.pop(bare[2], None)
            exported.pop(bare[2], None)
        if bare[0] == ":" and before in (None, ";", "&&") and after in (None, ";", "&&", "||"):
            for t in bare[1:]:
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
            lead = [t for t in lead if not t.startswith("-")]
        pairs = [NAME_VALUE.fullmatch(t) for t in lead]
        if lead and all(pairs):
            sure = before in (None, ";", "&&") and after in (None, ";", "&&", "||")
            for m in pairs:
                name = m.group(1)
                if m.group(0).startswith(name + "+=") and name in safe:
                    continue
                value = value_of(m.group(2), safe) if sure else None
                if value is not None and name not in safe:
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
            elif depth < 3 and kind == "eval":
                bad = empty_var_path(payload.replace(CHILD, "$"), safe, exported, depth + 1)
                if bad:
                    return bad
            elif depth < 3:
                bad = (empty_var_path(payload.replace(CHILD, "\x01"), dict(safe), dict(exported), depth + 1)
                       or empty_var_path(payload.replace("$", "\x01").replace(CHILD, "$"),
                                         {**NEVER_EMPTY, **exported, **given}, {**exported, **given}, depth + 1))
                if bad:
                    return bad
    bad = check_inner(set(unchecked))
    if bad:
        return bad
    for m in HEREDOC.finditer(cmd) if depth < 3 else ():
        line = cmd[cmd.rfind("\n", 0, m.start()) + 1:m.start() + len(m.group(0).split("\n", 1)[0])]
        if not SHELL_HEREDOC.search(line):
            continue
        if m.group(1):
            given = dict(re.findall(rf"(?:^|\s)({NAME})=([^\s$;&|'\"]+)(?=\s+(?:{NAME}=\S+\s+)*(?:sudo\s+)?(?:bash|sh|zsh)\b)",
                                    line))
            bad = empty_var_path(m.group(3), {**NEVER_EMPTY, **exported, **given}, {**exported, **given}, depth + 1)
        else:
            bad = empty_var_path(m.group(3), dict(safe), dict(exported), depth + 1)
        if bad:
            return bad
    return None


def split_heredocs(cmd):
    bodies = []

    def cut(m):
        bodies.append(m.group(3))
        return "<<" + m.group(2) + "\n"
    return HEREDOC.sub(cut, cmd), "\n".join(bodies)


def segments(cmd, depth=0):
    return [u for cur in raw_segments(cmd) for u in unwrap(cur, depth)]


def raw_segments(cmd, seps=False):
    cmd = cmd.replace("\\\n", " ").replace("`", " ; ")
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        toks = list(lex)
    except ValueError:
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
        while toks and toks[0].startswith("-") and len(toks) > 1:
            toks = toks[2:] if toks[0] in ("-u", "-n", "-g", "-I", "-L", "-P") else toks[1:]
    if toks and toks[0] == "eval" and depth < 3:
        return segments(" ".join(toks[1:]), depth + 1)
    if toks and os.path.basename(toks[0]) in SHELLS and depth < 3:
        for i, t in enumerate(toks[1:], 1):
            if re.fullmatch(r"-[a-z]*c[a-z]*", t) and i + 1 < len(toks):
                return segments(toks[i + 1], depth + 1)
    return [toks] if toks else []


def strip_git(toks):
    if not toks or os.path.basename(toks[0]) != "git":
        return None, [], None
    i, cdir = 1, None
    while i < len(toks) and toks[i].startswith("-"):
        if toks[i] == "-C" and i + 1 < len(toks):
            cdir = toks[i + 1]
        i += 2 if toks[i] in ("-C", "-c") else 1
    return (toks[i], toks[i + 1:], cdir) if i < len(toks) else (None, [], cdir)


def gh_words(toks):
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
    return (not os.path.exists(os.path.join(d, name))
            and git(d, "rev-parse", "--verify", "--quiet", name + "^{commit}") is not None)


def is_branch(d, name):
    return any(git(d, "show-ref", "--verify", "--quiet", ref) is not None
               for ref in (f"refs/heads/{name}", f"refs/remotes/origin/{name}"))


def capped(toks):
    i = 0
    if toks and re.fullmatch(r"python[0-9.]*", os.path.basename(toks[0])):
        i = 1
        while i < len(toks) and toks[i].startswith("-"):
            i += 1
    return i < len(toks) and os.path.basename(toks[i]) == CAPPED_RUN


def heavy_run(toks):
    while toks and (os.path.basename(toks[0]) in RUNNERS or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", toks[0])):
        runner = os.path.basename(toks[0])
        toks = toks[1:]
        while toks and toks[0].startswith("-") and runner in RUNNERS:
            toks = toks[2:] if toks[0] in RUNNERS[runner] else toks[1:]
        if runner in ("timeout", "gtimeout") and toks:
            toks = toks[1:]
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
    if "$(" in body or "`" in body:
        return True
    try:
        return any(t[0] not in ("git", "gh", "cd") for t in segments(body))
    except Exception:
        return True


SUDO_MSG = ("sudo: this command runs sudo. Never run it yourself. Give Fernando the command to run, marked as "
            "Fernando's to execute, and say why it needs sudo.")
STDOUT_REDIRECT = re.compile(r"(?:1|&)?>>?\|?(?!&)\S*")
BASE64_DECODE = re.compile(r"-[a-zA-Z]*[dD][a-zA-Z]*|-decode|--decode")
BASE64_PASSIVE = {"head", "tail", "cat", "tr", "fold", "cut"}
BASE64_FILTERS = {"grep", "egrep", "fgrep", "rg"}
HEREDOC_MARK = re.compile(r"<<-?\s*(['\"]?)[A-Za-z_][A-Za-z0-9_]*\1")
BASE64_QUIET = re.compile(r"-[a-zA-Z]*[cql][a-zA-Z]*|--(?:count|quiet|silent|files-with-matches)")
GREP_QUIET = re.compile(r"-[a-zA-Z]*L[a-zA-Z]*|--files-without-match")
BASE64_OUTPUT = {"-o", "-out", "--output"}
ENCODED_MSG = ("base64: this command prints base64 text into the transcript. Base64 data can trigger safety filters "
              "and fills the context. Write it to a file (> out.b64, or -o out.b64) and read the part you need.")
PR_TESTED = re.compile(r"\b(?:test\w*|verif\w*|ran|eval\w*|checked)\b", re.I)
PR_OUTCOME = re.compile(r"\b(?:outcomes?|metrics?|measur\w*)\b", re.I)
PR_FLAGS = {"--title": "title", "--body": "body", "--body-file": "file", "-t": "title", "-b": "body", "-F": "file"}
PR_BODY_MSG = ("pr-body: this gh pr create is missing: {missing}. A PR states a title, what changed, how it was "
               "tested and the outcome (the metric or the behavior) it moves. {how}Pass the text with --body or "
               "--body-file. --fill copies the commit messages and states neither the test nor the outcome.")


def sudo_in(toks, depth=0):
    while toks:
        head = os.path.basename(toks[0])
        if head == "sudo":
            return True
        if head == "command" and len(toks) > 1 and re.fullmatch(r"-[a-zA-Z]*[vV][a-zA-Z]*", toks[1]):
            return False
        r = REDIRECT.fullmatch(toks[0])
        if r:
            toks = toks[1:] if r.group(1) else toks[2:]
            continue
        if NAME_VALUE.fullmatch(toks[0]) or head in WRAPPERS or head in MORE_WRAPPERS:
            takes = VALUE_OPTS.get(head, ())
            toks = toks[1:]
            while toks and toks[0].startswith("-") and len(toks) > 1:
                toks = toks[2:] if toks[0] in takes else toks[1:]
            if head == "timeout" and toks:
                toks = toks[1:]
            continue
        break
    if not toks or depth >= 3:
        return False
    head = os.path.basename(toks[0])
    if head in SHELLS:
        return any(re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", t) and i + 1 < len(toks) and runs_sudo(toks[i + 1], depth + 1)
                   for i, t in enumerate(toks[1:], 1))
    if head == "eval":
        return runs_sudo(" ".join(toks[1:]), depth + 1)
    if head == "find":
        return any(sudo_in(toks[i + 1:], depth + 1) for i, t in enumerate(toks) if t in ("-exec", "-execdir", "-ok", "-okdir"))
    return False


def runs_sudo(cmd, depth=0):
    body, _ = split_heredocs(cmd)
    shell_bodies = [m.group(3) for m in HEREDOC.finditer(cmd)
                    if SHELL_HEREDOC.search(cmd[cmd.rfind("\n", 0, m.start()) + 1:m.start() + len(m.group(0).split("\n", 1)[0])])]
    for text in [body] + shell_bodies:
        outer, inner = blank_substitutions(mask_single_quotes(text))
        if any(sudo_in(toks, depth) for toks in raw_segments(outer)):
            return True
        if depth < 3 and any(runs_sudo(i, depth + 1) for i in inner):
            return True
    return False


def encodes_base64(toks):
    head, args = os.path.basename(toks[0]), toks[1:]
    if head == "openssl":
        if not args or not (args[0] == "base64" or (args[0] == "enc" and ("-base64" in args or "-a" in args))):
            return False
    elif head != "base64":
        return False
    return not any(BASE64_DECODE.fullmatch(a) or a in BASE64_OUTPUT or a.startswith("--output=") for a in args)


def stdout_redirected(parts, k):
    _, toks, after = parts[k]
    following = parts[k + 1][1] if k + 1 < len(parts) else []
    if after == "&" and toks and re.fullmatch(r"\d*>>?", toks[-1]):
        toks = toks[:-1]
    return any(STDOUT_REDIRECT.fullmatch(t) for t in toks) or bool(after == "&" and following[:1] and following[0].startswith(">"))


def shows_lines(toks):
    head = os.path.basename(toks[0])
    if head in BASE64_PASSIVE:
        return True
    quiet = [BASE64_QUIET] if head == "rg" else [BASE64_QUIET, GREP_QUIET]
    return head in BASE64_FILTERS and not any(q.fullmatch(a) for a in toks[1:] for q in quiet)


def pipeline_shows(parts, i):
    end = i
    while parts[end][2] == "|" and end + 1 < len(parts):
        end += 1
    runs = [[p for k, p, _ in commands(parts[j][1]) if k == "run"] for j in range(i + 1, end + 1)]
    return all(r and all(shows_lines(p) for p in r) for r in runs) and not any(stdout_redirected(parts, j) for j in range(i, end + 1))


def shell_heredoc_prints(cmd):
    bodies = []
    for m in HEREDOC.finditer(cmd):
        start = cmd.rfind("\n", 0, m.start()) + 1
        line = cmd[start:m.start() + len(m.group(0).split("\n", 1)[0])]
        if not SHELL_HEREDOC.search(line):
            continue
        header = HEREDOC_MARK.sub(" ", line, count=1)
        parts = raw_segments(mask_single_quotes(header), seps=True)
        if any(any(os.path.basename(t) in SHELLS for t in toks) and pipeline_shows(parts, k)
               for k, (_, toks, _) in enumerate(parts)):
            bodies.append(m.group(3))
    return bodies


def prints_base64(cmd, depth=0):
    body, _ = split_heredocs(cmd)
    outer, _ = blank_substitutions(mask_single_quotes(body))
    parts = raw_segments(outer, seps=True)
    for i, (_, toks, after) in enumerate(parts):
        for kind, payload, _ in commands(toks):
            if kind == "run" and encodes_base64(payload):
                if pipeline_shows(parts, i):
                    return True
            if kind in ("shell", "eval") and depth < 3 and after != "|" and not stdout_redirected(parts, i) and prints_base64(payload, depth + 1):
                return True
    return depth < 3 and any(prints_base64(b, depth + 1) for b in shell_heredoc_prints(cmd))


def pr_create_parts(rest):
    given, bodies, files, flags, i = False, [], [], set(), 0
    while i < len(rest):
        a = rest[i]
        name, eq, value = a.partition("=") if a.startswith("--") else (a[:2], "", a[2:])
        kind = PR_FLAGS.get(name)
        if kind:
            if not (eq or value) and i + 1 < len(rest):
                i += 1
                value = rest[i]
            if kind == "title":
                given = True
            (bodies if kind == "body" else files if kind == "file" else []).append(value)
        else:
            flags.add(a)
        i += 1
    return given, bodies, files, flags


def check_pr_body(rest, cwd, heredocs):
    title, bodies, files, flags = pr_create_parts(rest)
    if flags & {"--web", "-w", "--help", "-h"}:
        return None
    fill = bool(flags & {"--fill", "-f", "--fill-first", "--fill-verbose"})
    text, unseen = "\n".join(bodies), any("$" in v or "`" in v for v in bodies)
    for f in files:
        if f == "-" or f.startswith("/dev/"):
            unseen = True
            continue
        try:
            with open(os.path.join(cwd, os.path.expanduser(f))) as fh:
                text += "\n" + fh.read(1 << 20)
        except OSError:
            unseen = True
    if unseen:
        text += "\n" + heredocs
        if not heredocs.strip():
            return None
    missing = [m for ok, m in ((title or fill, "a title (--title)"), (text.strip(), "what changed"),
                               (PR_TESTED.search(text), "how it was tested"), (PR_OUTCOME.search(text), "the outcome it moves")) if not ok]
    if not missing:
        return None
    how = "" if bodies or files else "No --body or --body-file is given. "
    return decide("deny", PR_BODY_MSG.format(missing=", ".join(missing), how=how))


def repo_check_module():
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "scripts", "repo_check.py")
    spec = importlib.util.spec_from_file_location("repo_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def commit_hook_installed(repo):
    hook = git(repo, "rev-parse", "--git-path", "hooks/pre-commit")
    if hook:
        try:
            with open(os.path.join(repo, hook), errors="replace") as f:
                if "agent-config-repo-hook" in f.read():
                    return True
        except OSError:
            pass
    return bool(git(repo, "config", "--local", "--get", "hook.agent-config-repo-check-pre-commit.command"))


ADD_ALL_FLAGS = ("-A", "--all", "-u", "--update", "-a")


def commit_scope(repo, commit_args, adds):
    take_all = any(a == "--all" or re.fullmatch(r"-[a-zA-Z]*a[a-zA-Z]*", a) for a in commit_args if a.startswith("-"))
    if not take_all and not adds:
        return None
    if take_all and not adds:
        return "tracked"
    specs = [a for rest in adds for a in rest if not a.startswith("-")]
    flags = [a for rest in adds for a in rest if a.startswith("-")]
    if not specs or "." in specs or any(f in ADD_ALL_FLAGS for f in flags):
        return "everything"
    return specs


def commit_entries(rc, repo, commit_args, adds, cdir):
    scan = rc.load("comment_scan", "hooks/comment_scan.py")
    change = rc.Change(repo)
    paths = {p: old for status, p, old, _ in change.files()}
    scope = commit_scope(repo, commit_args, adds)
    if scope is not None:
        z = lambda *a: [x for x in (rc.git(repo, *a) or "").split("\0") if x]
        tracked = z("diff", "--name-only", "-z", "--diff-filter=ACMR", "HEAD") if change.old != rc.EMPTY_TREE else []
        untracked = z("ls-files", "-o", "--exclude-standard", "-z") if adds else []
        found = tracked + untracked if scope != "tracked" else tracked
        if isinstance(scope, list):
            wanted = [os.path.relpath(os.path.realpath(os.path.join(cdir, a)), os.path.realpath(repo)) for a in scope]
            found = [p for p in found if any(p == w or p.startswith(w.rstrip("/") + "/") for w in wanted)]
        for p in found:
            paths.setdefault(p, p)
    entries = []
    for p, old_path in sorted(paths.items()):
        if not scan.language(p):
            continue
        old = rc.text_of(rc.git(repo, "show", f"{change.old}:{old_path}", binary=True)) or ""
        try:
            with open(os.path.join(repo, p), "rb") as f:
                new = rc.text_of(f.read())
        except OSError:
            continue
        entries.append((p, old, new))
    return entries


def commit_comment_decision(cdir, commit_args, adds):
    top = git(cdir, "rev-parse", "--show-toplevel")
    if not top:
        return None
    rc = repo_check_module()
    if rc.in_progress(top) or commit_hook_installed(top):
        return None
    problems = rc.comment_report(top, commit_entries(rc, top, commit_args, adds, cdir))
    if not problems:
        return None
    return decide("deny", "new-comment: this git commit adds code comments or docstrings, and no commit hook guards this "
                          "repo, so the guard checks it. `repo-hooks.sh status` says why. "
                          + " ".join(problems[:3]))


def check_bash(cmd, cwd):
    cwd = cwd or os.getcwd()
    states = {}
    adds = []

    def state(d):
        d = os.path.normpath(os.path.join(cwd, os.path.expanduser(d))) if d else cwd
        if d not in states:
            states[d] = repo_state(d)
        return d, states[d]

    try:
        hit = changes_log_bash(cmd, cwd)
    except Exception:
        hit = False
    if hit:
        return decide("deny", CHANGES_LOG_MSG)
    body, heredocs = split_heredocs(cmd)
    if SECRET_REF.search(cmd) and secret_reach(body):
        return decide("deny", SECRET_MSG)
    try:
        printed = ("base64" in cmd or "openssl" in cmd) and prints_base64(cmd)
    except Exception:
        printed = False
    if printed:
        return decide("deny", ENCODED_MSG)
    for toks in segments(body):
        if toks[0] == "cd" and len(toks) > 1:
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
            r = commit_comment_decision(os.path.normpath(os.path.join(cwd, os.path.expanduser(cdir))) if cdir else cwd,
                                        rest, adds)
            if r:
                return r
        if sub in ("add", "stage"):
            adds.append(rest)
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
        if toks and toks[0] == "gh" and "create" in toks and gh_words(toks)[:2] == ["pr", "create"]:
            try:
                r = check_pr_body(toks[toks.index("create") + 1:], cwd, heredocs)
            except Exception:
                r = None
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
            if in_repo and "verify" in agent_config.both("selfMerge", d, "--local", "--no-includes"):
                return decide("deny", "self-merge: this repo merges its own PRs only through "
                                      "bash ~/.claude/scripts/self-merge.sh, which runs the tests and verify on the "
                                      "rebased head and merges that PR head only.")
            if not solo:
                return decide("ask", "merge-own: in a team repo a human merges the PR. Confirm with Fernando.")
    try:
        asks = "sudo" in cmd and runs_sudo(cmd)
    except Exception:
        asks = False
    if asks:
        return decide("ask", SUDO_MSG)
    try:
        bad = empty_var_path(cmd)
    except Exception:
        bad = None
    if bad:
        return decide("ask", f"empty-var-path: {bad} becomes / or a folder at / when the variable is empty. Guard it "
                             "with ${VAR:?} or set it to a path earlier in this command. Confirm with Fernando.")
    return None


TRIAGE_SIZE = re.compile(r"^\W*Size:(?![^\n]*small\s*\|\s*medium\s*\|\s*large)\W*(small|medium|large)\b", re.I | re.M)
TRIAGE_OTHER = re.compile(r"^\W*(Tests|Agents|Branch|End)\W*:", re.I | re.M)
TRIAGE_DENIES = 2
TRIAGE_POLLS = tuple(w * float(os.environ.get("GUARD_TRIAGE_WAIT") or 1) for w in (0.15, 0.15, 0.1))
TRIAGE_CMD = "python3 ~/.claude/scripts/triage.py"
TRIAGE_MSG = ("triage: no triage block was printed in this session before the first file change. Print it now, "
              f"then retry this same call: {TRIAGE_CMD} 'Size: small|medium|large, why' 'Tests: which tests run, "
              "and why' 'Agents: solo or fan-out, and why' 'Branch: <branch> in <worktree path>' "
              "'End: <where the task stops>'. Rule: AGENTS.md 'Task sizing'.")
TRIAGE_MSG_COPILOT = ("triage: no triage block was printed in this session before the first file change. Print it now "
                      "as plain text in your reply, five lines, then retry this same call: Size: small|medium|large, "
                      "why / Tests: which tests run, and why / Agents: solo or fan-out, and why / Branch: <branch> in "
                      "<worktree path> / End: <where the task stops>. Rule: AGENTS.md 'Task sizing'.")


TRIAGE_EXEMPT = re.compile(r"/claude-\d+/[^\n]*/scratchpad/|/\.claude/projects/[^/]+/memory/")


def is_triage(text):
    return bool(TRIAGE_SIZE.search(text)) and len({m.lower() for m in TRIAGE_OTHER.findall(text)}) >= 3


TRIAGE_HEAD = 8 << 20
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
    import mmap
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        size = len(m)
        split = min(size, lo + TRIAGE_HEAD)

        def hit(i):
            start = m.rfind(b"\n", 0, i) + 1
            stop = m.find(b"\n", i)
            stop = size if stop < 0 else stop
            head = m[start:min(stop, start + 700)]
            if head.startswith(COPILOT_TEXT_EVENTS):
                return start, stop, copilot_line_has_triage(m[start:stop])
            if head.startswith(b'{"type":"api-') or b'"isSidechain":true' in head or (
                    b'"role":"user"' in head and b"This session is being continued" not in head):
                return start, stop, False
            line = m[start:stop]
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

        pos = lo
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
    return os.path.join(os.environ.get("GUARD_TRIAGE_DIR") or os.environ.get("TMPDIR") or "/tmp",
                        "claude-triage-" + re.sub(r"[^\w.-]", "_", str(sid))[:80])


def check_triage(p, paths=None, copilot=False):
    sid, tp = p.get("session_id"), p.get("transcript_path")
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
    if not any(repo_state(q)[0] for q in paths):
        return None
    found, size = transcript_has_triage(tp)
    for wait in TRIAGE_POLLS:
        if found:
            break
        time.sleep(wait)
        found, size = transcript_has_triage(tp, size)
    if found:
        open(base + ".found", "w").close()
        return None
    for n in range(1, TRIAGE_DENIES + 1):
        try:
            os.close(os.open(f"{base}.deny-{n}", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            continue
        return decide("deny", TRIAGE_MSG_COPILOT if copilot else TRIAGE_MSG)
    open(base + ".found", "w").close()
    return None


SUBAGENT_WAIT_MS = 270000
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
except ValueError:
    FRESH_SESSION_TOKENS = 150000
WORKTREE_NEXT = re.compile(r"(^|[;&|(\n])\s*(\w+=\S*\s+)*(env\s+)?((ba|z)?sh\s+(-\w+\s+)*)?(\S*/)?worktree-next\.sh\b",
                           re.M)
FRESH_SESSION_MSG = ("fresh-session: this session holds {k}k tokens of context, and every request re-reads all of "
                     "it. Do not start the next task here. Finish this task at its End line, then tell Fernando "
                     "that the next task starts in a new session, where worktree-setup.sh runs again. To keep it in "
                     "this session anyway, Fernando runs the command with the ! prefix.")


def check_fresh_session(p):
    if p.get("agent_id") or not p.get("transcript_path"):
        return None
    if not WORKTREE_NEXT.search(str((p.get("tool_input") or {}).get("command", ""))):
        return None
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    from fresh_session import context_tokens
    ctx = context_tokens(p["transcript_path"])
    return decide("deny", FRESH_SESSION_MSG.format(k=ctx // 1000)) if ctx and ctx > FRESH_SESSION_TOKENS else None


def copilot_payload(p):
    sid = p.get("session_id")
    if p.get("transcript_path") or p.get("agent_id") or not isinstance(sid, str) or not re.fullmatch(r"[\w-]+", sid):
        return p
    home = os.environ.get("COPILOT_HOME") or os.path.join(HOME, ".copilot")
    return dict(p, transcript_path=os.path.join(home, "session-state", sid, "events.jsonl"))


def for_copilot(r):
    out = dict(r["hookSpecificOutput"])
    out.pop("hookEventName", None)
    reason = out["permissionDecisionReason"].replace("CLAUDE.md 'Branching'", "the 'Branching' section of your instructions")
    out["permissionDecisionReason"] = reason.replace("Use only the attribution lines Claude Code gives you.",
                                                     "Add no Co-authored-by line for Copilot.")
    return out


def log_decision(r, tool, cwd, agent):
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
        if copilot:
            tool = COPILOT_TOOLS.get(tool.lower(), tool)
            ti = dict(ti, **{dst: ti[src] for src, dst in COPILOT_FIELDS if src in ti and dst not in ti})
        where = p.get("cwd") or os.getcwd()
        if tool == "Bash":
            r = check_bash(ti.get("command", ""), where)
            if not r:
                r = check_subagent_wait(p, copilot) or check_fresh_session(p)
        elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
            named = any(k in ti for k in ("file_path", "notebook_path", "path"))
            paths = [path] if path else ([] if named else patch_paths(ti))
            cwd = p.get("cwd") if copilot else None
            hit = next(((q, d) for q, d in ((q, check_edit(q, cwd) or check_home_child(q, p, copilot)) for q in paths)
                        if d), None)
            r = hit and hit[1]
            if not r and path:
                before = existing_text(path, cwd)
                full = dict(ti, file_path=resolve(path, cwd))
                r = check_comment_rules(comment_items(tool, full, before)) or check_rewrite(tool, full, before)
            elif not r and not named:
                r = check_comment_rules(patch_items(ti, cwd))
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
    except Exception:
        return 0
    if r:
        log_decision(r, tool, where, "copilot" if copilot else "claude")
        print(json.dumps(for_copilot(r) if copilot else r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
