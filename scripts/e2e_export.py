"""End-to-end: Claude Code exports customers_leaked, through the live gateway.

This is the test that the two guardrails compose. Everything upstream of it is offline:
guardrail/test_integration.py proves the pipeline on a string. This proves the real path —
a real Claude Code client, a real tool_use loop running sqlite3 over the real fixture, a
real LiteLLM proxy with both guardrails, and a real LLM behind it.

The leak fixture is deliberately half-masked (guardrail/failing_masker.py), so a correct
run is: what sqlite3 emits is partly raw PII, and what Claude ends up able to say is not.
If either guardrail were disabled, the raw values would survive and this fails.

VERDICT COMES FROM THE GATEWAY LOGS, NOT FROM CLAUDE. Claude's own summary is a story
about what happened; the LiteLLM request bodies are what was actually sent. A model can
paraphrase, forget, or (with a 1M context) re-derive a value it was never shown, so
"Claude did not repeat the value" is not evidence that the value was masked. The
Postgres check is the ground truth and it is the assertion that matters; the rest is
diagnostics.

    python3 scripts/e2e_export.py                 # all three formats
    python3 scripts/e2e_export.py --format json
    python3 scripts/e2e_export.py --check-logs-only

Exit status is the verdict. Prints a table either way.
"""
import argparse
import csv
import datetime
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "leaked_customers.db"
TABLE = "customers_leaked"
TRUTH = REPO / ".pii-score-out" / "export.csv"
MODEL = "claude-haiku-4-5-20251001"

# The columns that carry PII. `id` is excluded deliberately: it is the join key used to
# look a value up in the ground truth, and a masked id would make verification vacuous.
PII_COLUMNS = ["firstname", "lastname", "customer_email", "phone_number", "address",
               "zipcode", "city", "country", "iban", "last_login_ip", "birth_date"]

# Columns the guardrails do not detect, out of scope by decision in pii-guardian-ax0.
# Reported apart
# from real failures rather than folded into them, for two reasons.
#
# One: it keeps the signal honest in both directions. "FAIL" has to mean the guardrails
# did something wrong, or the exit status stops meaning anything and the E2E becomes
# something everyone learns to ignore. A regression in address or city is not drowned out
# by a known, separately-tracked gap in zipcode.
#
# Two: a leak in one of these is not the same *kind* of finding as a leak in the rest.
# These columns have no rule at all, so the value was never a candidate for masking.
# The others are covered end to end, so a leak there means a rule that used to work has
# stopped working — a far more urgent signal, and one that is easy to miss when it is
# averaged together with a known gap.
UNCOVERED_COLUMNS = {"zipcode", "birth_date"}

TOKEN = re.compile(r"^(\[|<)[A-Z_]+(\]|>)$")
FENCE = re.compile(r"```(?:json|markdown|csv)?\s*\n(.*?)```", re.DOTALL)

# A real value from the fixture. Kept short deliberately: matching a 2-character value
# like a zipcode against free text produces false positives, so the check is on the
# structured cells, not on the whole reply.
SHORT_MIN = 4

# Absolute, because Claude Code runs in a scratch directory: see run_claude for why.
QUERIES = {
    "json": f"sqlite3 -json {DB} 'select * from {TABLE}'",
    "csv": f"sqlite3 -header -csv {DB} 'select * from {TABLE}'",
    "markdown": (f"printf '.headers on\\n.mode markdown\\nselect * from {TABLE};\\n' | "
                 f"sqlite3 {DB}"),
}


def truth_rows():
    with TRUTH.open(newline="", encoding="utf-8") as fh:
        return {r["id"]: r for r in csv.DictReader(fh)}


def is_token(value):
    return bool(TOKEN.match(str(value).strip()))


