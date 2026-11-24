import bisect
import os
import re
from collections import namedtuple

Hit = namedtuple("Hit", "line end_line kind text allowed")
Found = namedtuple("Found", "start end text kind block repl open", defaults=(False,))


class Unreadable(Exception):
    pass


class Unsafe(Exception):
    pass


LANGUAGE_EXTENSIONS = {
    "python": ("py", "pyi"),
    "shell": ("sh", "bash", "zsh", "bats"),
    "ruby": ("rb",),
    "perl": ("pl",),
    "toml": ("toml",),
    "yaml": ("yaml", "yml"),
    "conf": ("conf", "cfg", "ini"),
    "nix": ("nix",),
    "r": ("r",),
    "javascript": ("js", "mjs", "cjs", "jsx"),
    "typescript": ("ts", "mts", "cts"),
    "tsx": ("tsx",),
    "swift": ("swift",),
    "c": ("c", "h", "cc", "cpp", "hpp", "m", "mm"),
    "java": ("java",),
    "kotlin": ("kt", "kts"),
    "go": ("go",),
    "rust": ("rs",),
    "csharp": ("cs",),
    "php": ("php",),
    "css": ("css",),
    "scss": ("scss",),
    "less": ("less",),
    "jsonc": ("jsonc", "json5"),
    "sql": ("sql",),
    "lua": ("lua",),
    "haskell": ("hs",),
    "markup": ("html", "htm", "xml", "svg", "vue"),
    "lisp": ("el", "lisp"),
    "asm": ("asm",),
}
EXTENSION_LANGUAGE = {ext: lang for lang, exts in LANGUAGE_EXTENSIONS.items() for ext in exts}
NAME_LANGUAGE = {
    "Makefile": "make", "makefile": "make", "GNUmakefile": "make", "Dockerfile": "docker",
    "Brewfile": "ruby", "Gemfile": "ruby", "Rakefile": "ruby", "Podfile": "ruby",
    ".zshrc": "shell", ".bashrc": "shell", ".bash_profile": "shell", ".profile": "shell", ".zprofile": "shell",
    ".zshenv": "shell", ".envrc": "shell",
}
SHEBANG_LANGUAGE = {
    "sh": "shell", "bash": "shell", "zsh": "shell", "dash": "shell", "ksh": "shell", "ash": "shell",
    "python": "python", "python3": "python", "ruby": "ruby", "perl": "perl", "node": "javascript",
    "deno": "javascript", "bun": "javascript", "lua": "lua", "tsx": "tsx", "ts-node": "typescript",
    "uv": "python", "pipx": "python",
}
SHEBANG = re.compile(r"#![ \t]*(?:\S*/)?(?:env[ \t]+(?:-\S+[ \t]+)*)?([A-Za-z][\w.+-]*)")


def language(path, text=None):
    base = os.path.basename(path or "")
    if base in NAME_LANGUAGE:
        return NAME_LANGUAGE[base]
    if "." in base.lstrip("."):
        return EXTENSION_LANGUAGE.get(base.rsplit(".", 1)[-1].lower())
    if base.startswith(".") and base in NAME_LANGUAGE:
        return NAME_LANGUAGE[base]
    if text and text.startswith("#!"):
        m = SHEBANG.match(text)
        if m:
            name = re.sub(r"[\d.]+$", "", m.group(1)) if m.group(1).startswith("python") else m.group(1)
            return SHEBANG_LANGUAGE.get(m.group(1)) or SHEBANG_LANGUAGE.get(name)
    return None


_ID = r"[\w\-./@]+"
_CODE = r"[A-Za-z]+\d*"


def _list(item):
    return rf"{item}(?:\s*,\s*{item})*"


ALLOWED_COMMENT = re.compile("|".join((
    r"-\*-.*-\*-", r"coding[:=]\s*[-\w.]+",
    rf"noqa(?::\s*{_list(_CODE)})?", r"type:\s*ignore(?:\[[\w\-, ]+\])?", r"type:\s*[A-Za-z_][\w.\[\], |\"']*",
    r"pragma:\s*(?:no\s+(?:cover|branch)|nocover|nobranch)",
    rf"pylint:\s*(?:skip-file|(?:disable|enable|disable-next|disable-line)\s*=\s*{_list(_ID)})",
    rf"(?:mypy|pyright):\s*{_list(r'[\w\-]+(?:=[\w\-.]+)?')}",
    r"fmt:\s*(?:off|on|skip)", r"isort:\s*[\w\-]+",
    rf"eslint-(?:disable|enable)(?:-next-line|-line)?(?:\s+{_list(_ID)})?", r"eslint-env\s+[\w,\s\-]+",
    r"@ts-(?:expect-error|ignore|nocheck|check)", r"@(?:vitest|jest)-environment\s+[\w\-]+",
    r"/\s*<reference\s[^<>]*?/?>", r"prettier-ignore", r"swift-tools-version\s*:\s*\S+",
    r"swiftlint:(?:disable|enable)(?::(?:next|this|previous))?(?:\s+[a-z_]+)*",
    r"swift-format-ignore(?::\s*[\w, ]+)?", r"sourcery:\s*\S+(?:\s+[\w.=,\"'\-]+)*",
    r"periphery:ignore(?::\w+)?", r"swiftformat:\w+(?:\s+[\w,:]+)?",
    rf"shellcheck(?:\s+(?:disable={_list(r'SC\d+')}|source=\S+|source-path=\S+|shell=\w+))+",
    r"istanbul ignore (?:next|if|else|file)", r"(?:c8|v8) ignore (?:next|start|stop|file)(?:\s+\d+)?",
    r"nolint(?:nextline|begin|end)?(?::\s*" + _list(r"[\w\-]+") + r")?(?:\([\w\-, ]+\))?",
    r"go:build\s+!?\(?[\w.]+\)?(?:\s*(?:&&|\|\|)\s*!?\(?[\w.]+\)?)*", r"go:generate\s+\S.*",
    r"go:(?:embed|linkname|noinline|nosplit|noescape|norace|nocheckptr|nowritebarrier|uintptrescapes)\b[\w.\s/*\-]*",
    r"\+build\s+[\w.!, ]+", r"lint:ignore\s+\S.*", r"#?nosec(?:\s+G\d+(?:\s*,\s*G\d+)*)?",
    r"yaml-language-server:\s*\$schema=\S+", r"cloud-config", r"@license\b.*", r"@preserve\b.*",
    r"biome-ignore(?:-all|-start|-end)?(?:\s+[\w/\-.]+)*(?::.*)?", r"stylelint-(?:disable|enable)(?:-next-line|-line)?(?:\s+[\w/\-,]+)*",
    r"webpack\w+:\s*.+", r"@vite-ignore", r"@refresh\s+reset", r"@jsx\w*\s+\S+", r"@flow(?:\s+\w+)?",
    r"[#@]\s*sourceMappingURL=\S+", r"[#@]\s*sourceURL=\S+", r"[#@]__(?:PURE|NO_SIDE_EFFECTS)__",
    r"rubocop:(?:disable|enable|todo)\s+[\w/, ]+", r"frozen_string_literal:\s*\w+", r"typed:\s*\w+",
    r"syntax\s*=\s*\S+", r"escape\s*=\s*\S", r"check\s*=\s*\S+", r"sqlfluff:[\w:=\-,. ]+",
    r"clang-format\s+(?:off|on)", r"IWYU pragma:\s*[\w ]+", r"CHECKSTYLE:(?:OFF|ON)", r"@formatter:(?:off|on)",
    r"noinspection\s+[\w, ]+", r"NOPMD", r"NOSONAR", r"ReSharper\s+(?:disable|restore)\s+\w+(?:\s+\w+)*",
    r"<auto-generated\s*/?>", r"fall(?:s)?[ \-]?through\.?", r"htmlmin:ignore", r"@vue-ignore",
    r"\[if\b[^\]]*\]>?.*", r"<!\[endif\]", r"#(?:include|echo|exec|set|if|elif|else|endif|config|flastmod|fsize|printenv)\b.*",
    r"personal-data-ok",
)), re.I)
HEADER_COMMENT = re.compile(r"(?:spdx-|copyright\b|\(c\)|license\b|licensed\b|mark:).*", re.I)
REGION = re.compile(r"#\s*(?:end)?region\b.*", re.I)
REGION_BARE = re.compile(r"(?:end)?region\b", re.I)
CGO_EXPORT = re.compile(r"export\s+\w+\Z")
PROSE_WORDS = frozenset((
    "because", "since", "hack", "workaround", "needed", "need", "needs", "due", "so", "we", "it", "is", "are", "the",
    "this", "that", "for", "as", "otherwise", "but", "and", "odd", "why", "reason", "todo", "fixme", "to", "of", "a"))
