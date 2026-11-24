#!/usr/bin/env python3
HELP = """The rules of claude/AGENTS.md as git hooks, so they hold for Codex, Copilot and a person too.

Usage:
  repo_check.py commit-msg <message file> [--repo DIR]
  repo_check.py pre-commit [--repo DIR]

commit-msg stops a message with:
  em-dash, words, co-author   the guard's own checks (claude/hooks/guard.py check_text)
  no-test     the staged change adds a new function or method definition with a body (Python, JS and
              TS, Swift, Go, Rust, Kotlin) in a code file, and no test, eval or verify path.
              Exempt: a line `No-test: <reason>` in the body of the message. A change inside an existing
              function, a data-only class or struct, a moved definition, a changed signature and a pure deletion pass. Counted as a test:
              a file whose name says verify, check, e2e, canary, harness, smoke, vectors or fixtures.
  llm-api     the staged diff adds a hosted LLM API host, SDK import or SDK dependency.
              Exempt: a line `LLM-API: <reason>` in the body of the message.
pre-commit stops:
  binary      a new staged file that is a compiled output, archive, app bundle or model file, or is over
              5 MB, unless Git LFS tracks it. By extension: .o .a .so .dylib .dll .exe .class .jar .war
              .pyc .pyo .whl .egg .node .wasm .ipa .apk .aab .dmg .pkg .zip .tar .tgz .gz .bz2 .xz .7z .rar
              .pt .pth .ckpt .safetensors .gguf .ggml .onnx .tflite .mlmodel .h5 .pb .npy .npz .bin, and
              anything inside a .dSYM, .app, .mlpackage or .mlmodelc folder. Also a file that git sees as
              binary with another name whose content starts with a Mach-O, ELF or PE header. Images, fonts,
              PDFs and other assets under 5 MB pass. Track the rest with `git lfs track`. A file that HEAD
              already holds is not checked.
  cross-import  a new import of the inside of another service (services/A reaches services/B/...).
              Imports that HEAD already holds pass. Tests and evals may import the inside of another service.
              Use the contract or the API of B.
  comment     a staged added or modified file of a type the comment scan reads (hooks/comment_scan.py)
              that adds a code comment or a Python docstring, against the same file in HEAD. A kept or moved
              comment, a shebang or encoding line, a tool directive (noqa, type: ignore, eslint-disable) and
              a license header pass. Exempt: git config agent.comments allow (old name claude.comments),
              which only Fernando sets. The same rule as the guard rule new-comment, for Codex and a person.

On the work laptop (a copilot-instructions.md next to hooks/) the check comment always runs. Each other check runs
only when its rule is in that file and the module it needs is installed, so no rule of the personal setup is enforced there.

Each problem is one line on stderr that starts with its check id. Exit 1 when there is a problem.
Nothing is checked during a merge, rebase, cherry-pick or revert. An internal error exits 0 with a note:
a bug here never blocks a commit. `git commit --no-verify` skips the hook, as for every git hook.
Install in every repo under ~/dev with `bash ~/.claude/scripts/repo-hooks.sh install`.
"""
import argparse
import collections
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
CLAUDE = os.path.dirname(HERE)
MAX_BYTES = 5_000_000
MAX_SCAN_BYTES = 2_000_000
RULES_FILE = "copilot-instructions.md"
CHECK_RULES = {
    "message": ("No em dash", None),
    "no-test": ("Every feature ships with a test suite", "agent-metrics/pyramid_metrics.py"),
    "llm-api": ("LLM access: local Claude Code", "agent-metrics/pyramid_metrics.py"),
    "binary": ("Never commit binaries", None),
    "cross-import": ("Build everything as independent services", "agent-metrics/pyramid_metrics.py"),
}
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")
LLM_API_LINE = re.compile(r"^[ \t]*llm-api:[ \t]*\S.*\S", re.I | re.M)
SCISSORS = re.compile(r"^# -+ >8 -+$", re.M)
LFS_POINTER = b"version https://git-lfs.github.com/spec/"
WORD = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
VERIFY_WORDS = {"verify", "check", "checks", "e2e", "canary", "harness", "smoke", "vectors", "fixtures"}
CALL_WORDS = r"(?!(?:if|for|while|switch|catch|return|function|else|do|try|await|new|super|typeof)\b)"
DEFS = {
    ".py": [re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")],
    ".js": [re.compile(r"\bfunction\s*\*?\s*(\w+)"), re.compile(r"\b(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*(?::[^=]+)?=>"),
            re.compile(r"^\s*(?:(?:public|private|protected|static|async|get|set|override)\s+)*" + CALL_WORDS +
                       r"(\w+)\s*\([^)]*\)\s*(?::\s*[^{]+)?\{")],
    ".swift": [re.compile(r"\bfunc\s+(\w+)")],
    ".go": [re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)")],
    ".rs": [re.compile(r"\bfn\s+(\w+)")],
    ".kt": [re.compile(r"\bfun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?(\w+)")],
}
for _ext in (".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"):
    DEFS[_ext] = DEFS[".js"]
DEFS[".kts"] = DEFS[".kt"]
ARTIFACT_EXT = {".o", ".a", ".so", ".dylib", ".dll", ".exe", ".class", ".jar", ".war", ".pyc", ".pyo", ".whl", ".egg",
                ".node", ".wasm", ".ipa", ".apk", ".aab", ".dmg", ".pkg", ".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz",
                ".7z", ".rar", ".pt", ".pth", ".ckpt", ".safetensors", ".gguf", ".ggml", ".onnx", ".tflite", ".mlmodel",
                ".h5", ".pb", ".npy", ".npz", ".bin"}
TEXT_ALLOWED_EXT = {".pb", ".npy", ".bin"}
ARTIFACT_DIRS = (".dsym", ".app", ".mlpackage", ".mlmodelc")
EXEC_MAGIC = (b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
              b"\xca\xfe\xba\xbe", b"MZ")
_modules = {}


def load(name, rel):
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(name, os.path.join(CLAUDE, rel))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        _modules[name] = mod
    return _modules[name]


def guard():
    return load("guard", "hooks/guard.py")


def enabled(check):
    phrase, needs = CHECK_RULES[check]
    if needs and not os.path.isfile(os.path.join(CLAUDE, needs)):
        return False
    path = os.path.join(CLAUDE, RULES_FILE)
    if not os.path.isfile(path):
        return True
    with open(path, encoding="utf-8", errors="replace") as f:
        return phrase in f.read()


def metrics():
    return load("pyramid_metrics", "agent-metrics/pyramid_metrics.py")


def git(repo, *args, binary=False):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True)
    if p.returncode != 0:
        return None
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def in_progress(repo):
    for name in IN_PROGRESS:
        path = (git(repo, "rev-parse", "--git-path", name) or "").strip()
        if path and os.path.exists(os.path.join(repo, path)):
            return name
    return None


