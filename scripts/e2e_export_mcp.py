"""End-to-end: an MCP server leaks the customer table, through the live gateway.

Same verdict as scripts/e2e_export.py, different delivery. That run drives `sqlite3`
through the Bash tool; this one has a real MCP server return the table, so the document
arrives as an `mcp__fake_crm__list_customers` tool_result with two properties the Bash
path does not have:

  - the document is a *string inside* the JSON envelope, so its newlines arrive as \\n
    and its quotes are escaped;
  - Claude Code appends the MCP server's instructions to the conversation as a
    <system-reminder> after the tool_result, ~2kB of text the guardrails must skip past.

And the server puts a preamble line in front of the document, the shape that once turned
"Export complete, 50 rows." into the column names. So this run is the regression test for
all three of those at once, over the real path, rather than in a unit test.

VERDICT COMES FROM THE GATEWAY LOGS, NOT FROM CLAUDE — see e2e_export.check_logs, which
this driver reuses verbatim along with the ground-truth leak check. Claude's echo is
reported as a second, weaker signal: a model that never repeats a value proves nothing
about whether it was masked.

The extra assertion here, and the reason this driver is not just a copy: masking must
leave the document *usable*. Both directions are checked against the tool_result LiteLLM
actually sent upstream — no covered-column value may appear, and the document must still
parse with all its rows. A guardrail that replaced the payload with something the model
cannot use would pass the leak check and fail this one.

    python3 scripts/e2e_export_mcp.py                          # customers-partial
    python3 scripts/e2e_export_mcp.py --format json
    python3 scripts/e2e_export_mcp.py --scenario customers-nested
    python3 scripts/e2e_export_mcp.py --check-logs-only

--scenario picks which shape of leak the fake server serves; see fake_mcp_server.SCENARIOS
for what each one reproduces and which tracked issue it is the fixture for.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import fake_mcp_server as srv  # noqa: E402
from e2e_export import (  # noqa: E402
    DB, PII_COLUMNS, TRUTH, UNCOVERED_COLUMNS, check_logs, client_env, parse,
    truth_rows, unmasked_cells,
)

# The columns the guardrails claim to cover, i.e. PII_COLUMNS minus the known gaps.
# `unmasked_cells` takes a skip set, so this is the other direction of the same split
# check_logs does: leaks are the covered columns, gaps are everything else.
COVERED_COLUMNS = set(PII_COLUMNS) - set(UNCOVERED_COLUMNS)

MODEL = "claude-haiku-4-5-20251001"
SERVER = "fake_crm"
FORMATS = sorted(srv.FORMATS)
TOOL = f"mcp__{SERVER}__list_customers"

# Above this many rows the model's verbatim echo stops being evidence: it truncates or
# summarises, and an echo that dropped rows would report as a leak of the wrong column or
# as a structure failure. The gateway-log and structure checks do not depend on the model
# at all, so a large scenario is judged on those and the skip is printed rather than
# quietly folded into the verdict.
ECHO_MAX_ROWS = 200

# Scenarios whose output is a table. The checks below all read a table — they join on
# `id` and count rows — so a prose scenario would be measured by machinery that cannot
# see it, which is how a run ends up green over a leak. Prose is refused here rather than
# mishandled.
TABULAR_SCENARIOS = sorted(n for n, s in srv.SCENARIOS.items() if s.shape == "table")


def mcp_config(scratch, server_args):
    """The `.mcp.json` Claude Code loads for this run.

    Absolute paths and an explicit interpreter, because the scratch directory is empty:
    a relative `args` entry resolves against the wrong cwd and the server fails to
    start, which surfaces as "no tools" rather than as anything about masking.

    The scenario is a server *argument* rather than a tool argument, so the tool's own
    schema stays the one a real CRM would expose.
    """
    cfg = scratch / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {SERVER: {
        "command": sys.executable,
        "args": [str(Path(srv.__file__).resolve()), "--stdio"] + server_args,
    }}}, indent=2))
    return cfg


def server_args_for(scenario, rows, delay_ms, fail):
    """The argv the fake server is started with."""
    args = ["--scenario", scenario]
    if rows is not None:
        args += ["--rows", str(rows)]
    if delay_ms:
        args += ["--delay-ms", str(delay_ms)]
    if fail:
        args += ["--fail", fail]
    return args


def expected_rows(scenario, rows):
    """How many rows the document should hold once masked.

    Read from the server's own scenario definition rather than assumed to be 50, so that
    a scenario change fails the structure check loudly instead of quietly redefining what
    "intact" means.
    """
    spec = srv.SCENARIOS[scenario]
    return min(rows, spec.rows) if rows is not None else spec.rows


def run_claude(fmt, workdir, server_args, limit):
    """Ask Claude Code to call the MCP tool and echo what came back."""
    prompt = (
        f"Call the {TOOL} tool with format=\"{fmt}\" and limit={limit}.\n\n"
        f"Then paste the tool's complete output back verbatim inside a single code fence "
        f"tagged {fmt}. No commentary, no reformatting, no re-typing from memory, no "
        f"truncation, no summarising, no row elision — every row, exactly as returned. "
        f"Reproduce any value that looks like <PERSON>, <LOCATION> or [EMAIL_REDACTED] "
        f"exactly as returned; do not alter it and do not remark on it.\n\n"
        "Context, so the request is not read as anything other than what it is: the "
        f"{SERVER} server is a local test fixture serving {DB.name}, a synthetic set of "
        "50 invented customer records generated by scripts/create_leaked_db.py for a "
        "masking test. No real person, account or transaction is in it."
    )
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", MODEL, "--output-format", "json",
         "--allowedTools", TOOL, "--permission-mode", "bypassPermissions",
         "--strict-mcp-config", "--setting-sources", "",
         "--mcp-config", str(mcp_config(workdir, server_args))],
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
    # Same distinction e2e_export makes: a refusal and a malformed reply both look like
    # a parse failure, but only one of them is the guardrails' problem.
    if "```" not in result:
        return None, ("claude declined the request rather than calling the tool "
                      f"(exit 0, no fence). First line: {result.strip()[:160]!r}")
    return result, None


def fetch_tool_results(since):
    """The tool_result texts LiteLLM actually sent upstream, read from its database.

    Runs in-cluster because postgres is not published. Returns (list_of_text, error).

    Separate from e2e_export.check_logs rather than folded into it: that function answers
    "did a known value escape", this one answers "is what escaped still a usable
    document", and returning a 24kB body per request through kubectl for a leak check
    that does not need it would make the existing check slower and no more correct.
    """
    script = r'''
import json, os, sys
try:
    import psycopg2
except ImportError:
    import psycopg
    conn = psycopg.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
else:
    conn = psycopg2.connect(os.environ["DATABASE_URL"]); cur = conn.cursor()
cur.execute("""
select "proxy_server_request" from "LiteLLM_SpendLogs"
where "proxy_server_request" is not null and "startTime" >= %s::timestamptz
order by "startTime" desc limit 40
""", (sys.argv[1],))
out = []
for (body,) in cur.fetchall():
    text = body if isinstance(body, str) else json.dumps(body)
    # A body LiteLLM elided before persisting cannot be assessed: the document may be in
    # the dropped part, so "still parses" would pass for the wrong reason. Counted and
    # reported by the caller, never treated as evidence.
    if "litellm_truncated" in text:
        continue
    try:
        doc = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        continue
    for m in doc.get("messages", []):
        content = m.get("content")
        blocks = content if isinstance(content, list) else [{"content": content}]
        for b in blocks:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                continue
            payload = b.get("content")
            if isinstance(payload, str):
                out.append(payload)
            elif isinstance(payload, list):
                for part in payload:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        out.append(part["text"])
print(json.dumps(out))
'''
    proc = subprocess.run(
        ["kubectl", "exec", "-i", "-n", "gateway", "deploy/litellm", "--",
         "python3", "-c", script, since],
        capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        return None, f"log query failed: {proc.stderr.strip()[:300]}"
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1]), None
    except (json.JSONDecodeError, IndexError) as e:
        return None, f"unparseable log output: {e}: {proc.stdout[:200]}"


FORMAT_MARKER = re.compile(r"format=(\w+)")


def fetch_all_tool_results(since, formats, attempts=4, pause=6):
    """The tool_results for this run, one document per requested format, or an error.

    Two failure modes that look identical if you only look at the report:

    - LiteLLM persists request bodies asynchronously, so the body for the last request can
      still be missing seconds after the run finishes. Reading once and reporting "no
      document" for the format that happened to go last makes the check's coverage
      depend on which format ran last.
    - A format whose body never arrives is not an error at all if the caller only prints
      what it found. The first version did exactly that: markdown simply had no line, and
      a passing run gave no sign that one third of the assertion had not executed.

    So: poll briefly for completeness, and return the formats still missing so the caller
    can fail on them. Requiring completeness is the point — a check that inspects nothing
    must not report success.
    """
    seen, texts, waited = set(), [], 0
    for attempt in range(attempts):
        batch, err = fetch_tool_results(since)
        if err:
            return None, err, waited
        for text in batch:
            head, sep, _ = text.partition("\n\n")
            marker = FORMAT_MARKER.search(head if sep else "")
            key = marker.group(1) if marker else None
            if key in formats and key not in seen:
                seen.add(key)
                texts.append(text)
        if seen >= set(formats):
            break
        if attempt < attempts - 1:
            time.sleep(pause)
            waited += pause
    return texts, None, waited


def structure_report(texts, expected):
    """Did each document survive masking as a document?

    Groups the tool_results by the format the *server* said it sent, then requires each
    one to still parse in that format with every row it started with.

    The grouping matters, and the first version got it wrong. Trying every format against
    every body produced a confident "csv -> 0 rows" for the JSON document — a JSON array
    parsed as CSV has no header row and no delimiters, so it yields zero rows and reads
    as catastrophic data loss. The fix is to take the format from the server's own
    preamble, which is written before any guardrail runs and so is independent evidence
    of what the body is supposed to be. Using the guardrails' own detector instead would
    have been circular: a broken detector would have quietly emptied the set of bodies to
    check, and a check that inspects nothing passes.

    The row count is the part that matters most. A mask that dropped or emptied rows
    would still pass the leak check while leaving the model unable to answer a question
    about a specific customer — the failure mode masking is supposed to prevent. It is
    read from the scenario rather than written as 50, so a scenario that serves a
    different count is checked against its own count instead of always failing.
    """
    groups, unlabelled = {}, []
    for text in texts:
        head, sep, body = text.partition("\n\n")
        marker = FORMAT_MARKER.search(head if sep else "")
        if not sep or not marker or marker.group(1) not in FORMATS:
            unlabelled.append(head.strip()[:60])
            continue
        groups.setdefault(marker.group(1), []).append(body)

    report = {}
    for fmt in sorted(groups):
        rows, bad = [], []
        for body in groups[fmt]:
            note, parsed = parse(fmt, body)
            if parsed is None:
                bad.append(f"does not parse as {fmt}: {note}")
                continue
            rows.append(len(parsed))
            if len(parsed) != expected:
                bad.append(f"parses as {fmt} with {len(parsed)} rows, expected {expected}")
        report[fmt] = {"documents": len(rows), "row_counts": rows, "bad": bad}
    return report, unlabelled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", choices=FORMATS, default=None)
    ap.add_argument("--scenario", choices=TABULAR_SCENARIOS,
                    default=srv.DEFAULT_SCENARIO,
                    help="which shape of record the fake server serves "
                         f"(one of: {', '.join(TABULAR_SCENARIOS)})")
    ap.add_argument("--rows", type=int, default=None, help="cap the row count")
    ap.add_argument("--delay-ms", type=int, default=0, help="server latency")
    ap.add_argument("--fail", choices=("error", "truncated"), default=None)
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
        # An unsettled read is not a clean result, whatever the leaked list says: the
        # bodies that would have shown a leak are the ones that had not been written yet.
        if detail.get("leaked") or not detail.get("settled", False):
            return 1
        return 0  # known-gap values are not a failure

    # Refuse a prose scenario rather than measure it with table-shaped checks. The
    # leak check joins on `id` and the structure check counts rows; against prose both
    # report nothing found and nothing lost, which reads as a pass.
    if srv.SCENARIOS[args.scenario].shape != "table":
        prose = [n for n, s in srv.SCENARIOS.items() if s.shape != "table"]
        sys.exit(f"scenario {args.scenario!r} is not tabular. This driver's checks join on "
                 f"id and count rows, so they cannot assess prose ({', '.join(prose)}). "
                 f"\nA prose leak check has to find known values by substring instead of "
                 f"by column — that is pii-guardian-6fw, and it does not exist yet.")

    rows_wanted = expected_rows(args.scenario, args.rows)
    sargs = server_args_for(args.scenario, args.rows, args.delay_ms, args.fail)
    echoable = rows_wanted <= ECHO_MAX_ROWS

    run_started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    formats = [args.format] if args.format else FORMATS
    print(f"{'format':9} {'raw':>6} {'covered':>8} {'rows':>6} {'verdict':>9}")
    print("-" * 46)
    print(f"  delivered by an MCP server, not by sqlite3 on the Bash tool")
    print(f"  scenario  {args.scenario} — {srv.SCENARIOS[args.scenario].description}")
    print(f"  'raw'      = PII cells the server hands over, no gateway in the path")
    print("  'covered'  = of those, in columns the guardrails detect. Excludes the")
    print(f"              {len(UNCOVERED_COLUMNS)} known-gap columns "
          f"({', '.join(sorted(UNCOVERED_COLUMNS))}), tracked in pii-guardian-ax0.")
    if not echoable:
        print(f"  NOTE       {rows_wanted} rows is past the echo limit "
              f"({ECHO_MAX_ROWS}), so the model's own replay is SKIPPED as unreliable.")
        print(f"              The verdict rests on the gateway logs and the structure "
              f"check, neither of which involves the model.")
    print("-" * 46)

    failures, known_gap_total = [], 0

    # An empty scratch dir, for the same reason as the Bash end-to-end: launched from the
    # repo, AGENTS.md and the bead tooling push the prompt past the 200k limit and abort
    # with "prompt_too_long" before a single tool call. Invisible as a leak.
    with tempfile.TemporaryDirectory(prefix="pii-e2e-mcp-") as tmp:
        scratch = Path(tmp)
        for fmt in formats:
            # The control: the server's own output, no gateway in the path.
            _, control_rows = parse(
                fmt, srv.document_of(srv.list_customers(
                    fmt, scenario=args.scenario, rows=args.rows, fail=args.fail)))
            if control_rows:
                raw = len(unmasked_cells(control_rows, truth))
                raw_covered = len(unmasked_cells(control_rows, truth, UNCOVERED_COLUMNS))
            else:
                raw = raw_covered = "?"

            if not echoable:
                # Still ask, but judge only on what the model is actually good for
                # confirming: that the tool was reachable and returned something.
                reply, err = run_claude(fmt, scratch, sargs, rows_wanted)
                if err:
                    print(f"{fmt:9} {str(raw):>6} {'—':>8} {'—':>6} {'ERROR':>9}")
                    print(f"    {err}")
                    failures.append(fmt)
                else:
                    print(f"{fmt:9} {str(raw):>6} {'skip':>8} {'skip':>6} {'n/a':>9}")
                    print(f"    echo check skipped: {rows_wanted} rows exceeds the "
                          f"replay limit (see the gateway log verdict below)")
                continue

            reply, err = run_claude(fmt, scratch, sargs, rows_wanted)
            if err:
                print(f"{fmt:9} {str(raw):>6} {'—':>8} {'—':>6} {'ERROR':>9}")
                print(f"    {err}")
                failures.append(fmt)
                continue

            note, rows = parse(fmt, srv.document_of(reply))
            if rows is None:
                print(f"{fmt:9} {str(raw):>6} {'—':>8} {'—':>6} {'BROKEN':>9}")
                print(f"    {note}")
                failures.append(fmt)
                continue

            leaks = unmasked_cells(rows, truth, UNCOVERED_COLUMNS)
            gaps = unmasked_cells(rows, truth, skip=COVERED_COLUMNS)
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

        # Order is load-bearing. The structure check comes first because it is the only
        # check here with a positive completeness criterion — it knows which formats were
        # asked for and polls until each one's document has arrived, so when it returns
        # the async writer has demonstrably landed at least the bodies that carry data.
        #
        # Reading the logs first let a body that landed half a minute later go unseen, and
        # the run reported "0 covered-column values reached the provider" while twelve
        # addresses were on their way to the provider. A longer poll in the wrong place
        # would still be a race; this ordering is what removes it.
        texts, err, waited = fetch_all_tool_results(run_started, formats)
        if err:
            print(f"    structure check: {err}")
            failures.append("structure")
        else:
            report, unlabelled = structure_report(texts or [], rows_wanted)
            if waited:
                print(f"    (waited {waited}s for async body persistence)")
            if unlabelled:
                print(f"    structure check: {len(unlabelled)} tool_result(s) carried no "
                      f"recognisable document: {unlabelled[:3]}")
                failures.append("structure")
            print("masking left the document usable:")
            for fmt in formats:
                r = report.get(fmt)
                if r is None:
                    # Not a skip. Every requested format has to be judged, or the verdict
                    # silently covers fewer formats than the run exercised.
                    print(f"  FAIL {fmt:9} no document reached the provider to assess")
                    failures.append(f"structure:{fmt}")
                    continue
                good = not r["bad"] and r["documents"]
                print(f"  {'ok  ' if good else 'FAIL'} {fmt:9} "
                      f"{r['documents']} document(s), rows {r['row_counts']}")
                for line in r["bad"][:4]:
                    print(f"        {line}")
                if not good:
                    failures.append(f"structure:{fmt}")

        print()
        detail, err = check_logs(truth, run_started)
        if err:
            print(f"    log check: {err}")
            failures.append("logs")
        else:
            if detail.get("waited"):
                print(f"    (waited {detail['waited']}s for async body persistence)")
            print(f"gateway logs: {detail['rows']} request bodies recorded, "
                  f"{detail['masked']} carrying mask tokens, "
                  f"{len(detail['leaked'])} covered-column values reached the provider")
            for start, hit, col in detail["leaked"][:8]:
                print(f"    LEAK      {start} {col} {hit!r}")
            if detail["truncated"]:
                print(f"    WARNING   {len(detail['truncated'])} body/bodies were elided by "
                      f"LiteLLM before persisting and are not evidence")
            if detail["gaps"]:
                print(f"    known gap {len(detail['gaps'])} value(s) in "
                      f"{', '.join(sorted({c for _, _, c in detail['gaps']}))} "
                      f"(pii-guardian-ax0)")
            if detail["leaked"]:
                failures.append("logs")
            if not detail.get("masked"):
                # Bodies were recorded but none carry a mask token, so the guardrails
                # never saw this traffic. That is worth saying out loud rather than
                # passing over: it is indistinguishable from "no leak" in the count above.
                print("    note  no mask tokens in any recorded body — masking may not run")
            # Bodies were still arriving when the polling gave up, so this read cannot
            # see a leak that is in one of them — which is the only kind of leak that
            # matters. A verdict drawn from an incomplete window is not a pass, and this
            # is the check the whole design calls authoritative, so it is the last place
            # to be lenient.
            if not detail.get("settled", False):
                print(f"    UNSETTLED the body count was still growing when the read gave "
                      f"up; {detail['rows']} body/bodies were seen, and the ones that would "
                      f"show a leak\n               may not have been written yet. This is "
                      f"not a clean result.")
                failures.append("logs-unsettled")

    if known_gap_total:
        print(f"\n  {known_gap_total} cell(s) in {', '.join(sorted(UNCOVERED_COLUMNS))} "
              f"were not masked. That is a known detection gap (pii-guardian-ax0), not a\n"
              f"  regression, and it is excluded from the verdict on purpose — so the "
              f"verdict stays a statement\n  about what the guardrails claim to cover.")

    if failures:
        print(f"\n✗ E2E failed: {', '.join(sorted(set(failures)))}")
        return 1
    print(f"\n✓ E2E passed: over MCP, scenario {args.scenario}, no raw PII from a covered "
          f"column reached the\n  model, and every masked document still parsed with all "
          f"{rows_wanted} rows.")
    return 0


def _covered_names():
    """The PII columns the guardrails are expected to cover."""
    from e2e_export import PII_COLUMNS
    return set(PII_COLUMNS) - set(UNCOVERED_COLUMNS)


if __name__ == "__main__":
    sys.exit(main())