BIOME = re.compile(r"biome-ignore(?:-all|-start|-end)?\s+[\w/\-.]+(?:\s+[\w/\-.]+)*(?::.*)?", re.I)
LICENSE_BODY = re.compile(r"copyright|spdx-license-identifier|licensed under|all rights reserved|permission is hereby granted",
                          re.I)
BANG_LANGUAGES = frozenset(("shell", "python", "ruby", "perl", "yaml", "lua"))
BLOCK_PREFIXES = {"sql": ("!", "+"), "haskell": ("#",)}


def _directive_only(stripped):
    for part in re.split(r"\s+#\s+", stripped):
        if not ALLOWED_COMMENT.fullmatch(part.strip()):
            return False
        if any(w in PROSE_WORDS for w in re.split(r"[\s,=:]+", part.lower())):
            return False
    return True


def allowed_comment(text):
    stripped = text.strip()
    if HEADER_COMMENT.fullmatch(stripped) or REGION.fullmatch(stripped) or REGION_BARE.match(text) or CGO_EXPORT.match(text):
        return True
    if BIOME.fullmatch(stripped):
        return True
    return _directive_only(stripped)


def directive_head(text):
    stripped = text.strip()
    if allowed_comment(text):
        return None
    if "\n" in stripped:
        lines = [line.strip().lstrip("*").strip() for line in stripped.split("\n")]
        kept = [line for line in lines if line and allowed_comment(line)]
        return "\n".join(kept) or None
    for word in reversed(list(re.finditer(r"\S+", stripped))):
        head = re.sub(r"[\s\-\u2013\u2014:;,|/#]+$", "", stripped[:word.end()])
        if head and _directive_only(head):
            return head
    return None


def allowed(comment_text, lang):
    if lang in BANG_LANGUAGES and comment_text.startswith("!"):
        return True
    if lang == "markup" and "[if" in comment_text[:6]:
        return True
    raw = comment_text.split("\n")
    first = raw[0].lstrip("*") if raw[0].startswith("*") else raw[0]
    rest = [line.strip().lstrip("*").strip() for line in raw[1:]]
    lines = [line for line in [first] + rest if line.strip()]
    if not lines:
        return False
    return all(allowed_comment(line) for line in lines)


_SYMBOLS = r"!#$%&*+./<=>?@\\^|~:"
_CHAR = r"'(?:[^'\\\n]|\\[^\n]+?)'"


def _quoted(quote, escape=r"\\[\s\S]", multiline=False):
    inner = quote + (r"\\" if escape else "") + ("" if multiline else r"\n")
    body = rf"[^{inner}]" + (rf"|{escape}" if escape else "")
    return rf"{quote}(?:{body})*{quote}"


Str = namedtuple("Str", "pattern close escape interp multiline hashes")


def _str(pattern, close, escape=r"\\[\s\S]", interp=(), multiline=False, hashes=None):
    return Str(re.compile(pattern), close, escape, tuple(interp), multiline, hashes)


class Spec:
    def __init__(self, alts, flags=0, stop=None, start=None):
        self.alts = alts
        self.flags = flags
        self.stop = re.compile(stop, re.M) if stop else None
        self.start = re.compile(start) if start else None
        self.table = {name: (action, data) for name, _, action, data in alts}
        self.cache = {}

    def regex(self, frame=None):
        key = frame
        if key not in self.cache:
            inert = frame is not None and frame[2]
            parts = []
            if frame is not None:
                chars = "".join(re.escape(c) for c in {frame[0], frame[1]} if c)
                parts.append(f"(?P<F>[{chars}])")
            for name, pattern, action, _ in self.alts:
                if inert and action in ("line", "heredoc", "nest", "block", "lineblock"):
                    continue
                parts.append(f"(?P<{name}>{pattern})")
            self.cache[key] = re.compile("|".join(parts), self.flags)
        return self.cache[key]


_BODY_CACHE = {}


def _body_regex(st, close, hashes):
    key = (id(st), close, hashes)
    if key not in _BODY_CACHE:
        h = re.escape(hashes)
        parts = [f"(?P<c>{re.escape(close)})"]
        for index, (pattern, _, _, _) in enumerate(st.interp):
            parts.append(f"(?P<i{index}>{pattern.replace('<H>', h)})")
        if st.escape:
            parts.append(f"(?P<e>{st.escape.replace('<H>', h)})")
        if not st.multiline:
            parts.append(r"(?P<nl>\n)")
        _BODY_CACHE[key] = re.compile("|".join(parts))
    return _BODY_CACHE[key]


REGEX_KEYWORDS = frozenset(("return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do",
                            "else", "yield", "await", "if", "while"))
LOOSE_KEYWORDS = REGEX_KEYWORDS | frozenset((
    "and", "or", "not", "unless", "until", "when", "puts", "print", "split", "grep", "join", "push", "unshift", "x", "lt",
    "gt", "le", "ge", "eq", "ne", "cmp"))
LAST_WORD = re.compile(r"[\w$]+\Z")


def _operand_expected(text, pos, ends, command_arg=False, loose=False):
    j = pos
    while True:
        while j > 0 and text[j - 1] in " \t\r\n":
            j -= 1
        if j in ends:
            j = ends[j]
            continue
        break
    if j == 0:
        return True
    if command_arg and "\n" in text[j:pos]:
        return True
    c = text[j - 1]
    if c == "<" and text[pos - 1:pos] != " " and pos == j:
        return False
    if c in ")]}\"'`":
        return False
    if c.isalnum() or c in "_$@":
        word = LAST_WORD.search(text, max(0, j - 30), j)
        if word and word.group() == "def":
            return False
        if word and word.group() in (LOOSE_KEYWORDS if loose else REGEX_KEYWORDS):
            return True
        if command_arg and pos > j and text[pos - 1] in " \t" and text[pos + 1:pos + 2] not in (" ", "\t", "=", "\n", ""):
            return word is not None and word.group()[0].islower()
        return False
    if c in "+-" and j >= 2 and text[j - 2] == c:
        return False
    return True


PAIRS = {"(": ")", "[": "]", "{": "}", "<": ">"}


def _delimited(text, pos, opener, hi):
    closer = PAIRS.get(opener, opener)
    pattern = re.compile(r"[\\" + re.escape(opener) + re.escape(closer) + "]")
    depth = 0
    j = pos
    while j < hi:
        m = pattern.search(text, j, hi)
        if not m:
            return None
        c = m.group()
        if c == "\\":
            j = m.end() + 1
        elif c == closer and depth == 0:
            return m.end()
        elif opener != closer and c == opener:
            depth += 1
            j = m.end()
        else:
            depth -= 1
            j = m.end()
    return None


SHELL_NAMES = frozenset(("bash", "sh", "zsh", "dash", "ksh"))
SHELL_WRAPPERS = frozenset(("sudo", "env", "exec", "time", "nohup", "command", "nice", "doas", "stdbuf", "timeout", "setsid"))
SSH_VALUE_OPTIONS = frozenset("bcDEeFIiJLlmOopQRSWw")
COMMAND_BREAKS = frozenset(("|", "||", "&&", ";", "&", ";;", "|&"))
GROUP_OPENERS = frozenset(("{", "(", "do", "then", "else", "!", "while", "until", "for", "if"))
GROUP_CLOSERS = frozenset(("}", ")", "done", "fi", "esac"))
STDIN_FILES = frozenset(("/dev/stdin", "/proc/self/fd/0", "/dev/fd/0"))


def _command_name(word):
    return os.path.basename(word)


def _runs_shell(words):
    index = 0
    while index < len(words) and (re.fullmatch(r"[A-Za-z_]\w*=.*", words[index]) or words[index] in SHELL_WRAPPERS
                                  or words[index].startswith("-")):
        index += 2 if words[index] in ("-u", "-g", "-h", "-C", "-p") else 1
    if index >= len(words):
        return False
    name = _command_name(words[index])
    rest = words[index + 1:]
    if name in SHELL_NAMES:
        return True
    if name in ("source", ".") and rest[:1] and rest[0] in STDIN_FILES:
        return True
    if name == "eval" and any(re.search(r"\$\(\s*cat\s*\)|`\s*cat\s*`", word) for word in rest):
        return True
    if name == "ssh":
        position = 0
        host = False
        while position < len(rest):
            word = rest[position]
            if word.startswith("-") and not host:
                position += 2 if len(word) == 2 and word[1] in SSH_VALUE_OPTIONS else 1
                continue
            if not host:
                host = True
                position += 1
                continue
            break
        remote = rest[position:]
        return host and (not remote or _runs_shell(" ".join(remote).split()))
    if name.startswith("$"):
        return any(_runs_shell(word.split()) for word in rest)
    return False