class Change:

    def __init__(self, repo, new=None, old=None):
        self.repo, self.new = repo, new
        if old is None:
            old = "HEAD" if git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") else EMPTY_TREE
        self.old = old

    def diff(self, *args, paths=()):
        base = ["diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/"]
        span = ["--cached", self.old] if self.new is None else [self.old, self.new]
        return git(self.repo, *base, *args, *span, *(["--", *paths] if paths else [])) or ""

    def files(self):
        parts = self.diff("-M", "-z", "--name-status", "--diff-filter=ACMR").split("\0")
        out, i = [], 0
        while i < len(parts) and parts[i]:
            status = parts[i]
            if status[0] in "RC":
                out.append((status[0], parts[i + 2], parts[i + 1], int(status[1:] or 0)))
                i += 3
            else:
                out.append((status[0], parts[i + 1], parts[i + 1], 0))
                i += 2
        return out

    def spec(self, path):
        return f":{path}" if self.new is None else f"{self.new}:{path}"

    def blob(self, path):
        return git(self.repo, "show", self.spec(path), binary=True)

    def old_blob(self, path):
        return git(self.repo, "show", f"{self.old}:{path}", binary=True)

    def size(self, path):
        out = git(self.repo, "cat-file", "-s", self.spec(path))
        return int(out) if out and out.strip().isdigit() else 0

    def listing(self):
        if self.new is None:
            out = git(self.repo, "ls-files", "-z")
        else:
            out = git(self.repo, "ls-tree", "-r", "-z", "--name-only", self.new)
        return [f for f in (out or "").split("\0") if f]

    def old_listing(self):
        out = git(self.repo, "ls-tree", "-r", "-z", "--name-only", self.old)
        return [f for f in (out or "").split("\0") if f]

    def lfs_tracked(self, path):
        if self.new is None:
            out = git(self.repo, "check-attr", "--cached", "filter", "--", path) or ""
            if out.strip().endswith(": lfs"):
                return True
        return (self.blob(path) or b"").startswith(LFS_POINTER)


