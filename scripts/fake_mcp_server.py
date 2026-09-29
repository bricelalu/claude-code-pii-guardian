"""A fake MCP server for end-to-end tests. It hands back customer data on purpose.

Exists so an end-to-end can test the delivery path that actually matters in production.
scripts/e2e_export.py drives the same table through `sqlite3` on the Bash tool, but an
MCP tool result is a different shape of message:

  - it arrives as `mcp__fake_crm__list_customers`, and Claude Code appends the server's
    instructions to the conversation as a ~2kB <system-reminder> after the tool_result;
  - the document travels as a *string inside* the JSON envelope, so its newlines arrive
    as \\n and its quotes are escaped.

That second point is the one worth a separate run. A document embedded in a JSON string
is not the input the guardrails' document detection was originally written against, and
getting it wrong does not fail loudly — it returns the string unchanged.

## Scenarios

A scenario is a named shape of leak, so a later end-to-end can dial in exactly the
failure mode it is about instead of writing another server. Each one is pinned to the
tracked issue it reproduces, and `customers-nested` is asserted to still be a live leak:
if the guardrails get fixed, that test fails and the scenario has to be re-pointed on
purpose, rather than the end-to-end quietly reporting green over a fixed bug.

  customers-partial    the half-masked fixture, as stored. The control every run needs:
                       if this were already clean the harness would pass with the
                       guardrails doing nothing.
  customers-raw        fully unmasked, joined to the ground truth. Exposes zipcode and
                       birth_date, the two columns with no detection rule at all and
                       out of scope by decision (pii-guardian-ax0) — the only place
                       that is visible.
  customers-nested     the same rows one level down, under customer.profile. LeakGuard
                       completes by leaf path, so a nested token covers its column
                       (pii-guardian-64g, fixed).
  customers-oversized  far more rows than the fixture holds, for the latency budget and
                       the detection path at scale (pii-guardian-pl2).
  free-text            prose with PII and no table anywhere, so there is no column to
                       read a token from (pii-guardian-6fw).

## Knobs

Orthogonal to the scenario: --rows, --delay-ms, --fail (error | truncated). Truncation is
here because a guardrail handed half a document must not treat the half as the whole,
which is the failure that once made detection return None.

Deliberately dependency-free. MCP over stdio is newline-delimited JSON-RPC 2.0, and the
whole surface used here is initialize / tools/list / tools/call. Adding a client library
to read three rows out of SQLite would mean an install step in the middle of a test whose
job is to fail for one reason at a time.

    python3 scripts/fake_mcp_server.py --stdio
    python3 scripts/fake_mcp_server.py --print csv --scenario customers-nested
"""
import argparse
import csv
import io
import json
import os
import re
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(REPO, "leaked_customers.db")
TRUTH = os.path.join(REPO, ".pii-score-out", "export.csv")
TABLE = "customers_leaked"
FORMATS = ("json", "csv", "markdown")
MAX_ROWS = 50
OVERSIZE_ROWS = 5000

# A masking placeholder as the guardrails write it: <PERSON>, <LOCATION>, [EMAIL_REDACTED].
# Copied from the guardrails' own notion of a token rather than imported, so that this
# file stays runnable as a bare script — and so a change to the token format shows up
# here as a test failure instead of silently reclassifying masked cells as leaked ones.
TOKEN = re.compile(r"^(\[|<)[A-Z_]+(\]|>)$")

PII_COLUMNS = ["firstname", "lastname", "customer_email", "phone_number", "address",
               "zipcode", "city", "country", "iban", "last_login_ip", "birth_date"]

SERVER_INFO = {"name": "fake-crm", "version": "2.0.0"}
DEFAULT_PROTOCOL = "2024-11-05"
DEFAULT_SCENARIO = "customers-partial"

INSTRUCTIONS = (
    "# fake-crm\n\n"
    "Internal CRM. `list_customers` returns the customer table. Formats: json, csv, "
    "markdown. Rows are synthetic records used for masking tests."
)


