#!/usr/bin/env python3
"""Periodic evals for the critic skill. Paid: each run is headless local Claude Code
(`claude -p`, model under test claude-opus-5-5 by default), never an LLM API. Grading is
deterministic (regex and parse); no LLM judge is used, so there is no judge model to differ
from the model under test.

  bug    A branch holds a refactor of order pricing with one planted high-severity bug (the
         bulk discount returns the discount, not the discounted price). The reference is a
         short written spec in the repo. The branch's own checks pass. Measures: does the
         critic reject it (VERDICT: FAIL) with a [high] finding that names the right file and
         line, and no hedged finding?
  clean  The same refactor without the bug, plus boundary checks. Measures: does the critic
         pass it (VERDICT: PASS, no [high] or [medium] finding) and still not hedge?

Each run works in a blind temp directory (no word in the path or the prompt that reveals the
purpose). The deterministic graders live in this file and are unit-tested offline by
tests/test_skill_evals.py against one good and one bad canned report per case.

A case passes when at least --threshold of its --runs pass (default 2 of 3).

  python3 evals/run_evals.py                       # both cases, 3 runs each
  python3 evals/run_evals.py --case bug --runs 1   # one run
Exit 0 when every case meets its threshold, 1 otherwise.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "scripts"))
import evalkit as ek  # noqa: E402

SPEC = """# Pricing rules

1. All money is integer cents. No floats.
2. A line is `unit_cents * qty`.
3. A line of 10 or more units gets 10% off. The discount is rounded down to whole cents,
   and the customer pays the line total minus the discount.
4. An order is the sum of its lines.
"""

BASE_PRICING = '''"""Order pricing. All money is integer cents (see SPEC.md)."""


def line_total(unit_cents, qty):
    """Price of one order line. 10 or more units get 10% off, discount rounded down."""
    total = unit_cents * qty
    if qty >= 10:
        total -= total * 10 // 100
    return total


def order_total(lines):
    """Sum of line_total over (unit_cents, qty) pairs."""
    return sum(line_total(unit, qty) for unit, qty in lines)
'''

BASE_CHECKS = '''import unittest

from ledger.pricing import line_total, order_total


class Pricing(unittest.TestCase):
    def test_small_line_has_no_discount(self):
        self.assertEqual(line_total(100, 9), 900)

    def test_order_sums_lines(self):
        self.assertEqual(order_total([(100, 1), (250, 2)]), 600)


if __name__ == "__main__":
    unittest.main()
'''

REFACTOR_HEAD = '''"""Order pricing. All money is integer cents (see SPEC.md)."""

BULK_QTY = 10
BULK_PERCENT = 10


def bulk_price(total):
    """Price of a line total after the bulk discount (rounded down to whole cents)."""
'''
BUG_LINE = "    return total * BULK_PERCENT // 100"
FIX_LINE = "    return total - total * BULK_PERCENT // 100"
REFACTOR_TAIL = '''

def line_total(unit_cents, qty):
    """Price of one order line."""
    total = unit_cents * qty
    if qty >= BULK_QTY:
        total = bulk_price(total)
    return total


def order_total(lines):
    """Sum of line_total over (unit_cents, qty) pairs."""
    return sum(line_total(unit, qty) for unit, qty in lines)
'''

BUG_CHECKS = BASE_CHECKS.replace('''    def test_order_sums_lines''', '''    def test_bulk_line_is_cheaper(self):
        self.assertLess(line_total(100, 10), 1000)

    def test_order_sums_lines''')
CLEAN_CHECKS = BASE_CHECKS.replace('''    def test_order_sums_lines''', '''    def test_bulk_starts_at_ten(self):
        self.assertEqual(line_total(100, 10), 900)
        self.assertEqual(line_total(100, 9), 900)

    def test_discount_rounds_down(self):
        self.assertEqual(line_total(999, 11), 9891)

    def test_order_sums_lines''')

RUNNER = """import sys
sys.dont_write_bytecode = True  # a run must leave the tree as it found it
import os, unittest

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(here))
suite = unittest.defaultTestLoader.discover(here, pattern="check_*.py")
sys.exit(not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful())
"""

PROMPT = """/critic branch {branch} against main (read it with `git diff main...{branch}`; it is checked out). \
Reference: SPEC.md in the repo root. Changed paths: ledger/pricing.py, checks/check_pricing.py. \
Run the repo's checks with `python3 checks/run.py`."""

CASES = {
    "bug": {"branch": "change/bulk-price", "pricing": REFACTOR_HEAD + BUG_LINE + REFACTOR_TAIL,
            "checks": BUG_CHECKS, "verdict": "FAIL", "bug_line": BUG_LINE},
    "clean": {"branch": "change/bulk-price", "pricing": REFACTOR_HEAD + FIX_LINE + REFACTOR_TAIL,
              "checks": CLEAN_CHECKS, "verdict": "PASS", "bug_line": None},
}


def bug_line_number(case):
    """1-based line of the planted bug in ledger/pricing.py on the branch under review, or None."""
    c = CASES[case]
    if c["bug_line"] is None:
        return None
    for n, line in enumerate(c["pricing"].splitlines(), 1):
        if line == c["bug_line"]:
            return n
    raise AssertionError("planted bug line not found")