def _reader_runs_shell(text, marker, delimiter):
    import shlex
    start = text.rfind("\n", 0, marker) + 1
    while start > 0 and text[start - 2:start - 1] == "\\":
        start = text.rfind("\n", 0, start - 1) + 1
    end = text.find("\n", marker)
    line = text[start:len(text) if end == -1 else end].replace("\\\n", " ")
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    segments = [[]]
    piped = [False]
    for token in tokens:
        if token in COMMAND_BREAKS:
            segments.append([])
            piped.append(token in ("|", "|&"))
        else:
            segments[-1].append(token)
    for number, words in enumerate(segments):
        owns = any(w in ("<<" + delimiter, "<<-" + delimiter) or (w in ("<<", "<<-") and words[i + 1:i + 2] in ([delimiter], ["-" + delimiter]))
                   for i, w in enumerate(words))
        if owns:
            cleaned = []
            skip = False
            for word in words:
                if skip:
                    skip = False
                elif word in ("<<", "<<-"):
                    skip = True
                elif not (word.startswith("<<") and not word.startswith("<<<")):
                    cleaned.append(word)
            if any(w in (">", ">>") or w.startswith((">", "&>")) for w in cleaned):
                return False
            if _runs_shell(_unwrapped(cleaned)):
                return True
            if cleaned and cleaned[0] in GROUP_CLOSERS and any(_runs_shell(_unwrapped(group)) for group in _group_before(segments, number)):
                return True
            following = number + 1
            while following < len(segments) and piped[following]:
                if _runs_shell(_unwrapped(segments[following])):
                    return True
                following += 1
            return False
    return False


def _unwrapped(words):
    start, end = 0, len(words)
    while start < end and words[start] in GROUP_OPENERS:
        start += 1
    while end > start and words[end - 1] in GROUP_CLOSERS:
        end -= 1
    return words[start:end]


def _group_before(segments, number):
    group = []
    for words in reversed(segments[:number]):
        group.append(words)
        if words and words[0] in GROUP_OPENERS - {"then", "else", "!"}:
            break
    return group


def _skip_heredocs(text, newline, pending, hi):
    pos = newline + 1
    last = newline
    bodies = []
    for delimiter, indent, loose, shell in pending:
        lead = r"[ \t]*" if indent else ""
        tail = r"(?!\w)" if loose else r"[ \t]*\r?$"
        end = re.compile(rf"^{lead}{re.escape(delimiter)}{tail}", re.M).search(text, pos, hi)
        if not end:
            return None
        if shell:
            bodies.append((pos, end.start()))
        last = text.find("\n", end.end(), hi)
        last = hi if last == -1 else last
        pos = last + 1
    return last, bodies


JSX_TEXT = re.compile(r"(?P<close></)|(?P<open><(?=[A-Za-z_$>]))|(?P<expr>\{)")
JSX_TAG = re.compile(r"""(?P<self>/>)|(?P<end>>)|(?P<str>"[^"]*"|'[^']*')|(?P<expr>\{)|(?P<line>//[^\n]*)|(?P<block>/\*[\s\S]*?\*/)""")


def _jsx_step(text, i, hi, stack, found, ends):
    top = stack[-1]
    m = (JSX_TEXT if top[0] == "x" else JSX_TAG).search(text, i, hi)
    if not m:
        return hi
    g = m.lastgroup
    if g == "expr":
        stack.append(["c", "{", "}", 0, False])
    elif g == "open":
        stack.append(["t", False])
    elif g == "close":
        stack.pop()
        stack.append(["t", True])
    elif g == "self":
        stack.pop()
    elif g == "end":
        if top[1]:
            stack.pop()
        else:
            stack[-1] = ["x"]
    elif g in ("line", "block"):
        start, end = m.start(), m.end()
        if g == "line":
            found.append(Found(start, end, text[start + 2:end], "comment", False, None))
        else:
            found.append(Found(start, end, text[start + 2:end - 2], "comment", True, None))
        ends[end] = start
    return m.end()


def _lex(text, spec, lo=0, hi=None):
    hi = len(text) if hi is None else hi
    if spec.start:
        m = spec.start.search(text, lo, hi)
        if not m:
            return []
        lo = m.start()
    if spec.stop:
        m = spec.stop.search(text, lo, hi)
        if m:
            hi = m.start()
    found = []
    ends = {}
    stack = []
    pending = []
    esc_end = -1
    i = lo
    base = spec.regex()
    while i < hi:
        if stack and stack[-1][0] == "s":
            _, st, close, body = stack[-1]
            m = body.search(text, i, hi)
            if not m:
                break
            g = m.lastgroup
            if g == "c":
                i = m.end()
                stack.pop()
            elif g == "e":
                i = m.end()
            elif g == "nl":
                i = m.start()
                stack.pop()
            else:
                _, op, cl, inert = st.interp[int(g[1:])]
                i = m.end()
                stack.append(["c", op, cl, 0, inert])
            continue
        if stack and stack[-1][0] in ("x", "t"):
            i = _jsx_step(text, i, hi, stack, found, ends)
            continue
        if stack:
            frame = stack[-1]
            rx = spec.regex((frame[1], frame[2], frame[4]))
        else:
            rx = base
        m = rx.search(text, i, hi)
        if pending:
            nl = text.find("\n", i, m.start() if m else hi)
            if nl != -1:
                skipped = _skip_heredocs(text, nl, pending, hi)
                pending = []
                if skipped is not None:
                    i = skipped[0]
                    for body_start, body_end in skipped[1]:
                        found.extend(_lex(text, spec, body_start, body_end))
                    continue
        if not m:
            break
        g = m.lastgroup
        if g == "F":
            frame = stack[-1]
            if m.group() == frame[2] and (frame[3] == 0 or frame[1] is None):
                stack.pop()
            elif m.group() == frame[2]:
                frame[3] -= 1
            else:
                frame[3] += 1
            i = m.end()
            continue
        action, data = spec.table[g]
        s = m.start(g)
        if action == "skip":
            i = m.end()
        elif action == "line":
            if s == esc_end:
                i = s + 1
                continue
            e = m.end(g)
            if text[e - 1] == "\r":
                e -= 1
            marker = data if isinstance(data, int) else len(data.match(text, s).group())
            found.append(Found(s, e, text[s + marker:e], "comment", False, None))
            ends[e] = s
            i = m.end()
        elif action == "block":
            e = m.end(g)
            found.append(Found(s, e, text[s + data[0]:e - data[1]], "comment", True, None))
            ends[e] = s
            i = e
        elif action == "unterminated":
            found.append(Found(s, hi, text[m.end():hi], "comment", True, None, True))
            i = hi
        elif action == "nest":
            opener, closer = data
            both = re.compile(f"{opener}|{closer}")
            depth, j = 1, m.end()
            while depth:
                t = both.search(text, j, hi)
                if not t:
                    found.append(Found(s, hi, text[m.end():hi], "comment", True, None, True))
                    j = hi
                    break
                depth += 1 if re.fullmatch(opener, t.group()) else -1
                j = t.end()
            if not found or found[-1].start != s:
                found.append(Found(s, j, text[m.end():max(m.end(), j - len(closer.replace("\\", "")))], "comment", True, None))
            ends[j] = s
            i = j
        elif action == "luablock":
            level = re.compile(r"--\[(=*)\[").match(text, s).group(1)
            end = text.find("]" + level + "]", m.end(), hi)
            if end == -1:
                found.append(Found(s, hi, text[m.end():hi], "comment", True, None, True))
                i = hi
                continue
            e = end + len(level) + 2
            found.append(Found(s, e, text[m.end():end], "comment", True, None))
            ends[e] = s
            i = e
        elif action == "str":
            lm = data.pattern.match(text, s)
            hashes = data.hashes(lm) if data.hashes else ""
            close = data.close(lm) if callable(data.close) else data.close
            stack.append(["s", data, close, _body_regex(data, close, hashes)])
            i = lm.end()
        elif action == "regexlit":
            ok = _operand_expected(text, s, ends, data == "ruby", data is not None)
            i = m.end() if ok else s + 1
        elif action == "heredoc":
            names = m.groupdict()
            shell = data == "shell" and _reader_runs_shell(text, s, names.get("hd"))
            pending.append((names.get("hd") or names.get("hd2"), bool(names.get("hf")) or data == "php", data == "php", shell))
            i = m.end()
        elif action == "escape":
            i = m.end()
            esc_end = -1 if m.group() == "\\\n" else i
        elif action == "push":
            stack.append(["c", data[0], data[1], 0, data[2]])
            i = m.end()
        elif action == "lineblock":
            s, e = m.start("LL"), m.end("LL")
            if text[e - 1] == "\r":
                e -= 1
            found.append(Found(s, e, text[s + 1:e], "comment", False, None))
            ends[e] = s
            i = m.end()
        elif action == "pct":
            i = _percent(text, m, s, ends, hi, data)
        elif action == "jsx":
            if _operand_expected(text, s, ends):
                stack.append(["t", False])
            i = m.end()
        elif action == "phpclose":
            t = re.compile(r"<\?(?:php\b|=)?").search(text, m.end(), hi)
            i = t.end() if t else hi
        else:
            i = m.end()
    return found


