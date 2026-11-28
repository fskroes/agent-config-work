#!/usr/bin/env python3
USAGE = """Keep and read a decision log: the TSV trail of why each decision of a task was made.

A log has six tab-separated columns: ts, phase, decision, why, evidence, result. A medium or large task
commits it at .decisions/<task-slug>.tsv in the repo the work changes, so the reason for a value travels
with the code and a later agent finds it with one command (`why`). Rows are append-only: a wrong row gets a
new row that supersedes it.

  decisions.py log <file> <phase> <decision> <why> <evidence> <result>
  decisions.py check <file> [<file> ...]
  decisions.py why <term> [--repo PATH]
  decisions.py transcript [--project-dir DIR] [--session ID]

log         appends one row. Stamps ts in UTC (2026-11-28T09:02:00Z), writes the header when the file is
            empty or new, makes every cell one line (tab, CR, LF and other line breaks become a space),
            prefixes a cell that starts with = + - or @ with a single quote, and refuses an empty phase,
            decision, why or evidence. It opens the file for append only and holds an exclusive lock
            while it writes, so two agents that append at once each get a whole row and one header.
            It keeps the line ends of the file (CRLF stays CRLF) and adds a line break first when the
            last row has none. It never rewrites or truncates a row.
check       reads each log and prints one line per bad row: wrong header, wrong cell count, bad ts,
            empty phase, decision, why or evidence, a commit SHA in evidence that is not a commit of
            the repo that holds the log, a path or path:line in evidence that does not exist there.
            Evidence tokens: a hex word of 7 to 12 or of 40 characters with a digit and a letter is a SHA; a
            word with a / or a file name that ends in a common source, doc or data extension (.py, .sh,
            .ts, .md, .json, .tsv, .yml, .swift, .go and the like), with an optional :LINE or :START-END,
            is a path, relative to the repo root (or absolute). URLs, #123, plain words, abbreviations
            like e.g and i.e, and library names like Next.js or Node.js are not checked.
            CRLF line ends are read like LF.
why         prints every row of .decisions/*.tsv whose cells hold the term (any case): rows in the
            working tree first, then rows that are deleted from the tree but still in git history, each
            with the file (and commit) it came from. --repo defaults to the repo of the current folder.
transcript  prints the path of this run's Claude Code transcript:
            ~/.claude/projects/<project dir with every non-alphanumeric character as ->/<session id>.jsonl.
            The session id comes from CLAUDE_CODE_SESSION_ID, the project dir from CLAUDE_PROJECT_DIR,
            else the current folder. It never lists or reads other transcripts.

Examples:
  decisions.py log .decisions/csv-export.tsv build "batch size 37" "the Utrecht label printer jams at 38" \\
      "ops/printer.log:12, commit 3a9f1c2" "tests green"
  decisions.py check .decisions/csv-export.tsv
  decisions.py why BATCH_SIZE --repo ~/dev/shop
  column -s$'\\t' -t .decisions/csv-export.tsv     render a log in a terminal

Exit: 0 done (check: every row good; why: at least one row matched), 1 check found a bad row, why matched
nothing, or transcript is missing, 2 bad usage or a file that cannot be read or written.
"""
import argparse
import datetime as dt
import fcntl
import os
import re
import subprocess
import sys

HEADER = ("ts", "phase", "decision", "why", "evidence", "result")
FORMULA_START = ("=", "+", "-", "@")
LINE_BREAKS = re.compile(r"[\t\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SHA_RE = re.compile(r"^(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])(?:[0-9a-f]{7,12}|[0-9a-f]{40})$")
LINE_SUFFIX = re.compile(r"^(.+?)(?::(\d+)(?:-(\d+))?|#L(\d+)(?:-L(\d+))?)$")
FILE_EXTENSIONS = (
    "py", "pyi", "sh", "bash", "zsh", "fish", "js", "mjs", "cjs", "ts", "mts", "tsx", "jsx", "vue", "svelte",
    "md", "mdx", "rst", "txt", "json", "jsonl", "ndjson", "tsv", "csv", "yml", "yaml", "toml", "ini", "cfg",
    "conf", "env", "lock", "xml", "plist", "html", "htm", "css", "scss", "sass", "less", "sql", "swift", "m",
    "mm", "h", "hpp", "c", "cc", "cpp", "go", "rs", "rb", "java", "kt", "kts", "gradle", "php", "pl", "lua",
    "r", "ipynb", "tmpl", "tpl", "j2", "jinja", "mustache", "svg", "png", "jpg", "jpeg", "gif", "webp", "pdf",
    "log", "diff", "patch", "pbxproj", "xcconfig", "entitlements", "storyboard", "xib", "strings",
)
FILE_NAME = re.compile(r"^[\w.-]*\w\.(?:" + "|".join(FILE_EXTENSIONS) + r")$", re.IGNORECASE)
ABBREVIATION = re.compile(r"^[A-Za-z]\.[A-Za-z]$")
LIBRARY_NAME = re.compile(r"^[A-Z][A-Za-z0-9]*\.js$")
TOKEN_SPLIT = re.compile(r"[\s,;]+")
TOKEN_TRIM = "`'\"()[]<>"
REQUIRED = ("phase", "decision", "why", "evidence")
LOG_DIR = ".decisions"