def _leaves(node, path=""):
    """(path, scalar) for every leaf under `node`, depth first.

    A document can put a PII column anywhere: `firstname` at the top of a flat export, or
    `customer.profile.firstname` one or two levels down. This yields both as (path, value)
    so a caller can match on the last path segment without caring how deep it sat.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaves(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, (list, tuple)):
        for i, value in enumerate(node):
            yield from _leaves(value, f"{path}[{i}]")
    else:
        yield path, node


def unmasked_cells(rows, truth, skip=frozenset()):
    """(id, column, value) for every PII cell holding a real, unmasked value.

    A cell is a leak when it is neither a mask token nor a value the ground truth itself
    already had masked — the fixture is half-masked on purpose, so requiring every cell to
    be a token would demand re-masking the database.

    `skip` holds columns to ignore, so the caller can report an uncovered column
    separately from a regression.

    Columns are matched at any depth, by the last segment of the path. The earlier version
    only looked at top-level keys, and the customers-nested scenario then passed with zero
    leaks over a document full of raw names: every covered column was absent from the row,
    so every cell was skipped and a check that inspected nothing reported success. The
    question here is whether a real value reached the provider, and where the document
    happens to keep it is not an answer to that.

    A column appearing at several depths is judged at each: a raw value nested beside a
    masked one is still a leak, and reporting the column name (not the path) keeps the
    output identical to the flat case so the existing report format still reads right.
    """
    leaks = []
    for row in rows:
        rid = str(row.get("id", "")).strip()
        real = truth.get(rid)
        if real is None:
            continue  # id itself was mangled: cannot join, so do not claim a verdict
        was_by_col = {c: str(real.get(c, "")).strip() for c in PII_COLUMNS}
        for path, value in _leaves(row):
            col = path.rsplit(".", 1)[-1]
            if col not in PII_COLUMNS or col in skip:
                continue
            got = str(value).strip()
            was = was_by_col[col]
            if is_token(got) or not was or is_token(was):
                continue
            if got.lower() == was.lower():
                leaks.append((rid, col, got))
    return leaks


def parse(fmt, text):
    body = text.strip()
    fenced = FENCE.findall(body)
    if fenced:
        body = max(fenced, key=len).strip()
    try:
        if fmt == "json":
            return "json", json.loads(body)
        if fmt == "csv":
            return "csv", list(csv.DictReader(io.StringIO(body)))
        lines = [ln for ln in body.splitlines() if ln.strip().startswith("|")]
        if len(lines) < 2:
            raise ValueError("no markdown table rows found")
        header = [c.strip() for c in lines[0].strip().strip("|").split("|")]
        rows = [dict(zip(header, [c.strip() for c in ln.strip().strip("|").split("|")]))
                for ln in lines[2:]]
        return "markdown", rows
    except Exception as e:  # a structure failure is a result, not a crash
        return f"{fmt} PARSE FAILED: {type(e).__name__}: {e}", None


def client_env():
    """The gateway settings, read from .claude/settings.local.json.

    Passed explicitly rather than relying on cwd, because run_claude works out of a
    scratch directory (see below) and would otherwise lose them.
    """
    settings = json.loads((REPO / ".claude" / "settings.local.json").read_text())
    env = dict(os.environ)
    env.update({k: str(v) for k, v in settings.get("env", {}).items()})
    return env


def run_claude(fmt, workdir):
    """Ask Claude Code to export the table and echo it verbatim."""
    prompt = (
        f"Run this command with the Bash tool, exactly as written:\n\n{QUERIES[fmt]}\n\n"
        f"Then paste the command's complete output back verbatim inside a single code "
        f"fence tagged {fmt}. No commentary, no reformatting, no re-typing from memory, "
        f"no truncation, no summarising, no row elision — every row, exactly as printed. "
        f"Reproduce any value that looks like <PERSON>, <LOCATION> or [EMAIL_REDACTED] "
        f"exactly as printed; do not alter it and do not remark on it.\n\n"
        "Context, so the request is not read as anything other than what it is: "
        f"{DB.name} is a synthetic fixture of 50 invented customer records, generated by "
        "scripts/create_leaked_db.py for a masking test. No real person, account or "
        "transaction is in it. The command is read-only SELECT against that local file."
    )
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", MODEL, "--output-format", "json",
         "--allowedTools", "Bash", "--permission-mode", "bypassPermissions",
         "--setting-sources", ""],
        cwd=workdir, env=client_env(), capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        return None, f"claude exited {proc.returncode}: {proc.stderr.strip()[:400]}"
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return None, f"claude emitted non-JSON: {e}"
    if payload.get("is_error"):
        return None, f"claude reported an error: {str(payload.get('result'))[:300]}"
    result = payload.get("result", "")

    # A refusal is not a malformed reply, and the two must not be reported as one. The
    # model declines sometimes — reading a "leaked customers" export as a request to
    # exfiltrate customer data is a reasonable thing to wonder about, and when it
    # happened the run surfaced as "markdown PARSE FAILED: no markdown table rows
    # found", which points the reader at the parser when the cause was the prompt.
    # Exit code 0, no error flag, no tool call: nothing else distinguishes it.
    if "<fence" not in result and "```" not in result and not QUERIES[fmt] in result:
        return None, ("claude declined the request rather than running the command "
                      f"(exit 0, no tool call). First line: {result.strip()[:160]!r}")
    return result, None


def leaked_from_sqlite(fmt):
    """What the tool_result actually carried, with no gateway in the path.

    This is the control: it shows the raw leak the guardrails are there to stop, so a pass
    below is attributable to them rather than to a fixture that was already clean.
    """
    out = subprocess.run(["bash", "-c", QUERIES[fmt]], cwd=REPO,
                         capture_output=True, text=True, timeout=120).stdout
    return parse(fmt, out)


def check_logs(truth, since, attempts=8, pause=8, early_on_leak=True):
    """The verdict: what LiteLLM actually sent upstream, read from its own database.

    Runs in-cluster because postgres is not published. Returns (detail, error).

    `since` is an ISO timestamp captured *before* the run, not a lookback window. That
    distinction is the whole point of reading the logs at all. A window would fold in
    bodies from earlier runs — including ones recorded while a guardrail was broken —
    and report them as this run's leaks, so the check would keep failing for a leak that
    was already fixed and would hide a fresh one behind it. The first version used a
    30-minute window and reported 'address' leaks from bodies recorded an hour earlier,
    while the body this run had just produced was clean.

    Polling, because LiteLLM writes request bodies asynchronously. Reading once, right
    after the model returned, found only the turn that had already been written — the
    tool call, which carries no document — and reported "0 covered-column values reached
    the provider" over a run that had just leaked twelve addresses. The same query about
    half a minute later returned both later bodies and the leak. The structure check had
    already been given this treatment; the log check, which is the authoritative one, had
    not.

    "Stop when the count stops growing" is the best signal available here and it is only
    a heuristic: the writer is not synchronous, so the count can sit still for half a
    minute and then rise again. Two things blunt that, and callers depend on both:

      - `detail["settled"]` says whether the read actually stopped early. False means the
        attempts ran out while bodies were still arriving, which is not evidence of
        anything — the bodies that would have shown the leak are exactly the ones that
        had not been written yet. Callers must refuse to certify a run on it.
      - Read this *after* a check that has its own positive completeness criterion. The
        structure check knows which formats it asked for and polls until each one's
        document has arrived, so by the time it returns the writer has demonstrably
        landed at least the bodies carrying data. Ordering matters more than duration
        here: reading the logs before that wait is what let a late body go unseen, and a
        longer poll in the wrong place would still be a race.

    A leak is conclusive — further bodies cannot un-leak one — so the scan stops as soon
    as it finds one, which keeps the common passing case from paying the full window.
    """
    detail, err, waited, previous = None, None, 0, None
    for attempt in range(attempts):
        detail, err = check_logs_once(truth, since)
        if err:
            return None, err
        if early_on_leak and detail["leaked"]:
            detail["waited"] = waited
            detail["settled"] = True
            return detail, None
        if detail["rows"] == previous:
            detail["waited"] = waited
            detail["settled"] = True
            return detail, None
        previous = detail["rows"]
        if attempt < attempts - 1:
            time.sleep(pause)
            waited += pause
    detail["waited"] = waited
    detail["settled"] = False
    return detail, None


def check_logs_once(truth, since):
    """One pass over the gateway's own database. Returns (detail, error).

    Split from `check_logs` so the polling around it can be tested without a cluster, and
    so a caller that genuinely wants a single read can ask for one and say so.
    """
    script = r'''
import json, os, re, sys, datetime
try:
    import psycopg2
except ImportError:
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
    DBAPI = psycopg
else:
    conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
    DBAPI = psycopg2

# LiteLLM_SpendLogs, and specifically its `proxy_server_request` column.
#
# `messages` is populated as {} even with store_prompts_in_spend_logs on, so it cannot be
# the verdict. `proxy_server_request` holds the full body the guardrails rewrote — verified
# by reading back a tool_result and finding the masked values in it — so it is what the
# provider was actually called with. That is the column the verdict rests on.
sql = """
select "startTime", "proxy_server_request" from "LiteLLM_SpendLogs"
where "proxy_server_request" is not null
  and "startTime" >= %s::timestamptz