def _percent(text, m, s, ends, hi, kind):
    token = m.group(m.lastgroup)
    if kind == "perl":
        name = re.match(r"[a-z]+", token).group()
        if token[len(name):].strip() == "" or text[m.end():m.end() + 1] == ">":
            return m.end()
        j = m.end()
        opener = token[-1]
        parts = 2 if name in ("s", "tr", "y") else 1
        end = _delimited(text, j, opener, hi)
        if end is None:
            return m.end()
        if parts == 2:
            if opener in PAIRS:
                k = end
                while k < hi and text[k] in " \t\n":
                    k += 1
                if k >= hi:
                    return end
                end2 = _delimited(text, k + 1, text[k], hi)
                return end2 or end
            end2 = _delimited(text, end, opener, hi)
            return end2 or end
        return end
    if not _operand_expected(text, s, ends, True, True):
        return m.end()
    end = _delimited(text, m.end(), token[-1], hi)
    return end or m.end()


def _shell_interp():
    return (
        (r"\$\(\(", "(", ")", True), (r"\$\(", "(", ")", False), (r"\$\{", "{", "}", True), (r"`", None, "`", False),
    )


def _spec_js(jsx=False):
    template = _str("`", "`", interp=((r"\$\{", "{", "}", False),), multiline=True)
    alts = [
        ("L1", r"\A#![^\n]*", "line", 1),
        ("L0", r"//[^\n]*", "line", 2),
        ("B0", r"/\*[\s\S]*?\*/", "block", (2, 2)),
        ("U0", r"/\*", "unterminated", None),
        ("S0", _quoted('"'), "skip", None),
        ("S1", _quoted("'"), "skip", None),
        ("S2", r"`(?:[^`\\$]|\\[\s\S]|\$(?!\{))*`", "skip", None),
        ("S3", r"`", "str", template),
        ("R", r"/(?![/*])(?:[^/\\\[\n]|\\[^\n]|\[(?:[^\]\\\n]|\\[^\n])*\])+/[A-Za-z]*", "regexlit", None),
    ]
    if jsx:
        alts.append(("J", r"<(?=[A-Za-z_$>])(?![A-Za-z_$][\w$]*(?:\s+extends\b|\s*,|\s*=[^>]))", "jsx", None))
    return Spec(alts)


def _spec_jsonc(single=False):
    alts = [
        ("L0", r"//[^\n]*", "line", 2),
        ("B0", r"/\*[\s\S]*?\*/", "block", (2, 2)),
        ("U0", r"/\*", "unterminated", None),
        ("S0", _quoted('"'), "skip", None),
    ]
    if single:
        alts.append(("S1", _quoted("'"), "skip", None))
    return Spec(alts)


def _spec_c():
    raw = _str(r'(?:u8|[uUL])?R"([^()\\\s"]{0,16})\(', lambda lm: ")" + lm.group(1) + '"', escape=None, multiline=True)
    alts = [
        ("I0", r"^[ \t]*#[ \t]*(?:include|import|include_next)[ \t]*<[^>\n]*>", "skip", None),
        ("L0", r"//(?:[^\\\n]|\\[\s\S])*", "line", 2),
        ("B0", r"/\*[\s\S]*?\*/", "block", (2, 2)),
        ("U0", r"/\*", "unterminated", None),
        ("R0", r'(?<![\w])(?:u8|[uUL])?R"[^()\\\s"]{0,16}\(', "str", raw),
        ("S0", r'(?:u8|[uUL])?' + _quoted('"'), "skip", None),
        ("C0", r"(?<![\w])(?:u8|[uUL])?" + _CHAR, "skip", None),
    ]
    return Spec(alts, flags=re.M)


def _spec_java():
    block = _str('"""', '"""', multiline=True)
    alts = [
        ("L0", r"//[^\n]*", "line", 2),
        ("B0", r"/\*[\s\S]*?\*/", "block", (2, 2)),
        ("U0", r"/\*", "unterminated", None),
        ("T0", r'"""', "str", block),
        ("S0", _quoted('"'), "skip", None),
        ("C0", r"(?<![\w])" + _CHAR, "skip", None),
    ]
    return Spec(alts)


def _spec_kotlin():
    dollar = ((r"\$\{", "{", "}", False),)
    raw = _str('"""', '"""', escape=None, interp=dollar, multiline=True)
    plain = _str('"', '"', interp=dollar)
    alts = [
        ("L0", r"//[^\n]*", "line", 2),
        ("B0", r"/\*", "nest", (r"/\*", r"\*/")),
        ("T0", r'"""', "str", raw),
        ("S0", r'"(?:[^"\\\n$]|\\[\s\S]|\$(?!\{))*"', "skip", None),
        ("S1", r'"', "str", plain),
        ("C0", r"(?<![\w])" + _CHAR, "skip", None),
    ]
    return Spec(alts)


def _spec_swift():
    interp = ((r"\\<H>\(", "(", ")", False),)
    esc = r"\\<H>[\s\S]"
    multi = _str(r'(#*)"""', lambda lm: '"""' + lm.group(1), escape=esc, interp=interp, multiline=True,
                 hashes=lambda lm: lm.group(1))
    raw = _str(r'(#+)"', lambda lm: '"' + lm.group(1), escape=esc, interp=interp, hashes=lambda lm: lm.group(1))
    plain = _str('"', '"', escape=esc, interp=interp)
    regex = _str(r"(#+)/", lambda lm: "/" + lm.group(1), escape=None, multiline=True)
    alts = [
        ("L0", r"//[^\n]*", "line", 2),
        ("B0", r"/\*", "nest", (r"/\*", r"\*/")),
        ("T0", r'#*"""', "str", multi),
        ("X0", r"#+/", "str", regex),
        ("T1", r'#+"', "str", raw),
        ("S0", r'"(?:[^"\\\n]|\\[^(\n])*"', "skip", None),
        ("S1", r'"', "str", plain),
    ]
    return Spec(alts)


def _spec_go():
    alts = [
        ("L0", r"//[^\n]*", "line", 2),
        ("B0", r"/\*[\s\S]*?\*/", "block", (2, 2)),
        ("U0", r"/\*", "unterminated", None),
        ("S0", _quoted('"'), "skip", None),
        ("S1", r"`[^`]*`", "skip", None),
        ("C0", r"(?<![\w])" + _CHAR, "skip", None),
    ]
    return Spec(alts)


def _spec_rust():
    raw = _str(r'b?r(#*)"', lambda lm: '"' + lm.group(1), escape=None, multiline=True)
    char = r"(?<![\w])b?'(?:[^'\\\n]|\\(?:u\{[^}\n]*\}|x[0-9A-Fa-f]{2}|[^\n]))'"
    alts = [
        ("L0", r"//[^\n]*", "line", 2),
        ("B0", r"/\*", "nest", (r"/\*", r"\*/")),
        ("R0", r'(?<![\w])b?r#*"', "str", raw),
        ("S0", r'b?"(?:[^"\\]|\\[\s\S])*"', "skip", None),
        ("C0", char, "skip", None),
    ]
    return Spec(alts)