def clean_message(text):
    cut = SCISSORS.search(text)
    if cut:
        text = text[:cut.start()]
    return "\n".join(l for l in text.splitlines() if not l.startswith("#")).lstrip("\n")


def message_problems(text):
    if not enabled("message"):
        return []
    g = guard()
    seen, out = set(), []
    for n, line in enumerate(text.splitlines(), 1):
        decision = g.check_text(line, "commit message")
        if not decision:
            continue
        reason = decision["hookSpecificOutput"]["permissionDecisionReason"].replace("[guard] ", "", 1)
        if reason not in seen:
            seen.add(reason)
            out.append(f"{reason} (line {n})")
    return out


def words(path):
    return {w.lower() for w in WORD.findall(os.path.splitext(os.path.basename(path))[0])}


def is_verify_path(path):
    return bool(words(path) & VERIFY_WORDS)


def has_body(ext, text):
    if ext == ".py":
        return text.rstrip().endswith(":") or ":" in text
    if ext == ".kt" or ext == ".kts":
        return "{" in text or re.search(r"\)\s*(?::\s*[\w<>?., ]+)?\s*=\s", text) is not None
    return "{" in text or "=>" in text


def definitions(diff, sign):
    found, path = {}, None
    m = metrics()
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("--- ") or line.startswith("diff --git "):
            continue
        elif line[:1] == sign and path and not m.COMMENT_LINE.match(line[1:]):
            ext = os.path.splitext(path)[1].lower()
            text = line[1:]
            for pattern in DEFS.get(ext, ()):
                hit = pattern.search(text)
                if hit and (sign == "-" or has_body(ext, text)):
                    found.setdefault(hit.group(1), f"{path}: {text.strip()[:60]}")
                    break
    return found


def untested_problem(change, body):
    if not enabled("no-test"):
        return None
    m = metrics()
    paths = [p for status, p, _, sim in change.files() if not (status == "R" and sim == 100)]
    code = [p for p in paths if m.is_code_path(p)]
    if not code or any(m.is_test_path(p) or is_verify_path(p) for p in paths):
        return None
    if m.NO_TEST_LINE.search(body):
        return None
    diff = change.diff("-U0", paths=code)
    removed = definitions(diff, "-")
    new = [hit for name, hit in definitions(diff, "+").items() if name not in removed]
    if not new:
        return None
    return (f"no-test: the change adds a definition ({new[0]}) and no test, eval or verify path. "
            "Add one, or end the message with 'No-test: <reason>'.")


def llm_problem(change, body):
    if not enabled("llm-api"):
        return None
    hit = metrics().llm_api_added(change.diff("-U0"))
    if hit and not LLM_API_LINE.search(body):
        return (f"llm-api: the change adds a hosted LLM API ({hit}). Call local Claude Code instead "
                "(AGENTS.md, LLM access), or end the message with 'LLM-API: <reason>'.")
    return None


def commit_msg_problems(change, raw):
    text = clean_message(raw)
    body = text.split("\n", 1)[1] if "\n" in text else ""
    out = message_problems(text)
    for check in (untested_problem, llm_problem):
        problem = check(change, body)
        if problem:
            out.append(problem)
    return out


def artifact_kind(path, binary):
    parts = path.lower().split("/")
    ext = os.path.splitext(parts[-1])[1]
    if ext in ARTIFACT_EXT and (binary or ext not in TEXT_ALLOWED_EXT):
        return "a compiled output, archive or model file"
    if any(os.path.splitext(d)[1] in ARTIFACT_DIRS for d in parts[:-1]):
        return "part of an app bundle, dSYM or model package"
    return None


def binary_problems(change):
    out = []
    if not enabled("binary"):
        return out
    numstat = change.diff("-z", "--numstat", "--no-renames", "--diff-filter=A").split("\0")
    for entry in numstat:
        fields = entry.split("\t", 2)
        if len(fields) != 3:
            continue
        added, _, path = fields
        size = change.size(path)
        why = artifact_kind(path, added == "-")
        if not why and added == "-" and (change.blob(path) or b"")[:4].startswith(EXEC_MAGIC):
            why = "an executable or library by its header"
        if not why and size > MAX_BYTES:
            why = f"{size / 1_000_000:.1f} MB, over the 5 MB limit (5,000,000 bytes)"
        if not why or change.lfs_tracked(path):
            continue
        out.append(f"binary: {path} is a new file that is {why}. Track it with Git LFS (git lfs track), "
                   "or keep it out of the repo.")
    return out