class UsageError(Exception):
    pass


def clean(value):
    v = LINE_BREAKS.sub(" ", value).strip()
    return "'" + v if v.startswith(FORMULA_START) else v


def utc_now():
    return dt.datetime.now(dt.timezone.utc).strftime(TS_FORMAT)


def split_lines(data):
    lines = data.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def append_row(path, cells, now=utc_now):
    row = [now()] + [clean(c) for c in cells]
    missing = [name for name, value in zip(HEADER, row) if name in REQUIRED and not value]
    if missing:
        raise UsageError(f"empty {', '.join(missing)}: every row needs a phase, a decision, a why and evidence")
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        eol, prefix = "\n", ""
        if size:
            with open(path, "rb") as f:
                first = f.readline()
                f.seek(-1, os.SEEK_END)
                last = f.read(1)
            eol = "\r\n" if first.endswith(b"\r\n") else "\n"
            if last != b"\n":
                prefix = eol
        text = prefix
        if not size:
            text += "\t".join(HEADER) + eol
        text += "\t".join(row) + eol
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    return row


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def repo_root(path):
    r = git(path, "rev-parse", "--show-toplevel")
    return r.stdout.strip() if r.returncode == 0 else None


def is_commit(repo, sha):
    return git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def line_count(path):
    with open(path, "rb") as f:
        data = f.read()
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def evidence_problems(evidence, repo, base):
    problems = []
    for raw in TOKEN_SPLIT.split(evidence):
        token = raw.strip(TOKEN_TRIM).rstrip(".:").strip(TOKEN_TRIM)
        if not token or "://" in token or token.startswith("#"):
            continue
        if SHA_RE.match(token):
            if repo is None:
                problems.append(f"commit {token} cannot be checked: the log is not in a git repo")
            elif not is_commit(repo, token):
                problems.append(f"commit {token} is not in the repo")
            continue
        m = LINE_SUFFIX.match(token)
        name, start, end = (m.group(1), m.group(2) or m.group(4), m.group(3) or m.group(5)) if m else (token, None, None)
        if ABBREVIATION.match(name) or not ("/" in name or (FILE_NAME.match(name) and not LIBRARY_NAME.match(name))):
            continue
        full = os.path.join(base, os.path.expanduser(name))
        if not os.path.exists(full):
            problems.append(f"path {name} does not exist")
            continue
        if start is not None:
            if os.path.isdir(full):
                problems.append(f"{token}: {name} is a folder, it has no lines")
                continue
            last = int(end or start)
            n = line_count(full)
            if int(start) < 1 or last < int(start) or last > n:
                problems.append(f"{token}: {name} has {n} lines")
    return problems


def check_lines(lines, repo, base):
    bad = []
    if not lines:
        return [(1, "the file is empty, it needs the header line")]
    if tuple(lines[0].split("\t")) != HEADER:
        bad.append((1, "wrong header, want: " + " ".join(HEADER)))
    for n, line in enumerate(lines[1:], 2):
        cells = line.split("\t")
        if tuple(cells) == HEADER:
            bad.append((n, "header repeated inside the log"))
            continue
        if len(cells) != len(HEADER):
            bad.append((n, f"{len(cells)} cells, want {len(HEADER)}"))
            continue
        row = dict(zip(HEADER, cells))
        if not TS_RE.match(row["ts"]):
            bad.append((n, f"bad ts {row['ts']!r}, want UTC like 2026-11-28T09:02:00Z"))
        else:
            try:
                dt.datetime.strptime(row["ts"], TS_FORMAT)
            except ValueError:
                bad.append((n, f"bad ts {row['ts']!r}, not a real time"))
        for name in REQUIRED:
            if not row[name].strip():
                bad.append((n, f"empty {name}"))
        for p in evidence_problems(row["evidence"], repo, base):
            bad.append((n, f"evidence: {p}"))
    return bad


def check_file(path):
    try:
        with open(path, "rb") as f:
            data = f.read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise UsageError(f"cannot read {path}: {e}")
    folder = os.path.dirname(os.path.abspath(path))
    repo = repo_root(folder)
    return check_lines(split_lines(data), repo, repo or folder)


def parse_rows(data):
    lines = split_lines(data)
    out = []
    for n, line in enumerate(lines, 1):
        cells = line.split("\t")
        if tuple(cells) == HEADER or not line.strip():
            continue
        out.append((n, line, cells))
    return out


def matches(cells, term):
    t = term.casefold()
    return any(t in c.casefold() for c in cells)