def _spec_csharp():
    brace = ((r"\{(?!\{)", "{", "}", False),)
    verbatim_i = _str(r'(?:\$@|@\$)"', '"', escape=r'""|\{\{', interp=brace, multiline=True)
    plain_i = _str(r'\$"', '"', escape=r"\\[\s\S]|\{\{", interp=brace)
    raw = _str(r'\$?"""', '"""', escape=None, multiline=True)
    alts = [
        ("L0", r"//[^\n]*", "line", 2),
        ("B0", r"/\*[\s\S]*?\*/", "block", (2, 2)),
        ("U0", r"/\*", "unterminated", None),
        ("T0", r'\$?"""', "str", raw),
        ("V0", r'@"(?:[^"]|"")*"', "skip", None),
        ("V1", r'(?:\$@|@\$)"', "str", verbatim_i),
        ("I0", r'\$"(?:[^"\\\n{]|\\[\s\S]|\{\{)*"', "skip", None),
        ("I1", r'\$"', "str", plain_i),
        ("S0", _quoted('"'), "skip", None),
        ("C0", r"(?<![\w])" + _CHAR, "skip", None),
    ]
    return Spec(alts)


def _spec_php():
    heredoc = ("H", r"<<<(?P<hq>[\"']?)(?P<hd>\w+)(?P=hq)", "heredoc", "php")
    brace = ((r"\{\$", "{", "}", False),)
    alts = [
        ("L0", r"(?://|#(?!\[))(?:[^\n?]|\?(?!>))*", "line", re.compile(r"//|#")),
        ("B0", r"/\*[\s\S]*?\*/", "block", (2, 2)),
        ("U0", r"/\*", "unterminated", None),
        ("S0", r"'(?:[^'\\]|\\[\s\S])*'", "skip", None),
        ("S1", r'"(?:[^"\\{]|\\[\s\S]|\{(?!\$))*"', "skip", None),
        ("S2", r'"', "str", _str('"', '"', interp=brace, multiline=True)),
        heredoc,
        ("X", r"\?>", "phpclose", None),
    ]
    return Spec(alts, start=r"<\?(?:php\b|=)?")


def _spec_css(line=False):
    alts = []
    if line:
        alts.append(("L0", r"//[^\n]*", "line", 2))
    alts += [
        ("B0", r"/\*[\s\S]*?\*/", "block", (2, 2)),
        ("U0", r"/\*", "unterminated", None),
        ("UR", r"\burl\((?![ \t]*[\"'])[^)\n]*\)", "skip", None),
        ("S0", _quoted('"'), "skip", None),
        ("S1", _quoted("'"), "skip", None),
    ]
    return Spec(alts)


def _spec_sql():
    dollar = _str(r"\$([A-Za-z_]\w*)?\$", lambda lm: "$" + (lm.group(1) or "") + "$", escape=None, multiline=True)
    alts = [
        ("L0", r"--[^\n]*", "line", 2),
        ("B0", r"/\*", "nest", (r"/\*", r"\*/")),
        ("S0", r"[eE]?'(?:[^']|'')*'", "skip", None),
        ("S1", r'"(?:[^"]|"")*"', "skip", None),
        ("S2", r"`[^`]*`", "skip", None),
        ("D0", r"\$(?:[A-Za-z_]\w*)?\$", "str", dollar),
    ]
    return Spec(alts)


def _spec_lua():
    longstr = _str(r"\[(=*)\[", lambda lm: "]" + lm.group(1) + "]", escape=None, multiline=True)
    alts = [
        ("L1", r"\A#![^\n]*", "line", 1),
        ("LB", r"--\[=*\[", "luablock", None),
        ("L0", r"--[^\n]*", "line", 2),
        ("S0", _quoted('"'), "skip", None),
        ("S1", _quoted("'"), "skip", None),
        ("S2", r"\[=*\[", "str", longstr),
    ]
    return Spec(alts)


def _spec_haskell():
    alts = [
        ("B0", r"\{-", "nest", (r"\{-", r"-\}")),
        ("L0", rf"(?<![{_SYMBOLS}])-{{2,}}(?![{_SYMBOLS}\-])[^\n]*", "line", 2),
        ("S0", _quoted('"'), "skip", None),
        ("C0", r"(?<![\w'])'(?:[^'\\\n]|\\[^\n]+?)'", "skip", None),
    ]
    return Spec(alts)


def _spec_lisp(block=True):
    alts = []
    if block:
        alts.append(("B0", r"#\|", "nest", (r"#\|", r"\|#")))
    alts += [
        ("K0", r"#\\[\s\S]\w*", "skip", None),
        ("K1", r"(?<![\w\-])\?\\?[;\"(\[{#]", "skip", None),
        ("S0", r'"(?:[^"\\]|\\[\s\S])*"', "skip", None),
        ("L0", r";[^\n]*", "line", 1),
    ]
    if block:
        alts.append(("P0", r"\|[^|\n]*\|", "skip", None))
    return Spec(alts)


def _spec_asm():
    alts = [
        ("S0", r'"[^"\n]*"', "skip", None),
        ("S1", r"'[^'\n]*'", "skip", None),
        ("L0", r";[^\n]*", "line", 1),
    ]
    return Spec(alts)


def _spec_toml():
    alts = [
        ("S0", r'"""(?:[^"\\]|\\[\s\S]|"(?!""))*"{3,5}', "skip", None),
        ("S1", r"'''[\s\S]*?'{3,5}", "skip", None),
        ("S2", _quoted('"'), "skip", None),
        ("S3", r"'[^'\n]*'", "skip", None),
        ("L0", r"#[^\n]*", "line", 1),
    ]
    return Spec(alts)


def _spec_nix():
    interp = ((r"\$\{", "{", "}", False),)
    indented = _str("''", "''", escape=r"'''|''\$|''\\[\s\S]", interp=interp, multiline=True)
    plain = _str('"', '"', interp=interp, multiline=True)
    alts = [
        ("L0", r"(?<![^\s])#[^\n]*", "line", 1),
        ("B0", r"/\*[\s\S]*?\*/", "block", (2, 2)),
        ("U0", r"/\*", "unterminated", None),
        ("S0", r"''", "str", indented),
        ("S1", r'"', "str", plain),
    ]
    return Spec(alts)


def _spec_r():
    raw = _str(r'[rR]"(-*)\(', lambda lm: ")" + lm.group(1) + '"', escape=None, multiline=True)
    alts = [
        ("R0", r'(?<![\w.])[rR]"-*\(', "str", raw),
        ("S0", r'"(?:[^"\\]|\\[\s\S])*"', "skip", None),
        ("S1", r"'(?:[^'\\]|\\[\s\S])*'", "skip", None),
        ("S2", r"`[^`\n]*`", "skip", None),
        ("L0", r"#[^\n]*", "line", 1),
    ]
    return Spec(alts)


def _spec_conf():
    return Spec([("L0", r"(?:^[ \t]*)(?P<LL>#[^\n]*)", "lineblock", None)], flags=re.M)


def _spec_docker():
    alts = [
        ("H", r"<<(?P<hf>-?)(?P<hq>[\"']?)(?P<hd>\w+)(?P=hq)", "heredoc", None),
        ("L0", r"^[ \t]*(?P<LL>#[^\n]*)", "lineblock", None),
    ]
    return Spec(alts, flags=re.M)


def _spec_ruby():
    interp = ((r"#\{", "{", "}", False),)
    dq = _str('"', '"', interp=interp, multiline=True)
    bt = _str("`", "`", interp=interp, multiline=True)
    alts = [
        ("B0", r"^=begin\b[\s\S]*?^=end\b[^\n]*", "block", (6, 0)),
        ("U0", r"^=begin\b", "unterminated", None),
        ("L0", r"#[^\n]*", "line", 1),
        ("S0", r"'(?:[^'\\]|\\[\s\S])*'", "skip", None),
        ("S1", r'"(?:[^"\\#]|\\[\s\S]|#(?!\{))*"', "skip", None),
        ("S2", r'"', "str", dq),
        ("S3", r"`(?:[^`\\#]|\\[\s\S]|#(?!\{))*`", "skip", None),
        ("S4", r"`", "str", bt),
        ("G", r"\$[^\w\s]", "skip", None),
        ("Q", r"(?<=[\s(,=\[])\?[#/](?=[\s,)\]]|$)", "skip", None),
        ("Y", r"(?<![:\w]):(?:\[\]=?|<=>|===?|=~|\*\*|<<|>>|[/*+\-%<>&|^~!]=?)", "skip", None),
        ("H", r"(?<![\w)\]}])<<(?P<hf>[~-]?)(?P<hq>[\"'`]?)(?P<hd>[A-Za-z_]\w*)(?P=hq)", "heredoc", None),
        ("P", r"%[qQwWiIrsx]?[(\[{<|!/^]", "pct", "ruby"),
        ("R", r"/(?:[^/\\]|\\[\s\S])*?/[a-z]*", "regexlit", "ruby"),
    ]
    return Spec(alts, flags=re.M, stop=r"^__END__\r?$")