order by "startTime" desc limit 40
"""
cur.execute(sql, (sys.argv[1],))
rows = cur.fetchall()

# Real values from the fixture, long enough to be unambiguous, keyed by whether their
# column is one the guardrails claim to cover. Passed as {"leak": {value: column},
# "gap": {...}} so a hit can be attributed to a column rather than just reported as a
# bare string, which is what makes the known-gap split possible on this side too.
needle = json.loads(sys.argv[2])
tokens = 0
leaked = []
gaps = []
truncated = []
for start, body in rows:
    text = body if isinstance(body, str) else json.dumps(body)
    # LiteLLM replaces long strings with an elision before persisting them. A body cut
    # this way is not evidence of anything: the values being searched for may be in the
    # part that was dropped, so a truncated body scans clean and the check passes for the
    # wrong reason. Recorded and reported, never counted as proof. This bit for real —
    # MAX_STRING_LENGTH_PROMPT_IN_DB is raised in 22-litellm.yaml to prevent it.
    if "litellm_truncated" in text:
        truncated.append(str(start))
        continue
    if re.search(r"<PERSON>|<LOCATION>|<[A-Z_]+>|\[[A-Z_]+\]", text):
        tokens += 1

    # Search the tool_result blocks, not the whole body. The system prompt and tool
    # definitions travel in the same JSON and are full of English words: searching the
    # whole body flagged a customer's surname that happened to be a common word, which
    # is a false positive that would have masked a real leak. Only data the guardrails
    # are responsible for is in scope.
    haystack = []
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        haystack = [text]
    else:
        for m in doc.get("messages", []):
            content = m.get("content")
            blocks = content if isinstance(content, list) else [{"content": content}]
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") in ("tool_result", "text"):
                    payload = b.get("content")
                    haystack.append(payload if isinstance(payload, str)
                                    else json.dumps(payload))
    for chunk in haystack:
        low = chunk.lower()
        for bucket, target in (("leak", leaked), ("gap", gaps)):
            hit = next((v for v in needle.get(bucket, {}) if v.lower() in low), None)
            if hit is not None:
                target.append([str(start), hit, needle[bucket][hit]])
                break
print(json.dumps({"rows": len(rows), "masked": tokens, "leaked": leaked[:20],
                  "gaps": gaps[:20], "truncated": truncated}))
'''
    truth = truth_rows()
    # Only values long enough to be distinctive; "Lyon" is fine, "FR" is not. A short
    # value that is also an ordinary word (the fixture has a surname LANG) matches
    # unrelated text and makes the check cry wolf.
    #
    # Keyed by column, not a flat list, so the pod can tell a leak of a covered column
    # from one of a known-gap column. The two have to stay separable all the way to the
    # verdict: a zipcode reaching the provider is out of scope per pii-guardian-ax0, while
    # a city reaching the provider is a rule that stopped working, and averaging them
    # into one count hides the second behind the first.
    values = {}
    for row in truth.values():
        for col in PII_COLUMNS:
            v = str(row.get(col, "")).strip()
            if len(v) < SHORT_MIN or is_token(v) or v.isalpha():
                continue
            bucket = "gap" if col in UNCOVERED_COLUMNS else "leak"
            values.setdefault(bucket, {}).setdefault(v, col)
    proc = subprocess.run(
        ["kubectl", "exec", "-i", "-n", "gateway", "deploy/litellm", "--",
         "python3", "-c", script, since, json.dumps(values)],
        capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        return None, f"log query failed: {proc.stderr.strip()[:300]}"
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1]), None
    except (json.JSONDecodeError, IndexError) as e:
        return None, f"unparseable log output: {e}: {proc.stdout[:200]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", choices=sorted(QUERIES), default=None)
    ap.add_argument("--check-logs-only", action="store_true")
    args = ap.parse_args()

    if not DB.is_file():
        sys.exit(f"missing {DB} — regenerate with scripts/create_leaked_db.py")
    if not TRUTH.is_file():
        sys.exit(f"missing ground truth {TRUTH}")
    truth = truth_rows()

    if args.check_logs_only:
        since = os.environ.get("E2E_SINCE") or \
            datetime.datetime.now(datetime.timezone.utc).isoformat()
        detail, err = check_logs(truth, since)
        if err:
            sys.exit(err)
        print(json.dumps(detail, indent=2))
        return 0 if not detail["leaked"] else 1  # known-gap values are not a failure

    run_started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    formats = [args.format] if args.format else sorted(QUERIES)
    print(f"{'format':9} {'raw':>6} {'covered':>8} {'rows':>6} {'verdict':>9}")
    print("-" * 46)
    print("  'raw'      = PII cells in the fixture, with no gateway in the path (the control)")
    print("  'covered'  = of those, the ones in columns the guardrails detect. Excludes the")
    print(f"              {len(UNCOVERED_COLUMNS)} known-gap columns ({', '.join(sorted(UNCOVERED_COLUMNS))}),")
    print("              out of scope by decision (pii-guardian-ax0), reported below.")
    print("-" * 46)

    failures = []
    known_gap_total = 0
    # An empty scratch dir, so the request is the export prompt and the tool definitions —
    # nothing else. Launched from the repo, Claude Code picks up AGENTS.md and the bead
    # tooling, which together push the prompt past the 200k limit and abort with
    # "prompt_too_long" before a single tool call. That failure is invisible as a leak,
    # so the isolation is not optional.
    with tempfile.TemporaryDirectory(prefix="pii-e2e-") as scratch:
        for fmt in formats:
            # The control: no gateway, so these are the values the guardrails must stop.
            _, control_rows = leaked_from_sqlite(fmt)
            if control_rows:
                raw = len(unmasked_cells(control_rows, truth))
                raw_covered = len(unmasked_cells(control_rows, truth, UNCOVERED_COLUMNS))
            else:
                raw = raw_covered = "?"

            reply, err = run_claude(fmt, scratch)
            if err:
                print(f"{fmt:9} {str(raw):>6} {'—':>8} {'—':>6} {'ERROR':>9}")
                print(f"    {err}")
                failures.append(fmt)
                continue

            note, rows = parse(fmt, reply)
            if rows is None:
                print(f"{fmt:9} {str(raw):>6} {'—':>8} {'—':>6} {'BROKEN':>9}")
                print(f"    {note}")
                failures.append(fmt)
                continue

            leaks = unmasked_cells(rows, truth, UNCOVERED_COLUMNS)
            gaps = unmasked_cells(rows, truth, skip=set(PII_COLUMNS) - UNCOVERED_COLUMNS)
            known_gap_total += len(gaps)
            ok = not leaks
            print(f"{fmt:9} {str(raw):>6} {len(leaks):>8} {len(rows):>6} "
                  f"{'PASS' if ok else 'FAIL':>9}")
            for rid, col, val in leaks[:8]:
                print(f"    LEAK      {rid}.{col} = {val!r}")
            if gaps:
                print(f"    known gap {len(gaps)} cells in "
                      f"{', '.join(sorted({c for _, c, _ in gaps}))} (pii-guardian-ax0)")
            if not ok:
                failures.append(fmt)

    # Captured before the first request, not after: the gateway's spend-log writes are
    # asynchronous, so a timestamp taken afterwards can miss the very bodies being judged.
    #
    # This driver has no structure check to run first, so unlike e2e_export_mcp.py it
    # cannot lean on another check's positive completeness criterion. Polling and the
    # `settled` flag are the whole mitigation here, which is why an unsettled read is a
    # failure below and not a pass: a quiet half-minute is not proof that nothing is still
    # in flight.
    detail, err = check_logs(truth, run_started)
    print()
    if err:
        print(f"gateway log check: {err}")
        failures.append("logs")
    else:
        if detail.get("waited"):
            print(f"    (waited {detail['waited']}s for async body persistence)")
        print(f"gateway logs: {detail['rows']} request bodies recorded, "
              f"{detail['masked']} carrying mask tokens, "
              f"{len(detail['leaked'])} covered-column values reached the provider")
        for when, value, col in detail["leaked"][:8]:
            print(f"    LEAK      provider was sent {value!r} ({col})  {when}")
        if detail.get("gaps"):
            known_gap_total += len(detail["gaps"])
            print(f"    known gap {len(detail['gaps'])} value(s) in "
                  f"{', '.join(sorted({c for _, _, c in detail['gaps']}))} "
                  f"reached the provider (pii-guardian-ax0)")
        if detail.get("truncated"):
            print(f"    WARN  {len(detail['truncated'])} body/bodies were stored truncated "
                  f"and cannot be used as evidence — raise "
                  f"MAX_STRING_LENGTH_PROMPT_IN_DB in manifests/22-litellm.yaml")
            failures.append("logs")
        if not detail["rows"]:
            # The audit found no request to audit. Reporting that as clean would be the
            # worst possible outcome: a check that passes because it had nothing to look
            # at is indistinguishable, in the output, from one that passed on real
            # evidence. store_prompts_in_spend_logs off, a wrong namespace, a clock skew
            # between here and the pod, or writes still in flight all produce this.
            print("    ERROR no request body was recorded for this run, so there is no "
                  "evidence to judge — see the run above for whether any request was made")
            failures.append("logs")
        # A body recorded with no mask token at all would mean the guardrails never saw
        # this traffic, which is worth surfacing rather than passing over.
        elif not detail["masked"]:
            print("    note  no mask tokens in any recorded body — masking may not have run")
        if detail["leaked"]:
            failures.append("logs")
        if not detail.get("settled", False):
            # Reading before the async writes finished once produced "0 covered-column
            # values reached the provider" over a run that had leaked twelve addresses.
            # The bodies still to be written are exactly the ones carrying the document,
            # so an unsettled read cannot rule a leak out and must not be reported as one.
            print(f"    ERROR the body count was still growing when the read gave up "
                  f"({detail['rows']} seen). The bodies that would show a leak may not "
                  f"have been written\n           yet, so this is not a clean result.")
            failures.append("logs-unsettled")

    print()
    if known_gap_total:
        print(f"  {known_gap_total} cell(s) in {', '.join(sorted(UNCOVERED_COLUMNS))} were not "
              f"masked. Those columns are")
        print("  out of scope by decision (pii-guardian-ax0), not a regression, and they "
              "are excluded from")
        print("  the verdict above on purpose — so the verdict stays a statement about "
              "what the guardrails")
        print("  claim to cover, not about whether any PII is left.")
    if failures:
        print(f"✗ E2E failed: {', '.join(failures)}")
        return 1
    print("✓ E2E passed: no raw PII from a covered column reached the model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
