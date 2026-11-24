#!/usr/bin/env python3
USAGE = """Periodic eval for the large-task skill. Paid: one headless local Claude Code run
(`claude -p`, model under test claude-opus-5-5 by default), never an LLM API. Grading is
deterministic, from the stream-json transcript; no LLM judge is used.

  builders  The prompt invokes /large-task on a large feature (multi-currency invoices) for a small
            app in a throwaway git repo. The run is stopped as soon as the lead has launched its
            builder agents (or after 60 lead tool calls, or at the budget cap), so a run costs
            minutes, not hours. Measures, from what the lead did up to that point:
              1. before the first builder Agent call, the lead wrote down a
                 reference (Step 0) and a budget with a number in it,
              2. at least one builder Agent call was made,
              3. every builder Agent call has isolation "worktree",
              4. the first command of every builder prompt is `git switch -C <unit-branch> <task-branch>`:
                 on the first line, or right under a lead-in line that ends with a colon.
            Readers and critics (subagent_type reviewer, Explore, reader, Plan, or a critic/review
            description) are not builders.

Each run works in a blind temp directory. The deterministic graders are unit-tested offline by
tests/test_skill_evals.py against one good and one bad canned transcript.

A case passes when at least --threshold of its --runs pass (default 4 of 6). With 3 runs and a 2 of 3 bar, one
unlucky run fails the case: on 2026-11-20 configs with no rule change between them scored 1/3, 2/3 and 3/3. With
6 runs and a 4 of 6 bar, two failed runs still pass and three fail. The run cap stays at USD 15 (max_budget_usd): the lead sees it as "USD budget: $0/$15", and
the skill tells it that a cap does not cancel the tournament, so the eval checks that rule.

  python3 evals/run_evals.py                  # 6 runs
  python3 evals/run_evals.py --runs 1         # one run
Exit 0 when every case meets its threshold, 1 otherwise.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "scripts"))
import evalkit as ek  # noqa: E402

MAX_LEAD_CALLS = 60
AFTER_BUILDER_EVENTS = 4

FILES = {
    "README.md": "# invoicer\n\nSmall invoicing tool. Invoices are stored as JSON, amounts are integer cents in EUR.\n"
                 "`python3 -m invoicer new CUSTOMER CENTS`, `python3 -m invoicer report YYYY-MM`.\n",
    "invoicer/__init__.py": "",
    "invoicer/__main__.py": "import sys\n\nfrom invoicer.cli import main\n\nsys.exit(main(sys.argv[1:]))\n",
    "invoicer/models.py": '''from dataclasses import dataclass


@dataclass
class Invoice:
    customer: str
    cents: int
    issued: str  # YYYY-MM-DD
''',
    "invoicer/storage.py": '''import json
import os

PATH = os.environ.get("INVOICER_DATA", "data/invoices.json")


def load():
    if not os.path.exists(PATH):
        return []
    with open(PATH) as f:
        return json.load(f)


def save(rows):
    os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
    with open(PATH, "w") as f:
        json.dump(rows, f, indent=2)
''',
    "invoicer/report.py": '''from invoicer import storage


def month_total(month):
    """Sum of cents for invoices issued in month (YYYY-MM)."""
    return sum(r["cents"] for r in storage.load() if r["issued"].startswith(month))
''',
    "invoicer/cli.py": '''import datetime

from invoicer import report, storage


def main(argv):
    if argv[:1] == ["new"] and len(argv) == 3:
        rows = storage.load()
        rows.append({"customer": argv[1], "cents": int(argv[2]), "issued": datetime.date.today().isoformat()})
        storage.save(rows)
    elif argv[:1] == ["report"] and len(argv) == 2:
        print(report.month_total(argv[1]))
    else:
        print("usage: invoicer new CUSTOMER CENTS | report YYYY-MM")
        return 2
    return 0
''',
    "data/invoices.json": '[{"customer": "Acme", "cents": 12500, "issued": "2026-09-03"}]\n',
}

PROMPT = """/large-task
Task: add multi-currency support to the invoicer app in the current directory (a git repo, branch main). \
Today every invoice is EUR in integer cents. Needed: a currency code on each invoice (ISO 4217), \
conversion to EUR from a rates file, per-currency totals in the monthly report, a `--currency` option on \
`new`, and a migration of the invoices already stored. It changes storage, the report and the CLI, so it is a \
large task. Do not ask questions; decide and carry on. Do not build a verify skill for this repo in this task; that is a separate task."""


def build_repo(root):
    ek.write_files(root, FILES)
    ek.git(root, "init", "-q", "-b", "main")
    ek.git(root, "config", "agent.mode", "solo")
    ek.git(root, "config", "claude.mode", "solo")
    ek.git(root, "add", "-A")
    ek.git(root, "commit", "-q", "-m", "initial import")


NOT_BUILDER_TYPES = {"reviewer", "explore", "reader", "plan", "runner", "claude-code-guide", "statusline-setup",
                     "web-fetch"}
NOT_BUILDER_START = re.compile(r"^\W*(critic|verifier|verify)\b", re.I)
FIRST_LINE = re.compile(r"^[\s`$>*-]*(?:\d+[.)]\s*`?)?git switch -C\s+\S+\s+\S+")
SWITCH = re.compile(r"git switch -C\s+\S+\s+\S+")
FENCE = re.compile(r"^\s*```\w*\s*$")
REFERENCE_LINE = re.compile(r"^[\s*#>_-]*(?:step 0\b[^:\n]{0,20}[:.]\s*)?(?:frozen\s+)?(?:reference|rubric)(?:\s+(?:used|type))?"
                            r"[\s*_`]{0,4}:[\s*_`]*(?P<t>[^\n]+)", re.I | re.M)
REFUSAL = re.compile(r"^\W*(none|n/?a|not (needed|required|applicable)|no |nothing|tbd|todo|\?)", re.I)
BUDGET_LINE = re.compile(r"^(?P<label>[^\n]*\bbudget\b[^\n]*)(?P<body>(?:\n[ \t]*\S[^\n]*){0,6})", re.I | re.M)
BUDGET_HEADING = re.compile(r"^[\s*#>_-]*(?:step 0\b[^\n]{0,20})?budget[\s*_:]*$", re.I)
COUNTED = re.compile(r"critic|verif|run", re.I)
RUN_WORDS = r"(?:cold|claude|`claude`|claude-alt|`claude-alt`|extra|opus|and|critic|verifier|verify)"
BUDGET_NUMBER = re.compile(r"\bbudget\b[\s*_:=-]*[1-9]\d*\b"
                           rf"|\b[1-9]\d*\s+(?:{RUN_WORDS}\s+){{0,3}}runs?\b"
                           r"|\b(?:critic|verifier|verify)\s+runs?\s*[:=]\s*[1-9]", re.I)
LEAD_IN = re.compile(r"^[\s*_#>-]*first\b", re.I)
NOT_FIRST = re.compile(r"\b(?:not|never|don'?t|then|after|later|until|unless|skip|instead)\b|\bgit |[;&|]", re.I)
NO_BUDGET = re.compile(r"\bno budget\b|\bbudget\b[\s*_:=-]*(?:is\s+)?(?:none|tbd|todo|unset|not set|unknown|n/?a)\b", re.I)


def has_reference(pre):
    for m in REFERENCE_LINE.finditer(pre):
        t = m.group("t").strip(" *_`")
        if len(t) >= 10 and not REFUSAL.match(t):
            return True
    return False


def has_budget(pre):
    for m in BUDGET_LINE.finditer(pre):
        label = m.group("label")
        if NO_BUDGET.search(label):
            continue
        scope = label if not BUDGET_HEADING.match(label) else m.group("body")
        if BUDGET_NUMBER.search(scope) and COUNTED.search(scope):
            return True
    return False


def first_command_is_switch(prompt):
    lines = [l for l in str(prompt).splitlines() if l.strip() and not FENCE.match(l)][:2]
    if not lines:
        return False
    if FIRST_LINE.match(lines[0]):
        return True
    m = SWITCH.search(lines[0])
    lead_in = lines[0][:m.start()] if m else lines[0]
    if not LEAD_IN.match(lead_in) or NOT_FIRST.search(lead_in) or lead_in.count("`") > (1 if m else 0):
        return False
    if m:
        return True
    return lead_in.rstrip(" *_").endswith(":") and len(lines) > 1 and bool(FIRST_LINE.match(lines[1]))


def is_agent_call(name):
    return name in ("Agent", "Task")


def is_builder(inp):
    st = str(inp.get("subagent_type", "") or "").lower()
    if st in NOT_BUILDER_TYPES:
        return False
    return not NOT_BUILDER_START.match(str(inp.get("description", "") or ""))


def lead_items(events):
    out = []
    for b in ek.lead_blocks(events):
        if b.get("type") == "text":
            out.append(("text", b.get("text", "")))
        elif b.get("type") == "tool_use":
            out.append(("tool", (b.get("name", ""), b.get("input") or {})))
    return out


def building_starts(items):
    for i, (kind, p) in enumerate(items):
        if kind == "tool" and is_agent_call(p[0]) and is_builder(p[1]):
            return i
    return None


def written_before(items, end):
    parts = []
    for kind, p in items[:end]:
        if kind == "text":
            parts.append(p)
        else:
            name, inp = p
            parts += [str(inp.get(k, "")) for k in ("content", "new_string", "command")]
    return "\n".join(parts)


def grade(events):
    items = lead_items(events)
    problems = []
    start = building_starts(items)
    pre = written_before(items, len(items) if start is None else start)
    if not has_reference(pre):
        problems.append("no reference (real thing, analog or frozen rubric) written before building started")
    if not has_budget(pre):
        problems.append("no budget with a number (critic and verifier runs) written before building started")
    builders = [p[1] for k, p in items if k == "tool" and is_agent_call(p[0]) and is_builder(p[1])]
    if not builders:
        problems.append("no builder Agent call before the run was stopped")
    for n, inp in enumerate(builders, 1):
        if inp.get("isolation") != "worktree":
            problems.append(f"builder {n} ({inp.get('description', '?')}): isolation is {inp.get('isolation')!r}, not 'worktree'")
        first = next((l for l in str(inp.get("prompt", "")).splitlines() if l.strip()), "")
        if not first_command_is_switch(inp.get("prompt", "")):
            problems.append(f"builder {n} ({inp.get('description', '?')}): first prompt line is {first[:70]!r}, "
                            "not `git switch -C <unit-branch> <task-branch>`")
    return problems


def make_stop():
    state = {"count": 0, "mark": 0}

    def stop(events):
        calls = [p for k, p in lead_items(events) if k == "tool"]
        if len(calls) >= MAX_LEAD_CALLS:
            return True
        n = sum(1 for c in calls if is_agent_call(c[0]) and is_builder(c[1]))
        if n != state["count"]:
            state["count"], state["mark"] = n, len(events)
        return n >= 3 or (n >= 1 and len(events) - state["mark"] >= AFTER_BUILDER_EVENTS)
    return stop


def run(work, model, timeout, args):
    repo = os.path.join(work, "invoicer")
    os.makedirs(repo)
    build_repo(repo)
    ek.assert_blind(repo, PROMPT)
    events, code, secs, stopped = ek.claude_stream(PROMPT, repo, os.path.join(work, "run.log"), timeout, model=model,
                                                   stop_when=make_stop(), max_budget_usd=15,
                                                   tools=("Bash", "Read", "Write", "Edit", "Glob", "Grep", "Agent", "Skill"))
    problems = grade(events)
    return problems, {"stopped_early": stopped, "lead_calls": len(ek.tool_calls(events)), "cost_usd": ek.run_cost(events),
                      "cost_basis": ek.cost_basis(events)}


def main():
    return ek.run_cases({"builders": run}, USAGE, default_runs=6, default_threshold=4 / 6,
                        default_timeout=1500)


if __name__ == "__main__":
    sys.exit(main())