def build_repo(root, case):
    """Make the repo, return the 1-based line of the planted bug in ledger/pricing.py (or None)."""
    c = CASES[case]
    ek.write_files(root, {"SPEC.md": SPEC, "ledger/__init__.py": "", "ledger/pricing.py": BASE_PRICING,
                          "checks/check_pricing.py": BASE_CHECKS, "checks/run.py": RUNNER})
    ek.git(root, "init", "-q", "-b", "main")
    ek.git(root, "config", "claude.mode", "solo")
    ek.git(root, "add", "-A")
    ek.git(root, "commit", "-q", "-m", "initial import")
    ek.git(root, "switch", "-q", "-c", c["branch"])
    ek.write_files(root, {"ledger/pricing.py": c["pricing"], "checks/check_pricing.py": c["checks"]})
    ek.git(root, "add", "-A")
    ek.git(root, "commit", "-q", "-m", "Pull the bulk discount into bulk_price")
    return bug_line_number(case)


# ---- deterministic graders ---------------------------------------------------------------

HEDGE = re.compile(r"\b(may|might|possibly|i suspect)\b", re.I)
SEVERITY = re.compile(r"\[(high|medium|low)\]", re.I)
SECTION_END = re.compile(r"^\W*(blind comparison|could not check|follow-ups?)\b", re.I)


def verdict(text):
    m = re.search(r"VERDICT\W*(PASS|FAIL)", text)
    return m.group(1) if m else None


def findings_section(text):
    """Lines of the report from the verdict to the first closing section (blind comparison,
    could not check, follow-ups). The 'Findings' heading is not required: the report's heading
    wording varies, the `[high]` / `[medium]` / `[low]` tags are what the skill fixes."""
    out, on = [], False
    for line in text.splitlines():
        if not on:
            on = "VERDICT" in line
            continue
        if SECTION_END.match(line):
            break
        out.append(line)
    return out


def findings(text):
    """[(severity, item text)]: an item runs from its tag line to the next tag or the counts line."""
    items = []
    for line in findings_section(text):
        tags = list(SEVERITY.finditer(line))
        if tags:
            items.append([tags[0].group(1).lower(), line])
        elif items and not re.match(r"^\W*counts?\b", line, re.I):
            items[-1][1] += "\n" + line
    return [(s, t) for s, t in items]


def names_line(item, filename, lo, hi):
    """True when the item names `filename` as a whole file name (not the tail of check_pricing.py) and, in
    the 80 characters after that mention, cites a line in [lo, hi]: `:N`, `:A-B`, `line N` or `lines A-B`."""
    for name in re.finditer(r"(?<![\w])" + re.escape(filename), item):
        window = item[name.end():name.end() + 80]
        spans = [(m.group(1), m.group(2)) for m in re.finditer(
            r"^[`'\"]*[:#]L?(\d+)(?:\s*[-\u2013,]\s*L?(\d+))?", window)]
        spans += [(m.group(1), m.group(2)) for m in re.finditer(
            r"\blines?\s+(\d+)(?:\s*(?:-|\u2013|to|and)\s*(\d+))?", window, re.I)]
        for first, second in spans:
            a = int(first)
            b = int(second) if second else a
            if a > b:
                a, b = b, a
            if a <= hi and b >= lo:
                return True
    return False


def grade_report(case, text, bug_line=None):
    """Problems found in one critic report; [] means the run passes."""
    problems = []
    want = CASES[case]["verdict"]
    got = verdict(text)
    if got is None:
        return ["no 'VERDICT: PASS|FAIL' line in the report"]
    if got != want:
        problems.append(f"verdict {got}, expected {want}")
    items = findings(text)
    serious = [(s, t) for s, t in items if s in ("high", "medium")]
    if case == "bug":
        highs = [t for s, t in items if s == "high"]
        if not highs:
            problems.append("no [high] finding")
        elif bug_line is not None and not any(names_line(t, "pricing.py", bug_line - 1, bug_line + 1) for t in highs):
            problems.append(f"no [high] finding names ledger/pricing.py:{bug_line} (the planted bug)")
    else:
        if serious:
            problems.append(f"{len(serious)} high or medium finding(s) on a clean change: "
                            + "; ".join(t.strip().splitlines()[0][:100] for _, t in serious))
    for _, item in items:
        m = HEDGE.search(item)
        if m:
            problems.append(f"hedged wording {m.group(0)!r} in a finding: {item.strip().splitlines()[0][:100]}")
            break
    return problems


# ---- runs --------------------------------------------------------------------------------

def make_case(case):
    def run(work, model, timeout, args):
        repo = os.path.join(work, "ledger")
        os.makedirs(repo)
        bug_line = build_repo(repo, case)
        prompt = PROMPT.format(branch=CASES[case]["branch"])
        ek.assert_blind(repo, prompt)
        # Python cannot write __pycache__ into a read-only package folder, so the critic's own `python3 -c`
        # calls leave the tree as they found it (an env variable would not reach the critic's shell).
        pkg_dirs = [os.path.join(repo, d) for d in ("ledger", "checks")]
        for d in pkg_dirs:
            os.chmod(d, 0o555)
        try:
            events, code, secs, _ = ek.claude_stream(prompt, repo, os.path.join(work, "run.log"), timeout, model=model,
                                                     tools=("Bash", "Read", "Glob", "Grep"), max_budget_usd=15)
        finally:
            for d in pkg_dirs:
                os.chmod(d, 0o755)
        text = ek.final_text(events)
        with open(os.path.join(work, "report.txt"), "w") as f:
            f.write(text)
        problems = grade_report(case, text, bug_line)
        return problems, {"verdict": verdict(text), "cost_usd": ek.run_cost(events)}
    return run


def main():
    return ek.run_cases({name: make_case(name) for name in CASES}, __doc__)


if __name__ == "__main__":
    sys.exit(main())