def _spec_perl():
    alts = [
        ("B0", r"^=[a-zA-Z]\w*[\s\S]*?^=cut\b[^\n]*", "block", (1, 0)),
        ("B1", r"^=[a-zA-Z]\w*[\s\S]*\Z", "block", (1, 0)),
        ("G", r"\$[#'\"&;,.\\]", "skip", None),
        ("L0", r"#[^\n]*", "line", 1),
        ("S0", r"'(?:[^'\\]|\\[\s\S])*'", "skip", None),
        ("S1", r'"(?:[^"\\]|\\[\s\S])*"', "skip", None),
        ("S2", r"`(?:[^`\\]|\\[\s\S])*`", "skip", None),
        ("H", r"<<(?P<hf>~?)(?:(?P<hq>[\"'])(?P<hd>[^\"'\n]+)(?P=hq)|(?P<hd2>[A-Za-z_]\w*))", "heredoc", None),
        ("P", r"(?<![\w$@%&>\-:])(?:qq|qw|qx|qr|q|m|s|tr|y)[ \t]*[^\w\s,;=)}\]]", "pct", "perl"),
        ("R", r"/(?![/*])(?:[^/\\\n]|\\[^\n])+/[a-z]*", "regexlit", "perl"),
    ]
    return Spec(alts, flags=re.M, stop=r"^__(?:END|DATA)__\r?$")


def _shell_spec():
    dq = _str('"', '"', interp=_shell_interp(), multiline=True)
    alts = [
        ("E", r"\\[\s\S]", "escape", None),
        ("S0", r"'[^']*'", "skip", None),
        ("S1", r"\$'(?:[^'\\]|\\[\s\S])*'", "skip", None),
        ("S2", r'"(?:[^"\\$`]|\\[\s\S]|\$(?![({]))*"', "skip", None),
        ("S3", r'"', "str", dq),
        ("G", r"\$[#?$!@*\-0-9]", "skip", None),
        ("AR", r"\$\(\(", "push", ("(", ")", True)),
        ("A2", r"(?<![\w$])\(\(", "push", ("(", ")", True)),
        ("PE", r"\$\{", "push", ("{", "}", True)),
        ("CS", r"\$\(", "push", ("(", ")", False)),
        ("BT", r"`", "push", (None, "`", False)),
        ("H", r"(?<!<)<<(?!<)(?P<hf>-?)[ \t]*(?P<hq>[\"']?)\\?(?P<hd>[\w.\-]+)(?P=hq)", "heredoc", "shell"),
        ("L0", r"(?<![^\s;&|<>])#[^\n]*", "line", 1),
    ]
    return Spec(alts)


SPEC_BUILDERS = {
    "shell": _shell_spec, "javascript": lambda: _spec_js(True), "typescript": _spec_js, "tsx": lambda: _spec_js(True), "jsonc": lambda: _spec_jsonc(True),
    "c": _spec_c, "java": _spec_java, "kotlin": _spec_kotlin, "swift": _spec_swift, "go": _spec_go,
    "rust": _spec_rust, "csharp": _spec_csharp, "php": _spec_php, "css": _spec_css, "scss": lambda: _spec_css(True),
    "less": lambda: _spec_css(True), "sql": _spec_sql, "lua": _spec_lua, "haskell": _spec_haskell,
    "lisp": _spec_lisp, "asm": _spec_asm, "toml": _spec_toml, "nix": _spec_nix, "r": _spec_r, "conf": _spec_conf,
    "docker": _spec_docker, "ruby": _spec_ruby, "perl": _spec_perl,
}
_SPECS = {}


def _spec_for(lang):
    if lang not in _SPECS:
        _SPECS[lang] = SPEC_BUILDERS[lang]()
    return _SPECS[lang]


PLAIN_STRING = re.compile(r"[rRuU]*(?:'''|\"\"\"|'|\")")


def _python_docstring_text(tokens):
    parts = []
    for token in tokens:
        body = token[1].lstrip("rRuU")
        quote = 3 if body[:3] in ("'''", '"""') else 1
        parts.append(body[quote:-quote])
    return "".join(parts)


def _python_tokens(text):
    import io
    import tokenize
    readline = io.StringIO(text).readline
    try:
        from _tokenize import TokenizerIter
        return list(TokenizerIter(readline, extra_tokens=True))
    except (ImportError, TypeError):
        return list(tokenize.generate_tokens(readline))


def _lex_python(text):
    import tokenize
    try:
        tokens = _python_tokens(text)
    except (tokenize.TokenError, SyntaxError):
        raise Unreadable
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    COMMENT, STRING, NEWLINE, NL = tokenize.COMMENT, tokenize.STRING, tokenize.NEWLINE, tokenize.NL
    INDENT, DEDENT, OP, NAME = tokenize.INDENT, tokenize.DEDENT, tokenize.OP, tokenize.NAME
    ENDMARKER, ENCODING = tokenize.ENDMARKER, tokenize.ENCODING
    found = []
    statement_start = True
    eligible = True
    owner_module = True
    header = False
    depth = 0
    index = 0
    count = len(tokens)
    while index < count:
        token = tokens[index]
        kind = token[0]
        if kind == COMMENT:
            start, end = token[2], token[3]
            found.append(Found(starts[start[0] - 1] + start[1], starts[end[0] - 1] + end[1], token[1][1:], "comment",
                               False, None))
        elif kind == NEWLINE:
            statement_start = True
        elif kind in (NL, INDENT, DEDENT, ENCODING):
            pass
        elif statement_start:
            statement_start = False
            if kind == STRING and eligible and PLAIN_STRING.match(token[1]):
                last = index
                while tokens[last + 1][0] == STRING and PLAIN_STRING.match(tokens[last + 1][1]):
                    last += 1
                after = last + 1
                while tokens[after][0] == COMMENT:
                    after += 1
                joined = tokens[after][0] == OP and tokens[after][1] == ";" and tokens[after + 1][0] not in (NEWLINE, ENDMARKER)
                if joined:
                    start, end = token[2], tokens[after + 1][2]
                    found.append(Found(starts[start[0] - 1] + start[1], starts[end[0] - 1] + end[1],
                                       _python_docstring_text(tokens[index:last + 1]), "docstring", owner_module, ";"))
                    index = after
                    after = -1
                elif tokens[after][0] == OP and tokens[after][1] == ";":
                    last = after
                    after += 1
                if after != -1 and tokens[after][0] in (NEWLINE, ENDMARKER):
                    lookahead = after + 1
                    while lookahead < count and tokens[lookahead][0] in (NL, COMMENT):
                        lookahead += 1
                    sole = lookahead >= count or tokens[lookahead][0] in (DEDENT, ENDMARKER)
                    repl = "" if owner_module else ("pass" if sole else "")
                    start, end = token[2], tokens[last][3]
                    found.append(Found(starts[start[0] - 1] + start[1], starts[end[0] - 1] + end[1],
                                       _python_docstring_text(tokens[index:last + 1]), "docstring", owner_module, repl))
                    index = last
            eligible = False
            owner_module = False
            word = token[1] if kind == NAME else ""
            if word in ("def", "class") or (word == "async" and tokens[index + 1][1] == "def"):
                header = True
                depth = 0
        elif header and kind == OP:
            if token[1] in "([{":
                depth += 1
            elif token[1] in ")]}":
                depth -= 1
            elif token[1] == ":" and depth == 0:
                header = False
                eligible = True
                statement_start = True
        index += 1
    return found


MARKUP_TOKEN = re.compile(r"<!--|<!\[CDATA\[|<\?|<!|</?[A-Za-z][^\s/>]*")
MARKUP_ATTRIBUTES = re.compile(r"""(?:"[^"]*"|'[^']*'|[^>"'])*>?""")
SCRIPT_SKIP_TYPES = re.compile(r"json|template|importmap|speculationrules|x-", re.I)