def is_token(value):
    """Whether a cell is already a masking placeholder rather than data."""
    return bool(TOKEN.match(str(value).strip()))


# ── datasets ────────────────────────────────────────────────────────────────────────

def fetch(db=DB, table=TABLE, limit=MAX_ROWS):
    """The customer rows exactly as stored, read-only.

    Row order is explicit: SQLite promises nothing without it, and an end-to-end joins on
    `id`, so an unstable order would make the control and the measured run incomparable.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(f"select * from {table} order by id limit ?",
                           (int(limit),)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    finally:
        con.close()


def truth_by_id(truth=TRUTH):
    """The unmasked values, keyed by id, from the end-to-end ground truth.

    Only the raw scenario needs these. Reading them from the same file the end-to-end
    verifies against is deliberate: a second, independent copy of the real values would
    be one more thing that can disagree with the verdict.
    """
    if not os.path.isfile(truth):
        return {}
    with open(truth, newline="", encoding="utf-8") as fh:
        return {r["id"]: r for r in csv.DictReader(fh)}


def _partial(rows, **kw):
    return rows


def _raw(rows, truth=TRUTH, **kw):
    """Fully unmasked: every PII column replaced by its ground-truth value."""
    truth = truth_by_id(truth)
    out = []
    for row in rows:
        real = truth.get(str(row["id"]))
        out.append({c: (real.get(c) if real and real.get(c) not in (None, "") else v)
                    if c in PII_COLUMNS else v for c, v in row.items()})
    return out


def _nested(rows, **kw):
    """The same rows one level down, under customer.profile.

    `id` is deliberately left at the top level: the end-to-end joins on it, and nesting
    it too would make every row look unmatched and the leak check pass vacuously.
    """
    out = []
    for row in rows:
        rest = {c: v for c, v in row.items() if c not in ("firstname", "lastname", "address")}
        out.append({"id": row["id"], "customer": {"profile": {
            "firstname": row["firstname"], "lastname": row["lastname"],
            "address": row["address"]}}, **rest})
    return out


def _oversized(rows, **kw):
    """The fixture repeated until it is big enough to be a different code path.

    At 5000 rows the JSON body is ~370kB, which is where detection has to hold up. Not
    invented data: the same 50 records over and over, so a leak found here is the same
    leak as in customers-partial and the ground truth still applies.
    """
    out = []
    while len(out) < OVERSIZE_ROWS:
        out.extend(rows)
    return out[:OVERSIZE_ROWS]


def _free_text(rows, cap=None, **kw):
    """Prose with PII and no table anywhere — the pii-guardian-6fw shape.

    Returns a string rather than rows, which is what makes it a different *kind* of
    scenario: there is no column, so there is nothing for LeakGuard to read a masking
    token from and no sibling row to complete. The value is the contrast — the other four
    scenarios all have a table to lean on, and this one is the case where that table is
    what a fix would need to cope with not existing.
    """
    people = rows[:cap or 5]
    paragraphs = [
        f"{r['firstname']} {r['lastname']} wrote in from {r['address']}, "
        f"{r['city']} {r['zipcode']}. They can be reached on {r['phone_number']} "
        f"or {r['customer_email']}."
        for r in people]
    return (
        "Here are the customers who asked for a refund this week.\n\n"
        + "\n\n".join(paragraphs)
        + "\n\nLet me know if you would like the full list as a table instead.")


class Scenario:
    """A named shape of leak.

    `build` returns either rows (serialised by `render`) or, for a shape that is not a
    table at all, the finished document as a string. Both are allowed because the
    scenarios exist to reproduce *kinds* of leak, and "not tabular" is one of them.

    `shape` states which, rather than letting a caller infer it from the return value. The
    round-trip check only means something for a table, and a test that quietly skipped
    prose would be a test that stopped testing.
    """

    def __init__(self, name, description, build, rows=MAX_ROWS, shape="table"):
        self.name = name
        self.description = description
        self.build = build
        self.rows = rows
        self.shape = shape

    def rows_for(self, cap=None, **kw):
        return self.build(fetch(DB, TABLE, MAX_ROWS), cap=cap, **kw)


SCENARIOS = {s.name: s for s in (
    Scenario(
        "customers-partial",
        "The half-masked fixture exactly as stored. The control every run needs: if this "
        "were already clean, the harness would pass with the guardrails doing nothing.",
        _partial),
    Scenario(
        "customers-raw",
        "Fully unmasked, joined to the ground truth. Exposes zipcode and birth_date, the "
        "two columns with no detection rule at all and out of scope by decision "
        "(pii-guardian-ax0) — the only place that is observable from outside.",
        _raw),
    Scenario(
        "customers-nested",
        "The same rows one level down, under customer.profile. A document can put a PII "
        "column anywhere, so LeakGuard completes by leaf path rather than by top-level key "
        "— which is what it used to do, sending every sibling of a nested token to the "
        "provider raw (pii-guardian-64g, fixed).",
        _nested),
    Scenario(
        "customers-oversized",
        "5000 rows, about 370kB of JSON — where detection has to hold up rather than "
        "degrade (pii-guardian-pl2). Same 50 records repeated, so the ground truth and "
        "the leak check still apply.",
        _oversized, rows=OVERSIZE_ROWS),
    Scenario(
        "free-text",
        "Prose naming five customers, with no table anywhere. No column exists to read a "
        "token from, so completion has nothing to work from and only detection can help "
        "(pii-guardian-6fw).",
        _free_text, shape="prose"),
)}


def rows_for(scenario=DEFAULT_SCENARIO, cap=None, **kw):
    """The rows — or the document — a scenario serves."""
    spec = SCENARIOS.get(scenario)
    if spec is None:
        raise ValueError(
            f"unknown scenario {scenario!r}; available: {', '.join(sorted(SCENARIOS))}")
    return spec.rows_for(cap=cap, **kw)


# ── serialisers ─────────────────────────────────────────────────────────────────────

def render(rows, fmt):
    """The rows as a document in `fmt`.

    Every value goes through a real serialiser. The column names, the quoting and the
    markdown separator all matter to the guardrails under test: a hand-rolled join(",")
    would break the first time a customer note contained a comma, and the resulting shift
    of every column after it is indistinguishable from a leak.
    """
    if fmt == "json":
        return json.dumps(rows, separators=(",", ":"))
    if not rows:
        return ""
    if fmt == "csv":
        columns = list(_leaf_keys(rows))
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _get(row, c) for c in columns})
        return buf.getvalue().rstrip("\n")
    if fmt == "markdown":
        columns = list(_leaf_keys(rows))
        def cell(v):
            # Pipes would end the cell early and backslashes would escape the next
            # character, so both are neutralised the way CommonMark requires.
            return str(v).replace("\\", "\\\\").replace("|", "\\|")
        lines = ["| " + " | ".join(columns) + " |",
                 "|" + "|".join("---" for _ in columns) + "|"]
        for row in rows:
            lines.append("| " + " | ".join(cell(_get(row, c)) for c in columns) + " |")
        return "\n".join(lines)
    raise ValueError(f"unknown format: {fmt!r}")


def _leaf_keys(rows):
    """The column names for a set of rows, in first-seen order.

    A nested row has no flat header, so nested scenarios flatten for csv and markdown:
    `customer.profile.firstname`. csv and markdown have no way to express nesting, and
    emitting a bare `profile` column would silently drop the values under it.

    The union across rows matters as much as the flattening. Taking the keys of the
    first row alone would drop any column a later row adds — and a CSV writer given
    fieldnames it does not have a value for fills in blanks, so the loss would be silent
    and would look exactly like a masked cell.
    """
    keys = []
    seen = set()

    def walk(prefix, value):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(f"{prefix}.{k}" if prefix else k, v)
        elif prefix not in seen:
            seen.add(prefix)
            keys.append(prefix)

    for row in rows:
        for k, v in row.items():
            walk(k, v)
    return keys


def _get(row, path):
    for part in path.split("."):
        if not isinstance(row, dict) or part not in row:
            return ""
        row = row[part]
    return row if not isinstance(row, (dict, list)) else ""


def parse_back(doc, fmt):
    """Parse a rendered document back into rows.

    The inverse of `render`, and the reason `render` uses csv rather than string
    concatenation: without this, a formatter bug that shifted a column would only be
    discovered by the end-to-end, reported as a leak, and blamed on the guardrail.
    """
    doc = doc.strip()
    if fmt == "json":
        return json.loads(doc)
    if fmt == "csv":
        return list(csv.DictReader(io.StringIO(doc)))
    if fmt == "markdown":
        lines = [ln for ln in doc.splitlines() if ln.strip().startswith("|")]
        header = _split_md_cells(lines[0])
        out = [dict(zip(header, _split_md_cells(line))) for line in lines[2:]]
        return out
    raise ValueError(f"unknown format: {fmt!r}")


def _split_md_cells(line):
    """Split a markdown table row into cells, honouring `\\|` and `\\\\`.

    A plain `line.split("|")` gets this wrong in the direction that matters: a cell
    containing a pipe is counted as two cells, so every column after it shifts by one and
    the value read for a column is the tail of a neighbour. That is indistinguishable
    from a masking failure, which is why this is a real splitter and not a split.
    """
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    cells, buf, i = [], [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] in "|\\":
            buf.append(body[i + 1])
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


# ── the tool ────────────────────────────────────────────────────────────────────────

def list_customers(fmt="json", limit=None, scenario=DEFAULT_SCENARIO, rows=None,
                   delay_ms=0, fail=None):
    """The tool body. Separate from the protocol layer so it can be tested directly.

    The one-line preamble before the document is deliberate. A real MCP server says
    something before its payload, and the guardrails have to find the document underneath
    it rather than treat the first delimited line as the header — which is precisely the
    bug that turned "Export complete, 50 rows." into the column names.
    """
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}; got {fmt!r}")
    if delay_ms:
        time.sleep(delay_ms / 1000)
    spec = SCENARIOS.get(scenario)
    if spec is None:
        raise ValueError(
            f"unknown scenario {scenario!r}; available: {', '.join(sorted(SCENARIOS))}")
    cap = spec.rows if rows is None else max(1, min(int(rows), spec.rows))
    if limit is not None:
        cap = max(1, min(cap, int(limit)))
    data = rows_for(scenario, cap=cap)
    if isinstance(data, str):
        # A prose scenario has no rows to count and nothing to serialise, so the format
        # argument is only recorded in the preamble, which is how a real server asked for
        # a format it cannot honour would behave.
        body = data
    else:
        if not data:
            return "The customer table is empty."
        body = render(data, fmt)
        if fail == "truncated":
            # Cut mid-document rather than at a row boundary, which is the case that
            # matters: a guardrail handed half a table must not treat the half as the
            # whole.
            body = body[:int(len(body) * 0.6)]
    head = f"{cap} customer record(s), format={fmt}"
    if scenario != DEFAULT_SCENARIO:
        head += f", scenario={scenario}"
    return f"{head}.\n\n{body}"


def document_of(text):
    """The document part of a tool result: everything after the first blank line."""
    _, sep, body = text.partition("\n\n")
    return body if sep else text


# ── MCP protocol ────────────────────────────────────────────────────────────────────

def _result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}],
            "isError": bool(is_error)}


def call_tool(name, args=None, **overrides):
    """Invoke a tool the way the protocol layer does, returning the MCP result object."""
    args = dict(args or {})
    if name != "list_customers":
        return _result(f"No such tool: {name!r}. Available: list_customers.", True)
    if overrides.get("fail") == "error":
        return _result("upstream CRM is unavailable (simulated failure)", True)
    try:
        text = list_customers(
            args.get("format", "json"), args.get("limit"),
            # `or`, not `.get(k, default)`: a caller that omits the key and a caller that
            # passes None both mean "the default", and the second is what a JSON-RPC
            # arguments object with an explicit null gives us.
            args.get("scenario") or DEFAULT_SCENARIO,
            rows=overrides.get("rows"), delay_ms=overrides.get("delay_ms", 0),
            fail=overrides.get("fail"))
    except (ValueError, TypeError) as e:
        return _result(str(e), True)
    return _result(text)


DEFAULT_ARGS = {"scenario": DEFAULT_SCENARIO, "rows": None, "delay_ms": 0, "fail": None}


def _dispatch(method, params, args=None):
    """JSON-RPC method -> result. Raises ValueError only for a method we do not have."""
    cfg = dict(DEFAULT_ARGS)
    cfg.update(args or {})
    if method == "initialize":
        # Echo the client's protocol version rather than our own. A server that answers
        # with a version the client did not ask for is a negotiation the client may
        # refuse, and the refusal arrives as "no tools", not as a protocol error.
        version = (params or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        return {"protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS}
    if method == "tools/list":
        return {"tools": [{
            "name": "list_customers",
            "description": ("List customer records. Returns the customer table in json, "
                            "csv or markdown."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": list(FORMATS),
                               "description": "Serialisation to return."},
                    "limit": {"type": "integer", "minimum": 1,
                              "description": "How many rows, at most the scenario's."},
                    "scenario": {"type": "string", "enum": sorted(SCENARIOS),
                                 "description": "Which shape of record to return."},
                },
                "required": ["format"],
            }}]}
    if method == "tools/call":
        call = params or {}
        if call.get("name") == "list_customers":
            args_ = dict(call.get("arguments") or {})
            if not args_.get("scenario"):
                args_["scenario"] = cfg["scenario"]
            return call_tool("list_customers", args_, rows=cfg["rows"],
                             delay_ms=cfg["delay_ms"], fail=cfg["fail"])
        # An unknown tool is an error result, not a JSON-RPC error: the model should get
        # a chance to correct itself instead of the run dying.
        return _result(f"No such tool: {call.get('name')!r}. Available: list_customers.", True)
    if method in ("notifications/initialized", "initialized", "ping"):
        return {}
    raise ValueError(f"unknown method: {method}")


def serve(stdin=None, stdout=None, args=None):
    """The stdio loop. One JSON object per line, in and out."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": f"parse error: {e}"}}
        else:
            if isinstance(request, dict) and "id" not in request:
                continue  # a notification: acknowledged by saying nothing
            mid = request.get("id") if isinstance(request, dict) else None
            try:
                reply = {"jsonrpc": "2.0", "id": mid, "result": _dispatch(
                    request.get("method"), request.get("params"), args)}
            except Exception as e:  # noqa: BLE001 - one bad request must not end the session
                reply = {"jsonrpc": "2.0", "id": mid,
                         "error": {"code": -32601, "message": f"{type(e).__name__}: {e}"}}
        stdout.write(json.dumps(reply) + "\n")
        stdout.flush()


def main():
    ap = argparse.ArgumentParser(description="fake MCP server for end-to-end tests")
    ap.add_argument("--stdio", action="store_true", help="speak MCP on stdin/stdout")
    ap.add_argument("--print", choices=FORMATS, help="dump one format to stdout and exit")
    ap.add_argument("--scenario", default=DEFAULT_SCENARIO,
                    choices=sorted(SCENARIOS), help="which shape of record to serve")
    ap.add_argument("--rows", type=int, default=None, help="cap the row count")
    ap.add_argument("--delay-ms", type=int, default=0, help="latency before responding")
    ap.add_argument("--fail", choices=("error", "truncated"), default=None,
                    help="simulate an upstream failure or a truncated document")
    args = ap.parse_args()
    if args.print:
        try:
            print(list_customers(args.print, scenario=args.scenario, rows=args.rows,
                                 delay_ms=args.delay_ms, fail=args.fail))
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        return 0
    serve(args={"scenario": args.scenario, "rows": args.rows,
                "delay_ms": args.delay_ms, "fail": args.fail})
    return 0


if __name__ == "__main__":
    sys.exit(main())