def hits_in(repo, files, wanted, read):
    m = metrics()
    tmp = tempfile.mkdtemp(prefix="repo-check-")
    try:
        for path in wanted:
            data = read(path)
            if data is None:
                continue
            dest = os.path.join(tmp, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
        return m.cross_imports(tmp, files)[1]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cross_import_problems(change):
    if not enabled("cross-import"):
        return []
    m = metrics()
    new_files = change.listing()
    if not any(f.startswith("services/") for f in new_files):
        return []
    changed = [(p, old) for _, p, old, _ in change.files() if p.startswith("services/") and p.endswith(m.CODE_EXT)]
    if not changed:
        return []
    old_of = dict(changed)
    new_hits = [h for h in hits_in(change.repo, new_files, [p for p, _ in changed], change.blob)
                if h[3] == "internal" and not m.is_test_path(h[0])]
    old_files = change.old_listing()
    before = collections.Counter(
        (f, b) for f, _, b, k in hits_in(change.repo, old_files + [p for p in old_of if p not in old_files],
                                         [p for p, _ in changed], lambda p: change.old_blob(old_of[p])) if k == "internal")
    out, shown = [], collections.Counter()
    for f, line, b, _ in new_hits:
        shown[(f, b)] += 1
        if shown[(f, b)] > before[(f, b)]:
            out.append(f"cross-import: {f}:{line} imports the inside of services/{b}. "
                       f"Use the contract or the API of services/{b}.")
    return out


def text_of(blob):
    if blob is None or len(blob) > MAX_SCAN_BYTES or b"\0" in blob[:8192]:
        return None
    return blob.decode("utf-8", "replace")


def comments_allowed(repo):
    return (load("agent_config", "hooks/agent_config.py").get("comments", repo) or "").strip().lower() == "allow"


def squash_pending(repo):
    path = (git(repo, "rev-parse", "--git-path", "SQUASH_MSG") or "").strip()
    return bool(path) and os.path.exists(os.path.join(repo, path))


def comment_report(repo, entries):
    if comments_allowed(repo) or squash_pending(repo):
        return []
    scan = load("comment_scan", "hooks/comment_scan.py")
    out = []
    for path, old, new in entries:
        if new is None or not scan.language(path):
            continue
        hits = [h for h in scan.added(new, old or "", path) if not h.allowed]
        if hits:
            first = (hits[0].text.strip().splitlines() or [""])[0][:60]
            more = f" and {len(hits) - 1} more" if len(hits) > 1 else ""
            out.append(f"comment: {path}:{hits[0].line} adds a {hits[0].kind} ('{first}'){more}. Add no code comments "
                       "and no docstrings: rename or restructure the code, put the reason in the commit message, turn "
                       "a rule into a check. Tool directives and license headers pass.")
    return out


def comment_problems(change):
    scan = load("comment_scan", "hooks/comment_scan.py")
    entries = []
    for status, path, old_path, _ in change.files():
        if scan.language(path):
            old = text_of(change.old_blob(old_path)) if status in "MRC" else ""
            entries.append((path, old, text_of(change.blob(path))))
    return comment_report(change.repo, entries)


def pre_commit_problems(change):
    return binary_problems(change) + cross_import_problems(change) + comment_problems(change)


def main(argv=None):
    ap = argparse.ArgumentParser(description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["commit-msg", "pre-commit"])
    ap.add_argument("message_file", nargs="?")
    ap.add_argument("--repo", default=".")
    args = ap.parse_args(argv)
    try:
        repo = (git(args.repo, "rev-parse", "--show-toplevel") or "").strip()
        if not repo:
            print("repo_check: not a git repo, skipped", file=sys.stderr)
            return 0
        if in_progress(repo):
            return 0
        change = Change(repo)
        if args.mode == "commit-msg":
            if not args.message_file:
                ap.error("commit-msg needs the message file")
            with open(args.message_file, encoding="utf-8", errors="replace") as f:
                problems = commit_msg_problems(change, f.read())
        else:
            problems = pre_commit_problems(change)
    except Exception as e:
        print(f"repo_check: skipped, internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 0
    for line in problems:
        print(line, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