def _markup_declaration(text, pos, found):
    j = pos
    size = len(text)
    while j < size:
        c = text[j]
        if c == ">":
            return j + 1
        if c in "\"'":
            q = text.find(c, j + 1)
            j = q + 1 if q != -1 else size
        elif text.startswith("<!--", j):
            end = text.find("-->", j + 4)
            if end == -1:
                found.append(Found(j, size, text[j + 4:], "comment", True, None, True))
                return size
            found.append(Found(j, end + 3, text[j + 4:end], "comment", True, None))
            j = end + 3
        else:
            j += 1
    return size


def _lex_markup(text):
    found = []
    i = 0
    size = len(text)
    while True:
        m = MARKUP_TOKEN.search(text, i)
        if not m:
            break
        token = m.group()
        if token == "<!--":
            end = text.find("-->", m.end())
            if end == -1:
                found.append(Found(m.start(), size, text[m.end():], "comment", True, None, True))
                break
            found.append(Found(m.start(), end + 3, text[m.end():end], "comment", True, None))
            i = end + 3
        elif token == "<![CDATA[":
            end = text.find("]]>", m.end())
            i = end + 3 if end != -1 else size
        elif token == "<?":
            end = text.find("?>", m.end())
            i = end + 2 if end != -1 else size
        elif token == "<!":
            i = _markup_declaration(text, m.end(), found)
        else:
            name = token.lstrip("</").lower()
            attributes = MARKUP_ATTRIBUTES.match(text, m.end())
            i = attributes.end()
            if token.startswith("</") or name not in ("script", "style") or attributes.group().endswith("/>"):
                continue
            close = re.compile(rf"</{name}\b", re.I).search(text, i)
            end = close.start() if close else size
            attrs = attributes.group()
            lang = re.search(r"""\blang\s*=\s*["']?(\w+)""", attrs)
            if name == "script":
                kind = re.search(r"""\btype\s*=\s*["']?([\w/+.\-]+)""", attrs)
                if "src=" in attrs or (kind and SCRIPT_SKIP_TYPES.search(kind.group(1))):
                    i = end
                    continue
                spec = _spec_for("tsx" if lang and lang.group(1) in ("ts", "tsx") else "javascript")
            else:
                spec = _spec_for("scss" if lang and lang.group(1) in ("scss", "less") else "css")
            try:
                found.extend(_lex(text, spec, i, end))
            except Unreadable:
                pass
            i = end
    return found


YAML_QUOTE = re.compile(r"[\"'#]")
YAML_BLOCK = re.compile(r"(?:^|\s)[|>][+\-0-9]*[ \t]*$")


def _yaml_close(body, start, quote):
    j = start
    while True:
        q = body.find(quote, j)
        if q == -1:
            return -1
        if quote == '"':
            backslashes = 0
            k = q - 1
            while k >= start and body[k] == "\\":
                backslashes += 1
                k -= 1
            if backslashes % 2:
                j = q + 1
                continue
            return q
        if body[q + 1:q + 2] == "'":
            j = q + 2
            continue
        return q


def _lex_yaml(text):
    found = []
    base = 0
    block_parent = None
    quote = None
    for line in text.split("\n"):
        body = line.rstrip("\r")
        stripped = body.lstrip(" ")
        indent = len(body) - len(stripped)
        here = base
        base += len(line) + 1
        if block_parent is not None:
            if stripped == "" or indent > block_parent:
                continue
            block_parent = None
        k = 0
        if quote:
            end = _yaml_close(body, 0, quote)
            if end == -1:
                continue
            k = end + 1
            quote = None
        cut = len(body)
        while True:
            m = YAML_QUOTE.search(body, k)
            if not m:
                break
            c, p = m.group(), m.start()
            if c == "#":
                if p == 0 or body[p - 1] in " \t":
                    found.append(Found(here + p, here + len(body), body[p + 1:], "comment", False, None))
                    cut = p
                    break
                k = p + 1
            elif p == 0 or body[p - 1] in " \t[{,:":
                end = _yaml_close(body, p + 1, c)
                if end == -1:
                    quote = c
                    cut = -1
                    break
                k = end + 1
            else:
                k = p + 1
        if cut != -1 and YAML_BLOCK.search(body[:cut]):
            block_parent = indent
    return found


MAKE_RECIPE = re.compile(r"""'[^']*'|"(?:[^"\\]|\\.)*"|\\.|(?P<c>(?<![^\s;&|<>])#.*)""")
MAKE_PLAIN = re.compile(r"\\.|(?P<c>#.*)")


def _lex_make(text):
    found = []
    base = 0
    recipe = False
    continued = False
    for line in text.split("\n"):
        body = line.rstrip("\r")
        here = base
        base += len(line) + 1
        if not continued:
            recipe = body.startswith("\t")
        for m in (MAKE_RECIPE if recipe else MAKE_PLAIN).finditer(body):
            if m.group("c") is not None:
                found.append(Found(here + m.start("c"), here + m.end("c"), m.group("c")[1:], "comment", False, None))
                break
        continued = (len(body) - len(body.rstrip("\\"))) % 2 == 1
    return found


def _find_all(text, lang):
    shift = 1 if text.startswith("﻿") else 0
    body = text[shift:]
    if lang == "python":
        found = _lex_python(body)
    elif lang == "markup":
        found = _lex_markup(body)
    elif lang == "yaml":
        found = _lex_yaml(body)
    elif lang == "make":
        found = _lex_make(body)
    else:
        found = _lex(body, _spec_for(lang))
    if shift:
        found = [f._replace(start=f.start + 1, end=f.end + 1) for f in found]
    found.sort(key=lambda f: f.start)
    return found


def _line_starts(text):
    return [0] + [m.end() for m in re.finditer("\n", text)]


def _line_of(starts, offset):
    return bisect.bisect_right(starts, offset)


def _normal(text):
    return " ".join(part for part in (line.strip().lstrip("*").strip() for line in text.split("\n")) if part)


def _spans_in_lines(text, found):
    starts = _line_starts(text)
    return [(_line_of(starts, f.start), _line_of(starts, max(f.start, f.end - 1))) for f in found]


def _mark_license_runs(text, found, flags, lines):
    covered = set()
    for first, last in lines:
        covered.update(range(first, last + 1))
    source_lines = text.split("\n")
    index = 0
    while index < len(found):
        run_end = index
        while run_end + 1 < len(found) and not found[run_end].block and not found[run_end + 1].block \
                and lines[run_end + 1][0] == lines[run_end][1] + 1:
            run_end += 1
        first_line = lines[index][0]
        body = "\n".join(f.text for f in found[index:run_end + 1])
        if first_line <= 40 and LICENSE_BODY.search(body):
            top = all(n in covered or not source_lines[n - 1].strip() for n in range(1, first_line))
            if top:
                for k in range(index, run_end + 1):
                    flags[k] = True
        index = run_end + 1


def _go_adjust(text, found, flags, path, lines):
    starts = _line_starts(text)
    whole = [text[starts[first - 1]:f.start].strip() == "" for f, (first, _) in zip(found, lines)]
    for m in re.finditer(r'^import\s+"C"\s*$', text, re.M):
        line = _line_of(starts, m.start())
        k = len(found) - 1
        while k >= 0 and lines[k][1] >= line:
            k -= 1
        want = line - 1
        while k >= 0 and lines[k][1] == want and whole[k]:
            flags[k] = True
            want = lines[k][0] - 1
            k -= 1
    if path.endswith("_test.go"):
        for k, f in enumerate(found):
            if re.match(r"\s*(?:unordered )?output:", f.text, re.I):
                flags[k] = True
                j = k + 1
                while j < len(found) and lines[j][0] == lines[j - 1][1] + 1 and not found[j].block:
                    flags[j] = True
                    j += 1


INLINE_METADATA_OPEN = re.compile(r"\s*/// [a-zA-Z0-9-]+\s*")
INLINE_METADATA_CLOSE = re.compile(r"\s*///\s*")


def _mark_inline_metadata(found, flags):
    inside = False
    for k, f in enumerate(found):
        if f.kind != "comment":
            inside = False
            continue
        if not inside and INLINE_METADATA_OPEN.fullmatch(f.text):
            inside = True
        if inside:
            flags[k] = True
            if INLINE_METADATA_CLOSE.fullmatch(f.text):
                inside = False


