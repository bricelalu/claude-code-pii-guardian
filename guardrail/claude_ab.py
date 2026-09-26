#!/usr/bin/env python3
"""A/B test: does code-guard break Claude Code? The same scripted tasks run with `claude -p`
directly against Anthropic and through the gateway, on a small CRM repo full of customer PII.
Each run gets a fresh copy of the repo and is checked for:
  - success: the task's own check (tests pass, the right cell changed, nothing else changed)
  - corruption: a placeholder (<PERSON>, [EMAIL_REDACTED]...) written into a file
  - Edit errors: failed Edit calls (e.g. old_string holds a masked name -> "string not found")
  - wall time, turns, cost

Needs `task up`, a RunPod worker, and `claude` logged in (direct arm). Output: .pii-score-out/claude-ab/

  python3 guardrail/claude_ab.py [--reps 2] [--model claude-haiku-4-5-20251001]
"""
import argparse
import concurrent.futures as cf
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / ".pii-score-out" / "claude-ab"
PLACEHOLDER = re.compile(r"<(PERSON|LOCATION|CREDIT_CARD)>|\[[A-Z0-9_]+_REDACTED\]")

CUSTOMERS_PY = '''"""Customer records for the CRM demo.

Maintainer: Élodie Fontaine <e.fontaine@atelier-numerique.fr>
"""

CUSTOMERS = [
    {"id": 1, "name": "Jean Dupont", "city": "Lyon", "phone": "06 12 34 56 78", "plan": "pro"},
    {"id": 2, "name": "Lucía Fernández", "city": "Sevilla", "phone": "+34 691 905 865", "plan": "free"},
    {"id": 3, "name": "Giulia Rossi", "city": "Bologna", "phone": "+39 051 123 4567", "plan": "free"},
]


def find(customer_id):
    return next(c for c in CUSTOMERS if c["id"] == customer_id)
'''
EXPORT_PY = '''from crm.customers import CUSTOMERS


def to_csv(customers=CUSTOMERS):
    lines = ["id,name,city,plan"]
    for c in customers:
        lines.append(f'{c["id"]},{c["name"]},{c["plan"]}')
    return "\\n".join(lines)
'''
CUSTOMERS_CSV = """id,firstname,lastname,email,phone,city,country,iban
1,Jean,DUPONT,jean.dupont@atelier-numerique.fr,06 12 34 56 78,Lyon,FR,FR76 3000 6000 0112 3456 7890 189
2,Lucía,Fernández,l.fernandez@taller-digital.es,+34 691 905 865,Sevilla,ES,ES91 2100 0418 4502 0005 1332
3,Giulia,Rossi,g.rossi@officina-digitale.it,+39 051 123 4567,Bologna,FR,IT60 X054 2811 1010 0000 0123 456
"""
TEST_PY = '''import unittest

from crm.customers import find
from crm.export import to_csv


class CrmTest(unittest.TestCase):
    def test_find(self):
        self.assertEqual(find(1)["city"], "Lyon")

    def test_csv_has_city(self):
        self.assertIn("1,Jean Dupont,Lyon,pro", to_csv().splitlines())
'''
FILES = {"crm/__init__.py": "", "crm/customers.py": CUSTOMERS_PY, "crm/export.py": EXPORT_PY,
         "data/customers.csv": CUSTOMERS_CSV, "tests/__init__.py": "", "tests/test_crm.py": TEST_PY}


def unittest_passes(repo):
    return subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=repo, capture_output=True).returncode == 0