def tree_rows(repo):
    folder = os.path.join(repo, LOG_DIR)
    found = []
    if not os.path.isdir(folder):
        return found
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not name.endswith(".tsv") or not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            data = f.read().decode("utf-8", errors="replace")
        for n, line, cells in parse_rows(data):
            found.append((f"{LOG_DIR}/{name}:{n}", line, cells))
    return found


def history_rows(repo, present):
    r = git(repo, "log", "-p", "--no-color", "--no-renames", "--no-ext-diff", "--format=@@commit %H %cs",
            "--", f"{LOG_DIR}/*.tsv")
    if r.returncode:
        return []
    found, seen, commit, day, path = [], set(), "", "", ""
    for raw in r.stdout.split("\n"):
        if raw.startswith("@@commit "):
            _, commit, day = raw.split(" ", 2)
        elif raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else ""
        elif raw.startswith("--- "):
            continue
        elif raw.startswith("+") and path:
            line = raw[1:].rstrip("\r")
            cells = line.split("\t")
            if tuple(cells) == HEADER or not line.strip() or line in present or line in seen:
                continue
            seen.add(line)
            found.append((f"{path}@{commit[:10]} ({day}, deleted from the tree)", line, cells))
    return found


def show(where, cells):
    print(where)
    names = HEADER if len(cells) == len(HEADER) else [f"cell{i}" for i in range(1, len(cells) + 1)]
    for name, value in zip(names, cells):
        print(f"  {name:<9} {value}")


def cmd_why(term, repo_arg):
    if not term.strip():
        raise UsageError("why needs a non-empty term")
    start = os.path.abspath(os.path.expanduser(repo_arg or os.getcwd()))
    if not os.path.isdir(start):
        raise UsageError(f"no folder {start}")
    repo = repo_root(start)
    in_git = repo is not None
    repo = repo or start
    tree = tree_rows(repo)
    present = {line for _, line, _ in tree}
    history = history_rows(repo, present) if in_git else []
    hits = [(w, c) for w, _, c in tree + history if matches(c, term)]
    for i, (where, cells) in enumerate(hits):
        if i:
            print()
        show(where, cells)
    if hits:
        return 0
    where = f"{repo}/{LOG_DIR}/*.tsv"
    scope = f"{len(tree)} rows in the tree, {len(history)} deleted rows in git history" if in_git else \
        f"{len(tree)} rows in the tree, no git history (not a git repo)"
    print(f"decisions: no row matches {term!r} in {where} ({scope}). No logged reason; try a shorter term, "
          f"then `git log -S {term!r}` and the why skill.", file=sys.stderr)
    return 1


def cmd_transcript(project_dir, session):
    session = session or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9-]+", session or ""):
        raise UsageError("no session id: set CLAUDE_CODE_SESSION_ID or pass --session")
    base = os.path.abspath(os.path.expanduser(project_dir or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()))
    munged = re.sub(r"[^A-Za-z0-9]", "-", base)
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    path = os.path.join(home, "projects", munged, f"{session}.jsonl")
    print(path)
    if os.path.isfile(path):
        return 0
    print(f"decisions: {path} does not exist; pass --project-dir with the folder this session started in",
          file=sys.stderr)
    return 1


def parser():
    p = argparse.ArgumentParser(prog="decisions.py", description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("log", help="append one row")
    lg.add_argument("file")
    for name in HEADER[1:]:
        lg.add_argument(name)
    ck = sub.add_parser("check", help="exit 1 with a reason per bad row")
    ck.add_argument("files", nargs="+")
    wh = sub.add_parser("why", help="print the rows whose cells hold a term, tree and git history")
    wh.add_argument("term")
    wh.add_argument("--repo")
    tr = sub.add_parser("transcript", help="print this run's transcript path")
    tr.add_argument("--project-dir")
    tr.add_argument("--session")
    return p


def cmd_log(args):
    if args[:1] == ["--"]:
        args = args[1:]
    if len(args) != len(HEADER):
        raise UsageError(f"log takes {len(HEADER)} arguments, got {len(args)}: "
                         "log <file> <phase> <decision> <why> <evidence> <result>")
    try:
        append_row(args[0], args[1:])
    except OSError as e:
        raise UsageError(f"cannot write {args[0]}: {e}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["log"] and not set(argv[1:2]) & {"-h", "--help"}:
        try:
            return cmd_log(argv[1:])
        except UsageError as e:
            print(f"decisions: {e}", file=sys.stderr)
            return 2
    a = parser().parse_args(argv)
    try:
        if a.cmd == "check":
            code = 0
            for path in a.files:
                for n, reason in check_file(path):
                    print(f"{path}:{n}: {reason}")
                    code = 1
            return code
        if a.cmd == "why":
            return cmd_why(a.term, a.repo)
        return cmd_transcript(a.project_dir, a.session)
    except UsageError as e:
        print(f"decisions: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