def _findings(text, path):
    lang = language(path, text)
    if lang is None:
        return None, [], []
    found = _find_all(text, lang)
    block_prefixes = BLOCK_PREFIXES.get(lang, ("!",))
    flags = [
        f.kind == "comment" and (allowed(f.text, lang) or (f.block and f.text.startswith(block_prefixes))
                                 or (f.start <= 1 and text[f.start:f.start + 2] == "#!"))
        for f in found
    ]
    lines = []
    if found:
        lines = _spans_in_lines(text, found)
        if lang == "python" and "__doc__" in text:
            for k, f in enumerate(found):
                if f.kind == "docstring" and f.block:
                    flags[k] = True
        if lang == "python" and "# ///" in text:
            _mark_inline_metadata(found, flags)
        if lang == "go":
            _go_adjust(text, found, flags, path, lines)
        _mark_license_runs(text, found, flags, lines)
    return lang, list(zip(found, flags)), lines


def scan(text, path):
    try:
        _, items, lines = _findings(text, path)
        return [Hit(first, last, f.kind, f.text, ok) for (f, ok), (first, last) in zip(items, lines)]
    except Exception:
        return []


def added(new_text, old_text, path):
    try:
        hits = [h for h in scan(new_text, path) if not h.allowed]
        if not hits:
            return []
        try:
            _, old_items, _ = _findings(old_text, path)
            known = {_normal(f.text) for f, _ in old_items}
        except Exception:
            flat = _normal(old_text)
            known = {_normal(h.text) for h in hits if _normal(h.text) and _normal(h.text) in flat}
        return [h for h in hits if _normal(h.text) not in known]
    except Exception:
        return []


def _prior_backslash(text, line_start, comment_ends):
    if line_start == 0:
        return False
    previous = text[:line_start - 1].rstrip("\r")
    if len(previous) in comment_ends:
        return False
    return (len(previous) - len(previous.rstrip("\\"))) % 2 == 1


def _blank_run_before(text, pos):
    count = 0
    while pos > 0:
        start = text.rfind("\n", 0, pos - 1) + 1
        if text[start:pos - 1].strip() == "":
            count += 1
            pos = start
        else:
            break
    return count


def _blank_run_after(text, pos):
    count = 0
    while pos < len(text):
        nl = text.find("\n", pos)
        if nl == -1 or text[pos:nl].strip() != "":
            break
        count += 1
        pos = nl + 1
    return count


def _cut(text, s, e, repl, glue, comment_ends=frozenset()):
    ls = text.rfind("\n", 0, s) + 1
    if ls == 0 and text.startswith("\ufeff"):
        ls = 1
    le = text.find("\n", e)
    le = len(text) if le == -1 else le
    before = text[ls:s]
    after = text[e:le]
    carriage = "\r" if after.endswith("\r") else ""
    blank_before = before.strip() == ""
    blank_after = after.strip() == ""
    multiline = "\n" in text[s:e]
    kept_tail = after.lstrip().startswith("#")
    if repl == ";":
        return text[:s] + text[e:]
    if repl:
        if blank_after:
            return text[:s] + repl + carriage + text[le:]
        if kept_tail:
            return text[:s] + repl + text[e:]
        raise Unsafe
    if repl is not None and blank_before and kept_tail:
        return text[:s] + text[e:].lstrip(" \t")
    if repl is not None and not (blank_before and blank_after):
        raise Unsafe
    if blank_before and blank_after:
        if _prior_backslash(text, ls, comment_ends):
            return text[:ls] + text[le:]
        end = le + 1 if le < len(text) else le
        new = text[:ls] + text[end:]
        following = _blank_run_after(new, ls)
        drop = following if ls <= 1 and not text[:ls].strip("\ufeff") else min(_blank_run_before(new, ls), following)
        pos = ls
        for _ in range(drop):
            pos = new.find("\n", pos) + 1
        return new[:ls] + new[pos:]
    if blank_after:
        return text[:s].rstrip(" \t") + carriage + text[le:]
    if blank_before:
        return text[:s] + text[e:].lstrip(" \t")
    left_space = before[-1] in " \t"
    right_space = after[0] in " \t"
    if multiline:
        return text[:s].rstrip(" \t") + "\n" + text[e:].lstrip(" \t")
    rest = text[e:].lstrip(" \t")
    if left_space and rest[:1] in (")", "]", "}", ",", ";"):
        return text[:s].rstrip(" \t") + rest
    if left_space and right_space:
        return text[:s] + rest
    if left_space or right_space or glue:
        return text[:s] + text[e:]
    return text[:s] + " " + text[e:]


def _plan(items):
    plan = []
    for f, ok in items:
        head = None if ok or f.kind != "comment" else directive_head(f.text)
        plan.append(("keep", None) if ok else ("reduce", head) if head else ("drop", None))
    return plan


def _reduced(text, f, head):
    raw = text[f.start:f.end]
    lead = f.text[:len(f.text) - len(f.text.lstrip())]
    if not f.block:
        return raw[:len(raw) - len(f.text)] + lead + head
    closer = "*/" if raw.endswith("*/") else "-->" if raw.endswith("-->") else raw[-2:]
    if "\n" in raw:
        opener = next(o for o in ("/**", "/*!", "/*", "<!--", raw[:2]) if raw.startswith(o))
        if "\n" not in head:
            return opener + " " + head + " " + closer
        prefix = " * " if closer == "*/" else "  "
        return opener + "\n" + "\n".join(prefix + line for line in head.split("\n")) + "\n" + (" " if closer == "*/" else "") + closer
    opener = raw[:len(raw) - len(f.text) - len(closer)]
    return opener + lead + head + " " + closer


def _pieces(chunk):
    return re.findall(r"\w+|[)\]};,]|[^\w\s)\]};,]+", chunk)


def _tokens(text, found, emit, glue):
    out = []
    buffer = ""
    pos = 0
    for f, value in zip(found, emit):
        buffer += text[pos:f.start]
        if value is not None:
            out.extend(_pieces(buffer))
            out.append(("comment", value))
            buffer = ""
        elif not glue:
            out.extend(_pieces(buffer))
            buffer = ""
        pos = f.end
    out.extend(_pieces(buffer + text[pos:]))
    return out


def _python_equal(original, stripped, removed_lines):
    import ast
    left = ast.parse(original.lstrip("\ufeff"))
    right = ast.parse(stripped.lstrip("\ufeff"))
    for node in ast.walk(left):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            first = body[0] if body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str) and first.lineno in removed_lines):
                node.body = body[1:] or ([] if isinstance(node, ast.Module) else [ast.Pass()])
    return ast.dump(left) == ast.dump(right)


def equivalent(original, stripped, path):
    try:
        lang, items, lines = _findings(original, path)
        _, new_items, _ = _findings(stripped, path)
        if lang is None:
            return original == stripped
        plan = _plan(items)
        removed = [(f, first) for (f, _), (first, _), (action, _) in zip(items, lines, plan) if action != "keep"]
        kept = [(f.kind, _normal(f.text) if action == "keep" else _normal(head))
                for (f, _), (action, head) in zip(items, plan) if action != "drop"]
        after = [(f.kind, _normal(f.text)) for f, _ in new_items]
        if kept != after:
            return False
        if lang == "python":
            removed_lines = {first for f, first in removed if f.kind == "docstring"}
            return _python_equal(original, stripped, removed_lines)
        glue = lang == "markup"
        found = [f for f, _ in items]
        new_found = [f for f, _ in new_items]
        emit = [_normal(f.text) if action == "keep" else _normal(head) if action == "reduce" else None
                for f, (action, head) in zip(found, plan)]
        before = _tokens(original, found, emit, glue)
        now = _tokens(stripped, new_found, [_normal(f.text) for f in new_found], glue)
        return before == now
    except Exception:
        return False


def strip_checked(text, path):
    try:
        lang, items, _ = _findings(text, path)
        if lang is None:
            return text, "unsupported"
        plan = _plan(items)
        if all(action == "keep" for action, _ in plan):
            return text, "clean"
        if any(f.open for f, _ in items):
            return text, "skipped"
        glue = lang == "markup"
        out = text
        comment_ends = frozenset(f.end for f, _ in items)
        for (f, _), (action, head) in sorted(zip(items, plan), key=lambda pair: pair[0][0].start, reverse=True):
            if action == "reduce":
                out = out[:f.start] + _reduced(text, f, head) + out[f.end:]
            elif action == "drop":
                out = _cut(out, f.start, f.end, f.repl, glue, comment_ends)
        _, new_items, _ = _findings(out, path)
        if any(not ok for _, ok in new_items) or not equivalent(text, out, path):
            return text, "skipped"
        return out, "stripped"
    except Exception:
        return text, "skipped"


def strip(text, path):
    return strip_checked(text, path)[0]