def load(repo, rel):
    spec = importlib.util.spec_from_file_location(rel.replace("/", "_"), repo / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_fix_bug(repo):
    return (unittest_passes(repo) and (repo / "crm/customers.py").read_text() == CUSTOMERS_PY
            and (repo / "data/customers.csv").read_text() == CUSTOMERS_CSV)


def check_edit_source(repo):
    return (repo / "crm/customers.py").read_text() == CUSTOMERS_PY.replace(
        '"+34 691 905 865", "plan": "free"', '"+34 691 905 865", "plan": "pro"')


def check_edit_csv(repo):
    return (repo / "data/customers.csv").read_text() == CUSTOMERS_CSV.replace(
        "Bologna,FR,IT60", "Bologna,IT,IT60")


def check_add_feature(repo):
    module = load(repo, "crm/customers.py")
    original = load_original_customers()
    # Not the whole suite: the fixture's deliberate bug (fix-bug task) still fails here.
    return (getattr(module, "count_by_plan", lambda: None)() == {"pro": 1, "free": 2}
            and module.CUSTOMERS == original and "count_by_plan" in (repo / "tests/test_crm.py").read_text())


def load_original_customers():
    namespace = {}
    exec(CUSTOMERS_PY, namespace)
    return namespace["CUSTOMERS"]


# (name, prompt, check). The two "edit" tasks touch lines that hold PII: the risky case.
TASKS = [
    ("fix-bug", "The test suite fails. Fix the bug in the code (not in the tests), then run "
                "`python3 -m unittest` to confirm.", check_fix_bug),
    ("edit-source", 'In crm/customers.py, customer 2 should be on the "pro" plan instead of "free". '
                    "Make that change only.", check_edit_source),
    ("edit-csv", "In data/customers.csv, customer 3's country is wrong: it should be IT, not FR. "
                 "Fix that cell only.", check_edit_csv),
    ("add-feature", "Add a function `count_by_plan()` to crm/customers.py that returns a dict mapping "
                    "each plan to its number of customers, add a unittest for it in tests/test_crm.py, "
                    "then run `python3 -m unittest`.", check_add_feature),
]


def env_for(mode, gateway, key):
    env = {k: v for k, v in os.environ.items()
           if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_") and k != "CLAUDE_PID"
           and k not in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")}
    if mode == "gateway":
        env.update(ANTHROPIC_BASE_URL=gateway, ANTHROPIC_AUTH_TOKEN=key)
    return env


def run(task, mode, rep, args, key):
    name, prompt, check = task
    repo = pathlib.Path(tempfile.mkdtemp(prefix=f"ab-{name}-{mode}-"))
    for rel, content in FILES.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content)
    cmd = ["claude", "-p", prompt, "--model", args.model, "--output-format", "stream-json", "--verbose",
           "--permission-mode", "acceptEdits", "--strict-mcp-config", "--no-session-persistence",
           "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep", "Bash(python3:*)"]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=repo, env=env_for(mode, args.gateway, key), capture_output=True,
                              text=True, timeout=args.timeout)
        stdout, timed_out = proc.stdout, False
    except subprocess.TimeoutExpired as e:
        stdout, timed_out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), True
    elapsed = time.perf_counter() - t0
    tool_names, edit_errors, errors, final = {}, [], 0, {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "tool_use":
                tool_names[block["id"]] = block["name"]
            elif block.get("type") == "tool_result" and block.get("is_error"):
                errors += 1
                if tool_names.get(block.get("tool_use_id")) in ("Edit", "MultiEdit"):
                    text = block.get("content")
                    edit_errors.append(json.dumps(text)[:200])
        if event.get("type") == "result":
            final = event
    corrupted = sorted({str(p.relative_to(repo)) for p in repo.rglob("*")
                        if p.is_file() and p.suffix in (".py", ".csv") and PLACEHOLDER.search(p.read_text())})
    try:
        ok = not timed_out and not corrupted and check(repo)
    except Exception:  # noqa: BLE001 - a broken module is a failed task
        ok = False
    shutil.rmtree(repo, ignore_errors=True)
    return {"task": name, "mode": mode, "rep": rep, "ok": ok, "corrupted": corrupted, "timed_out": timed_out,
            "edit_errors": edit_errors, "tool_errors": errors, "seconds": round(elapsed, 1),
            "turns": final.get("num_turns"), "cost": final.get("total_cost_usd"),
            "api_error": final.get("is_error"), "result": str(final.get("result", ""))[:300]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--gateway", default=os.environ.get("GATEWAY_URL", "http://litellm.local:8080"))
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    key = os.environ["LITELLM_MASTER_KEY"]
    jobs = [(t, m, r) for r in range(args.reps) for t in TASKS for m in ("direct", "gateway")]
    with cf.ThreadPoolExecutor(2) as ex:  # one direct and one gateway run at a time
        results = list(ex.map(lambda j: run(*j, args, key), jobs))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    lines = ["| Task | Mode | Success | Edit errors | Other tool errors | Corrupted files | Avg time | Avg turns |",
             "|---|---|---|---|---|---|---|---|"]
    for name, *_ in TASKS:
        for mode in ("direct", "gateway"):
            rs = [r for r in results if r["task"] == name and r["mode"] == mode]
            lines.append(
                f"| {name} | {mode} | {sum(r['ok'] for r in rs)}/{len(rs)} | {sum(len(r['edit_errors']) for r in rs)} "
                f"| {sum(r['tool_errors'] - len(r['edit_errors']) for r in rs)} "
                f"| {sum(bool(r['corrupted']) for r in rs)} | {sum(r['seconds'] for r in rs) / len(rs):.0f}s "
                f"| {sum(r['turns'] or 0 for r in rs) / len(rs):.1f} |")
    details = [f"- {r['task']} / {r['mode']} #{r['rep']}: {'ok' if r['ok'] else 'FAILED'}"
               f"{' corrupted ' + str(r['corrupted']) if r['corrupted'] else ''}"
               f"{' edit errors ' + str(r['edit_errors']) if r['edit_errors'] else ''}"
               f"{'' if r['ok'] else ' — ' + r['result']}" for r in results]
    report = "\n".join(["# Claude Code A/B: direct vs through code-guard", "", *lines, "", *details, ""])
    (OUT / "report.md").write_text(report)
    print(report)


if __name__ == "__main__":
    main()
